from concurrent.futures import ThreadPoolExecutor
import requests
import pandas as pd
import time
import os
import threading
from datetime import datetime
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

# --- CONFIGURATION ---
CSV_FILENAME = "kalshi_nba_arbitrage_data.csv"
SERIES_TICKER = "KXNBAGAME"
BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"
MAX_WORKERS = 30  # I/O bound; 50 caused timeouts, 30 is the sweet spot

# Thread-local session for connection reuse across requests
_local = threading.local()

def get_session():
    if not hasattr(_local, 'session'):
        _local.session = requests.Session()
    return _local.session

def parse_kalshi_date(date_str):
    if not date_str:
        return None
    try:
        # pd.Timestamp handles variable-precision microseconds that fromisoformat rejects
        return pd.Timestamp(date_str).to_pydatetime().replace(tzinfo=None)
    except Exception:
        return None

def is_valid_nba_game(market):
    title = market.get('title', '').lower()
    if " vs " not in title and " @" not in title and " vs." not in title and " at " not in title:
        return False
    bad_keywords = ["season", "champion", "series", "mvp", "finals"]
    if any(k in title for k in bad_keywords):
        return False
    return True

def get_settled_markets(existing_tickers=None):
    url = f"{BASE_URL}/markets"
    all_markets = []
    cursor = None
    TARGET_LIMIT = 10000
    consecutive_all_existing = 0

    print(f"Fetching markets...")

    while len(all_markets) < TARGET_LIMIT:
        params = {"status": "settled", "series_ticker": SERIES_TICKER, "limit": 1000}
        if cursor:
            params["cursor"] = cursor

        try:
            response = get_session().get(url, params=params)
            response.raise_for_status()
            data = response.json()

            batch = data.get("markets", [])
            if not batch:
                break

            # Early stopping: if we have existing data and this whole page is already processed
            if existing_tickers:
                batch_event_tickers = {m['event_ticker'] for m in batch}
                if batch_event_tickers.issubset(existing_tickers):
                    consecutive_all_existing += 1
                    if consecutive_all_existing >= 2:
                        print("  All recent markets already in CSV, stopping early.")
                        break
                else:
                    consecutive_all_existing = 0

            all_markets.extend(batch)

            if len(all_markets) >= TARGET_LIMIT:
                break

            cursor = data.get("cursor")
            if not cursor:
                break

            print(f"  Fetched {len(all_markets)} markets so far...")
        except Exception as e:
            print(f"Error fetching markets: {e}")
            break

    return all_markets

def get_candlesticks(market_ticker, start_time, closed_time):
    url = f"{BASE_URL}/series/{SERIES_TICKER}/markets/{market_ticker}/candlesticks"
    params = {"period_interval": 1, "start_ts": start_time, "end_ts": closed_time}

    max_retries = 5
    base_wait = 2

    for attempt in range(max_retries):
        try:
            response = get_session().get(url, params=params)
            if response.status_code == 200:
                return response.json().get("candlesticks", [])

            elif response.status_code == 429:
                wait_time = base_wait * (attempt + 1)
                print(f"    [429] Rate Limit on {market_ticker}. Sleeping {wait_time}s...")
                time.sleep(wait_time)
                continue

            else:
                print(f"Error {response.status_code} for {market_ticker}")
                return []

        except Exception as e:
            print(f"Got Exception {e} in get_candlesticks")
            return []
    return []


def fetch_event_candles(market_a_ticker, market_b_ticker, start_time, closed_time):
    with ThreadPoolExecutor(max_workers=MAX_CANDLE_WORKERS_PER_EVENT) as executor:
        future_a = executor.submit(get_candlesticks, market_a_ticker, start_time, closed_time)
        future_b = executor.submit(get_candlesticks, market_b_ticker, start_time, closed_time)
        return future_a.result(), future_b.result()

def to_epoch(iso_string):
    if not iso_string:
        return int(time.time())
    try:
        dt = pd.Timestamp(iso_string)
        return int(dt.timestamp())
    except:
        return int(time.time())

def get_safe_max(candles):
    """
    Returns the maximum realistic price you could have sold at.
    Uses 'close' price instead of 'high' to filter out 1-second wicks.
    Checks for non-zero volume.
    """
    valid_prices = []
    for c in candles:
        # We are SELLING, so we look at the BID side.
        bid_data = c.get('yes_bid', {})
        close_price = get_candle_price(bid_data, 'close')
        
        # Filter: Volume must exist (price wasn't just a quote, it traded)
        if get_candle_volume(c) > 0 and close_price > 0:
            # Use CLOSE, not HIGH. 
            # High is often a trap. Close means it stayed there.
            valid_prices.append(close_price)
            
    return max(valid_prices) if valid_prices else 0

def fetch_all_candlesticks(tasks):
    """Submit all candlestick requests to a single flat thread pool (no nested pools)."""
    request_keys = []
    for i, pair in enumerate(tasks):
        m_a, m_b = pair
        closed_t = to_epoch(m_a['close_time'])
        start_window = closed_t - (4 * 60 * 60)
        request_keys.append((i, 'a', m_a['ticker'], start_window, closed_t))
        request_keys.append((i, 'b', m_b['ticker'], start_window, closed_t))

    total = len(request_keys)
    print(f"Fetching {total} candlestick series with {MAX_WORKERS} workers...")

    results = {}
    done = 0
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        future_map = {
            ex.submit(get_candlesticks, ticker, start, end): (i, side)
            for i, side, ticker, start, end in request_keys
        }
        for fut in as_completed(future_map):
            done += 1
            i, side = future_map[fut]
            results[(i, side)] = fut.result()
            if done % 100 == 0:
                print(f"  {done}/{total} candlestick series fetched...")

    return results

def process_event_task(markets_pair, candles_a, candles_b):
    if len(markets_pair) != 2:
        return None
    market_a, market_b = markets_pair

    if not candles_a or not candles_b:
        return None

    candles_a.sort(key=lambda x: x['end_period_ts'])
    candles_b.sort(key=lambda x: x['end_period_ts'])

    try:
        valid_candles_a = [c for c in candles_a if c.get('yes_bid') and c.get('yes_ask')]
        valid_candles_b = [c for c in candles_b if c.get('yes_bid') and c.get('yes_ask')]

        if not valid_candles_a or not valid_candles_b:
            return None

        start_ask_a = next((c['yes_ask']['open'] for c in valid_candles_a if c['yes_ask']['open'] > 0), 0)
        start_ask_b = next((c['yes_ask']['open'] for c in valid_candles_b if c['yes_ask']['open'] > 0), 0)

        if start_ask_a > start_ask_b:
            fav, underdog = market_a["yes_sub_title"], market_b["yes_sub_title"]
            und_start_cost = start_ask_b
            und_candles = valid_candles_b
            und_market = market_b
        else:
            fav, underdog = market_b["yes_sub_title"], market_a["yes_sub_title"]
            und_start_cost = start_ask_a
            und_candles = valid_candles_a
            und_market = market_a

        underdog_max_price = get_safe_max(und_candles)

        date_val = parse_kalshi_date(market_a.get('close_time'))
        date_str = date_val.strftime('%Y-%m-%d') if date_val else "N/A"

        scenario = "Favorite Dominated"
        if und_market["result"] == "yes":
            scenario = "Underdog Won"
        elif und_start_cost < underdog_max_price:
            scenario = "Underdog Improved Odds and Lost"

        return {
            "Date": date_str,
            "Event_Ticker": market_a["event_ticker"],
            "Favorite_Team": fav,
            "Underdog_Team": underdog,
            "Und_Start_Price": und_start_cost,
            "Und_Best_Price": underdog_max_price,
            "Arbitrage_Opportunity": scenario,
            "Swing": underdog_max_price - und_start_cost,
            "Winner": "Underdog" if und_market["result"] == "yes" else "Favorite"
        }

    except Exception:
        return None

def main():
    existing_tickers = set()
    if os.path.exists(CSV_FILENAME):
        try:
            df = pd.read_csv(CSV_FILENAME)
            if 'Event_Ticker' in df.columns:
                existing_tickers = set(df['Event_Ticker'].tolist())
        except pd.errors.EmptyDataError:
            pass

    markets = get_settled_markets(existing_tickers=existing_tickers)
    event_ticker_to_data = defaultdict(list)
    nba_league_schedule = get_season_schedule_map()
    seen_game_keys = set()

    if not nba_league_schedule:
        print("ERROR: Bad nba schedule")
        return

    for market in markets:
        if not is_valid_nba_game(market):
            continue
        if market['event_ticker'] in existing_tickers:
            continue
        event_ticker_to_data[market['event_ticker']].append(market)
        if len(event_ticker_to_data[market['event_ticker']]) == 2 and game_key != (None, None):
            seen_game_keys.add(game_key)

    tasks = [pair for pair in event_ticker_to_data.values() if len(pair) == 2]
    print(f"Processing {len(tasks)} new game pairs...")

    if not tasks:
        print("No new qualifying games found.")
        return

    # Fetch all candlesticks in one flat parallel pool
    candle_cache = fetch_all_candlesticks(tasks)

    new_data = []
    for i, pair in enumerate(tasks):
        res = process_event_task(pair, candle_cache.get((i, 'a'), []), candle_cache.get((i, 'b'), []))
        if res:
            new_data.append(res)

    if new_data:
        mode = 'a' if os.path.exists(CSV_FILENAME) else 'w'
        header = not os.path.exists(CSV_FILENAME)
        pd.DataFrame(new_data).to_csv(CSV_FILENAME, mode=mode, header=header, index=False)
        print(f"Success: Added {len(new_data)} new games.")
    else:
        print("No new qualifying games found.")

if __name__ == "__main__":
    main()

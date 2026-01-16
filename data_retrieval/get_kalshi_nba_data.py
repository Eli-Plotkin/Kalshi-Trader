import requests
import pandas as pd
import time
import os
from datetime import datetime
from collections import defaultdict

# --- CONFIGURATION ---
CSV_FILENAME = "kalshi_nba_arbitrage_data.csv"
SERIES_TICKER = "KXNBAGAME"
BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"

# Global Session
session = requests.Session()

def parse_kalshi_date(date_str):
    if not date_str: 
        print("None provided to parse_kalshi_date")
        return None
    try:
        clean_ts = date_str.replace('Z', '+00:00')
        return datetime.fromisoformat(clean_ts).replace(tzinfo=None)
    except ValueError as e:
        print(f"parse_kalshi_Date got ValueError: {e}")
        return None

def is_valid_nba_game(market):
    title = market.get('title', '').lower()
    if " vs " not in title and " @" not in title and " vs." not in title:
        return False
    bad_keywords = ["season", "champion", "series", "mvp", "finals"]
    if any(k in title for k in bad_keywords):
        return False
    return True

def get_settled_markets():
    url = f"{BASE_URL}/markets"
    all_markets = []
    cursor = None
    TARGET_LIMIT = 10000 
    
    print(f"Fetching first {TARGET_LIMIT} markets...")
    
    while len(all_markets) < TARGET_LIMIT:
        params = {"status": "settled", "series_ticker": SERIES_TICKER, "limit": 1000}
        if cursor: params["cursor"] = cursor
            
        try:
            response = session.get(url, params=params)
            response.raise_for_status()
            data = response.json()
            
            batch = data.get("markets", [])
            all_markets.extend(batch)
            
            if len(all_markets) >= TARGET_LIMIT:
                break 
            
            cursor = data.get("cursor")
            if not cursor: break 
            
            print(f"  Fetched {len(all_markets)} markets so far...")
            time.sleep(0.2) # Polite sleep between pages
        except Exception as e:
            print(f"Error fetching markets: {e}")
            break
            
    return all_markets 

def get_candlesticks(market_ticker, start_time, closed_time):
    # DIRECT URL (More reliable)
    url = f"{BASE_URL}/series/{SERIES_TICKER}/markets/{market_ticker}/candlesticks"
    params = {"period_interval": 1, "start_ts": start_time, "end_ts": closed_time}
    
    # Retry Logic for Rate Limits
    max_retries = 5
    base_wait = 2
    
    for attempt in range(max_retries):
        try:
            response = session.get(url, params=params)
            
            if response.status_code == 200:
                return response.json().get("candlesticks", [])
            
            elif response.status_code == 429:
                wait_time = base_wait * (attempt + 1)
                print(f"    [429] Rate Limit on {market_ticker}. Sleeping {wait_time}s...")
                time.sleep(wait_time)
                continue
            
            else:
                print(f"    Error {response.status_code} for {market_ticker}")
                return []
                
        except Exception as e:
            print(f"Got Exception {e} in get_candlesticks")
            return []
            
    return []

def to_epoch(iso_string):
    if not iso_string: return int(time.time())
    try:
        dt = pd.Timestamp(iso_string)
        return int(dt.timestamp())
    except:
        return int(time.time())

# Get the max bid someone has placed. Use to determine whats highest we could've sold at
def get_safe_max(candles):
    valid_highs = [c['yes_bid']['high'] for c in candles if c['yes_bid']['high'] > 0]
    return max(valid_highs) if valid_highs else 0

def process_event_task(markets_pair):
    if len(markets_pair) != 2: return None
    market_a, market_b = markets_pair

    closed_t = to_epoch(market_a['close_time'])
    start_window = closed_t - (4 * 60 * 60)
    
    # Fetch candles sequentially
    candles_a = get_candlesticks(market_a["ticker"], start_window, closed_t)
    candles_b = get_candlesticks(market_b["ticker"], start_window, closed_t)

    if not candles_a or not candles_b: return None

    candles_a.sort(key=lambda x: x['end_period_ts'])
    candles_b.sort(key=lambda x: x['end_period_ts'])
    
    try:        
        valid_candles_a = [c for c in candles_a if c.get('yes_bid') and c.get('yes_ask')]
        valid_candles_b = [c for c in candles_b if c.get('yes_bid') and c.get('yes_ask')]        
        
        if not valid_candles_a or not valid_candles_b: return None

        # Get Open Prices (Ask & Bid)
        start_ask_a = next((c['yes_ask']['open'] for c in valid_candles_a if c['yes_ask']['open'] > 0), 0)
        start_ask_b = next((c['yes_ask']['open'] for c in valid_candles_b if c['yes_ask']['open'] > 0), 0)
        
        # Logic: Use Bid Price to determine Favorite/Swing
        if start_ask_a > start_ask_b:
            fav, underdog = market_a["yes_sub_title"], market_b["yes_sub_title"]
            und_start_cost  = start_ask_b 
            und_candles = valid_candles_b
            und_market = market_b
        else:
            fav, underdog = market_b["yes_sub_title"], market_a["yes_sub_title"]
            und_start_cost  = start_ask_a 
            und_candles = valid_candles_a
            und_market = market_a

        # Min/Max
        underdog_max_price = get_safe_max(und_candles)

        date_val = parse_kalshi_date(market_a.get('close_time'))
        date_str = date_val.strftime('%Y-%m-%d') if date_val else "N/A"
        
        # Scenario
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

    markets = get_settled_markets()
    event_ticker_to_data = defaultdict(list)

    for market in markets:
        if not is_valid_nba_game(market): continue
        if market['event_ticker'] in existing_tickers: continue
        event_ticker_to_data[market['event_ticker']].append(market)

    tasks = [pair for pair in event_ticker_to_data.values() if len(pair) == 2]

    new_data = []
    
    # SEQUENTIAL LOOP (No Threading)
    for i, pair in enumerate(tasks):
        res = process_event_task(pair)
        if res:
            new_data.append(res)
            
        # Log progress
        if (i + 1) % 10 == 0:
            print(f"Processed {i + 1}/{len(tasks)} games... (Found {len(new_data)} valid so far)")
        
        # POLITE SLEEP (Prevents 429 Errors)
        time.sleep(0.5) 

    if new_data:
        mode = 'a' if os.path.exists(CSV_FILENAME) else 'w'
        header = not os.path.exists(CSV_FILENAME)
        pd.DataFrame(new_data).to_csv(CSV_FILENAME, mode=mode, header=header, index=False)
        print(f"Success: Added {len(new_data)} new games.")
    else:
        print("No new qualifying games found.")

if __name__ == "__main__":
    main()
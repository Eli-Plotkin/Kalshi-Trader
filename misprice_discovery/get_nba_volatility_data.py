import os
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import pandas as pd
import requests

from misprice_discovery.helpers import parse_event_ticker

CSV_FILENAME = "kalshi_nba_volatility_data.csv"
SERIES_TICKER = "KXNBAGAME"
BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"
MAX_WORKERS = 30

CURRENT_SCHEDULE_URL = "https://cdn.nba.com/static/json/staticData/scheduleLeagueV2.json"
ARCHIVED_SCHEDULE_URL_TEMPLATE = "https://cdn.nba.com/static/json/staticData/scheduleLeagueV2_{index}.json"
MAX_ARCHIVE_MISSES = 6

_local = threading.local()


def get_session():
    if not hasattr(_local, 'session'):
        _local.session = requests.Session()
    return _local.session


def price_to_cents(value):
    if value is None:
        return 0
    try:
        s = str(value).strip()
        v = float(s)
        if '.' in s and v <= 1.0:
            return int(round(v * 100))
        return int(round(v))
    except (TypeError, ValueError):
        return 0


def get_candle_price(side_data, field_name):
    raw = side_data.get(field_name) or side_data.get(f"{field_name}_dollars")
    return price_to_cents(raw)


def get_candle_volume(candle):
    vol = candle.get("volume")
    if vol is None:
        vol = candle.get("volume_fp")
    try:
        return float(vol)
    except (TypeError, ValueError):
        return 0.0


def is_valid_nba_game(market):
    title = market.get('title', '').lower()
    if not any(sep in title for sep in [" vs ", " @", " vs.", " at "]):
        return False
    return not any(k in title for k in ["season", "champion", "series", "mvp", "finals"])


def market_close_epoch(market):
    """Returns the market close_time as a unix timestamp, or None if unavailable."""
    raw = market.get('close_time')
    if not raw:
        return None
    try:
        return int(pd.Timestamp(raw).timestamp())
    except Exception:
        return None


def _fetch_market_pages(url, source_name, extra_params, existing_tickers):
    all_markets = []
    cursor = None
    consecutive_existing = 0

    while True:
        params = {"series_ticker": SERIES_TICKER, "limit": 1000}
        if extra_params:
            params.update(extra_params)
        if cursor:
            params["cursor"] = cursor

        try:
            response = get_session().get(url, params=params, timeout=20)
            response.raise_for_status()
            data = response.json()
            batch = data.get("markets", [])
            if not batch:
                break

            if existing_tickers:
                if {m['event_ticker'] for m in batch}.issubset(existing_tickers):
                    consecutive_existing += 1
                    if consecutive_existing >= 2:
                        break
                else:
                    consecutive_existing = 0

            all_markets.extend(batch)
            cursor = data.get("cursor")
            if not cursor:
                break
            time.sleep(0.5)
        except Exception as e:
            print(f"Error fetching {source_name} markets: {e}")
            break

    print(f"  {source_name}: {len(all_markets)} markets")
    return all_markets


def get_settled_markets(existing_tickers=None):
    """Fetches settled markets from both live and historical endpoints, deduplicated by ticker."""
    print("Fetching settled markets (live + historical)...")
    with ThreadPoolExecutor(max_workers=2) as ex:
        live_fut = ex.submit(_fetch_market_pages, f"{BASE_URL}/markets", "live",
                             {"status": "settled"}, existing_tickers)
        hist_fut = ex.submit(_fetch_market_pages, f"{BASE_URL}/historical/markets", "historical",
                             None, existing_tickers)
        live, hist = live_fut.result(), hist_fut.result()

    deduped = {m["ticker"]: m for m in live + hist if m.get("ticker")}
    print(f"  Total unique markets: {len(deduped)}")
    return list(deduped.values())


def get_candlesticks(market_ticker, start_ts, end_ts):
    urls = [
        f"{BASE_URL}/series/{SERIES_TICKER}/markets/{market_ticker}/candlesticks",
        f"{BASE_URL}/historical/markets/{market_ticker}/candlesticks",
    ]
    params = {"period_interval": 1, "start_ts": start_ts, "end_ts": end_ts}

    for attempt in range(5):
        saw_rate_limit = False
        for url in urls:
            try:
                resp = get_session().get(url, params=params, timeout=20)
                if resp.status_code == 200:
                    return resp.json().get("candlesticks", [])
                if resp.status_code == 429:
                    saw_rate_limit = True
                elif resp.status_code != 404:
                    print(f"    [{resp.status_code}] {market_ticker}")
            except Exception as e:
                print(f"    Exception on {market_ticker}: {e}")

        if saw_rate_limit:
            wait = 2 * (attempt + 1)
            print(f"    [429] {market_ticker} sleeping {wait}s...")
            time.sleep(wait)
        else:
            return []

    return []


def load_multiseason_schedules():
    schedules = []

    try:
        data = get_session().get(CURRENT_SCHEDULE_URL, timeout=15).json()
        league = data.get("leagueSchedule")
        if league:
            schedules.append(league)
            print("  Loaded current season schedule")
    except Exception as e:
        print(f"  Failed to fetch current schedule: {e}")

    consecutive_misses = 0
    for index in range(1, 100):
        if consecutive_misses >= MAX_ARCHIVE_MISSES:
            break
        url = ARCHIVED_SCHEDULE_URL_TEMPLATE.format(index=index)
        try:
            resp = get_session().get(url, timeout=10)
            if resp.status_code == 404:
                consecutive_misses += 1
                continue
            league = resp.json().get("leagueSchedule")
            if league:
                schedules.append(league)
                consecutive_misses = 0
            else:
                consecutive_misses += 1
        except Exception:
            consecutive_misses += 1

    print(f"  Loaded {len(schedules)} season schedules")
    return schedules


def build_schedule_index(schedules):
    """Flat O(1) lookup: (date, sorted_matchup_tuple) -> tip-off metadata."""
    index = {}
    for league in schedules:
        for date_entry in league.get("gameDates", []):
            try:
                game_date = datetime.strptime(
                    date_entry.get("gameDate", "")[:10], "%m/%d/%Y"
                ).date()
            except ValueError:
                continue

            for game in date_entry.get("games", []):
                if game.get("gameLabel") == "Preseason":
                    continue
                home = game.get("homeTeam", {}).get("teamTricode")
                away = game.get("awayTeam", {}).get("teamTricode")
                utc_str = game.get("gameDateTimeUTC")
                if not home or not away or not utc_str:
                    continue

                matchup = tuple(sorted((home, away)))
                tip_off_dt = datetime.fromisoformat(utc_str.replace("Z", "+00:00"))
                index[(game_date, matchup)] = {
                    "tip_off_ts": int(tip_off_dt.timestamp()),
                    "game_label": game.get("gameLabel") or "Regular",
                }

    return index


def get_tip_off_ts(event_ticker, schedule_index):
    result = parse_event_ticker(event_ticker)
    if not result:
        return None
    target_date, matchup = result
    if not target_date or not matchup:
        return None
    return schedule_index.get((target_date, matchup))


def fetch_all_candlesticks(tasks, schedule_index):
    """
    Window: [tip_off, close_time] — the full in-game period.
    Skips pairs where either anchor is unavailable.
    """
    request_keys = []
    meta_map = {}

    for i, (event_ticker, pair) in enumerate(tasks):
        schedule_entry = get_tip_off_ts(event_ticker, schedule_index)
        if not schedule_entry:
            continue

        m_a, m_b = pair
        close_ts = market_close_epoch(m_a)
        if not close_ts:
            continue

        tip_off_ts = schedule_entry["tip_off_ts"]
        if tip_off_ts >= close_ts:
            continue

        meta_map[i] = {
            "tip_off_ts": tip_off_ts,
            "close_ts": close_ts,
            "game_label": schedule_entry["game_label"],
        }
        request_keys.append((i, 'a', m_a['ticker'], tip_off_ts, close_ts))
        request_keys.append((i, 'b', m_b['ticker'], tip_off_ts, close_ts))

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
                print(f"  {done}/{total} series fetched...")

    return results, meta_map


def get_safe_max(candles):
    """
    Best realistic sell price over the window.
    Uses yes_bid close (what a seller receives) on candles with actual volume.
    """
    best = 0
    for c in candles:
        if get_candle_volume(c) > 0:
            price = get_candle_price(c.get('yes_bid', {}), 'close')
            if price > best:
                best = price
    return best


def process_event_task(event_ticker, markets_pair, candles_a, candles_b, meta):
    if len(markets_pair) != 2 or not candles_a or not candles_b:
        return None

    market_a, market_b = markets_pair
    candles_a.sort(key=lambda x: x['end_period_ts'])
    candles_b.sort(key=lambda x: x['end_period_ts'])

    try:
        valid_a = [c for c in candles_a if c.get('yes_bid') and c.get('yes_ask')]
        valid_b = [c for c in candles_b if c.get('yes_bid') and c.get('yes_ask')]
        if not valid_a or not valid_b:
            return None

        start_ask_a = next(
            (get_candle_price(c['yes_ask'], 'open') for c in valid_a
             if get_candle_price(c['yes_ask'], 'open') > 0), 0
        )
        start_ask_b = next(
            (get_candle_price(c['yes_ask'], 'open') for c in valid_b
             if get_candle_price(c['yes_ask'], 'open') > 0), 0
        )

        if start_ask_a >= start_ask_b:
            fav_market, und_market = market_a, market_b
            und_start = start_ask_b
            und_candles = valid_b
        else:
            fav_market, und_market = market_b, market_a
            und_start = start_ask_a
            und_candles = valid_a

        und_best = get_safe_max(und_candles)

        if und_market["result"] == "yes":
            scenario = "Underdog Won"
        elif und_start < und_best:
            scenario = "Underdog Improved Odds and Lost"
        else:
            scenario = "Favorite Dominated"

        date_str = datetime.fromtimestamp(meta["tip_off_ts"]).strftime('%Y-%m-%d')

        return {
            "Date": date_str,
            "Event_Ticker": event_ticker,
            "Game_Phase": meta["game_label"],
            "Favorite_Team": fav_market.get("yes_sub_title"),
            "Underdog_Team": und_market.get("yes_sub_title"),
            "Und_Start_Price": und_start,
            "Und_Best_Price": und_best,
            "Swing": und_best - und_start,
            "Scenario": scenario,
            "Winner": "Underdog" if und_market["result"] == "yes" else "Favorite",
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

    print("Loading NBA schedules...")
    schedules = load_multiseason_schedules()
    if not schedules:
        print("ERROR: No NBA schedule data available.")
        return
    schedule_index = build_schedule_index(schedules)
    print(f"Schedule index: {len(schedule_index)} games indexed")

    event_ticker_to_data = defaultdict(list)
    for market in markets:
        if not is_valid_nba_game(market):
            continue
        if market['event_ticker'] in existing_tickers:
            continue
        event_ticker_to_data[market['event_ticker']].append(market)

    tasks = [
        (et, pair)
        for et, pair in event_ticker_to_data.items()
        if len(pair) == 2
    ]
    print(f"Processing {len(tasks)} new game pairs...")

    if not tasks:
        print("No new qualifying games found.")
        return

    candle_cache, meta_map = fetch_all_candlesticks(tasks, schedule_index)

    new_data = []
    for i, (event_ticker, pair) in enumerate(tasks):
        if i not in meta_map:
            continue
        res = process_event_task(
            event_ticker,
            pair,
            candle_cache.get((i, 'a'), []),
            candle_cache.get((i, 'b'), []),
            meta_map[i],
        )
        if res:
            new_data.append(res)

    if new_data:
        file_exists = os.path.exists(CSV_FILENAME)
        pd.DataFrame(new_data).to_csv(
            CSV_FILENAME,
            mode='a' if file_exists else 'w',
            header=not file_exists,
            index=False,
        )
        print(f"Success: Added {len(new_data)} new games.")
    else:
        print("No new qualifying games found.")


if __name__ == "__main__":
    main()

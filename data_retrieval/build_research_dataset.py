import csv
import os
import time
import threading
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

import requests

from tip_off_time import parse_event_ticker

# Research dataset configuration.
SERIES_TICKER = "KXNBAGAME"
BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"
CURRENT_SCHEDULE_URL = "https://cdn.nba.com/static/json/staticData/scheduleLeagueV2.json"
ARCHIVED_SCHEDULE_URL_TEMPLATE = "https://cdn.nba.com/static/json/staticData/scheduleLeagueV2_{index}.json"
STATS_SCHEDULE_URL = "https://stats.nba.com/stats/scheduleleaguev2"
STATS_NBA_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://www.nba.com/",
    "Origin": "https://www.nba.com",
    "x-nba-stats-origin": "stats",
    "x-nba-stats-token": "true",
}
HISTORICAL_CUTOFF_URL = f"{BASE_URL}/historical/cutoff"
DATASET_FILENAME = "kalshi_nba_research_dataset.csv"
PRE_TIP_LOOKBACK_MINUTES = 180
PRE_TIP_CUTOFF_MINUTES = 15
AVERAGE_TIP_OFF_DELAY_MINUTES = 12
REQUEST_TIMEOUT_SECONDS = 20
TARGET_LIMIT = None
DESCRIPTION_PREFIX = "Description: "
MAX_CANDLE_WORKERS_PER_EVENT = 2
MAX_EVENT_WORKERS = 2
MAX_MARKET_FETCH_WORKERS = 2
MAX_SCHEDULE_FETCH_WORKERS = 4
MAX_SCHEDULE_ARCHIVE_BATCHES_WITHOUT_HIT = 3

thread_local = threading.local()


def get_session():
    if not hasattr(thread_local, "session"):
        thread_local.session = requests.Session()
    return thread_local.session


def price_to_cents(value):
    if value is None:
        return 0

    try:
        if isinstance(value, str):
            value = value.strip()
            if not value:
                return 0
            if "." in value:
                return int(round(float(value) * 100))
            return int(round(float(value)))

        if isinstance(value, float) and value <= 1:
            return int(round(value * 100))

        return int(round(float(value)))
    except (TypeError, ValueError):
        return 0


def get_candle_price(side_data, field_name):
    return price_to_cents(
        side_data.get(field_name) or side_data.get(f"{field_name}_dollars")
    )


def get_candle_volume(candle):
    raw_volume = candle.get("volume")
    if raw_volume is None:
        raw_volume = candle.get("volume_fp")

    try:
        return float(raw_volume)
    except (TypeError, ValueError):
        return 0.0


def is_valid_quote_price(price_cents):
    return price_cents is not None and 1 < price_cents < 99


def parse_kalshi_date(date_str):
    if not date_str:
        return None

    try:
        return datetime.fromisoformat(date_str.replace("Z", "+00:00"))
    except ValueError:
        return None


def row_to_csv_ready(row, fieldnames):
    return {field: "" if row.get(field) is None else row.get(field) for field in fieldnames}


def is_description_row(row, key_column):
    return str(row.get(key_column, "")).startswith(DESCRIPTION_PREFIX)


def load_existing_keys(csv_filename, key_column):
    if not os.path.exists(csv_filename):
        return set()

    keys = set()
    with open(csv_filename, newline="") as csv_file:
        reader = csv.DictReader(csv_file)
        if not reader.fieldnames or key_column not in reader.fieldnames:
            return set()
        for row in reader:
            if not row or is_description_row(row, key_column):
                continue
            key_value = row.get(key_column)
            if key_value:
                keys.add(str(key_value))
    return keys


KNOWN_LABELS_TO_PHASE = {
    "Preseason": "Preseason",
    "NBA Finals": "Finals",
}
_LOGGED_UNKNOWN_LABELS = set()


def game_label_to_phase(game_label):
    if not game_label:
        return "Regular"
    if game_label in _LOGGED_UNKNOWN_LABELS:
        return "Regular"
    if game_label in KNOWN_LABELS_TO_PHASE:
        return KNOWN_LABELS_TO_PHASE[game_label]
    if "Play-In" in game_label:
        return "PlayIn"
    if "First Round" in game_label or "Semifinals" in game_label or "Finals" in game_label:
        return "Playoffs"
    if "Star" in game_label:
        return game_label
    if "Star" in game_label:
        return game_label
    
    _LOGGED_UNKNOWN_LABELS.add(game_label)
    print(f"Unrecognized game label '{game_label}', defaulting to Regular")
    return "Regular"


def describe_market_prefix(prefix):
    return "favorite" if prefix == "fav" else "underdog"


def describe_market_column(prefix, suffix):
    side_name = describe_market_prefix(prefix)
    descriptions = {
        "market_ticker": f"Kalshi market ticker for the {side_name} side; assumed to be the YES market for that team winning.",
        "team": f"Team name pulled from Kalshi yes_sub_title for the {side_name} side.",
        "result": f"Kalshi settlement result for the {side_name} side; 'yes' means that team won.",
        "candle_count": f"Number of one-minute candles retrieved for the {side_name} inside the pre-tip window.",
        "candles_with_volume": f"Count of pre-tip candles with positive reported volume for the {side_name}; used as a liquidity sanity check.",
        "total_volume": f"Sum of reported candle volume in the pre-tip window for the {side_name}; assumes Kalshi candle volume is reliable.",
        "entry_ask_cents": f"First valid pre-tip YES ask for the {side_name}, in cents; assumes the first clean ask is the trade entry candidate.",
        "entry_bid_cents": f"First valid pre-tip YES bid for the {side_name}, in cents; used to estimate entry spread.",
        "entry_spread_cents": f"Entry ask minus entry bid for the {side_name}, in cents; a simple quoted spread estimate.",
        "entry_ask_ts": f"Timestamp of the first valid pre-tip ask for the {side_name}.",
        "entry_bid_ts": f"Timestamp of the first valid pre-tip bid for the {side_name}.",
        "last_pre_tip_ask_cents": f"Last valid pre-tip YES ask seen for the {side_name}, in cents.",
        "last_pre_tip_bid_cents": f"Last valid pre-tip YES bid seen for the {side_name}, in cents; quoted, not necessarily traded.",
        "last_pre_tip_ask_ts": f"Timestamp of the last valid pre-tip ask for the {side_name}.",
        "last_pre_tip_bid_ts": f"Timestamp of the last valid pre-tip bid for the {side_name}.",
        "min_pre_tip_ask_cents": f"Lowest valid pre-tip YES ask for the {side_name}, in cents; quote-based, not volume-filtered.",
        "max_pre_tip_ask_cents": f"Highest valid pre-tip YES ask for the {side_name}, in cents; quote-based, not volume-filtered.",
        "min_pre_tip_bid_cents": f"Lowest valid pre-tip YES bid for the {side_name}, in cents; quote-based, not volume-filtered.",
        "max_pre_tip_bid_cents": f"Highest valid pre-tip YES bid for the {side_name}, in cents; quote-based, not volume-filtered.",
        "min_pre_tip_ask_ts": f"Timestamp of the lowest quoted pre-tip ask for the {side_name}.",
        "max_pre_tip_ask_ts": f"Timestamp of the highest quoted pre-tip ask for the {side_name}.",
        "min_pre_tip_bid_ts": f"Timestamp of the lowest quoted pre-tip bid for the {side_name}.",
        "max_pre_tip_bid_ts": f"Timestamp of the highest quoted pre-tip bid for the {side_name}.",
        "min_traded_pre_tip_bid_cents": f"Lowest pre-tip YES bid close for the {side_name} on candles with positive volume; used as a more realistic traded level.",
        "max_traded_pre_tip_bid_cents": f"Highest pre-tip YES bid close for the {side_name} on candles with positive volume; used as a more realistic sellable level.",
        "min_traded_pre_tip_bid_ts": f"Timestamp of the lowest volume-backed pre-tip bid close for the {side_name}.",
        "max_traded_pre_tip_bid_ts": f"Timestamp of the highest volume-backed pre-tip bid close for the {side_name}.",
        "ask_quote_count": f"Count of valid pre-tip ask quotes for the {side_name}; valid means strictly between 1 and 99 cents.",
        "bid_quote_count": f"Count of valid pre-tip bid quotes for the {side_name}; valid means strictly between 1 and 99 cents.",
        "traded_bid_quote_count": f"Count of valid pre-tip bid closes for the {side_name} on candles with positive volume.",
    }
    return descriptions.get(suffix, f"{side_name.title()} field '{suffix}' generated by the research builder.")


def describe_dataset_column(fieldname):
    base_descriptions = {
        "Event_Ticker": "Unique Kalshi event ticker for the NBA game; used as the per-game primary key.",
        "Date": "Game date from scheduled tip-off in UTC date format; not local arena time.",
        "Game_Label": "NBA schedule label for the game, such as Regular Season or Play-In.",
        "Game_Phase": "Normalized game phase: Preseason, Regular, PlayIn, Playoffs, or Finals.",
        "Season": "NBA season identifier derived from game date, e.g. 2024-25 or 2025-26.",
        "Days_To_Season_End": "Days between game date and last regular-season game date for that season.",
        "Home_Team": "Home team tricode from the NBA schedule mapping.",
        "Away_Team": "Away team tricode from the NBA schedule mapping.",
        "Scheduled_Tipoff_UTC": "Official scheduled tip time from the NBA schedule in UTC.",
        "Adjusted_Tipoff_UTC": "Scheduled tip plus a fixed 12-minute delay assumption to approximate real tip-off.",
        "Window_Start_UTC": "Start of the research window in UTC; assumed to be 180 minutes before adjusted tip-off.",
        "Window_End_UTC": "End of the research window in UTC; assumed to be 15 minutes before adjusted tip-off.",
        "Window_Lookback_Minutes": "Configured lookback length for the pre-tip window in minutes.",
        "Window_Cutoff_Minutes": "Configured cutoff before adjusted tip-off in minutes.",
        "Favorite_Determined_By": "Rule used to label favorite versus underdog; higher entry ask implies higher implied win probability.",
        "Und_Best_Bid_PreTip_PnL_Cents": "Underdog per-share PnL if bought at first valid ask and sold at the best volume-backed pre-tip bid close.",
        "Und_Last_PreTip_Bid_PnL_Cents": "Underdog per-share PnL if bought at first valid ask and exited at the last quoted pre-tip bid.",
        "Und_Hold_To_Settle_PnL_Cents": "Underdog per-share PnL if bought at first valid ask and held to settlement.",
        "Favorite_Hold_To_Settle_PnL_Cents": "Favorite per-share PnL if bought at first valid ask and held to settlement.",
        "Fav_Low_Ask_PreTip_Cents": "Lowest quoted pre-tip ask for the favorite, in cents.",
        "Fav_Low_Bid_PreTip_Cents": "Lowest volume-backed pre-tip bid close for the favorite, in cents.",
        "Favorite_Won": "Boolean settlement outcome for the favorite side.",
        "Underdog_Won": "Boolean settlement outcome for the underdog side.",
    }
    if fieldname in base_descriptions:
        return base_descriptions[fieldname]
    if fieldname.startswith("Und_Target_") and fieldname.endswith("_Hit_PreTip"):
        target = fieldname.replace("Und_Target_", "").replace("_Hit_PreTip", "")
        return f"Boolean flag showing whether the underdog's best volume-backed pre-tip bid improved by at least {target.replace('c', '')} cents from entry."
    if fieldname.startswith("fav_"):
        return describe_market_column("fav", fieldname[len("fav_"):])
    if fieldname.startswith("und_"):
        return describe_market_column("und", fieldname[len("und_"):])
    return f"Generated research field '{fieldname}'."


def build_description_row(fieldnames):
    return {
        field: f"{DESCRIPTION_PREFIX}{describe_dataset_column(field)}"
        for field in fieldnames
    }


def file_has_description_row(csv_filename):
    if not os.path.exists(csv_filename):
        return False

    with open(csv_filename, newline="") as csv_file:
        reader = csv.reader(csv_file)
        next(reader, None)
        second_row = next(reader, None)
    return bool(second_row and second_row[0].startswith(DESCRIPTION_PREFIX))


def ensure_description_row(csv_filename, fieldnames):
    if not os.path.exists(csv_filename) or file_has_description_row(csv_filename):
        return

    with open(csv_filename, newline="") as csv_file:
        rows = list(csv.reader(csv_file))

    if not rows:
        return

    existing_header = rows[0]
    if existing_header != list(fieldnames):
        raise ValueError(
            f"{csv_filename} has an older schema. Remove it and rerun the builder to regenerate with descriptions."
        )

    description_values = [build_description_row(fieldnames)[field] for field in fieldnames]
    rows.insert(1, description_values)

    with open(csv_filename, "w", newline="") as csv_file:
        writer = csv.writer(csv_file)
        writer.writerows(rows)


def fetch_json(url, params=None):
    response = get_session().get(url, params=params, timeout=REQUEST_TIMEOUT_SECONDS)
    response.raise_for_status()
    return response.json()


def fetch_paginated_markets(url, source_name, extra_params=None, target_limit=TARGET_LIMIT):
    all_markets = []
    cursor = None

    while True:
        params = {
            "series_ticker": SERIES_TICKER,
            "limit": 1000,
        }
        if extra_params:
            params.update(extra_params)
        if cursor:
            params["cursor"] = cursor

        try:
            data = fetch_json(url, params=params)
        except Exception as exc:
            print(f"Error fetching {source_name} markets: {exc}")
            break

        batch = data.get("markets", [])
        for market in batch:
            copied_market = dict(market)
            copied_market["_market_source"] = source_name
            all_markets.append(copied_market)
            if target_limit is not None and len(all_markets) >= target_limit:
                break

        cursor = data.get("cursor")
        print(
            f"{source_name.title()} markets page fetched: batch={len(batch)}, "
            f"total={len(all_markets)}, next_cursor={'yes' if cursor else 'no'}"
        )
        if (target_limit is not None and len(all_markets) >= target_limit) or not cursor or not batch:
            break

        time.sleep(0.8)

    return all_markets


def get_historical_cutoff():
    try:
        return fetch_json(HISTORICAL_CUTOFF_URL)
    except Exception as exc:
        print(f"Failed to fetch historical cutoff: {exc}")
        return None


def get_event_key(event_ticker):
    try:
        target_date, matchup = parse_event_ticker(event_ticker)
    except (ValueError, AttributeError):
        return None
    if not target_date or not matchup:
        return None
    return target_date, matchup


def season_string_for_date(target_date):
    start_year = target_date.year if target_date.month >= 7 else target_date.year - 1
    end_year_suffix = str(start_year + 1)[-2:]
    return f"{start_year}-{end_year_suffix}"


def normalize_season_string(season_value):
    if season_value is None:
        return None

    season_text = str(season_value).strip()
    if not season_text:
        return None

    digits = "".join(ch if ch.isdigit() else " " for ch in season_text).split()
    if len(digits) < 2:
        return None

    start_year = digits[0]
    end_year = digits[1]
    if len(start_year) != 4:
        return None
    if len(end_year) == 4:
        end_year = end_year[-2:]
    elif len(end_year) != 2:
        return None

    return f"{start_year}-{end_year}"


def infer_required_seasons(markets):
    seasons = set()
    for market in markets:
        event_key = get_event_key(market.get("event_ticker"))
        if not event_key:
            continue
        target_date, _ = event_key
        normalized = normalize_season_string(season_string_for_date(target_date))
        if normalized:
            seasons.add(normalized)
    return seasons


def summarize_market_coverage(markets):
    unique_events = {market.get("event_ticker") for market in markets if market.get("event_ticker")}
    live_count = sum(1 for market in markets if market.get("_market_source") == "live")
    historical_count = sum(1 for market in markets if market.get("_market_source") == "historical")

    close_times = []
    for market in markets:
        close_time = parse_kalshi_date(market.get("close_time"))
        if close_time is not None:
            close_times.append(close_time)

    print(
        f"Fetched {len(markets)} markets across {len(unique_events)} events "
        f"(live={live_count}, historical={historical_count})"
    )
    if close_times:
        print(
            f"Market close-time coverage: {min(close_times).isoformat()} "
            f"to {max(close_times).isoformat()}"
        )


def get_settled_markets():
    target_description = TARGET_LIMIT if TARGET_LIMIT is not None else "all available"
    print(
        f"Fetching {target_description} settled NBA markets across live and historical APIs..."
    )

    with ThreadPoolExecutor(max_workers=MAX_MARKET_FETCH_WORKERS) as executor:
        live_future = executor.submit(
            fetch_paginated_markets,
            f"{BASE_URL}/markets",
            "live",
            {"status": "settled"},
        )
        historical_future = executor.submit(
            fetch_paginated_markets,
            f"{BASE_URL}/historical/markets",
            "historical",
        )
        live_markets = live_future.result()
        historical_markets = historical_future.result()

    deduped_markets = {}
    for market in live_markets + historical_markets:
        ticker = market.get("ticker")
        if not ticker:
            continue
        deduped_markets[ticker] = market

    markets = list(deduped_markets.values())
    summarize_market_coverage(markets)
    return markets


def get_candlesticks(market, start_ts, end_ts):
    market_ticker = market["ticker"]
    market_source = market.get("_market_source", "live")
    params = {"period_interval": 1, "start_ts": start_ts, "end_ts": end_ts}
    max_retries = 5
    urls = []

    if market_source == "historical":
        urls.append(f"{BASE_URL}/historical/markets/{market_ticker}/candlesticks")
        urls.append(f"{BASE_URL}/series/{SERIES_TICKER}/markets/{market_ticker}/candlesticks")
    else:
        urls.append(f"{BASE_URL}/series/{SERIES_TICKER}/markets/{market_ticker}/candlesticks")
        urls.append(f"{BASE_URL}/historical/markets/{market_ticker}/candlesticks")

    for attempt in range(max_retries):
        saw_rate_limit = False
        saw_server_error = False
        for url in urls:
            try:
                response = get_session().get(url, params=params, timeout=REQUEST_TIMEOUT_SECONDS)
                if response.status_code == 200:
                    candles = response.json().get("candlesticks", [])
                    candles.sort(key=lambda candle: candle.get("end_period_ts", 0))
                    return candles

                if response.status_code == 404:
                    continue

                if response.status_code == 429:
                    saw_rate_limit = True
                    continue

                if response.status_code >= 500:
                    saw_server_error = True
                    print(f"[{response.status_code}] {market_ticker} via {url}: will retry")
                    continue

                print(f"Error {response.status_code} fetching candles for {market_ticker} via {url}")
                return []
            except Exception as exc:
                print(f"Exception fetching candles for {market_ticker} via {url}: {exc}")
                saw_server_error = True
                continue

        if saw_rate_limit or saw_server_error:
            wait_seconds = 3 * (attempt + 1)
            label = "429" if saw_rate_limit else "5xx"
            if attempt > 2:
                print(f"[{label}] {market_ticker}: sleeping {wait_seconds}s (attempt {attempt + 1}/{max_retries})")
            time.sleep(wait_seconds)
            continue

        print(f"Error 404 fetching candles for {market_ticker} on both live and historical endpoints")
        return []

    return []


def fetch_event_candles(market_a, market_b, start_ts, end_ts, candle_executor):
    future_a = candle_executor.submit(get_candlesticks, market_a, start_ts, end_ts)
    future_b = candle_executor.submit(get_candlesticks, market_b, start_ts, end_ts)
    return future_a.result(), future_b.result()


def get_schedule():
    try:
        data = fetch_json(CURRENT_SCHEDULE_URL)
        return data.get("leagueSchedule")
    except Exception as exc:
        print(f"Failed to fetch NBA schedule: {exc}")
        return None


def fetch_schedule_payload(url):
    response = get_session().get(url, timeout=REQUEST_TIMEOUT_SECONDS)
    response.raise_for_status()
    return response.json()


def extract_normalized_season(league_schedule):
    if not league_schedule:
        return None

    season_candidates = (
        league_schedule.get("seasonYear"),
        league_schedule.get("season"),
        league_schedule.get("leagueYear"),
    )
    for candidate in season_candidates:
        normalized = normalize_season_string(candidate)
        if normalized:
            return normalized

    return None


def get_multiseason_schedules(required_seasons):
    if not required_seasons:
        return {}

    schedules_by_season = {}
    archive_index = 1
    consecutive_archive_misses = 0

    try:
        payload = fetch_schedule_payload(CURRENT_SCHEDULE_URL)
    except Exception:
        payload = None

    if payload:
        league_schedule = payload.get("leagueSchedule")
        normalized_season = extract_normalized_season(league_schedule)
        if league_schedule and normalized_season in required_seasons:
            schedules_by_season.setdefault(normalized_season, league_schedule)

    while (
        required_seasons - set(schedules_by_season)
        and consecutive_archive_misses < MAX_SCHEDULE_ARCHIVE_BATCHES_WITHOUT_HIT
    ):
        batch_urls = [
            ARCHIVED_SCHEDULE_URL_TEMPLATE.format(index=index)
            for index in range(archive_index, archive_index + MAX_SCHEDULE_FETCH_WORKERS)
        ]
        archive_index += len(batch_urls)
        found_in_batch = False

        with ThreadPoolExecutor(max_workers=MAX_SCHEDULE_FETCH_WORKERS) as executor:
            futures = {
                executor.submit(fetch_schedule_payload, url): url
                for url in batch_urls
            }
            for future in as_completed(futures):
                try:
                    payload = future.result()
                except Exception:
                    continue

                league_schedule = payload.get("leagueSchedule")
                normalized_season = extract_normalized_season(league_schedule)
                if not league_schedule or not normalized_season:
                    continue

                found_in_batch = True
                if normalized_season in required_seasons:
                    schedules_by_season.setdefault(normalized_season, league_schedule)

        if found_in_batch:
            consecutive_archive_misses = 0
        else:
            consecutive_archive_misses += 1

    missing_seasons = sorted(required_seasons - set(schedules_by_season))
    if missing_seasons:
        print(f"Warning: Missing NBA schedule data for seasons: {', '.join(missing_seasons)}")

    # Backfill from stats.nba.com — the CDN archives often lack postseason games,
    # but the stats API returns the full season including playoffs.
    for season in sorted(required_seasons):
        try:
            response = get_session().get(
                STATS_SCHEDULE_URL,
                params={"Season": season, "LeagueID": "00"},
                headers=STATS_NBA_HEADERS,
                timeout=30,
            )
            if response.status_code == 200:
                league_schedule = response.json().get("leagueSchedule")
                normalized = extract_normalized_season(league_schedule)
                if league_schedule and normalized == season:
                    schedules_by_season[season] = league_schedule
                    print(f"Loaded full schedule for {season} from stats.nba.com")
        except Exception as exc:
            print(f"Warning: stats.nba.com schedule fetch failed for {season}: {exc}")

    print(
        f"Loaded NBA schedules for {len(schedules_by_season)}/{len(required_seasons)} required seasons"
    )
    return schedules_by_season


def build_schedule_index(league_schedules):
    schedule_index = {}

    for league_schedule in league_schedules:
        game_dates = league_schedule.get("gameDates") if league_schedule else None
        if not game_dates:
            continue

        for date_entry in game_dates:
            game_date = date_entry.get("gameDate")
            if not game_date:
                continue

            try:
                nba_date = datetime.strptime(game_date[:10], "%m/%d/%Y").date()
            except ValueError:
                continue

            for game in date_entry.get("games", []):
                home_team = game.get("homeTeam", {}).get("teamTricode")
                away_team = game.get("awayTeam", {}).get("teamTricode")
                if not home_team or not away_team:
                    continue
                matchup = tuple(sorted((home_team, away_team)))

                game_time_utc = game.get("gameDateTimeUTC")
                if not game_time_utc:
                    continue

                scheduled_tipoff = datetime.fromisoformat(
                    game_time_utc.replace("Z", "+00:00")
                )
                adjusted_tipoff = scheduled_tipoff + timedelta(
                    minutes=AVERAGE_TIP_OFF_DELAY_MINUTES
                )

                schedule_index[(nba_date, matchup)] = {
                    "home_team": home_team,
                    "away_team": away_team,
                    "game_label": game.get("gameLabel"),
                    "scheduled_tipoff_utc": scheduled_tipoff,
                    "adjusted_tipoff_utc": adjusted_tipoff,
                }

    return schedule_index


def build_season_date_ranges(schedule_index):
    """Build date ranges using only regular-season games per season."""
    ranges = {}
    for (game_date, _matchup), metadata in schedule_index.items():
        if game_label_to_phase(metadata.get("game_label")) != "Regular":
            continue
        season = season_string_for_date(game_date)
        if season not in ranges:
            ranges[season] = [game_date, game_date]
        else:
            if game_date < ranges[season][0]:
                ranges[season][0] = game_date
            if game_date > ranges[season][1]:
                ranges[season][1] = game_date
    return ranges


def classify_missing_game(event_ticker, season_date_ranges):
    event_key = get_event_key(event_ticker)
    if not event_key:
        return "missing_schedule_mapping"
    target_date, _ = event_key
    season = season_string_for_date(target_date)
    date_range = season_date_ranges.get(season)
    if not date_range:
        return "missing_schedule_mapping"
    if target_date > date_range[1]:
        return "postseason_game"
    if target_date < date_range[0]:
        return "preseason_game"
    return "missing_schedule_mapping"


def get_game_metadata(event_ticker, schedule_index):
    event_key = get_event_key(event_ticker)
    if not event_key:
        return None
    return schedule_index.get(event_key)


def candle_timestamp_iso(candle):
    ts = candle.get("end_period_ts")
    if ts is None:
        return None

    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def summarize_price_series(candles, side_key):
    summary = {
        "quote_count": 0,
        "first_open_cents": None,
        "first_open_ts": None,
        "last_close_cents": None,
        "last_close_ts": None,
        "min_open_cents": None,
        "min_open_ts": None,
        "max_open_cents": None,
        "max_open_ts": None,
        "min_close_cents": None,
        "min_close_ts": None,
        "max_close_cents": None,
        "max_close_ts": None,
        "traded_close_count": 0,
        "min_traded_close_cents": None,
        "min_traded_close_ts": None,
        "max_traded_close_cents": None,
        "max_traded_close_ts": None,
    }

    for candle in candles:
        side_data = candle.get(side_key, {})
        open_price = get_candle_price(side_data, "open")
        close_price = get_candle_price(side_data, "close")
        candle_volume = get_candle_volume(candle)
        candle_ts = candle_timestamp_iso(candle)

        if is_valid_quote_price(open_price):
            summary["quote_count"] += 1
            if summary["first_open_cents"] is None:
                summary["first_open_cents"] = open_price
                summary["first_open_ts"] = candle_ts
            if summary["min_open_cents"] is None or open_price < summary["min_open_cents"]:
                summary["min_open_cents"] = open_price
                summary["min_open_ts"] = candle_ts
            if summary["max_open_cents"] is None or open_price > summary["max_open_cents"]:
                summary["max_open_cents"] = open_price
                summary["max_open_ts"] = candle_ts

        if is_valid_quote_price(close_price):
            summary["last_close_cents"] = close_price
            summary["last_close_ts"] = candle_ts
            if summary["min_close_cents"] is None or close_price < summary["min_close_cents"]:
                summary["min_close_cents"] = close_price
                summary["min_close_ts"] = candle_ts
            if summary["max_close_cents"] is None or close_price > summary["max_close_cents"]:
                summary["max_close_cents"] = close_price
                summary["max_close_ts"] = candle_ts
            if candle_volume > 0:
                summary["traded_close_count"] += 1
                if (
                    summary["min_traded_close_cents"] is None
                    or close_price < summary["min_traded_close_cents"]
                ):
                    summary["min_traded_close_cents"] = close_price
                    summary["min_traded_close_ts"] = candle_ts
                if (
                    summary["max_traded_close_cents"] is None
                    or close_price > summary["max_traded_close_cents"]
                ):
                    summary["max_traded_close_cents"] = close_price
                    summary["max_traded_close_ts"] = candle_ts

    return summary


def summarize_market_window(candles):
    candle_count = len(candles)
    candles_with_volume = sum(1 for candle in candles if get_candle_volume(candle) > 0)
    total_volume = sum(get_candle_volume(candle) for candle in candles)

    ask_summary = summarize_price_series(candles, "yes_ask")
    bid_summary = summarize_price_series(candles, "yes_bid")

    entry_ask = ask_summary["first_open_cents"]
    entry_bid = bid_summary["first_open_cents"]
    entry_spread = None
    if entry_ask is not None and entry_bid is not None:
        entry_spread = entry_ask - entry_bid

    return {
        "candle_count": candle_count,
        "candles_with_volume": candles_with_volume,
        "total_volume": round(total_volume, 4),
        "ask": ask_summary,
        "bid": bid_summary,
        "entry_ask_cents": entry_ask,
        "entry_bid_cents": entry_bid,
        "entry_spread_cents": entry_spread,
    }


def choose_favorite_and_underdog(market_a, market_b, summary_a, summary_b):
    ask_a = summary_a["entry_ask_cents"]
    ask_b = summary_b["entry_ask_cents"]

    if ask_a is None or ask_b is None:
        return None

    if ask_a == ask_b:
        bid_a = summary_a["entry_bid_cents"] if summary_a["entry_bid_cents"] is not None else -1
        bid_b = summary_b["entry_bid_cents"] if summary_b["entry_bid_cents"] is not None else -1
        if bid_a == bid_b:
            return None
        favored_first = bid_a > bid_b
    else:
        favored_first = ask_a > ask_b

    if favored_first:
        return {
            "favorite_market": market_a,
            "favorite_summary": summary_a,
            "underdog_market": market_b,
            "underdog_summary": summary_b,
        }

    return {
        "favorite_market": market_b,
        "favorite_summary": summary_b,
        "underdog_market": market_a,
        "underdog_summary": summary_a,
    }


def settlement_pnl_cents(entry_ask_cents, won_market):
    if entry_ask_cents is None:
        return None
    if won_market:
        return 100 - entry_ask_cents
    return -entry_ask_cents


def add_market_summary(row, prefix, market, summary):
    ask = summary["ask"]
    bid = summary["bid"]

    row[f"{prefix}_market_ticker"] = market.get("ticker")
    row[f"{prefix}_team"] = market.get("yes_sub_title")
    row[f"{prefix}_result"] = market.get("result")
    row[f"{prefix}_candle_count"] = summary["candle_count"]
    row[f"{prefix}_candles_with_volume"] = summary["candles_with_volume"]
    row[f"{prefix}_total_volume"] = summary["total_volume"]
    row[f"{prefix}_entry_ask_cents"] = summary["entry_ask_cents"]
    row[f"{prefix}_entry_bid_cents"] = summary["entry_bid_cents"]
    row[f"{prefix}_entry_spread_cents"] = summary["entry_spread_cents"]
    row[f"{prefix}_entry_ask_ts"] = ask["first_open_ts"]
    row[f"{prefix}_entry_bid_ts"] = bid["first_open_ts"]
    row[f"{prefix}_last_pre_tip_ask_cents"] = ask["last_close_cents"]
    row[f"{prefix}_last_pre_tip_bid_cents"] = bid["last_close_cents"]
    row[f"{prefix}_last_pre_tip_ask_ts"] = ask["last_close_ts"]
    row[f"{prefix}_last_pre_tip_bid_ts"] = bid["last_close_ts"]
    row[f"{prefix}_min_pre_tip_ask_cents"] = ask["min_open_cents"]
    row[f"{prefix}_max_pre_tip_ask_cents"] = ask["max_open_cents"]
    row[f"{prefix}_min_pre_tip_bid_cents"] = bid["min_close_cents"]
    row[f"{prefix}_max_pre_tip_bid_cents"] = bid["max_close_cents"]
    row[f"{prefix}_min_pre_tip_ask_ts"] = ask["min_open_ts"]
    row[f"{prefix}_max_pre_tip_ask_ts"] = ask["max_open_ts"]
    row[f"{prefix}_min_pre_tip_bid_ts"] = bid["min_close_ts"]
    row[f"{prefix}_max_pre_tip_bid_ts"] = bid["max_close_ts"]
    row[f"{prefix}_min_traded_pre_tip_bid_cents"] = bid["min_traded_close_cents"]
    row[f"{prefix}_max_traded_pre_tip_bid_cents"] = bid["max_traded_close_cents"]
    row[f"{prefix}_min_traded_pre_tip_bid_ts"] = bid["min_traded_close_ts"]
    row[f"{prefix}_max_traded_pre_tip_bid_ts"] = bid["max_traded_close_ts"]
    row[f"{prefix}_ask_quote_count"] = ask["quote_count"]
    row[f"{prefix}_bid_quote_count"] = bid["quote_count"]
    row[f"{prefix}_traded_bid_quote_count"] = bid["traded_close_count"]


def build_research_row(event_ticker, pair, game_metadata, summaries, season_date_ranges):
    market_a, market_b = pair
    summary_a, summary_b = summaries

    role_map = choose_favorite_and_underdog(market_a, market_b, summary_a, summary_b)
    if role_map is None:
        return None, "could_not_determine_favorite"

    favorite_market = role_map["favorite_market"]
    favorite_summary = role_map["favorite_summary"]
    underdog_market = role_map["underdog_market"]
    underdog_summary = role_map["underdog_summary"]

    underdog_entry = underdog_summary["entry_ask_cents"]
    underdog_best_bid = underdog_summary["bid"]["max_traded_close_cents"]
    underdog_last_bid = underdog_summary["bid"]["last_close_cents"]
    favorite_entry = favorite_summary["entry_ask_cents"]
    favorite_low_ask = favorite_summary["ask"]["min_open_cents"]
    favorite_low_bid = favorite_summary["bid"]["min_traded_close_cents"]

    game_date = game_metadata["scheduled_tipoff_utc"].date()
    row = {
        "Event_Ticker": event_ticker,
        "Date": game_date.isoformat(),
        "Game_Label": game_metadata["game_label"],
        "Game_Phase": game_label_to_phase(game_metadata["game_label"]),
        "Season": season_string_for_date(game_date),
        "Days_To_Season_End": (
            (season_date_ranges.get(season_string_for_date(game_date), [None, None])[1] - game_date).days
            if season_date_ranges.get(season_string_for_date(game_date), [None, None])[1] is not None
            else None
        ),
        "Home_Team": game_metadata["home_team"],
        "Away_Team": game_metadata["away_team"],
        "Scheduled_Tipoff_UTC": game_metadata["scheduled_tipoff_utc"].isoformat(),
        "Adjusted_Tipoff_UTC": game_metadata["adjusted_tipoff_utc"].isoformat(),
        "Window_Start_UTC": (
            game_metadata["adjusted_tipoff_utc"] - timedelta(minutes=PRE_TIP_LOOKBACK_MINUTES)
        ).isoformat(),
        "Window_End_UTC": (
            game_metadata["adjusted_tipoff_utc"] - timedelta(minutes=PRE_TIP_CUTOFF_MINUTES)
        ).isoformat(),
        "Window_Lookback_Minutes": PRE_TIP_LOOKBACK_MINUTES,
        "Window_Cutoff_Minutes": PRE_TIP_CUTOFF_MINUTES,
        "Favorite_Determined_By": "highest_entry_ask_then_highest_entry_bid",
        "Und_Best_Bid_PreTip_PnL_Cents": (
            underdog_best_bid - underdog_entry
            if underdog_entry is not None and underdog_best_bid is not None
            else None
        ),
        "Und_Last_PreTip_Bid_PnL_Cents": (
            underdog_last_bid - underdog_entry
            if underdog_entry is not None and underdog_last_bid is not None
            else None
        ),
        "Und_Hold_To_Settle_PnL_Cents": settlement_pnl_cents(
            underdog_entry,
            underdog_market.get("result") == "yes",
        ),
        "Favorite_Hold_To_Settle_PnL_Cents": settlement_pnl_cents(
            favorite_entry,
            favorite_market.get("result") == "yes",
        ),
        "Und_Target_5c_Hit_PreTip": (
            underdog_best_bid is not None
            and underdog_entry is not None
            and (underdog_best_bid - underdog_entry) >= 5
        ),
        "Und_Target_10c_Hit_PreTip": (
            underdog_best_bid is not None
            and underdog_entry is not None
            and (underdog_best_bid - underdog_entry) >= 10
        ),
        "Und_Target_15c_Hit_PreTip": (
            underdog_best_bid is not None
            and underdog_entry is not None
            and (underdog_best_bid - underdog_entry) >= 15
        ),
        "Fav_Low_Ask_PreTip_Cents": favorite_low_ask,
        "Fav_Low_Bid_PreTip_Cents": favorite_low_bid,
        "Favorite_Won": favorite_market.get("result") == "yes",
        "Underdog_Won": underdog_market.get("result") == "yes",
    }

    add_market_summary(row, "fav", favorite_market, favorite_summary)
    add_market_summary(row, "und", underdog_market, underdog_summary)

    return row, None


def process_event_pair(event_ticker, pair, schedule_index, season_date_ranges, candle_executor):
    if len(pair) != 2:
        print(f"Skipping {event_ticker}: expected 2 markets, got {len(pair)}")
        return None

    game_metadata = get_game_metadata(event_ticker, schedule_index)
    if not game_metadata:
        reason = classify_missing_game(event_ticker, season_date_ranges)
        print(f"Skipping {event_ticker}: {reason}")
        return None

    window_start = game_metadata["adjusted_tipoff_utc"] - timedelta(
        minutes=PRE_TIP_LOOKBACK_MINUTES
    )
    window_end = game_metadata["adjusted_tipoff_utc"] - timedelta(
        minutes=PRE_TIP_CUTOFF_MINUTES
    )
    if window_start >= window_end:
        print(f"Skipping {event_ticker}: invalid window")
        return None

    market_a, market_b = pair
    candles_a, candles_b = fetch_event_candles(
        market_a,
        market_b,
        int(window_start.timestamp()),
        int(window_end.timestamp()),
        candle_executor,
    )

    if not candles_a or not candles_b:
        return None

    summary_a = summarize_market_window(candles_a)
    summary_b = summarize_market_window(candles_b)

    if summary_a["entry_ask_cents"] is None or summary_b["entry_ask_cents"] is None:
        return None

    row, reason = build_research_row(
        event_ticker, pair, game_metadata, (summary_a, summary_b), season_date_ranges
    )
    if row is not None:
        return row

    print(f"Skipping {event_ticker}: {reason}")
    return None


def build_dataset_rows(markets, schedule_index, existing_dataset_keys):
    event_to_markets = defaultdict(list)
    for market in markets:
        event_to_markets[market.get("event_ticker")].append(market)

    season_date_ranges = build_season_date_ranges(schedule_index)
    dataset_rows = []
    work_items = []
    skipped = 0

    for event_ticker, pair in sorted(event_to_markets.items()):
        if not event_ticker:
            continue

        if event_ticker in existing_dataset_keys:
            continue

        work_items.append((event_ticker, pair))

    total_work_items = len(work_items)
    if total_work_items == 0:
        return dataset_rows

    candle_worker_count = max(MAX_EVENT_WORKERS * MAX_CANDLE_WORKERS_PER_EVENT, 1)
    with ThreadPoolExecutor(max_workers=candle_worker_count) as candle_executor:
        with ThreadPoolExecutor(max_workers=MAX_EVENT_WORKERS) as executor:
            futures = {
                executor.submit(
                    process_event_pair,
                    event_ticker,
                    pair,
                    schedule_index,
                    season_date_ranges,
                    candle_executor,
                ): event_ticker
                for event_ticker, pair in work_items
            }
            for index, future in enumerate(as_completed(futures), start=1):
                event_ticker = futures[future]
                try:
                    row = future.result()
                except Exception as exc:
                    print(f"Worker exception for {event_ticker}: {exc}")
                    row = None

                if row is not None:
                    dataset_rows.append(row)
                else:
                    skipped += 1

                if index % 25 == 0 or index == total_work_items:
                    print(
                        f"Processed {index}/{total_work_items} events "
                        f"(dataset={len(dataset_rows)}, skipped={skipped})"
                    )

    return dataset_rows


def append_rows(csv_filename, rows):
    if not rows:
        return

    fieldnames = list(rows[0].keys())
    file_exists = os.path.exists(csv_filename)

    if file_exists:
        with open(csv_filename, newline="") as csv_file:
            reader = csv.reader(csv_file)
            existing_header = next(reader, None)
        if existing_header != fieldnames:
            raise ValueError(
                f"{csv_filename} schema does not match the current builder output. Remove it and rerun to regenerate."
            )
        ensure_description_row(csv_filename, fieldnames)

    mode = "a" if file_exists else "w"
    with open(csv_filename, mode, newline="") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=fieldnames)
        if not file_exists:
            writer.writeheader()
            writer.writerow(row_to_csv_ready(build_description_row(fieldnames), fieldnames))
        for row in rows:
            writer.writerow(row_to_csv_ready(row, fieldnames))


def main():
    existing_dataset_keys = load_existing_keys(DATASET_FILENAME, "Event_Ticker")

    cutoff = get_historical_cutoff()
    if cutoff:
        print(
            "Historical cutoff market_settled_ts="
            f"{cutoff.get('market_settled_ts')}"
        )

    markets = get_settled_markets()
    required_seasons = infer_required_seasons(markets)
    print(f"Need NBA schedule coverage for seasons: {', '.join(sorted(required_seasons))}")
    schedules_by_season = get_multiseason_schedules(required_seasons)
    if not schedules_by_season:
        print("ERROR: schedule unavailable")
        return
    schedule_index = build_schedule_index(schedules_by_season.values())
    print(
        f"Indexed {len(schedule_index)} NBA schedule entries for constant-time event lookup "
        f"across {len(schedules_by_season)} seasons"
    )

    dataset_rows = build_dataset_rows(
        markets,
        schedule_index,
        existing_dataset_keys,
    )

    append_rows(DATASET_FILENAME, dataset_rows)

    print(f"Research rows added: {len(dataset_rows)}")
    print(f"Dataset file: {DATASET_FILENAME}")


if __name__ == "__main__":
    main()

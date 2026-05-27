"""Multi-sport extension of build_research_dataset.py.

Tests the same favorite-pricing hypothesis (favorites priced >= some cutoff
pre-game have positive EV when bought-and-held to settlement) across multiple
Kalshi sports series. For each sport we pull settled Kalshi markets, join to
the sport's public schedule API to get a true game-start time, then take the
same 15-minute pre-game window of YES-ask candles and compute settlement PnL.

Output: kalshi_multisport_research_dataset.csv with a Sport column.
"""

import csv
import os
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable, Optional
from zoneinfo import ZoneInfo

ET_TZ = ZoneInfo("America/New_York")

from build_nba_research_dataset import (
    BASE_URL,
    DESCRIPTION_PREFIX,
    PRE_TIP_LOOKBACK_MINUTES,
    AVERAGE_TIP_OFF_DELAY_MINUTES,
    REQUEST_TIMEOUT_SECONDS,
    build_schedule_index,
    choose_favorite_and_underdog,
    get_multiseason_schedules,
    get_session,
    load_existing_keys,
    parse_kalshi_date,
    season_string_for_date,
    settlement_pnl_cents,
    summarize_market_window,
)

DATASET_FILENAME = "kalshi_multisport_research_dataset.csv"
MAX_MARKET_FETCH_WORKERS = 2
MAX_EVENT_WORKERS = 2
MAX_CANDLE_WORKERS = 4
MAX_SCHEDULE_DATE_WORKERS = 6

# Shared fixed schema so the CSV stays rectangular as we add sports.
ASK_MIN_FIELDS = [f"fav_ask_min_{i + 1:02d}" for i in range(PRE_TIP_LOOKBACK_MINUTES)]
FIELDNAMES = [
    "Sport",
    "Series_Ticker",
    "Event_Ticker",
    "Date",
    "Home_Team",
    "Away_Team",
    "Scheduled_Start_UTC",
    "Adjusted_Start_UTC",
    "Window_Start_UTC",
    "Favorite_Market_Ticker",
    "Favorite_Team",
    "Favorite_Avg_Ask_Cents",
    "Favorite_Total_Volume",
    "Favorite_Won",
    "Favorite_Hold_To_Settle_PnL_Cents",
] + ASK_MIN_FIELDS


# ----------------------------------------------------------------------------
# Sport configuration
# ----------------------------------------------------------------------------


@dataclass
class SportConfig:
    name: str
    series_ticker: str
    ticker_parser: Callable[[str], Optional[tuple]]
    schedule_fetcher: Callable[[set], dict]


# Known Kalshi team abbreviations per sport. NFL and NHL mix 2- and 3-letter
# codes (e.g. LA/SEA, TB/MTL); we use these sets to disambiguate parsing.
KNOWN_TEAMS_BY_SPORT = {
    "NFL": {
        "ARI", "ATL", "BAL", "BUF", "CAR", "CHI", "CIN", "CLE", "DAL", "DEN",
        "DET", "GB", "HOU", "IND", "JAC", "KC", "LA", "LAC", "LV", "MIA",
        "MIN", "NE", "NO", "NYG", "NYJ", "PHI", "PIT", "SEA", "SF", "TB",
        "TEN", "WAS",
    },
    "NHL": {
        "ANA", "BOS", "BUF", "CAR", "CBJ", "CGY", "CHI", "COL", "DAL", "DET",
        "EDM", "FLA", "LA", "MIN", "MTL", "NJ", "NSH", "NYI", "NYR", "OTT",
        "PHI", "PIT", "SEA", "SJ", "STL", "TB", "TOR", "UTA", "VAN", "VGK",
        "WPG", "WSH",
        # Legacy 3-letter codes appearing in older Kalshi tickers (2024-25 era):
        "LAK", "TBL", "NJD", "SJS",
    },
    "MLB": {
        "ARI", "ATH", "ATL", "BAL", "BOS", "CHC", "CIN", "CLE", "COL", "CWS",
        "DET", "HOU", "KC", "LAA", "LAD", "MIA", "MIL", "MIN", "NYM", "NYY",
        "PHI", "PIT", "SD", "SEA", "SF", "STL", "TB", "TEX", "TOR", "WSH",
        # Kalshi uses AZ for Arizona Diamondbacks (MLB Stats API uses ARI).
        "AZ",
    },
    "NBA": {
        "ATL", "BKN", "BOS", "CHA", "CHI", "CLE", "DAL", "DEN", "DET", "GSW",
        "HOU", "IND", "LAC", "LAL", "MEM", "MIA", "MIL", "MIN", "NOP", "NYK",
        "OKC", "ORL", "PHI", "PHX", "POR", "SAC", "SAS", "TOR", "UTA", "WAS",
    },
}


def _split_team_codes(teams_str, known_teams):
    """Find a valid (team_a, team_b) split of `teams_str` using known codes.

    Tries 3+3, 3+2, 2+3, 2+2 in that priority order (longer codes first).
    Returns the first split where both halves are known, else None.
    """
    total = len(teams_str)
    if known_teams is None:
        if total < 4:
            return None
        # No known-teams hint: fall back to 3+3 (legacy NBA/MLB behavior).
        return teams_str[:3], teams_str[3:]

    for left_len, right_len in ((3, 3), (3, 2), (2, 3), (2, 2)):
        if left_len + right_len != total:
            continue
        left, right = teams_str[:left_len], teams_str[left_len:]
        if left in known_teams and right in known_teams:
            return left, right
    return None


def _try_parse_layout(remainder, with_hhmm, known_teams):
    """Try one date-layout interpretation. Returns (date, matchup, hhmm) or None."""
    date_len = 7 + (4 if with_hhmm else 0)
    if len(remainder) < date_len + 4:
        return None
    date_segment = remainder[:date_len]
    teams_str = remainder[date_len:]

    hhmm = None
    if with_hhmm:
        try:
            hhmm = int(date_segment[-4:])
        except ValueError:
            return None
        raw_date = date_segment[:-4]
    else:
        raw_date = date_segment
    try:
        target_date = datetime.strptime(raw_date, "%y%b%d").date()
    except ValueError:
        return None

    split = _split_team_codes(teams_str, known_teams)
    if split is None:
        return None
    return target_date, tuple(sorted(split)), hhmm


def parse_generic_ticker(
    event_ticker: str,
    series_prefix: str,
    has_start_time: bool = False,
    known_teams=None,
):
    """Parse a Kalshi sports event ticker.

    Base format:    `{SERIES}-{YY}{MMM}{DD}{TEAM1}{TEAM2}`         (NBA, NFL, NHL)
    With start time: `{SERIES}-{YY}{MMM}{DD}{HHMM}{TEAM1}{TEAM2}`  (newer MLB)

    `has_start_time` is a hint, not a hard requirement. MLB tickers from 2025
    omit HHMM while 2026 ones include it; when has_start_time=True we try the
    HHMM layout first, then fall back to the no-HHMM layout.

    Team codes can be 2 or 3 letters when `known_teams` is provided.

    Returns (date, sorted matchup tuple, hhmm_or_None) or None.
    """
    if not event_ticker:
        return None
    ticker_root = (
        event_ticker.rsplit("-", 1)[0] if event_ticker.count("-") >= 2 else event_ticker
    )
    clean = ticker_root.replace("-", "")
    if not clean.startswith(series_prefix):
        return None
    remainder = clean[len(series_prefix):]

    if has_start_time:
        result = _try_parse_layout(remainder, with_hhmm=True, known_teams=known_teams)
        if result is not None:
            return result
        return _try_parse_layout(remainder, with_hhmm=False, known_teams=known_teams)
    return _try_parse_layout(remainder, with_hhmm=False, known_teams=known_teams)


# ----------------------------------------------------------------------------
# Schedule fetchers (one per sport)
# ----------------------------------------------------------------------------


def _append_to_index(index, key, metadata):
    index.setdefault(key, []).append(metadata)


def fetch_nba_schedule(dates):
    if not dates:
        return {}
    seasons = {season_string_for_date(d) for d in dates}
    schedules_by_season = get_multiseason_schedules(seasons)
    single_per_key = build_schedule_index(schedules_by_season.values())
    return {key: [metadata] for key, metadata in single_per_key.items()}


def _fetch_json_safe(url, params=None, headers=None):
    try:
        response = get_session().get(
            url, params=params, headers=headers, timeout=REQUEST_TIMEOUT_SECONDS
        )
        if response.status_code != 200:
            return None
        return response.json()
    except Exception as exc:
        print(f"Schedule fetch failed for {url}: {exc}")
        return None


def _fetch_mlb_team_abbreviations():
    """MLB /schedule omits abbreviations; fetch them from /teams once."""
    data = _fetch_json_safe(
        "https://statsapi.mlb.com/api/v1/teams", params={"sportId": 1}
    )
    if not data:
        return {}
    return {
        team.get("id"): (team.get("abbreviation") or "").upper()
        for team in data.get("teams", [])
        if team.get("id") and team.get("abbreviation")
    }


def _mlb_month_ranges(start_date, end_date):
    """Yield (chunk_start, chunk_end) date pairs covering [start_date, end_date]
    in roughly-monthly chunks."""
    cursor = start_date
    while cursor <= end_date:
        # End-of-month or end_date, whichever comes first.
        year, month = cursor.year, cursor.month
        if month == 12:
            next_first = cursor.replace(year=year + 1, month=1, day=1)
        else:
            next_first = cursor.replace(month=month + 1, day=1)
        chunk_end = min(next_first - timedelta(days=1), end_date)
        yield cursor, chunk_end
        cursor = chunk_end + timedelta(days=1)


def _index_mlb_games_from_payload(payload, team_abbr, index):
    if not payload:
        return 0
    added = 0
    for date_entry in payload.get("dates", []):
        for game in date_entry.get("games", []):
            game_dt = parse_kalshi_date(game.get("gameDate"))
            if not game_dt:
                continue
            home_team = game.get("teams", {}).get("home", {}).get("team", {})
            away_team = game.get("teams", {}).get("away", {}).get("team", {})
            home = team_abbr.get(home_team.get("id"))
            away = team_abbr.get(away_team.get("id"))
            if not home or not away:
                continue
            matchup = tuple(sorted((home, away)))
            local_date = game_dt.astimezone(ET_TZ).date()
            _append_to_index(
                index,
                (local_date, matchup),
                {
                    "home_team": home,
                    "away_team": away,
                    "scheduled_tipoff_utc": game_dt,
                    "adjusted_tipoff_utc": game_dt
                    + timedelta(minutes=AVERAGE_TIP_OFF_DELAY_MINUTES),
                    "game_label": game.get("seriesDescription"),
                },
            )
            added += 1
    return added


def fetch_mlb_schedule(dates):
    if not dates:
        return {}
    # MLB Stats /schedule truncates results when the date range spans many
    # months. Chunk by month and fetch in parallel for completeness.
    # Also pad by 1 day on each side: Kalshi keys by local game date, but the
    # API filters by UTC date — west-coast night games cross midnight UTC.
    min_d = min(dates) - timedelta(days=1)
    max_d = max(dates) + timedelta(days=1)
    team_abbr = _fetch_mlb_team_abbreviations()
    if not team_abbr:
        print("[MLB] failed to fetch team abbreviations; schedule lookups will fail")
        return {}

    chunks = list(_mlb_month_ranges(min_d, max_d))

    def fetch_chunk(chunk_start, chunk_end):
        return _fetch_json_safe(
            "https://statsapi.mlb.com/api/v1/schedule",
            params={
                "sportId": 1,
                "startDate": chunk_start.isoformat(),
                "endDate": chunk_end.isoformat(),
            },
        )

    index = {}
    with ThreadPoolExecutor(max_workers=MAX_SCHEDULE_DATE_WORKERS) as executor:
        futures = [executor.submit(fetch_chunk, s, e) for s, e in chunks]
        for future in as_completed(futures):
            try:
                payload = future.result()
            except Exception as exc:
                print(f"[MLB] schedule chunk failed: {exc}")
                continue
            _index_mlb_games_from_payload(payload, team_abbr, index)
    return index


# Kalshi-side codes that differ from the schedule API. Keys are Kalshi codes,
# values are the API code we should rewrite them to before matching.
KALSHI_TO_API_ABBR = {
    "NFL": {"JAC": "JAX", "WAS": "WSH", "LA": "LAR"},
    "NHL": {"TB": "TBL", "LA": "LAK", "NJ": "NJD", "SJ": "SJS"},
    # MLB Stats API uses the same codes as current (2026) Kalshi (AZ, ATH, etc).
    # 2025-era Kalshi tickers used the older `ARI` code for Arizona — alias to AZ.
    "MLB": {"ARI": "AZ"},
    "NBA": {},
}


def normalize_kalshi_codes(sport_name, matchup):
    aliases = KALSHI_TO_API_ABBR.get(sport_name, {})
    return tuple(sorted(aliases.get(code, code) for code in matchup))


def fetch_nfl_schedule(dates):
    if not dates:
        return {}
    index = {}

    # ESPN groups games by UTC date, but Kalshi tickers use the game's local
    # (ET) date. Sunday Night Football starting 8:20pm ET lands on the next UTC
    # day, so we fan out to (d-1, d, d+1) to make sure we see every game whose
    # local date might map to either neighbor.
    fan_dates = set()
    for d in dates:
        fan_dates.add(d - timedelta(days=1))
        fan_dates.add(d)
        fan_dates.add(d + timedelta(days=1))

    def fetch_one(d):
        return _fetch_json_safe(
            "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard",
            params={"dates": d.strftime("%Y%m%d")},
        )

    seen_event_ids = set()
    with ThreadPoolExecutor(max_workers=MAX_SCHEDULE_DATE_WORKERS) as executor:
        futures = {executor.submit(fetch_one, d): d for d in fan_dates}
        for future in as_completed(futures):
            data = future.result()
            if not data:
                continue
            for event in data.get("events", []):
                event_id = event.get("id")
                if event_id and event_id in seen_event_ids:
                    continue
                if event_id:
                    seen_event_ids.add(event_id)
                game_dt = parse_kalshi_date(event.get("date"))
                if not game_dt:
                    continue
                competitions = event.get("competitions") or []
                if not competitions:
                    continue
                competitors = competitions[0].get("competitors", [])
                home = away = None
                for c in competitors:
                    abbr = (c.get("team") or {}).get("abbreviation")
                    if not abbr:
                        continue
                    if c.get("homeAway") == "home":
                        home = abbr.upper()
                    elif c.get("homeAway") == "away":
                        away = abbr.upper()
                if not home or not away:
                    continue
                matchup = tuple(sorted((home, away)))
                # Kalshi NFL tickers use ET local date; convert UTC start to ET.
                local_date = game_dt.astimezone(ET_TZ).date()
                _append_to_index(
                    index,
                    (local_date, matchup),
                    {
                        "home_team": home,
                        "away_team": away,
                        "scheduled_tipoff_utc": game_dt,
                        "adjusted_tipoff_utc": game_dt
                        + timedelta(minutes=AVERAGE_TIP_OFF_DELAY_MINUTES),
                        "game_label": (event.get("season") or {}).get("slug"),
                    },
                )
    return index


def fetch_nhl_schedule(dates):
    if not dates:
        return {}
    # NHL's /v1/schedule/{YYYY-MM-DD} returns the full week containing that date.
    # Bucket dates to week starts (Monday) to minimize duplicate fetches.
    week_starts = sorted({d - timedelta(days=d.weekday()) for d in dates})
    index = {}

    def fetch_one(d):
        return _fetch_json_safe(f"https://api-web.nhle.com/v1/schedule/{d.isoformat()}")

    with ThreadPoolExecutor(max_workers=MAX_SCHEDULE_DATE_WORKERS) as executor:
        futures = {executor.submit(fetch_one, d): d for d in week_starts}
        for future in as_completed(futures):
            data = future.result()
            if not data:
                continue
            for game_week in data.get("gameWeek", []):
                for game in game_week.get("games", []):
                    game_dt = parse_kalshi_date(game.get("startTimeUTC"))
                    if not game_dt:
                        continue
                    home = (game.get("homeTeam") or {}).get("abbrev")
                    away = (game.get("awayTeam") or {}).get("abbrev")
                    if not home or not away:
                        continue
                    home = home.upper()
                    away = away.upper()
                    matchup = tuple(sorted((home, away)))
                    local_date = game_dt.astimezone(ET_TZ).date()
                    _append_to_index(
                        index,
                        (local_date, matchup),
                        {
                            "home_team": home,
                            "away_team": away,
                            "scheduled_tipoff_utc": game_dt,
                            "adjusted_tipoff_utc": game_dt
                            + timedelta(minutes=AVERAGE_TIP_OFF_DELAY_MINUTES),
                            "game_label": str(game.get("gameType", "")),
                        },
                    )
    return index


SPORTS = [
    SportConfig(
        "NBA",
        "KXNBAGAME",
        lambda t: parse_generic_ticker(
            t, "KXNBAGAME", known_teams=KNOWN_TEAMS_BY_SPORT["NBA"]
        ),
        fetch_nba_schedule,
    ),
    SportConfig(
        "NFL",
        "KXNFLGAME",
        lambda t: parse_generic_ticker(
            t, "KXNFLGAME", known_teams=KNOWN_TEAMS_BY_SPORT["NFL"]
        ),
        fetch_nfl_schedule,
    ),
    SportConfig(
        "MLB",
        "KXMLBGAME",
        lambda t: parse_generic_ticker(
            t, "KXMLBGAME", has_start_time=True, known_teams=KNOWN_TEAMS_BY_SPORT["MLB"]
        ),
        fetch_mlb_schedule,
    ),
    SportConfig(
        "NHL",
        "KXNHLGAME",
        lambda t: parse_generic_ticker(
            t, "KXNHLGAME", known_teams=KNOWN_TEAMS_BY_SPORT["NHL"]
        ),
        fetch_nhl_schedule,
    ),
]


# ----------------------------------------------------------------------------
# Kalshi market + candle fetchers (parameterized by series)
# ----------------------------------------------------------------------------


def fetch_paginated_markets(series_ticker, url, source_name, extra_params=None):
    markets = []
    cursor = None
    max_retries = 6
    while True:
        params = {"series_ticker": series_ticker, "limit": 1000}
        if extra_params:
            params.update(extra_params)
        if cursor:
            params["cursor"] = cursor

        data = None
        for attempt in range(max_retries):
            try:
                response = get_session().get(
                    url, params=params, timeout=REQUEST_TIMEOUT_SECONDS
                )
                if response.status_code == 200:
                    data = response.json()
                    break
                if response.status_code == 429 or response.status_code >= 500:
                    wait_seconds = min(30, 2 * (attempt + 1))
                    print(
                        f"[{series_ticker}] {response.status_code} on {source_name}; "
                        f"sleeping {wait_seconds}s (attempt {attempt + 1}/{max_retries})"
                    )
                    time.sleep(wait_seconds)
                    continue
                print(
                    f"[{series_ticker}] error {response.status_code} fetching {source_name}: "
                    f"{response.text[:200]}"
                )
                break
            except Exception as exc:
                wait_seconds = min(30, 2 * (attempt + 1))
                print(
                    f"[{series_ticker}] exception on {source_name}: {exc}; "
                    f"sleeping {wait_seconds}s (attempt {attempt + 1}/{max_retries})"
                )
                time.sleep(wait_seconds)

        if data is None:
            print(f"[{series_ticker}] giving up on {source_name} after {max_retries} attempts")
            break

        batch = data.get("markets", [])
        for market in batch:
            market = dict(market)
            market["_market_source"] = source_name
            markets.append(market)
        cursor = data.get("cursor")
        if not cursor or not batch:
            break
        time.sleep(0.5)
    return markets


def get_settled_markets_for_sport(series_ticker):
    with ThreadPoolExecutor(max_workers=MAX_MARKET_FETCH_WORKERS) as executor:
        live_future = executor.submit(
            fetch_paginated_markets,
            series_ticker,
            f"{BASE_URL}/markets",
            "live",
            {"status": "settled"},
        )
        hist_future = executor.submit(
            fetch_paginated_markets,
            series_ticker,
            f"{BASE_URL}/historical/markets",
            "historical",
        )
        live, hist = live_future.result(), hist_future.result()
    deduped = {}
    for market in live + hist:
        ticker = market.get("ticker")
        if ticker:
            deduped[ticker] = market
    markets = list(deduped.values())
    print(f"[{series_ticker}] fetched {len(markets)} settled markets")
    return markets


def get_candlesticks(series_ticker, market, start_ts, end_ts):
    market_ticker = market["ticker"]
    source = market.get("_market_source", "live")
    params = {"period_interval": 1, "start_ts": start_ts, "end_ts": end_ts}
    if source == "historical":
        urls = [
            f"{BASE_URL}/historical/markets/{market_ticker}/candlesticks",
            f"{BASE_URL}/series/{series_ticker}/markets/{market_ticker}/candlesticks",
        ]
    else:
        urls = [
            f"{BASE_URL}/series/{series_ticker}/markets/{market_ticker}/candlesticks",
            f"{BASE_URL}/historical/markets/{market_ticker}/candlesticks",
        ]

    for attempt in range(5):
        retry = False
        for url in urls:
            try:
                r = get_session().get(url, params=params, timeout=REQUEST_TIMEOUT_SECONDS)
                if r.status_code == 200:
                    candles = r.json().get("candlesticks", [])
                    candles.sort(key=lambda c: c.get("end_period_ts", 0))
                    return candles
                if r.status_code == 404:
                    continue
                if r.status_code == 429 or r.status_code >= 500:
                    retry = True
                    continue
            except Exception:
                retry = True
                continue
        if retry:
            time.sleep(3 * (attempt + 1))
            continue
        return []
    return []


# ----------------------------------------------------------------------------
# Per-event pipeline
# ----------------------------------------------------------------------------


def resolve_events_to_metadata(event_to_markets, schedule_index, parser, sport_name=None):
    """Map each event_ticker to a single schedule entry.

    For matchup-dates with multiple games (MLB doubleheaders), sort the events
    by ticker HHMM and the schedule entries by start time, then pair by order.
    This avoids timezone math while still uniquely resolving each event.
    """
    grouped = defaultdict(list)
    for event_ticker in event_to_markets:
        parsed = parser(event_ticker)
        if not parsed:
            continue
        target_date, matchup, hhmm = parsed
        if sport_name:
            matchup = normalize_kalshi_codes(sport_name, matchup)
        grouped[(target_date, matchup)].append((hhmm, event_ticker))

    resolved = {}
    for key, entries in grouped.items():
        schedule_entries = schedule_index.get(key, [])
        if not schedule_entries:
            continue
        entries.sort(key=lambda x: (x[0] is None, x[0] if x[0] is not None else 0))
        schedule_sorted = sorted(schedule_entries, key=lambda m: m["scheduled_tipoff_utc"])
        for i, (_, event_ticker) in enumerate(entries):
            if i < len(schedule_sorted):
                resolved[event_ticker] = schedule_sorted[i]
    return resolved


def process_event(sport, event_ticker, pair, metadata, candle_executor):
    if len(pair) != 2 or metadata is None:
        return None

    scheduled = metadata["scheduled_tipoff_utc"]
    window_start = scheduled - timedelta(minutes=PRE_TIP_LOOKBACK_MINUTES)
    window_end = scheduled
    if window_start >= window_end:
        return None
    window_start_ts = int(window_start.timestamp())
    window_end_ts = int(window_end.timestamp())

    market_a, market_b = pair
    fa = candle_executor.submit(
        get_candlesticks, sport.series_ticker, market_a, window_start_ts, window_end_ts
    )
    fb = candle_executor.submit(
        get_candlesticks, sport.series_ticker, market_b, window_start_ts, window_end_ts
    )
    candles_a, candles_b = fa.result(), fb.result()
    if not candles_a or not candles_b:
        return None

    summary_a = summarize_market_window(candles_a, window_start_ts)
    summary_b = summarize_market_window(candles_b, window_start_ts)
    if summary_a["avg_ask_cents"] is None or summary_b["avg_ask_cents"] is None:
        return None

    role = choose_favorite_and_underdog(market_a, market_b, summary_a, summary_b)
    if role is None:
        return None

    favorite_market = role["favorite_market"]
    favorite_summary = role["favorite_summary"]
    entry = favorite_summary["avg_ask_cents"]
    won = favorite_market.get("result") == "yes"

    row = {
        "Sport": sport.name,
        "Series_Ticker": sport.series_ticker,
        "Event_Ticker": event_ticker,
        "Date": scheduled.date().isoformat(),
        "Home_Team": metadata["home_team"],
        "Away_Team": metadata["away_team"],
        "Scheduled_Start_UTC": scheduled.isoformat(),
        "Adjusted_Start_UTC": metadata["adjusted_tipoff_utc"].isoformat(),
        "Window_Start_UTC": window_start.isoformat(),
        "Favorite_Market_Ticker": favorite_market.get("ticker"),
        "Favorite_Team": favorite_market.get("yes_sub_title"),
        "Favorite_Avg_Ask_Cents": entry,
        "Favorite_Total_Volume": favorite_summary["total_volume"],
        "Favorite_Won": won,
        "Favorite_Hold_To_Settle_PnL_Cents": settlement_pnl_cents(entry, won),
    }
    for i, price in enumerate(favorite_summary["ask_series"]):
        row[f"fav_ask_min_{i + 1:02d}"] = price
    return row


def build_dataset_for_sport(sport, existing_keys):
    markets = get_settled_markets_for_sport(sport.series_ticker)
    if not markets:
        return []

    sample_tickers = [m.get("event_ticker") for m in markets[:3] if m.get("event_ticker")]
    print(f"[{sport.name}] sample event tickers: {sample_tickers}", flush=True)

    event_to_markets = defaultdict(list)
    for market in markets:
        event_ticker = market.get("event_ticker")
        if not event_ticker or event_ticker in existing_keys:
            continue
        event_to_markets[event_ticker].append(market)

    if not event_to_markets:
        print(f"[{sport.name}] nothing new to process")
        return []

    dates = set()
    for event_ticker in event_to_markets:
        parsed = sport.ticker_parser(event_ticker)
        if parsed:
            dates.add(parsed[0])

    print(f"[{sport.name}] fetching schedule across {len(dates)} unique dates")
    schedule_index = sport.schedule_fetcher(dates)
    total_schedule_games = sum(len(v) for v in schedule_index.values())
    print(
        f"[{sport.name}] indexed {total_schedule_games} schedule games "
        f"across {len(schedule_index)} matchup-dates"
    )

    resolved = resolve_events_to_metadata(
        event_to_markets, schedule_index, sport.ticker_parser, sport.name
    )
    unresolved = len(event_to_markets) - len(resolved)
    if unresolved:
        print(
            f"[{sport.name}] {unresolved} events have no schedule match (will be skipped)",
            flush=True,
        )
        parse_failures = 0
        no_match_count = 0
        parse_fail_samples_shown = 0
        no_match_samples_shown = 0
        SAMPLE_LIMIT = 5
        for event_ticker in event_to_markets:
            if event_ticker in resolved:
                continue
            parsed = sport.ticker_parser(event_ticker)
            if not parsed:
                parse_failures += 1
                if parse_fail_samples_shown < SAMPLE_LIMIT:
                    print(f"  [{sport.name}] PARSE FAIL: {event_ticker}", flush=True)
                    parse_fail_samples_shown += 1
                continue
            no_match_count += 1
            if no_match_samples_shown < SAMPLE_LIMIT:
                target_date, matchup, _ = parsed
                normalized = normalize_kalshi_codes(sport.name, matchup)
                schedule_for_date = [k for k in schedule_index if k[0] == target_date][:6]
                print(
                    f"  [{sport.name}] NO MATCH: {event_ticker} "
                    f"(date={target_date}, kalshi_matchup={matchup}, normalized={normalized}) "
                    f"-- schedule keys for that date: {schedule_for_date}",
                    flush=True,
                )
                no_match_samples_shown += 1
        print(
            f"  [{sport.name}] totals: {parse_failures} parse failures, "
            f"{no_match_count} schedule-no-match",
            flush=True,
        )

    rows = []
    work = list(event_to_markets.items())
    total = len(work)
    skipped = 0

    with ThreadPoolExecutor(max_workers=MAX_CANDLE_WORKERS) as candle_executor:
        with ThreadPoolExecutor(max_workers=MAX_EVENT_WORKERS) as executor:
            futures = {
                executor.submit(
                    process_event,
                    sport,
                    event_ticker,
                    pair,
                    resolved.get(event_ticker),
                    candle_executor,
                ): event_ticker
                for event_ticker, pair in work
            }
            for i, future in enumerate(as_completed(futures), 1):
                event_ticker = futures[future]
                try:
                    row = future.result()
                except Exception as exc:
                    print(f"[{sport.name}] {event_ticker} error: {exc}")
                    row = None
                if row is not None:
                    rows.append(row)
                else:
                    skipped += 1
                if i % 25 == 0 or i == total:
                    print(
                        f"[{sport.name}] processed {i}/{total} "
                        f"(rows={len(rows)}, skipped={skipped})"
                    )

    print(f"[{sport.name}] built {len(rows)} rows")
    return rows


# ----------------------------------------------------------------------------
# CSV output
# ----------------------------------------------------------------------------


def describe_field(name):
    descriptions = {
        "Sport": "Sport name, e.g. NBA, NFL, MLB, NHL.",
        "Series_Ticker": "Kalshi series ticker for the sport.",
        "Event_Ticker": "Kalshi event ticker; unique identifier for the game.",
        "Date": "Scheduled game-start date in UTC.",
        "Home_Team": "Home team code from the sport's schedule API.",
        "Away_Team": "Away team code from the sport's schedule API.",
        "Scheduled_Start_UTC": "Scheduled game-start time from the public schedule.",
        "Adjusted_Start_UTC": "Scheduled start + 12 minutes to approximate true start.",
        "Window_Start_UTC": "15 minutes before scheduled start.",
        "Favorite_Market_Ticker": "Kalshi market ticker for the favorite side.",
        "Favorite_Team": "Team name from yes_sub_title for the favorite side.",
        "Favorite_Avg_Ask_Cents": "Average YES ask (cents) across valid 1-minute candles in the pre-game window.",
        "Favorite_Total_Volume": "Total contracts traded in the pre-game window for the favorite.",
        "Favorite_Won": "True if the favorite YES contract settled at $1.",
        "Favorite_Hold_To_Settle_PnL_Cents": "Per-share PnL if entered at avg ask and held to settlement; (100 - entry) if won, (-entry) if lost.",
    }
    if name in descriptions:
        return descriptions[name]
    if name.startswith("fav_ask_min_"):
        minute = name[len("fav_ask_min_"):]
        return f"YES ask close (cents) at minute {minute} of the pre-game window; empty if no candle."
    return f"Generated field '{name}'."


def append_rows(filename, rows):
    if not rows:
        return
    file_exists = os.path.exists(filename)

    if file_exists:
        with open(filename, newline="") as csv_file:
            reader = csv.reader(csv_file)
            existing_header = next(reader, None)
        if existing_header != FIELDNAMES:
            raise ValueError(
                f"{filename} schema mismatch with current builder. "
                "Remove the file and rerun to regenerate."
            )

    existing_keys = load_existing_keys(filename, "Event_Ticker")
    fresh_rows = [r for r in rows if str(r.get("Event_Ticker", "")) not in existing_keys]
    if not fresh_rows:
        return

    mode = "a" if file_exists else "w"
    with open(filename, mode, newline="") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=FIELDNAMES)
        if not file_exists:
            writer.writeheader()
            writer.writerow(
                {field: f"{DESCRIPTION_PREFIX}{describe_field(field)}" for field in FIELDNAMES}
            )
        for row in fresh_rows:
            writer.writerow(
                {field: ("" if row.get(field) is None else row.get(field)) for field in FIELDNAMES}
            )


def main():
    existing_keys = load_existing_keys(DATASET_FILENAME, "Event_Ticker")
    all_rows = []

    with ThreadPoolExecutor(max_workers=len(SPORTS)) as executor:
        futures = {}
        for i, sport in enumerate(SPORTS):
            if i > 0:
                # Stagger cold-start so we don't burst Kalshi's rate limiter with
                # 4 simultaneous paginated market fetches.
                time.sleep(5)
            futures[executor.submit(build_dataset_for_sport, sport, existing_keys)] = sport
        for future in as_completed(futures):
            sport = futures[future]
            try:
                sport_rows = future.result()
                all_rows.extend(sport_rows)
            except Exception as exc:
                print(f"[{sport.name}] failed: {exc}")

    append_rows(DATASET_FILENAME, all_rows)
    print(f"\nTotal new rows: {len(all_rows)}")
    print(f"Dataset file: {DATASET_FILENAME}")


if __name__ == "__main__":
    main()

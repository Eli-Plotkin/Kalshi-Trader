from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from difflib import SequenceMatcher
from uuid import uuid4

from .config import ScannerConfig
from .schemas import EventMapping, FairPrice, KalshiMarketSnapshot, SportsbookEvent

try:
    from zoneinfo import ZoneInfo

    EASTERN = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover - missing tzdata on some platforms
    EASTERN = None


_MONTH_ABBREVIATIONS: dict[str, int] = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}


TEAM_ALIASES: dict[str, str] = {
    "okc": "oklahoma city thunder",
    "oklahoma city": "oklahoma city thunder",
    "sas": "san antonio spurs",
    "san antonio": "san antonio spurs",
    "nyk": "new york knicks",
    "new york": "new york knicks",
    "lal": "los angeles lakers",
    "la lakers": "los angeles lakers",
    "lac": "los angeles clippers",
    "la clippers": "los angeles clippers",
}


def normalize_team_name(value: str) -> str:
    text = re.sub(r"[^a-z0-9 ]+", " ", value.lower())
    text = re.sub(r"\s+", " ", text).strip()
    return TEAM_ALIASES.get(text, text)


def title_similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, normalize_team_name(a), normalize_team_name(b)).ratio()


def game_date_from_ticker(ticker: str) -> date | None:
    """Extract the game date embedded in a Kalshi sports ticker.

    Sports tickers carry the date of the game in the venue's *local* (Eastern)
    calendar, e.g. `KXNBAGAME-26JAN14LALBOS-LAL` -> 2026-01-14.

    Verified against all 6,188 events in `kalshi_multisport_research_dataset.csv`
    (NBA, NFL, MLB, NHL): the embedded date equals the Eastern-time game date
    in 100% of rows. It differs from the *UTC* date for any game starting after
    7pm ET — 64% of the NBA/NFL sample — so comparing it to a UTC date would
    reject most evening games.

    Some series append a start time after the date (`KXMLBGAME-25JUL141310STLDET`
    is a 13:10 game); anchoring the match to the leading `-` and taking exactly
    seven characters keeps those correct.

    Month names are resolved from an explicit table rather than `strptime("%b")`,
    which reads locale-specific abbreviations — under `LC_TIME=tr_TR` the months
    are "Oca/Şub/..." and every Kalshi ticker would fail to parse, silently
    disabling the same-game date check.

    Returns None when no date is present (`KXNBAGAME-XYZ-LAL`) or the date is
    not a real calendar date. Callers must treat None as "no signal" rather
    than as a mismatch.
    """
    match = re.search(r"-(\d{2})([A-Z]{3})(\d{2})", ticker.upper())
    if not match:
        return None
    year, month_abbr, day = match.groups()
    month = _MONTH_ABBREVIATIONS.get(month_abbr)
    if month is None:
        return None
    try:
        # Kalshi tickers are contemporary; 2000-2099 avoids strptime's
        # 1969/2068 pivot entirely.
        return date(2000 + int(year), month, int(day))
    except ValueError:
        return None


def _eastern_game_dates(commence: datetime) -> set[date]:
    """Eastern calendar date(s) a UTC start time could correspond to.

    Exact when tzdata is available. Without it, Eastern is either UTC-4 (EDT)
    or UTC-5 (EST), so the local date is the UTC date or the day before —
    accepting both keeps the check useful (it still rejects a ticker off by two
    or more days) instead of dropping it entirely.
    """
    if EASTERN is not None:
        return {commence.astimezone(EASTERN).date()}
    return {commence.date(), commence.date() - timedelta(days=1)}


def _as_utc(value: datetime) -> datetime:
    """Treat naive timestamps as UTC so arithmetic never raises."""
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def check_temporal_consistency(
    market: KalshiMarketSnapshot,
    event: SportsbookEvent,
    config: ScannerConfig,
) -> tuple[float, list[str]]:
    """Decide whether the market and event describe the same scheduled game.

    Returns `(confidence, flags)`. Two independent signals:

    1. The game date embedded in the Kalshi ticker must match the event's
       Eastern-time date. This is the strong same-game check: it rules out
       binding to the same fixture on a different day, which is a live failure
       mode on this venue because Kalshi opens game markets days ahead.
    2. `close_time` must fall in a plausible settlement window *after* the
       event starts. Closing before the start means this is not the market we
       think it is (or is a period/half market); closing implausibly long
       after suggests a different event entirely.
    """
    flags: list[str] = []
    confidence = 1.0
    commence = _as_utc(event.commence_time)

    game_date = game_date_from_ticker(market.ticker)
    if game_date is not None:
        expected = _eastern_game_dates(commence)
        if game_date not in expected:
            expected_str = "|".join(str(d) for d in sorted(expected))
            flags.append(f"game_date_mismatch:{game_date}!={expected_str}")
            confidence = 0.0

    if market.close_time is not None:
        offset_minutes = (
            _as_utc(market.close_time) - commence
        ).total_seconds() / 60.0
        if offset_minutes < -config.max_close_before_commence_minutes:
            flags.append(f"closes_before_start_minutes:{-offset_minutes:.1f}")
            confidence = 0.0
        elif offset_minutes > config.max_settlement_window_minutes:
            excess = offset_minutes - config.max_settlement_window_minutes
            flags.append(f"settlement_offset_minutes:{offset_minutes:.1f}")
            confidence = min(confidence, max(0.0, 1.0 - excess / 240.0))

    return confidence, flags


def infer_yes_outcome(market: KalshiMarketSnapshot, event: SportsbookEvent) -> tuple[str | None, float]:
    """Infer whether Kalshi YES maps to home or away.

    This is deliberately conservative. Future work can add an LLM verifier, but
    this function should remain the first hard gate.
    """
    yes_text = market.yes_subtitle or market.title
    home_score = title_similarity(yes_text, event.home_team)
    away_score = title_similarity(yes_text, event.away_team)
    if max(home_score, away_score) < 0.55:
        return None, max(home_score, away_score)
    if abs(home_score - away_score) < 0.08:
        return None, max(home_score, away_score)
    return ("home" if home_score > away_score else "away"), max(home_score, away_score)


def build_mapping(
    *,
    market: KalshiMarketSnapshot,
    event: SportsbookEvent,
    config: ScannerConfig,
    created_at: datetime | None = None,
) -> EventMapping:
    created_at = created_at or datetime.now(timezone.utc)
    flags: list[str] = []

    yes_outcome, team_conf = infer_yes_outcome(market, event)
    if yes_outcome is None:
        flags.append("ambiguous_yes_outcome")
        yes_outcome = "home"

    time_conf, time_flags = check_temporal_consistency(market, event, config)
    flags.extend(time_flags)

    confidence = min(team_conf, time_conf)
    return EventMapping(
        mapping_id=str(uuid4()),
        kalshi_ticker=market.ticker,
        sportsbook_event_id=event.event_id,
        mapped_yes_outcome=yes_outcome,  # type: ignore[arg-type]
        confidence=confidence,
        mismatch_flags=flags,
        created_at=created_at,
    )


def fair_yes_probability(mapping: EventMapping, fair_price: FairPrice) -> float:
    return fair_price.home_prob if mapping.mapped_yes_outcome == "home" else fair_price.away_prob


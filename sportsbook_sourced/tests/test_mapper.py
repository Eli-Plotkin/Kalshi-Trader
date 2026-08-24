from __future__ import annotations

import locale
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from sportsbook_sourced import mapper
from sportsbook_sourced.config import DEFAULT_SCANNER_CONFIG
from sportsbook_sourced.mapper import (
    build_mapping,
    fair_yes_probability,
    game_date_from_ticker,
    infer_yes_outcome,
    normalize_team_name,
    title_similarity,
)
from sportsbook_sourced.schemas import (
    EventMapping,
    FairPrice,
    KalshiMarketSnapshot,
    SportsbookEvent,
)


# ────────────────────────────────────────────────────────────────────────────
# normalize_team_name + title_similarity
# ────────────────────────────────────────────────────────────────────────────


def test_normalize_collapses_punctuation_and_whitespace():
    assert normalize_team_name("  Los Angeles,  LAKERS!  ") == "los angeles lakers"


def test_normalize_resolves_tricode():
    assert normalize_team_name("OKC") == "oklahoma city thunder"
    assert normalize_team_name("gsw") == "golden state warriors"


def test_normalize_passthrough_for_unknown():
    assert normalize_team_name("Boston Celtics") == "boston celtics"
    # Multi-word shorthand is not a tricode and is left as-is; title_similarity's
    # token-subset matching (not normalize_team_name) is what resolves it.
    assert normalize_team_name("San Antonio") == "san antonio"


def test_title_similarity_exact_match():
    assert title_similarity("Lakers", "lakers") == 1.0


def test_title_similarity_tricode_resolves():
    # Both normalize to "oklahoma city thunder"
    assert title_similarity("OKC", "Oklahoma City Thunder") == 1.0


def test_title_similarity_short_name_is_a_token_subset_of_the_full_name():
    """Kalshi always sends short names ("Lakers", "76ers"); sportsbooks send
    full club names. A short name whose tokens are a subset of the full
    name's tokens is the same team, regardless of the length difference that
    sinks character-overlap scoring."""
    assert title_similarity("Lakers", "Los Angeles Lakers") == 1.0
    assert title_similarity("Warriors", "Golden State Warriors") == 1.0
    assert title_similarity("76ers", "Philadelphia 76ers") == 1.0
    assert title_similarity("San Antonio", "San Antonio Spurs") == 1.0


def test_title_similarity_unrelated_low():
    assert title_similarity("Lakers", "Celtics") < 0.5


# ────────────────────────────────────────────────────────────────────────────
# infer_yes_outcome
# ────────────────────────────────────────────────────────────────────────────


def _market(
    *,
    title: str = "Will the Lakers beat the Celtics?",
    yes_subtitle: str | None = "Lakers",
    close_time: datetime | None = None,
) -> KalshiMarketSnapshot:
    return KalshiMarketSnapshot(
        ticker="KXNBAGAME-XYZ-LAL",
        title=title,
        yes_subtitle=yes_subtitle,
        close_time=close_time,
        yes_bid_cents=50,
        yes_ask_cents=52,
        volume=1000.0,
        open_interest=500.0,
        collected_at=datetime(2026, 5, 20, 0, 0, tzinfo=timezone.utc),
    )


def _event(
    *,
    home: str = "Lakers",
    away: str = "Celtics",
    commence_time: datetime | None = None,
) -> SportsbookEvent:
    return SportsbookEvent(
        event_id="e1",
        league="nba",
        home_team=home,
        away_team=away,
        commence_time=commence_time or datetime(2026, 5, 20, 19, 30, tzinfo=timezone.utc),
    )


def test_infer_yes_outcome_clear_home():
    market = _market(yes_subtitle="Lakers")
    event = _event(home="Lakers", away="Celtics")
    outcome, conf = infer_yes_outcome(market, event)
    assert outcome == "home"
    assert conf > 0.9


def test_infer_yes_outcome_clear_away():
    market = _market(yes_subtitle="Celtics")
    event = _event(home="Lakers", away="Celtics")
    outcome, conf = infer_yes_outcome(market, event)
    assert outcome == "away"
    assert conf > 0.9


def test_infer_yes_outcome_falls_back_to_title():
    # No yes_subtitle, but title leans heavily home
    market = _market(yes_subtitle=None, title="Lakers")
    event = _event(home="Lakers", away="Celtics")
    outcome, conf = infer_yes_outcome(market, event)
    assert outcome == "home"
    assert conf > 0.9


def test_infer_yes_outcome_ambiguous_returns_none():
    """Neither team is a plausible match — rejected by the < 0.55 floor,
    the same branch that fires when a market has no team name at all."""
    # Generic title that doesn't favor either side
    market = _market(yes_subtitle=None, title="NBA Game")
    event = _event(home="Lakers", away="Celtics")
    outcome, conf = infer_yes_outcome(market, event)
    assert outcome is None
    assert conf < 0.55


def test_infer_yes_outcome_too_close_to_call():
    """Both teams clear the plausibility floor but can't be told apart —
    a distinct rejection path from `test_infer_yes_outcome_ambiguous_returns_none`.
    `infer_yes_outcome` returns None for both, so `conf` is the only signal
    that distinguishes "no match" from "matched, but which one?".

    "Los Angeles" is a real same-city ambiguity: it's a token subset of both
    full club names, so both sides saturate at 1.0 and the tie-break (score
    diff < 0.08) is what rejects — not the plausibility floor.
    """
    market = _market(yes_subtitle="Los Angeles")
    event = _event(home="Los Angeles Lakers", away="Los Angeles Clippers")
    outcome, conf = infer_yes_outcome(market, event)
    assert outcome is None
    assert conf >= 0.55, (
        "expected to fail the tie-break (scores within 0.08), not the "
        "plausibility floor — got a low score instead"
    )


# ────────────────────────────────────────────────────────────────────────────
# build_mapping
# ────────────────────────────────────────────────────────────────────────────


def test_build_mapping_high_confidence_clean():
    now = datetime(2026, 5, 20, 0, 0, tzinfo=timezone.utc)
    commence = datetime(2026, 5, 20, 19, 30, tzinfo=timezone.utc)
    market = _market(yes_subtitle="Lakers", close_time=commence)
    event = _event(home="Lakers", away="Celtics", commence_time=commence)
    mapping = build_mapping(
        market=market, event=event, config=DEFAULT_SCANNER_CONFIG, created_at=now
    )
    assert mapping.mapped_yes_outcome == "home"
    assert mapping.confidence > 0.9
    assert mapping.mismatch_flags == []


def test_build_mapping_flags_ambiguous_yes_outcome():
    now = datetime(2026, 5, 20, 0, 0, tzinfo=timezone.utc)
    market = _market(yes_subtitle=None, title="NBA Game")
    event = _event(home="Lakers", away="Celtics")
    mapping = build_mapping(
        market=market, event=event, config=DEFAULT_SCANNER_CONFIG, created_at=now
    )
    assert "ambiguous_yes_outcome" in mapping.mismatch_flags
    # An ambiguous match must not be recorded as a definite side (P0.3): a
    # coin-flip stored as "home" would silently invert the fair probability
    # for half of all ambiguous markets the moment the confidence gate loosens.
    assert mapping.mapped_yes_outcome is None


@pytest.mark.parametrize("settlement_hours", [0, 1.5, 3, 5, 8])
def test_build_mapping_accepts_normal_settlement_offsets(settlement_hours):
    """Kalshi game markets close for settlement, hours after tip-off.

    A ~2.5h NBA game typically closes 3-4h after start and NFL runs longer, so
    every value here is a routine offset, not a mapping error.
    """
    now = datetime(2026, 5, 20, 0, 0, tzinfo=timezone.utc)
    commence = datetime(2026, 5, 20, 19, 30, tzinfo=timezone.utc)
    market = _market(
        yes_subtitle="Lakers",
        close_time=commence + timedelta(hours=settlement_hours),
    )
    event = _event(home="Lakers", away="Celtics", commence_time=commence)
    mapping = build_mapping(
        market=market, event=event, config=DEFAULT_SCANNER_CONFIG, created_at=now
    )
    assert mapping.mismatch_flags == []
    assert mapping.confidence > 0.9


def test_build_mapping_flags_close_before_start():
    """A market closing before the event starts is not the market we think it
    is — most likely a period/half market or a different fixture."""
    now = datetime(2026, 5, 20, 0, 0, tzinfo=timezone.utc)
    commence = datetime(2026, 5, 20, 19, 30, tzinfo=timezone.utc)
    market = _market(yes_subtitle="Lakers", close_time=commence - timedelta(hours=2))
    event = _event(home="Lakers", away="Celtics", commence_time=commence)
    mapping = build_mapping(
        market=market, event=event, config=DEFAULT_SCANNER_CONFIG, created_at=now
    )
    assert any(f.startswith("closes_before_start_minutes:")
               for f in mapping.mismatch_flags)
    assert mapping.confidence == 0.0


def test_build_mapping_flags_implausible_settlement_window():
    """3 days past commence is 3840 minutes past the 480-minute window — far
    enough that the confidence penalty saturates at 0, not just "reduced"."""
    now = datetime(2026, 5, 20, 0, 0, tzinfo=timezone.utc)
    commence = datetime(2026, 5, 20, 19, 30, tzinfo=timezone.utc)
    market = _market(yes_subtitle="Lakers", close_time=commence + timedelta(days=3))
    event = _event(home="Lakers", away="Celtics", commence_time=commence)
    mapping = build_mapping(
        market=market, event=event, config=DEFAULT_SCANNER_CONFIG, created_at=now
    )
    assert any(f.startswith("settlement_offset_minutes:")
               for f in mapping.mismatch_flags)
    assert mapping.confidence == 0.0


def test_build_mapping_settlement_window_boundary_is_exclusive():
    """`max_settlement_window_minutes` (480) is compared with a strict `>`, so
    exactly 480 minutes is accepted (covered by the settlement_hours=8 case in
    `test_build_mapping_accepts_normal_settlement_offsets`) and 481 is not.
    Without this, a fencepost slip in that comparison has no failing test on
    the reject side of the boundary.
    """
    now = datetime(2026, 5, 20, 0, 0, tzinfo=timezone.utc)
    commence = datetime(2026, 5, 20, 19, 30, tzinfo=timezone.utc)
    market = _market(yes_subtitle="Lakers",
                     close_time=commence + timedelta(minutes=481))
    event = _event(home="Lakers", away="Celtics", commence_time=commence)
    mapping = build_mapping(
        market=market, event=event, config=DEFAULT_SCANNER_CONFIG, created_at=now
    )
    assert any(f.startswith("settlement_offset_minutes:")
               for f in mapping.mismatch_flags)
    assert mapping.confidence < 1.0


def test_build_mapping_no_close_time_passes_time_check():
    """Missing close_time leaves the settlement check with nothing to test."""
    now = datetime(2026, 5, 20, 0, 0, tzinfo=timezone.utc)
    market = _market(yes_subtitle="Lakers", close_time=None)
    event = _event(home="Lakers", away="Celtics")
    mapping = build_mapping(
        market=market, event=event, config=DEFAULT_SCANNER_CONFIG, created_at=now
    )
    assert mapping.mismatch_flags == []


# ────────────────────────────────────────────────────────────────────────────
# game_date_from_ticker / same-game date check
# ────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("ticker,expected", [
    # Shapes taken verbatim from kalshi_multisport_research_dataset.csv.
    ("KXNBAGAME-26JAN14LALBOS-LAL", date(2026, 1, 14)),
    ("KXNFLGAME-26JAN18HOUNE-NE", date(2026, 1, 18)),
    ("KXNBAGAME-26MAY25NYKCLE", date(2026, 5, 25)),
    ("KXNHLGAME-26JAN14LALBOS", date(2026, 1, 14)),
    # MLB appends a start time after the date; the date must still win.
    ("KXMLBGAME-25JUL141310STLDET", date(2025, 7, 14)),
    ("KXMLBGAME-25JUL140705CINPHI", date(2025, 7, 14)),
])
def test_game_date_from_ticker_parses_real_tickers(ticker, expected):
    assert game_date_from_ticker(ticker) == expected


@pytest.mark.parametrize("ticker", [
    "KXNBAGAME-XYZ-LAL",         # no date component
    "KXNBAGAME-26FOO14LALBOS",   # unparseable month
    "KXNBAGAME-26JAN32LALBOS",   # day out of range
    "KXNBAGAME-26FEB30LALBOS",   # not a real calendar date
    "KXNBAGAME-1226JAN14LALBOS", # digits break the dash anchor
    "SOMETHING-ELSE",
    "",
])
def test_game_date_from_ticker_returns_none_when_unparseable(ticker):
    assert game_date_from_ticker(ticker) is None


def test_game_date_from_ticker_is_case_insensitive():
    assert game_date_from_ticker("kxnbagame-26jan14lalbos-lal") == date(2026, 1, 14)


@pytest.mark.parametrize("loc", ["C", "en_US.UTF-8", "de_DE.UTF-8", "tr_TR.UTF-8"])
def test_game_date_from_ticker_is_locale_independent(loc):
    """`strptime("%b")` reads locale month abbreviations, so under Turkish
    ("Oca/Şub/...") every Kalshi ticker would fail to parse and silently
    disable the same-game date check. Month names come from a fixed table.
    """
    previous = locale.setlocale(locale.LC_TIME)
    try:
        try:
            locale.setlocale(locale.LC_TIME, loc)
        except locale.Error:
            pytest.skip(f"locale {loc} unavailable")
        assert game_date_from_ticker("KXNBAGAME-26JAN14LALBOS") == date(2026, 1, 14)
    finally:
        locale.setlocale(locale.LC_TIME, previous)


@pytest.mark.parametrize("et_local,ticker_date", [
    (datetime(2026, 3, 8, 20, 0), "26MAR08"),    # day US DST begins
    (datetime(2026, 11, 1, 20, 0), "26NOV01"),   # day US DST ends
    (datetime(2026, 1, 14, 22, 30), "26JAN14"),  # late tip, next UTC day
    (datetime(2026, 1, 14, 12, 0), "26JAN14"),   # matinee, same UTC day
    (datetime(2026, 7, 4, 19, 0), "26JUL04"),    # mid-DST evening
])
def test_ticker_date_check_survives_dst_transitions(et_local, ticker_date):
    """The Eastern date is resolved with real tz rules, so the UTC offset
    changing between EST and EDT must not produce a spurious mismatch."""
    eastern = ZoneInfo("America/New_York")
    commence = et_local.replace(tzinfo=eastern).astimezone(timezone.utc)
    market = replace(
        _market(yes_subtitle="Lakers", close_time=commence + timedelta(hours=3)),
        ticker=f"KXNBAGAME-{ticker_date}LALBOS-LAL",
    )
    event = _event(home="Lakers", away="Celtics", commence_time=commence)
    mapping = build_mapping(
        market=market, event=event, config=DEFAULT_SCANNER_CONFIG,
        created_at=commence - timedelta(hours=1),
    )
    assert mapping.mismatch_flags == []
    assert mapping.confidence > 0.9


class TestEasternDateFallbackWithoutTzdata:
    """`zoneinfo` needs a tz database, which some platforms lack. The fallback
    must degrade to a usable check rather than skipping it silently.
    """

    @staticmethod
    def _mapping_without_tzdata(monkeypatch, ticker):
        monkeypatch.setattr(mapper, "EASTERN", None)
        commence = datetime(2026, 1, 15, 1, 0, tzinfo=timezone.utc)  # 8pm ET Jan 14
        market = replace(
            _market(yes_subtitle="Lakers",
                    close_time=commence + timedelta(hours=3)),
            ticker=ticker,
        )
        event = _event(home="Lakers", away="Celtics", commence_time=commence)
        return build_mapping(
            market=market, event=event, config=DEFAULT_SCANNER_CONFIG,
            created_at=commence - timedelta(hours=1),
        )

    def test_accepts_the_eastern_date(self, monkeypatch):
        mapping = self._mapping_without_tzdata(
            monkeypatch, "KXNBAGAME-26JAN14LALBOS-LAL")
        assert mapping.mismatch_flags == []

    def test_accepts_the_utc_date(self, monkeypatch):
        """Without tz rules both candidates must pass — a matinee's Eastern
        date equals its UTC date."""
        mapping = self._mapping_without_tzdata(
            monkeypatch, "KXNBAGAME-26JAN15LALBOS-LAL")
        assert mapping.mismatch_flags == []

    def test_still_rejects_a_date_two_days_off(self, monkeypatch):
        mapping = self._mapping_without_tzdata(
            monkeypatch, "KXNBAGAME-26JAN17LALBOS-LAL")
        assert any(f.startswith("game_date_mismatch:")
                   for f in mapping.mismatch_flags)
        assert mapping.confidence == 0.0


# Same-game-date rejection and the UTC/Eastern distinction are covered by
# test_ticker_date_check_survives_dst_transitions (above) and by
# TestMappingRealPayloads::test_ticker_date_binds_the_mapping_to_the_right_calendar_day
# in test_integration.py, which exercises it against a production-shaped
# payload rather than a hand-built one.


# ────────────────────────────────────────────────────────────────────────────
# fair_yes_probability
# ────────────────────────────────────────────────────────────────────────────


def _fair_price(home_prob: float = 0.6) -> FairPrice:
    return FairPrice(
        event_id="e1",
        league="nba",
        market_type="moneyline",
        home_team="Lakers",
        away_team="Celtics",
        home_prob=home_prob,
        away_prob=1.0 - home_prob,
        source_count=2,
        sharp_source_count=1,
        staleness_seconds=10,
        book_disagreement_cents=1.0,
        confidence=0.95,
        computed_at=datetime(2026, 5, 20, 0, 0, tzinfo=timezone.utc),
    )


def _mapping(mapped: str = "home") -> EventMapping:
    return EventMapping(
        mapping_id="m1",
        kalshi_ticker="KXNBAGAME-XYZ-LAL",
        sportsbook_event_id="e1",
        mapped_yes_outcome=mapped,  # type: ignore[arg-type]
        confidence=0.95,
        mismatch_flags=[],
        created_at=datetime(2026, 5, 20, 0, 0, tzinfo=timezone.utc),
    )


def test_fair_yes_probability_home_mapping():
    assert fair_yes_probability(_mapping("home"), _fair_price(0.6)) == 0.6


def test_fair_yes_probability_away_mapping():
    assert fair_yes_probability(_mapping("away"), _fair_price(0.6)) == 0.4

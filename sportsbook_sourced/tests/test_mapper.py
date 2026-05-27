from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sportsbook_sourced.config import DEFAULT_SCANNER_CONFIG, ScannerConfig
from sportsbook_sourced.mapper import (
    build_mapping,
    fair_yes_probability,
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


def test_normalize_resolves_alias():
    assert normalize_team_name("OKC") == "oklahoma city thunder"
    assert normalize_team_name("San Antonio") == "san antonio spurs"


def test_normalize_passthrough_for_unknown():
    assert normalize_team_name("Boston Celtics") == "boston celtics"


def test_title_similarity_exact_match():
    assert title_similarity("Lakers", "lakers") == 1.0


def test_title_similarity_alias_resolves():
    # Both normalize to "oklahoma city thunder"
    assert title_similarity("OKC", "Oklahoma City Thunder") == 1.0


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


def test_infer_yes_outcome_falls_back_to_title():
    # No yes_subtitle, but title leans heavily home
    market = _market(yes_subtitle=None, title="Lakers")
    event = _event(home="Lakers", away="Celtics")
    outcome, conf = infer_yes_outcome(market, event)
    assert outcome == "home"


def test_infer_yes_outcome_ambiguous_returns_none():
    # Generic title that doesn't favor either side
    market = _market(yes_subtitle=None, title="NBA Game")
    event = _event(home="Lakers", away="Celtics")
    outcome, conf = infer_yes_outcome(market, event)
    assert outcome is None


def test_infer_yes_outcome_too_close_to_call():
    # yes_subtitle is similar to both teams — should reject as ambiguous
    market = _market(yes_subtitle="The Team")
    event = _event(home="The Lakers", away="The Celtics")
    outcome, conf = infer_yes_outcome(market, event)
    assert outcome is None


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
    # Falls back to home
    assert mapping.mapped_yes_outcome == "home"


def test_build_mapping_flags_time_delta():
    now = datetime(2026, 5, 20, 0, 0, tzinfo=timezone.utc)
    commence = datetime(2026, 5, 20, 19, 30, tzinfo=timezone.utc)
    # Kalshi close_time is 90 minutes after game start — beyond default 30min threshold
    market_close = commence + timedelta(minutes=90)
    market = _market(yes_subtitle="Lakers", close_time=market_close)
    event = _event(home="Lakers", away="Celtics", commence_time=commence)
    mapping = build_mapping(
        market=market, event=event, config=DEFAULT_SCANNER_CONFIG, created_at=now
    )
    assert any(f.startswith("time_delta_minutes:") for f in mapping.mismatch_flags)
    # Confidence should drop below the clean-match level
    assert mapping.confidence < 0.9


def test_build_mapping_no_close_time_passes_time_check():
    """Missing close_time is not flagged — known limitation, see project status."""
    now = datetime(2026, 5, 20, 0, 0, tzinfo=timezone.utc)
    market = _market(yes_subtitle="Lakers", close_time=None)
    event = _event(home="Lakers", away="Celtics")
    mapping = build_mapping(
        market=market, event=event, config=DEFAULT_SCANNER_CONFIG, created_at=now
    )
    # Time check is skipped entirely when close_time is None
    assert all(not f.startswith("time_delta_minutes:") for f in mapping.mismatch_flags)


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

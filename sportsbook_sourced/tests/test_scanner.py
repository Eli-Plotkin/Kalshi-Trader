from __future__ import annotations

from datetime import datetime, timezone

import pytest

from sportsbook_sourced.config import ScannerConfig
from sportsbook_sourced.scanner import scan_opportunity
from sportsbook_sourced.schemas import EventMapping, FairPrice, KalshiMarketSnapshot


# ────────────────────────────────────────────────────────────────────────────
# Fixtures
# ────────────────────────────────────────────────────────────────────────────


NOW = datetime(2026, 5, 20, 0, 0, tzinfo=timezone.utc)


def _market(*, yes_bid: int = 40, yes_ask: int = 42) -> KalshiMarketSnapshot:
    return KalshiMarketSnapshot(
        ticker="KXNBAGAME-LAL",
        title="Lakers vs Celtics",
        yes_subtitle="Lakers",
        close_time=NOW,
        yes_bid_cents=yes_bid,
        yes_ask_cents=yes_ask,
        volume=1000.0,
        open_interest=500.0,
        collected_at=NOW,
    )


def _fair(*, home_prob: float = 0.6, sharp: int = 2, source: int = 3,
          stale: int = 30, disagreement: float = 1.0) -> FairPrice:
    return FairPrice(
        event_id="e1",
        league="nba",
        market_type="moneyline",
        home_team="Lakers",
        away_team="Celtics",
        home_prob=home_prob,
        away_prob=1.0 - home_prob,
        source_count=source,
        sharp_source_count=sharp,
        staleness_seconds=stale,
        book_disagreement_cents=disagreement,
        confidence=0.95,
        computed_at=NOW,
    )


def _mapping(*, mapped: str = "home", confidence: float = 0.95,
             flags: list[str] | None = None) -> EventMapping:
    return EventMapping(
        mapping_id="m1",
        kalshi_ticker="KXNBAGAME-LAL",
        sportsbook_event_id="e1",
        mapped_yes_outcome=mapped,  # type: ignore[arg-type]
        confidence=confidence,
        mismatch_flags=flags or [],
        created_at=NOW,
    )


def _config(**overrides) -> ScannerConfig:
    base = dict(
        min_net_edge_cents=3.0,
        min_mapping_confidence=0.92,
        max_odds_staleness_seconds=180,
        max_commence_time_delta_minutes=30,
        max_book_disagreement_cents=8.0,
        min_source_count=2,
        min_sharp_source_count=1,
        max_position_usd=10.0,
        liquidity_buffer_cents=1.0,
        stale_odds_buffer_cents=1.0,
        mapping_risk_buffer_cents=1.0,
    )
    base.update(overrides)
    return ScannerConfig(**base)


# ────────────────────────────────────────────────────────────────────────────
# Side selection: scanner picks the higher-edge side
# ────────────────────────────────────────────────────────────────────────────


def test_scanner_picks_yes_when_yes_underpriced():
    # Fair YES = 60%, Kalshi yes_ask = 42c → gross = 60 - 42 = 18c
    # Fair NO = 40%, Kalshi no_ask = 100-40 = 60c → gross = 40 - 60 = -20c
    market = _market(yes_bid=40, yes_ask=42)
    opp = scan_opportunity(
        market=market,
        fair_price=_fair(home_prob=0.6),
        mapping=_mapping(),
        config=_config(),
        computed_at=NOW,
    )
    assert opp.side == "yes"
    assert opp.gross_edge_cents > 0


def test_scanner_picks_no_when_no_underpriced():
    # Fair YES = 30%, Kalshi yes_ask = 42 → YES edge = 30-42 = -12
    # Fair NO = 70%, Kalshi no_ask = 100-40 = 60 → NO edge = 70-60 = 10
    market = _market(yes_bid=40, yes_ask=42)
    opp = scan_opportunity(
        market=market,
        fair_price=_fair(home_prob=0.3),
        mapping=_mapping(),
        config=_config(),
        computed_at=NOW,
    )
    assert opp.side == "no"
    assert opp.gross_edge_cents > 0


# ────────────────────────────────────────────────────────────────────────────
# Action gating
# ────────────────────────────────────────────────────────────────────────────


def test_scanner_buy_action_when_all_gates_pass():
    market = _market(yes_bid=40, yes_ask=42)
    opp = scan_opportunity(
        market=market,
        fair_price=_fair(home_prob=0.6),
        mapping=_mapping(),
        config=_config(),
        computed_at=NOW,
    )
    # Gross edge 18c, fee at 42c ≈ 1.71c, buffers 3c → net ≈ 13c > 3c threshold
    assert opp.action == "buy"
    assert opp.reason == "tradeable"


def test_scanner_skips_on_low_mapping_confidence():
    opp = scan_opportunity(
        market=_market(),
        fair_price=_fair(home_prob=0.6),
        mapping=_mapping(confidence=0.5),
        config=_config(),
        computed_at=NOW,
    )
    assert opp.action == "skip"
    assert "mapping_confidence" in opp.reason


def test_scanner_skips_on_mapping_flags():
    opp = scan_opportunity(
        market=_market(),
        fair_price=_fair(home_prob=0.6),
        mapping=_mapping(flags=["ambiguous_yes_outcome"]),
        config=_config(),
        computed_at=NOW,
    )
    assert opp.action == "skip"
    assert "mapping_flags" in opp.reason


def test_scanner_skips_on_low_source_count():
    opp = scan_opportunity(
        market=_market(),
        fair_price=_fair(home_prob=0.6, source=1),
        mapping=_mapping(),
        config=_config(),
        computed_at=NOW,
    )
    assert opp.action == "skip"
    assert "source_count" in opp.reason


def test_scanner_skips_on_low_sharp_source_count():
    opp = scan_opportunity(
        market=_market(),
        fair_price=_fair(home_prob=0.6, sharp=0),
        mapping=_mapping(),
        config=_config(min_sharp_source_count=1),
        computed_at=NOW,
    )
    assert opp.action == "skip"
    assert "sharp_source_count" in opp.reason


def test_scanner_skips_on_stale_odds():
    opp = scan_opportunity(
        market=_market(),
        fair_price=_fair(home_prob=0.6, stale=600),
        mapping=_mapping(),
        config=_config(),
        computed_at=NOW,
    )
    assert opp.action == "skip"
    assert "odds_stale" in opp.reason


def test_scanner_skips_on_book_disagreement():
    opp = scan_opportunity(
        market=_market(),
        fair_price=_fair(home_prob=0.6, disagreement=15.0),
        mapping=_mapping(),
        config=_config(),
        computed_at=NOW,
    )
    assert opp.action == "skip"
    assert "book_disagreement" in opp.reason


def test_scanner_skips_on_insufficient_net_edge():
    # Fair = 0.45, yes_ask = 42 → gross = 3c, minus fee + 3c buffers → net negative
    market = _market(yes_bid=40, yes_ask=42)
    opp = scan_opportunity(
        market=market,
        fair_price=_fair(home_prob=0.45),
        mapping=_mapping(),
        config=_config(),
        computed_at=NOW,
    )
    assert opp.action == "skip"
    assert "net_edge" in opp.reason


def test_scanner_aggregates_multiple_skip_reasons():
    opp = scan_opportunity(
        market=_market(),
        fair_price=_fair(home_prob=0.6, source=1, sharp=0, stale=600),
        mapping=_mapping(confidence=0.5),
        config=_config(),
        computed_at=NOW,
    )
    assert opp.action == "skip"
    # Multiple reasons joined with semicolons
    assert opp.reason.count(";") >= 2


# ────────────────────────────────────────────────────────────────────────────
# Sizing
# ────────────────────────────────────────────────────────────────────────────


def test_scanner_max_contracts_respects_budget():
    market = _market(yes_bid=40, yes_ask=42)
    opp = scan_opportunity(
        market=market,
        fair_price=_fair(home_prob=0.6),
        mapping=_mapping(),
        config=_config(max_position_usd=10.0),
        computed_at=NOW,
    )
    # $10 / 42c = 23 contracts (floor division)
    assert opp.max_contracts == 23


def test_scanner_zero_contracts_when_budget_too_small():
    market = _market(yes_bid=40, yes_ask=42)
    opp = scan_opportunity(
        market=market,
        fair_price=_fair(home_prob=0.6),
        mapping=_mapping(),
        config=_config(max_position_usd=0.10),  # 10c, less than 42c ask
        computed_at=NOW,
    )
    assert opp.max_contracts == 0
    assert opp.action == "skip"
    assert "no_contracts_for_budget" in opp.reason


# ────────────────────────────────────────────────────────────────────────────
# Fee and buffer arithmetic
# ────────────────────────────────────────────────────────────────────────────


def test_scanner_net_edge_subtracts_fee_and_buffers():
    market = _market(yes_bid=40, yes_ask=42)
    opp = scan_opportunity(
        market=market,
        fair_price=_fair(home_prob=0.6),
        mapping=_mapping(),
        config=_config(),
        computed_at=NOW,
    )
    # gross = 60-42 = 18
    # fee at 42c = 0.07 * 42 * 58 = 170.52/100 = 1.7052c
    # buffers = 3c
    # net = 18 - 1.7052 - 3 ≈ 13.29c
    assert opp.gross_edge_cents == 18.0
    assert opp.fee_cents_per_contract == pytest.approx(0.07 * 42 * 58 / 100.0)
    assert opp.net_edge_cents == pytest.approx(18.0 - opp.fee_cents_per_contract - 3.0)

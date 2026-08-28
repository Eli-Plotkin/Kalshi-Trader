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


def _market(*, yes_bid: int = 40, yes_ask: int = 42,
            raw_orderbook: dict | None = None) -> KalshiMarketSnapshot:
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
        raw_orderbook=raw_orderbook or {},
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
        max_close_before_commence_minutes=15,
        max_settlement_window_minutes=480,
        max_book_disagreement_cents=8.0,
        min_source_count=2,
        min_sharp_source_count=1,
        max_position_usd=10.0,
        liquidity_buffer_cents=1.0,
        stale_odds_buffer_cents=1.0,
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
        fair_price=_fair(home_prob=0.6, source=1, sharp=0),
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
    # buffers = 2c (liquidity + stale; mapping_risk_buffer_cents was removed,
    # see CLAUDE.md P2.10 -- a flat few-cent haircut can't hedge the
    # catastrophic case of a genuinely wrong mapping, and the hard
    # min_mapping_confidence gate is the real defense against that)
    # net = 18 - 1.7052 - 2 ≈ 14.29c
    assert opp.gross_edge_cents == 18.0
    assert opp.fee_cents_per_contract == pytest.approx(0.07 * 42 * 58 / 100.0)
    assert opp.net_edge_cents == pytest.approx(18.0 - opp.fee_cents_per_contract - 2.0)


# ────────────────────────────────────────────────────────────────────────────
# Liquidity buffer scales with orderbook depth (P2.9)
# ────────────────────────────────────────────────────────────────────────────


def test_liquidity_buffer_falls_back_to_flat_when_orderbook_is_unfetched():
    """No raw_orderbook at all (the default) must behave exactly like
    before this feature existed -- the flat config value, not a penalty."""
    market = _market(yes_bid=40, yes_ask=42)  # raw_orderbook defaults to {}
    opp = scan_opportunity(
        market=market, fair_price=_fair(home_prob=0.6), mapping=_mapping(),
        config=_config(), computed_at=NOW,
    )
    assert opp.net_edge_cents == pytest.approx(18.0 - opp.fee_cents_per_contract - 2.0)


def test_liquidity_buffer_shrinks_for_a_deep_book():
    # side="yes" (fair 0.6 vs yes_ask 42c); 500 resting at the top of book,
    # well past the 100-contract reference size -> buffer < the flat 1c.
    market = _market(yes_bid=40, yes_ask=42,
                     raw_orderbook={"yes_dollars": [["0.42", "500"]]})
    opp = scan_opportunity(
        market=market, fair_price=_fair(home_prob=0.6), mapping=_mapping(),
        config=_config(), computed_at=NOW,
    )
    expected_buffer = min(1.0 * 100 / 500, 50.0)
    assert expected_buffer < 1.0
    assert opp.net_edge_cents == pytest.approx(
        18.0 - opp.fee_cents_per_contract - expected_buffer - 1.0)  # +stale


def test_liquidity_buffer_grows_for_a_thin_book_up_to_the_cap():
    market = _market(yes_bid=40, yes_ask=42,
                     raw_orderbook={"yes_dollars": [["0.42", "1"]]})
    opp = scan_opportunity(
        market=market, fair_price=_fair(home_prob=0.6), mapping=_mapping(),
        config=_config(), computed_at=NOW,
    )
    # 1.0 * 100 / 1 = 100, capped at liquidity_buffer_cap_cents (50.0).
    assert opp.net_edge_cents == pytest.approx(
        18.0 - opp.fee_cents_per_contract - 50.0 - 1.0)  # +stale


def test_a_confirmed_empty_book_gets_the_maximum_buffer_and_zero_contracts():
    """A fetched-but-empty side (genuinely zero resting orders) is more
    conservative than an unfetched one, not the same -- see
    kalshi_feed.top_of_book's (0, 0) vs None distinction."""
    market = _market(yes_bid=40, yes_ask=42, raw_orderbook={"yes_dollars": []})
    opp = scan_opportunity(
        market=market, fair_price=_fair(home_prob=0.6), mapping=_mapping(),
        config=_config(), computed_at=NOW,
    )
    assert opp.max_contracts == 0
    assert opp.action == "skip"
    assert "no_contracts_for_budget" in opp.reason


def test_max_contracts_capped_by_a_thin_book_even_with_ample_budget():
    market = _market(yes_bid=40, yes_ask=42,
                     raw_orderbook={"yes_dollars": [["0.42", "3"]]})
    opp = scan_opportunity(
        market=market, fair_price=_fair(home_prob=0.6), mapping=_mapping(),
        config=_config(max_position_usd=10.0),  # budget alone allows 23
        computed_at=NOW,
    )
    assert opp.max_contracts == 3


def test_max_contracts_uses_budget_when_the_book_is_deep_enough():
    market = _market(yes_bid=40, yes_ask=42,
                     raw_orderbook={"yes_dollars": [["0.42", "500"]]})
    opp = scan_opportunity(
        market=market, fair_price=_fair(home_prob=0.6), mapping=_mapping(),
        config=_config(max_position_usd=10.0),
        computed_at=NOW,
    )
    assert opp.max_contracts == 23  # unaffected: book depth isn't the binding constraint

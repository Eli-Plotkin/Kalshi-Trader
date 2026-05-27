from __future__ import annotations

from datetime import datetime, timezone

import pytest

from sportsbook_sourced.evaluation import (
    closing_line_value_cents,
    evaluate_opportunity,
    realized_pnl_cents,
)
from sportsbook_sourced.schemas import Opportunity


NOW = datetime(2026, 5, 20, 0, 0, tzinfo=timezone.utc)


def _opp(*, side: str = "yes", price: int = 42, fair_prob: float = 0.6) -> Opportunity:
    return Opportunity(
        opportunity_id="o1",
        kalshi_ticker="KXNBAGAME-LAL",
        sportsbook_event_id="e1",
        side=side,  # type: ignore[arg-type]
        action="buy",
        fair_prob=fair_prob,
        kalshi_price_cents=price,
        gross_edge_cents=18.0,
        fee_cents_per_contract=1.7,
        net_edge_cents=13.3,
        max_contracts=23,
        reason="tradeable",
        computed_at=NOW,
    )


# ────────────────────────────────────────────────────────────────────────────
# closing_line_value_cents
# ────────────────────────────────────────────────────────────────────────────


def test_clv_yes_positive_when_market_moves_toward_fair():
    # Bought YES at 42c, close fair prob = 0.55 → close value 55c → CLV = +13c
    assert closing_line_value_cents(
        side="yes", entry_price_cents=42, fair_prob_at_close=0.55
    ) == pytest.approx(13.0)


def test_clv_yes_negative_when_market_moves_away():
    # Bought YES at 42c, close fair prob = 0.35 → close value 35c → CLV = -7c
    assert closing_line_value_cents(
        side="yes", entry_price_cents=42, fair_prob_at_close=0.35
    ) == pytest.approx(-7.0)


def test_clv_no_positive_when_close_low():
    # Bought NO at 50c (yes_bid=50, so no_ask=50), close YES prob = 0.30 → close NO = 70c → CLV = +20c
    assert closing_line_value_cents(
        side="no", entry_price_cents=50, fair_prob_at_close=0.30
    ) == pytest.approx(20.0)


def test_clv_no_zero_when_close_matches_entry():
    # Bought NO at 50c, close YES prob = 0.50 → close NO = 50c → CLV = 0
    assert closing_line_value_cents(
        side="no", entry_price_cents=50, fair_prob_at_close=0.50
    ) == pytest.approx(0.0)


def test_clv_unknown_side_raises():
    with pytest.raises(ValueError):
        closing_line_value_cents(side="maybe", entry_price_cents=50, fair_prob_at_close=0.5)


# ────────────────────────────────────────────────────────────────────────────
# realized_pnl_cents
# ────────────────────────────────────────────────────────────────────────────


def test_realized_pnl_yes_win():
    # Bought YES at 42c × 10, resolved yes → exit at 100 → PnL = 10 * (100-42) = 580
    assert realized_pnl_cents(
        side="yes", entry_price_cents=42, count=10, resolved_yes=True
    ) == 580.0


def test_realized_pnl_yes_loss():
    # Bought YES at 42c × 10, resolved no → exit at 0 → PnL = 10 * (0-42) = -420
    assert realized_pnl_cents(
        side="yes", entry_price_cents=42, count=10, resolved_yes=False
    ) == -420.0


def test_realized_pnl_no_win():
    # Bought NO at 50c × 10, resolved no → exit at 100 → PnL = 10 * (100-50) = 500
    assert realized_pnl_cents(
        side="no", entry_price_cents=50, count=10, resolved_yes=False
    ) == 500.0


def test_realized_pnl_no_loss():
    # Bought NO at 50c × 10, resolved yes → exit at 0 → PnL = 10 * (0-50) = -500
    assert realized_pnl_cents(
        side="no", entry_price_cents=50, count=10, resolved_yes=True
    ) == -500.0


def test_realized_pnl_unresolved_returns_none():
    assert realized_pnl_cents(
        side="yes", entry_price_cents=42, count=10, resolved_yes=None
    ) is None


def test_realized_pnl_unknown_side_raises():
    with pytest.raises(ValueError):
        realized_pnl_cents(side="maybe", entry_price_cents=42, count=10, resolved_yes=True)


# ────────────────────────────────────────────────────────────────────────────
# evaluate_opportunity
# ────────────────────────────────────────────────────────────────────────────


def test_evaluate_full_resolution_includes_clv_and_pnl():
    opp = _opp(side="yes", price=42)
    eval_ = evaluate_opportunity(
        opportunity=opp,
        fair_prob_at_close=0.55,
        resolved_yes=True,
        count=10,
        evaluated_at=NOW,
    )
    assert eval_.clv_cents == pytest.approx(13.0)
    assert eval_.pnl_cents == 580.0
    assert eval_.resolved_side == "yes"
    assert eval_.fair_prob_at_close == 0.55


def test_evaluate_unresolved_market_still_has_clv():
    opp = _opp(side="yes", price=42)
    eval_ = evaluate_opportunity(
        opportunity=opp,
        fair_prob_at_close=0.55,
        resolved_yes=None,
        count=10,
    )
    assert eval_.clv_cents == pytest.approx(13.0)
    assert eval_.pnl_cents is None
    assert eval_.resolved_side is None


def test_evaluate_no_close_data_zeros_clv():
    opp = _opp(side="yes", price=42)
    eval_ = evaluate_opportunity(
        opportunity=opp,
        fair_prob_at_close=None,
        resolved_yes=True,
        count=10,
    )
    assert eval_.clv_cents is None
    assert eval_.pnl_cents == 580.0

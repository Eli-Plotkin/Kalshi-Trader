from __future__ import annotations

from datetime import datetime, timezone

import pytest

from sportsbook_sourced.paper import (
    KELLY_FRACTION_MULTIPLIER,
    PaperPortfolio,
    Position,
    kelly_fraction,
    paper_buy,
)
from sportsbook_sourced.schemas import Opportunity


NOW = datetime(2026, 5, 20, 0, 0, tzinfo=timezone.utc)


def _opp(
    *,
    action: str = "buy",
    side: str = "yes",
    price: int = 42,
    max_contracts: int = 10,
    net_edge_cents: float = 13.3,
) -> Opportunity:
    return Opportunity(
        opportunity_id="o1",
        kalshi_ticker="KXNBAGAME-LAL",
        sportsbook_event_id="e1",
        side=side,  # type: ignore[arg-type]
        action=action,  # type: ignore[arg-type]
        fair_prob=0.6,
        kalshi_price_cents=price,
        gross_edge_cents=18.0,
        fee_cents_per_contract=1.7,
        net_edge_cents=net_edge_cents,
        max_contracts=max_contracts,
        reason="tradeable",
        computed_at=NOW,
    )


# ────────────────────────────────────────────────────────────────────────────
# kelly_fraction — pure formula, ported from
# misprice_discovery/kelly_simulator.py::derive_kelly_fractions
# ────────────────────────────────────────────────────────────────────────────


def test_kelly_fraction_matches_the_ported_formula():
    # f = mean_PnL / max_profit_per_share, using net_edge_cents in place of
    # their realized mean PnL (both are already fee/cost-adjusted).
    assert kelly_fraction(net_edge_cents=13.3, price_cents=42) == pytest.approx(13.3 / 58)


def test_kelly_fraction_zero_for_a_nonpositive_edge():
    assert kelly_fraction(net_edge_cents=0.0, price_cents=42) == 0.0
    assert kelly_fraction(net_edge_cents=-5.0, price_cents=42) == 0.0


def test_kelly_fraction_clamped_to_one():
    # An edge larger than max_profit_per_contract shouldn't imply staking
    # more than the whole bankroll on a single contract's worth of edge.
    assert kelly_fraction(net_edge_cents=500.0, price_cents=42) == 1.0


def test_kelly_fraction_zero_when_price_leaves_no_possible_profit():
    # price_cents=100 -> max_profit=0; avoid dividing by zero, and there's
    # no room for a Kalshi contract to pay out more than it costs anyway.
    assert kelly_fraction(net_edge_cents=5.0, price_cents=100) == 0.0


def test_paper_buy_fills_when_cash_sufficient():
    portfolio = PaperPortfolio(cash_cents=10_000, positions={})
    opp = _opp(side="yes", price=42, max_contracts=10)
    order = paper_buy(opp, portfolio=portfolio)
    assert order.status == "filled"
    assert order.count == 10
    assert order.limit_price_cents == 42
    # Cash drawn down by 10 * 42 = 420
    assert portfolio.cash_cents == 10_000 - 420
    position = portfolio.positions["KXNBAGAME-LAL"]
    assert position.yes_count == 10
    assert position.no_count == 0


def test_paper_buy_no_side_records_a_no_count_not_a_negative_yes_count():
    portfolio = PaperPortfolio(cash_cents=10_000, positions={})
    opp = _opp(side="no", price=58, max_contracts=10)
    order = paper_buy(opp, portfolio=portfolio)
    assert order.status == "filled"
    position = portfolio.positions["KXNBAGAME-LAL"]
    assert position.no_count == 10
    assert position.yes_count == 0
    assert portfolio.cash_cents == 10_000 - 580


def test_paper_buy_rejects_when_action_is_skip():
    portfolio = PaperPortfolio(cash_cents=10_000, positions={})
    opp = _opp(action="skip", max_contracts=10)
    order = paper_buy(opp, portfolio=portfolio)
    assert order.status == "rejected"
    assert order.count == 0
    # Cash unchanged
    assert portfolio.cash_cents == 10_000
    assert "KXNBAGAME-LAL" not in portfolio.positions


def test_paper_buy_rejects_when_zero_contracts():
    portfolio = PaperPortfolio(cash_cents=10_000, positions={})
    opp = _opp(max_contracts=0)
    order = paper_buy(opp, portfolio=portfolio)
    assert order.status == "rejected"
    assert portfolio.cash_cents == 10_000


def test_paper_buy_rejects_when_cash_insufficient():
    portfolio = PaperPortfolio(cash_cents=100, positions={})
    opp = _opp(price=42, max_contracts=10)  # cost 420 > 100 cash
    order = paper_buy(opp, portfolio=portfolio)
    assert order.status == "rejected"
    assert portfolio.cash_cents == 100


def test_paper_buy_accumulates_position():
    portfolio = PaperPortfolio(
        cash_cents=10_000, positions={"KXNBAGAME-LAL": Position(yes_count=5)},
    )
    opp = _opp(side="yes", price=42, max_contracts=3)
    paper_buy(opp, portfolio=portfolio)
    assert portfolio.positions["KXNBAGAME-LAL"].yes_count == 8


def test_paper_buy_tracks_yes_and_no_on_the_same_ticker_independently():
    """The exact ambiguity P3.14 raised: 5 YES then 3 NO on one ticker must
    not collapse into a signed net of 2 -- the true exposure (500 payout if
    YES wins, 300 if NO wins) is completely different from what "2 YES
    contracts" would imply."""
    portfolio = PaperPortfolio(cash_cents=10_000, positions={})
    paper_buy(_opp(side="yes", price=42, max_contracts=5), portfolio=portfolio)
    paper_buy(_opp(side="no", price=42, max_contracts=3), portfolio=portfolio)

    position = portfolio.positions["KXNBAGAME-LAL"]
    assert position.yes_count == 5
    assert position.no_count == 3


def test_paper_buy_records_fill_model_version():
    portfolio = PaperPortfolio(cash_cents=10_000, positions={})
    order = paper_buy(_opp(), portfolio=portfolio)
    # Fill model version must be present so backtests / evaluations can
    # tag results with the assumption shape they were generated under.
    assert order.fill_model_version


# ────────────────────────────────────────────────────────────────────────────
# Kelly-proportional sizing (P3.13)
#
# All the tests above use small max_contracts against a generous cash
# balance, so the existing budget/liquidity ceiling was already the binding
# constraint before and after this change -- they exercise the *ceiling*,
# not sizing. These use a deliberately large max_contracts (simulating a
# relaxed budget cap) so Kelly is actually what determines count.
# ────────────────────────────────────────────────────────────────────────────


def test_paper_buy_sizes_below_max_contracts_when_kelly_is_the_binding_constraint():
    portfolio = PaperPortfolio(cash_cents=10_000, positions={})
    # net_edge=3.0 is the smallest edge that ever reaches action="buy"
    # (min_net_edge_cents's default threshold).
    opp = _opp(price=50, net_edge_cents=3.0, max_contracts=1000)
    order = paper_buy(opp, portfolio=portfolio)
    assert order.status == "filled"
    assert order.count == 3
    assert order.count < opp.max_contracts, "the ceiling must not be what decided size here"


def test_paper_buy_a_larger_edge_stakes_more_at_the_same_price_and_cash():
    """The exact complaint P3.13 raised: a 3c edge and a big edge must not
    buy identical position sizes when the ceiling isn't binding."""
    portfolio_small_edge = PaperPortfolio(cash_cents=10_000, positions={})
    portfolio_big_edge = PaperPortfolio(cash_cents=10_000, positions={})

    small_edge_order = paper_buy(
        _opp(price=50, net_edge_cents=3.0, max_contracts=1000),
        portfolio=portfolio_small_edge,
    )
    big_edge_order = paper_buy(
        _opp(price=50, net_edge_cents=30.0, max_contracts=1000),
        portfolio=portfolio_big_edge,
    )

    assert small_edge_order.count == 3
    assert big_edge_order.count == 30
    assert big_edge_order.count > small_edge_order.count


def test_paper_buy_rejects_when_kelly_rounds_down_to_zero_contracts():
    """A viable edge with a genuinely small bankroll should size down to
    nothing rather than force a trade -- distinct from the existing
    max_contracts=0 / insufficient-cash-for-the-ceiling rejection tests."""
    portfolio = PaperPortfolio(cash_cents=100, positions={})
    opp = _opp(price=50, net_edge_cents=3.0, max_contracts=1000)
    order = paper_buy(opp, portfolio=portfolio)
    assert order.status == "rejected"
    assert order.count == 0
    assert portfolio.cash_cents == 100


def test_paper_buy_still_respects_max_contracts_as_a_hard_ceiling():
    """Kelly can suggest more than the ceiling allows (a large edge against
    a large bankroll) -- max_contracts (budget + liquidity depth) must still
    win, since it encodes constraints Kelly doesn't know about."""
    portfolio = PaperPortfolio(cash_cents=1_000_000, positions={})
    opp = _opp(price=50, net_edge_cents=40.0, max_contracts=5)
    order = paper_buy(opp, portfolio=portfolio)
    assert order.status == "filled"
    assert order.count == 5


def test_kelly_fraction_multiplier_is_a_quarter():
    # Pin the specific constant, not just its effect -- a future accidental
    # edit (e.g. to 0.5) would still pass every sizing test above with
    # different but plausible-looking numbers unless the constant itself is
    # checked directly.
    assert KELLY_FRACTION_MULTIPLIER == 0.25

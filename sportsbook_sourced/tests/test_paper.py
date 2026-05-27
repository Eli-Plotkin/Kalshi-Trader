from __future__ import annotations

from datetime import datetime, timezone

from sportsbook_sourced.paper import PaperPortfolio, paper_buy
from sportsbook_sourced.schemas import Opportunity


NOW = datetime(2026, 5, 20, 0, 0, tzinfo=timezone.utc)


def _opp(
    *,
    action: str = "buy",
    side: str = "yes",
    price: int = 42,
    max_contracts: int = 10,
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
        net_edge_cents=13.3,
        max_contracts=max_contracts,
        reason="tradeable",
        computed_at=NOW,
    )


def test_paper_buy_fills_when_cash_sufficient():
    portfolio = PaperPortfolio(cash_cents=10_000, positions={})
    opp = _opp(side="yes", price=42, max_contracts=10)
    order = paper_buy(opp, portfolio=portfolio)
    assert order.status == "filled"
    assert order.count == 10
    assert order.limit_price_cents == 42
    # Cash drawn down by 10 * 42 = 420
    assert portfolio.cash_cents == 10_000 - 420
    # Long YES → positive position
    assert portfolio.positions["KXNBAGAME-LAL"] == 10


def test_paper_buy_no_side_records_negative_position():
    portfolio = PaperPortfolio(cash_cents=10_000, positions={})
    opp = _opp(side="no", price=58, max_contracts=10)
    order = paper_buy(opp, portfolio=portfolio)
    assert order.status == "filled"
    # NO side is signed negative in the position map
    assert portfolio.positions["KXNBAGAME-LAL"] == -10
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
    portfolio = PaperPortfolio(cash_cents=10_000, positions={"KXNBAGAME-LAL": 5})
    opp = _opp(side="yes", price=42, max_contracts=3)
    paper_buy(opp, portfolio=portfolio)
    assert portfolio.positions["KXNBAGAME-LAL"] == 8


def test_paper_buy_records_fill_model_version():
    portfolio = PaperPortfolio(cash_cents=10_000, positions={})
    order = paper_buy(_opp(), portfolio=portfolio)
    # Fill model version must be present so backtests / evaluations can
    # tag results with the assumption shape they were generated under.
    assert order.fill_model_version

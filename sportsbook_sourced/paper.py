from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import uuid4

from .schemas import Opportunity, PaperOrder


FILL_MODEL_VERSION = "immediate_best_ask_v1"

# Quarter Kelly: full Kelly assumes fair_prob is exactly correct, but it's a
# de-vigged blend of sportsbook lines with real disagreement/staleness noise
# baked in -- betting the full fraction against an estimate that can be wrong
# is how Kelly sizing blows up bankrolls. Quarter is the standard practical
# hedge against that model-error risk, and matches this package's already
# conservative posture (gates, buffers, a hard minimum edge threshold).
KELLY_FRACTION_MULTIPLIER = 0.25


@dataclass
class PaperPortfolio:
    cash_cents: int
    positions: dict[str, int]


def kelly_fraction(*, net_edge_cents: float, price_cents: int) -> float:
    """Fraction of bankroll a single contract's edge justifies staking.

    Ported from `misprice_discovery/kelly_simulator.py::derive_kelly_fractions`
    (`f = mean_PnL / max_profit_per_share`), not reinvented: `net_edge_cents`
    (already fee- and buffer-adjusted) stands in for their realized mean PnL,
    and `100 - price_cents` is the identical max-profit-per-contract
    definition (a Kalshi contract can only ever pay out 100).

    Clamped to `[0.0, 1.0]` -- a negative edge stakes nothing rather than
    shorting the position implicitly, and a fraction can never exceed the
    whole bankroll regardless of how large the edge looks.
    """
    max_profit = 100 - price_cents
    if max_profit <= 0:
        return 0.0
    return max(0.0, min(net_edge_cents / max_profit, 1.0))


def paper_buy(opportunity: Opportunity, *, portfolio: PaperPortfolio) -> PaperOrder:
    """Very simple paper fill model for scanner validation.

    Position size is `min(opportunity.max_contracts, quarter-Kelly count)` --
    `max_contracts` (budget + P2.9's liquidity-depth cap) stays a ceiling
    computed once at scan time, but the actual stake scales with both the
    edge size and the portfolio's *current* cash, not a fixed dollar cap, so
    a 3.1c edge and a 22c edge no longer buy identical position sizes (P3.13).
    No partial fills, queue position, or slippage modeling yet.
    """
    if opportunity.action != "buy":
        count = 0
    else:
        f = kelly_fraction(
            net_edge_cents=opportunity.net_edge_cents,
            price_cents=opportunity.kalshi_price_cents,
        )
        stake_cents = f * KELLY_FRACTION_MULTIPLIER * portfolio.cash_cents
        kelly_count = (
            int(stake_cents // opportunity.kalshi_price_cents)
            if opportunity.kalshi_price_cents > 0 else 0
        )
        count = min(opportunity.max_contracts, kelly_count)

    cost = count * opportunity.kalshi_price_cents
    if count <= 0 or cost > portfolio.cash_cents:
        status = "rejected"
        count = 0
    else:
        status = "filled"
        portfolio.cash_cents -= cost
        signed = count if opportunity.side == "yes" else -count
        portfolio.positions[opportunity.kalshi_ticker] = (
            portfolio.positions.get(opportunity.kalshi_ticker, 0) + signed
        )

    return PaperOrder(
        paper_order_id=str(uuid4()),
        opportunity_id=opportunity.opportunity_id,
        ticker=opportunity.kalshi_ticker,
        side=opportunity.side,
        action="buy",
        count=count,
        limit_price_cents=opportunity.kalshi_price_cents,
        status=status,  # type: ignore[arg-type]
        fill_model_version=FILL_MODEL_VERSION,
        created_at=datetime.now(timezone.utc),
    )


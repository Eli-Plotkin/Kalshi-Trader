from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import uuid4

from .schemas import Opportunity, PaperOrder


FILL_MODEL_VERSION = "immediate_best_ask_v1"


@dataclass
class PaperPortfolio:
    cash_cents: int
    positions: dict[str, int]


def paper_buy(opportunity: Opportunity, *, portfolio: PaperPortfolio) -> PaperOrder:
    """Very simple paper fill model for scanner validation.

    If the opportunity is tradeable and cash covers the full limit cost, fill
    immediately at the Kalshi ask used by the scanner. No partial fills, queue
    position, or slippage modeling yet.
    """
    count = opportunity.max_contracts
    cost = count * opportunity.kalshi_price_cents
    if opportunity.action != "buy":
        status = "rejected"
        count = 0
    elif count <= 0 or cost > portfolio.cash_cents:
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


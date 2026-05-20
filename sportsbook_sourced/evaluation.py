from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from .schemas import Opportunity, TradeEvaluation


def closing_line_value_cents(
    *,
    side: str,
    entry_price_cents: int,
    fair_prob_at_close: float,
) -> float:
    close_value = fair_prob_at_close * 100.0
    if side == "yes":
        return close_value - entry_price_cents
    if side == "no":
        return (100.0 - close_value) - entry_price_cents
    raise ValueError(f"unknown side: {side}")


def realized_pnl_cents(
    *,
    side: str,
    entry_price_cents: int,
    count: int,
    resolved_yes: Optional[bool],
) -> Optional[float]:
    if resolved_yes is None:
        return None
    if side == "yes":
        exit_price = 100 if resolved_yes else 0
    elif side == "no":
        exit_price = 0 if resolved_yes else 100
    else:
        raise ValueError(f"unknown side: {side}")
    return count * (exit_price - entry_price_cents)


def evaluate_opportunity(
    *,
    opportunity: Opportunity,
    fair_prob_at_close: Optional[float],
    resolved_yes: Optional[bool],
    count: int,
    evaluated_at: datetime | None = None,
) -> TradeEvaluation:
    evaluated_at = evaluated_at or datetime.now(timezone.utc)
    clv = None
    if fair_prob_at_close is not None:
        clv = closing_line_value_cents(
            side=opportunity.side,
            entry_price_cents=opportunity.kalshi_price_cents,
            fair_prob_at_close=fair_prob_at_close,
        )
    pnl = realized_pnl_cents(
        side=opportunity.side,
        entry_price_cents=opportunity.kalshi_price_cents,
        count=count,
        resolved_yes=resolved_yes,
    )
    resolved_side = None
    if resolved_yes is not None:
        resolved_side = "yes" if resolved_yes else "no"
    return TradeEvaluation(
        opportunity_id=opportunity.opportunity_id,
        evaluated_at=evaluated_at,
        entry_price_cents=opportunity.kalshi_price_cents,
        fair_prob_at_entry=opportunity.fair_prob,
        fair_prob_at_close=fair_prob_at_close,
        clv_cents=clv,
        resolved_side=resolved_side,  # type: ignore[arg-type]
        pnl_cents=pnl,
    )


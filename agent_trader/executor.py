from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from .schemas import Decision


@dataclass
class ExecutionResult:
    ok: bool
    action: str
    ticker: str
    client_order_id: Optional[str]
    order_id: Optional[str]
    filled_count: int
    reason: str
    raw_order_response: dict


def _client_order_id(cycle_id: int, ticker: str, seq: int) -> str:
    return f"{cycle_id}-{ticker}-{seq}"


def _sanity_check(decision: Decision, ticker: str, cash_cents: int) -> Optional[str]:
    """Returns a rejection reason string, or None if the decision passes sanity."""
    if not ticker or not isinstance(ticker, str):
        return "invalid_ticker"
    if decision.action in ("hold", "skip", "close_position"):
        return None
    if decision.size_usd <= 0:
        return "non_positive_size"
    cost_cents = int(round(decision.size_usd * 100))
    if cost_cents > cash_cents and decision.action in ("buy_yes", "buy_no"):
        return "insufficient_cash"
    return None


def execute_decision(
    client,
    decision: Decision,
    ticker: str,
    cycle_id: int,
    seq: int,
    yes_ask_cents: int,
    no_ask_cents: int,
    cash_cents: int,
    current_position_count: int = 0,
) -> ExecutionResult:
    """
    Place the order described by `decision` idempotently.

    Price = current ask side (limit-at-market). Count derived from size_usd /
    price-per-contract-cents. The orchestrator owns the price/size decision;
    executor's job is correctness + idempotency.
    """
    reject = _sanity_check(decision, ticker, cash_cents)
    if reject:
        logging.warning("executor reject %s: %s", ticker, reject)
        return ExecutionResult(
            ok=False,
            action=decision.action,
            ticker=ticker,
            client_order_id=None,
            order_id=None,
            filled_count=0,
            reason=reject,
            raw_order_response={},
        )

    if decision.action in ("hold", "skip"):
        return ExecutionResult(
            ok=True,
            action=decision.action,
            ticker=ticker,
            client_order_id=None,
            order_id=None,
            filled_count=0,
            reason="no_op",
            raw_order_response={},
        )

    if decision.action == "close_position":
        if current_position_count == 0:
            return ExecutionResult(
                ok=True,
                action=decision.action,
                ticker=ticker,
                client_order_id=None,
                order_id=None,
                filled_count=0,
                reason="no_position_to_close",
                raw_order_response={},
            )
        # Sell whichever side we hold. Kalshi reports `position` as a signed
        # integer (positive = YES contracts held, negative = NO contracts held).
        action = "sell"
        count = abs(current_position_count)
        if current_position_count > 0:
            side = "yes"
            price = max(1, yes_ask_cents - 1)
        else:
            side = "no"
            price = max(1, no_ask_cents - 1)
    elif decision.action == "buy_yes":
        side = "yes"
        action = "buy"
        price = yes_ask_cents
        if price <= 0:
            return ExecutionResult(False, decision.action, ticker, None, None, 0, "no_ask_price", {})
        count = max(1, int((decision.size_usd * 100) // price))
    elif decision.action == "buy_no":
        side = "no"
        action = "buy"
        price = no_ask_cents
        if price <= 0:
            return ExecutionResult(False, decision.action, ticker, None, None, 0, "no_ask_price", {})
        count = max(1, int((decision.size_usd * 100) // price))
    else:
        return ExecutionResult(False, decision.action, ticker, None, None, 0, "unknown_action", {})

    client_order_id = _client_order_id(cycle_id, ticker, seq)
    order = client.place_limit_order(
        ticker=ticker,
        count=count,
        price=price,
        action=action,
        side=side,
        client_order_id=client_order_id,
    )

    if order == "INSUFFICIENT_FUNDS":
        return ExecutionResult(False, decision.action, ticker, client_order_id, None, 0, "insufficient_funds", {})
    if not order:
        return ExecutionResult(False, decision.action, ticker, client_order_id, None, 0, "api_error", {})

    return ExecutionResult(
        ok=True,
        action=decision.action,
        ticker=ticker,
        client_order_id=client_order_id,
        order_id=order.get("order_id"),
        filled_count=int(order.get("count_filled") or 0),
        reason="placed",
        raw_order_response=order,
    )

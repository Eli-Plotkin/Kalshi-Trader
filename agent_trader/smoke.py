"""
Day 1 smoke test: prove the plumbing.

Flow:
  1. Auth via existing KalshiClient
  2. Init SQLite chain log
  3. Reconcile cash + positions
  4. Discover eligible markets (liquidity floor)
  5. Pick the cheapest-priced market in the top 5 by volume
  6. Place a 1-contract limit order at 1¢ (deliberately far below market so it sits unfilled)
  7. Verify the order exists, then cancel it
  8. Log every step into the chain_log table
  9. Exit clean

No LLM calls. No agent decisions. Just: can we talk to Kalshi, persist state,
place and cancel an order, and walk away with a clean log?

Run with:
  python -m agent_trader.smoke
"""
from __future__ import annotations

import logging
import sys
import time

from kalshi.client import KalshiClient
from kalshi.config import API_KEY_ID, BASE_URL, PRIVATE_KEY_PATH

from . import runtime
from .market_discovery import discover_eligible_markets


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("agent_trader.smoke")


TEST_PRICE_CENTS = 1
TEST_COUNT = 1


def run() -> int:
    log.info("init DB at %s", runtime.DB_PATH)
    conn = runtime.init_db()

    cycle_id = runtime.open_cycle(conn, active_prompts={"_smoke": "day1"})
    log.info("smoke cycle_id=%d", cycle_id)

    client = KalshiClient(BASE_URL, API_KEY_ID, PRIVATE_KEY_PATH)

    # 1. Reconcile
    portfolio = runtime.reconcile(client)
    log.info("balance=%d cents, positions=%d", portfolio.cash_cents, len(portfolio.positions))
    runtime.log_step(conn, cycle_id, "_account", "reconcile", {
        "cash_cents": portfolio.cash_cents,
        "position_count": len(portfolio.positions),
    })

    if portfolio.cash_cents < TEST_PRICE_CENTS * TEST_COUNT:
        runtime.close_cycle(conn, cycle_id, "aborted", "insufficient_cash_for_smoke")
        log.error("not enough cash (%d cents) to place a 1c test order", portfolio.cash_cents)
        return 2

    # 2. Discover
    markets = discover_eligible_markets(client, min_daily_volume=100, min_hours_to_close=48)
    if not markets:
        runtime.close_cycle(conn, cycle_id, "aborted", "no_eligible_markets")
        log.error("no eligible markets found")
        return 3

    markets.sort(key=lambda m: m.volume_24h, reverse=True)
    top = markets[:5]
    log.info("top 5 by 24h volume:")
    for m in top:
        log.info("  %-30s vol=%d yes_ask=%dc title=%s", m.ticker, m.volume_24h, m.yes_ask_cents, m.title[:60])

    target = top[0]
    runtime.log_step(conn, cycle_id, target.ticker, "smoke_target", {
        "ticker": target.ticker,
        "title": target.title,
        "yes_ask_cents": target.yes_ask_cents,
        "volume_24h": target.volume_24h,
        "candidates": [m.ticker for m in top],
    })

    # 3. Place a 1c buy_yes limit. Sits in the book unfilled; we cancel it.
    client_order_id = f"{cycle_id}-{target.ticker}-smoke"
    log.info("placing 1-contract buy_yes at 1c on %s (client_order_id=%s)", target.ticker, client_order_id)
    order = client.place_limit_order(
        ticker=target.ticker,
        count=TEST_COUNT,
        price=TEST_PRICE_CENTS,
        action="buy",
        side="yes",
        client_order_id=client_order_id,
    )

    if order == "INSUFFICIENT_FUNDS":
        runtime.log_step(conn, cycle_id, target.ticker, "smoke_order_placement", {"result": "insufficient_funds"})
        runtime.close_cycle(conn, cycle_id, "aborted", "insufficient_funds")
        return 4
    if not order or not order.get("order_id"):
        runtime.log_step(conn, cycle_id, target.ticker, "smoke_order_placement", {"result": "api_error", "raw": order})
        runtime.close_cycle(conn, cycle_id, "aborted", "order_placement_api_error")
        return 5

    order_id = order["order_id"]
    runtime.log_step(conn, cycle_id, target.ticker, "smoke_order_placement", {
        "client_order_id": client_order_id,
        "order_id": order_id,
        "raw": order,
    })
    log.info("order placed: %s", order_id)

    # 4. Status check (read-back)
    time.sleep(1)
    status = client.get_order_status(order_id)
    runtime.log_step(conn, cycle_id, target.ticker, "smoke_order_status", status or {"none": True})
    log.info("order status: %s", (status or {}).get("status"))

    # 5. Cancel
    cancelled = client.cancel_order(order_id)
    runtime.log_step(conn, cycle_id, target.ticker, "smoke_order_cancel", {"cancelled": cancelled})
    log.info("cancel returned: %s", cancelled)

    # 6. Final status check
    final_status = client.get_order_status(order_id)
    runtime.log_step(conn, cycle_id, target.ticker, "smoke_order_final", final_status or {"none": True})
    log.info("final order status: %s", (final_status or {}).get("status"))

    runtime.close_cycle(conn, cycle_id, "ok", "smoke_complete")
    conn.close()
    log.info("smoke complete. cycle %d written to %s", cycle_id, runtime.DB_PATH)
    return 0


if __name__ == "__main__":
    sys.exit(run())

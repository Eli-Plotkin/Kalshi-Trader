"""Pre-flight checks for going live. Run before the first real order.

Read-only checks (safe on prod or demo):
  python -m kalshi.preflight

Order-path check (places ONE 1-contract bid at 1c, far from the market, then
cancels it). Refuses to run unless KALSHI_ENV=demo:
  KALSHI_ENV=demo python -m kalshi.preflight --place-test-order

It verifies the things the bot depends on but the unit tests can only fake:
auth signing, balance units, orderbook shape and sort order, position field
names, the read-only reconciliation endpoints (fills, detailed positions,
settlements, order lookup by id / client_order_id), and the V2 order response / status / cancel round trip.
"""

from __future__ import annotations

import argparse
import sys
import uuid


def _ok(name, detail=""):
    return (name, True, detail)


def _bad(name, detail=""):
    return (name, False, detail)


def check_balance(client):
    bal = client.get_balance()
    if bal is None:
        return _bad("balance", "fetch failed (check key id / key file / KALSHI_ENV)")
    if not isinstance(bal, int):
        return _bad("balance", f"expected integer cents, got {type(bal).__name__}: {bal!r}")
    return _ok("balance", f"{bal} cents (${bal / 100:,.2f})")


def check_markets_and_book(client):
    markets = client.fetch_nba_markets()
    if not markets:
        return [_bad("nba markets", "none open (offseason, or wrong series/env)")], None
    results = [_ok("nba markets", f"{len(markets)} open")]
    ticker = markets[0]["ticker"]
    book = client.get_orderbook(ticker)
    if book is None:
        results.append(_bad("orderbook", f"{ticker}: no book returned"))
        return results, ticker
    if "no_dollars" not in book and "yes_dollars" not in book:
        results.append(_bad("orderbook", f"unexpected keys {sorted(book)}; strategy.get_implied_ask expects no_dollars"))
        return results, ticker
    for side in ("yes_dollars", "no_dollars"):
        levels = book.get(side) or []
        prices = [float(level[0]) for level in levels]
        if prices != sorted(prices):
            results.append(_bad("orderbook order", f"{side} not ascending -- get_implied_ask takes [-1] as best bid"))
            return results, ticker
    results.append(_ok("orderbook", f"{ticker}: keys {sorted(book)}; levels ascending (best bid last)"))
    return results, ticker


def check_positions(client):
    try:
        positions = client.list_positions(raise_on_error=True)
    except Exception as e:
        return _bad("positions", f"fetch failed: {e}")
    if positions and "position_fp" not in positions[0]:
        return _bad("positions", f"no position_fp in {sorted(positions[0])}; main._kalshi_holdings would see nothing")
    return _ok("positions", f"{len(positions)} entries")


def check_fills(client):
    """GET /portfolio/fills. Returns (result, sample_fill_or_None)."""
    try:
        fills = client.list_fills()
    except Exception as e:
        return _bad("fills", f"fetch failed: {e}"), None
    if not isinstance(fills, list):
        return _bad("fills", f"expected a list, got {type(fills).__name__}"), None
    for f in fills:
        if not f.get("fill_id") or not f.get("ticker") or f.get("price_cents") is None \
                or not isinstance(f.get("count"), int):
            return _bad("fills", f"unparseable fill (id/ticker/price/count missing): {f!r}"), None
    return _ok("fills", f"{len(fills)} fills" + ("" if fills else " (empty is fine)")), (fills[0] if fills else None)


def check_positions_detailed(client):
    try:
        positions = client.list_positions_detailed()
    except Exception as e:
        return _bad("positions detailed", f"fetch failed: {e}")
    if not isinstance(positions, list):
        return _bad("positions detailed", f"expected a list, got {type(positions).__name__}")
    for p in positions:
        if not p.get("ticker") or not isinstance(p.get("position"), int):
            return _bad("positions detailed", f"unparseable position: {p!r}")
    missing = [p["ticker"] for p in positions if p.get("exposure_cents") is None]
    if missing:
        return _bad("positions detailed", f"no exposure field parsed for {missing[:3]}")
    return _ok("positions detailed", f"{len(positions)} entries" + ("" if positions else " (empty is fine)"))


def check_settlements(client):
    try:
        settlements = client.list_settlements()
    except Exception as e:
        return _bad("settlements", f"fetch failed: {e}")
    if not isinstance(settlements, list):
        return _bad("settlements", f"expected a list, got {type(settlements).__name__}")
    for s in settlements:
        if not s.get("ticker"):
            return _bad("settlements", f"settlement without ticker: {s!r}")
    return _ok("settlements", f"{len(settlements)} records" + ("" if settlements else " (empty is fine)"))


def check_order_lookup(client, sample_fill=None):
    """Read-only order lookup checks: single-order GET (when a real order id is known from
    a fill) and client_order_id lookup (a random id must come back None, not an error)."""
    out = []
    probe = f"preflight-nonexistent-{uuid.uuid4()}"
    try:
        found = client.get_order_by_client_id(probe)
    except Exception as e:
        out.append(_bad("order lookup by client id", f"lookup failed: {e}"))
    else:
        out.append(_bad("order lookup by client id", f"random id unexpectedly matched {found!r}")
                   if found is not None else
                   _ok("order lookup by client id", "unknown client_order_id -> None"))
    oid = (sample_fill or {}).get("order_id")
    if not oid:
        out.append(_ok("get order", "skipped: no fills to take an order_id from"))
        return out
    try:
        order = client.get_order(oid)
    except Exception as e:
        out.append(_bad("get order", f"GET order {oid} failed: {e}"))
        return out
    if order is None:
        out.append(_bad("get order", f"order {oid} from a fill returned not-found"))
    elif not order.get("order_id") or not order.get("status") or not isinstance(order.get("filled"), int):
        out.append(_bad("get order", f"missing order_id/status/filled: {order!r}"))
    else:
        out.append(_ok("get order", f"{oid}: status={order['status']} filled={order['filled']}"))
        cid = order.get("client_order_id")
        if cid:
            try:
                again = client.get_order_by_client_id(cid, ticker=order.get("ticker"))
            except Exception as e:
                out.append(_bad("client id round trip", f"lookup failed: {e}"))
            else:
                out.append(_ok("client id round trip", f"{cid} found") if again and again.get("order_id") == oid
                           else _bad("client id round trip", f"{cid} not found by lookup (got {again!r})"))
    return out


def check_order_roundtrip(client, ticker):
    """Place a 1c bid for 1 contract, read it back, cancel it."""
    out = []
    order = client.place_limit_order(ticker=ticker, count=1, price=1, action="buy",
                                     client_order_id=None)
    if not isinstance(order, dict) or "order_id" not in order:
        return [_bad("place order", f"unexpected result: {order!r}")]
    oid = order["order_id"]
    out.append(_ok("place order", f"order_id={oid}; response keys {sorted(order)}"))
    status = client.get_order_status(oid)
    if not status:
        out.append(_bad("order status", "GET order returned nothing"))
    elif "fill_count_fp" in status or "fill_count" in status:
        out.append(_ok("order status", f"fill field present; keys {sorted(status)}"))
    else:
        out.append(_bad("order status", f"no fill_count(_fp); keys {sorted(status)}"))
    out.append(_ok("cancel", "cancelled") if client.cancel_order(oid, market_ticker=ticker)
               else _bad("cancel", "cancel returned False -- CHECK THE DASHBOARD for a resting order"))
    return out


def run_preflight(client, env, place_test_order=False):
    results = [check_balance(client)]
    book_results, ticker = check_markets_and_book(client)
    results += book_results
    results.append(check_positions(client))
    # Reconciliation endpoints (read-only; an empty list is a pass).
    fills_result, sample_fill = check_fills(client)
    results.append(fills_result)
    results.append(check_positions_detailed(client))
    results.append(check_settlements(client))
    results += check_order_lookup(client, sample_fill)
    if place_test_order:
        if env != "demo":
            results.append(_bad("order round trip", f"refusing: KALSHI_ENV={env!r}, must be 'demo'"))
        elif ticker is None:
            results.append(_bad("order round trip", "no market to test against"))
        else:
            results += check_order_roundtrip(client, ticker)
    return results


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--place-test-order", action="store_true")
    args = parser.parse_args(argv)

    from kalshi.client import KalshiClient
    from kalshi.config import API_KEY_ID, BASE_URL, KALSHI_ENV, PRIVATE_KEY_PATH

    print(f"Kalshi env: {KALSHI_ENV} ({BASE_URL})")
    client = KalshiClient(BASE_URL, API_KEY_ID, PRIVATE_KEY_PATH)
    results = run_preflight(client, KALSHI_ENV, args.place_test_order)
    for name, ok, detail in results:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {detail}")
    failed = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

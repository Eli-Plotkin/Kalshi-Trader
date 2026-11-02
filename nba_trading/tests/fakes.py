"""Shared test doubles for the Kalshi reads the bot now depends on.

Mix `KalshiReadsMixin` into a fake client to get the reconciliation endpoints with sane defaults,
driven by plain attributes the individual test can set:

  positions    legacy-shaped list [{"ticker": ..., "position_fp": "7.00", "exposure_cents": 650}]
               (or an Exception instance to make the fetch fail)
  fills        list of normalized fill dicts returned by list_fills
  orders       {order_id: normalized order dict} for get_order
  by_client_id {client_order_id: normalized order dict} for get_order_by_client_id
  lookup_error an Exception to raise from get_order_by_client_id
"""

from __future__ import annotations


def order_dict(order_id="oid-1", client_order_id=None, ticker="KXNBA-G1-BOS", status="executed",
               filled=0, remaining=0, limit_cents=71, avg_fill_cents=None):
    return {"order_id": order_id, "client_order_id": client_order_id, "ticker": ticker,
            "status": status, "filled": filled, "remaining": remaining, "limit_cents": limit_cents,
            "avg_fill_cents": avg_fill_cents, "created_time": None, "expiration_time": None,
            "side": "yes"}


class KalshiReadsMixin:
    positions = ()
    fills = ()
    orders = None
    by_client_id = None
    lookup_error = None

    def list_positions_detailed(self, count_filter="position"):
        pos = self.positions
        if isinstance(pos, Exception):
            raise pos
        out = []
        for p in pos:
            qty = round(float(p.get("position_fp", p.get("position", 0))))
            out.append({"ticker": p["ticker"], "position": qty,
                        "exposure_cents": p.get("exposure_cents"),
                        "realized_pnl_cents": None, "fees_paid_cents": None})
        return out

    def list_fills(self, ticker=None, order_id=None, min_ts=None):
        return list(self.fills)

    def get_order(self, order_id):
        return (self.orders or {}).get(order_id)

    def get_order_by_client_id(self, client_order_id, ticker=None, include_historical=True):
        if self.lookup_error is not None:
            raise self.lookup_error
        return (self.by_client_id or {}).get(client_order_id)

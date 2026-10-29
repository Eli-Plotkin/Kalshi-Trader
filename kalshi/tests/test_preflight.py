"""Preflight logic tests against a fake client (no network)."""

from __future__ import annotations

from kalshi.preflight import run_preflight


class Fake:
    def __init__(self, **over):
        self.over = over
        self.cancelled = []
        self.calls = []

    def get_balance(self):
        return self.over.get("balance", 12345)

    def fetch_nba_markets(self):
        return self.over.get("markets", [{"ticker": "KXNBAGAME-X-BOS"}])

    def get_orderbook(self, t):
        return self.over.get("book", {"yes_dollars": [["0.30", 5], ["0.31", 5]],
                                      "no_dollars": [["0.60", 5], ["0.65", 5]]})

    def list_positions(self, raise_on_error=False):
        return self.over.get("positions", [{"ticker": "T", "position_fp": "2.00"}])

    def place_limit_order(self, **kw):
        return self.over.get("order", {"order_id": "o1", "fill_count": "0.00"})

    def get_order_status(self, oid):
        return self.over.get("status", {"order_id": oid, "fill_count_fp": "0.00"})

    def cancel_order(self, oid, market_ticker=None):
        self.cancelled.append(oid)
        return True

    # --- read-only reconciliation endpoints ---
    def list_fills(self, **kw):
        self.calls.append(("list_fills", kw))
        r = self.over.get("fills", [{"fill_id": "f1", "order_id": "o9", "ticker": "T", "count": 2,
                                     "price_cents": 93, "side": "yes", "action": "buy",
                                     "fee_cents": 1, "ts": "2026-10-29T00:00:00Z"}])
        if isinstance(r, Exception):
            raise r
        return r

    def list_positions_detailed(self):
        r = self.over.get("pos_detail", [{"ticker": "T", "position": 2, "exposure_cents": 186,
                                          "realized_pnl_cents": 0, "fees_paid_cents": 1}])
        if isinstance(r, Exception):
            raise r
        return r

    def list_settlements(self, min_ts=None):
        r = self.over.get("settlements", [{"ticker": "T", "result": "yes", "yes_count": 2, "no_count": 0,
                                           "revenue_cents": 200, "settled_ts": "x"}])
        if isinstance(r, Exception):
            raise r
        return r

    def get_order(self, oid):
        self.calls.append(("get_order", oid))
        r = self.over.get("order_by_id", {"order_id": oid, "client_order_id": "c9", "ticker": "T",
                                          "status": "executed", "filled": 2})
        if isinstance(r, Exception):
            raise r
        return r

    def get_order_by_client_id(self, cid, ticker=None):
        self.calls.append(("by_client", cid))
        if isinstance(self.over.get("by_client"), Exception):
            raise self.over["by_client"]
        if cid == "c9":
            return self.over.get("by_client_known", {"order_id": "o9", "client_order_id": "c9"})
        return self.over.get("by_client_unknown", None)


def _by_name(results):
    return {n: ok for n, ok, _ in results}


def test_read_only_checks_pass_on_good_data():
    r = _by_name(run_preflight(Fake(), "prod"))
    assert all(r.values()), r


def test_unsorted_book_fails():
    bad = {"no_dollars": [["0.70", 1], ["0.60", 1]]}
    assert not _by_name(run_preflight(Fake(book=bad), "prod"))["orderbook order"]


def test_non_integer_balance_fails():
    assert not _by_name(run_preflight(Fake(balance="123.45"), "prod"))["balance"]


def test_missing_position_fp_fails():
    r = _by_name(run_preflight(Fake(positions=[{"ticker": "T", "position": 2}]), "prod"))
    assert not r["positions"]


def test_order_roundtrip_refused_outside_demo():
    f = Fake()
    r = _by_name(run_preflight(f, "prod", place_test_order=True))
    assert not r["order round trip"] and f.cancelled == []


def test_order_roundtrip_on_demo_places_and_cancels():
    f = Fake()
    r = _by_name(run_preflight(f, "demo", place_test_order=True))
    assert r["place order"] and r["order status"] and r["cancel"]
    assert f.cancelled == ["o1"]


def test_status_without_fill_field_fails():
    r = _by_name(run_preflight(Fake(status={"status": "resting"}), "demo", place_test_order=True))
    assert not r["order status"]


# --- reconciliation (read-only) checks -----------------------------------------

def test_reconciliation_checks_present_and_pass():
    r = _by_name(run_preflight(Fake(), "prod"))
    for name in ("fills", "positions detailed", "settlements", "order lookup by client id",
                 "get order", "client id round trip"):
        assert r[name], name


def test_empty_lists_pass_and_skip_get_order():
    f = Fake(fills=[], pos_detail=[], settlements=[])
    r = _by_name(run_preflight(f, "prod"))
    assert r["fills"] and r["positions detailed"] and r["settlements"] and r["get order"]
    assert not any(c[0] == "get_order" for c in f.calls)


def test_endpoint_errors_fail_the_check():
    f = Fake(fills=RuntimeError("500"), pos_detail=RuntimeError("500"), settlements=RuntimeError("500"),
             by_client=RuntimeError("500"))
    r = _by_name(run_preflight(f, "prod"))
    assert not r["fills"] and not r["positions detailed"] and not r["settlements"]
    assert not r["order lookup by client id"]


def test_unparseable_fill_fails():
    r = _by_name(run_preflight(Fake(fills=[{"fill_id": "f", "ticker": "T", "count": 1, "price_cents": None}]), "prod"))
    assert not r["fills"]


def test_position_without_exposure_fails():
    r = _by_name(run_preflight(Fake(pos_detail=[{"ticker": "T", "position": 1, "exposure_cents": None}]), "prod"))
    assert not r["positions detailed"]


def test_random_client_id_matching_fails():
    r = _by_name(run_preflight(Fake(by_client_unknown={"order_id": "zz"}), "prod"))
    assert not r["order lookup by client id"]


def test_order_from_fill_not_found_fails():
    r = _by_name(run_preflight(Fake(order_by_id=None), "prod"))
    assert not r["get order"]


def test_client_id_round_trip_mismatch_fails():
    r = _by_name(run_preflight(Fake(by_client_known={"order_id": "other"}), "prod"))
    assert not r["client id round trip"]


def test_get_order_raising_fails_check():
    r = _by_name(run_preflight(Fake(order_by_id=RuntimeError("boom")), "prod"))
    assert not r["get order"]

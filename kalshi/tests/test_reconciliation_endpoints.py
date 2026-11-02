"""Tests for the read-only reconciliation endpoints on KalshiClient
(get_order, get_order_by_client_id, list_fills, list_positions_detailed,
list_settlements). Fixtures follow the response shapes in the Kalshi trade-api v2
docs. No network: the session is replaced by a recording fake."""

from __future__ import annotations

import pytest
import requests
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from kalshi import client as kc
from kalshi.client import KalshiClient

BASE = "https://api.elections.kalshi.com/trade-api/v2"


@pytest.fixture(scope="module")
def key_path(tmp_path_factory):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
                            serialization.NoEncryption())
    p = tmp_path_factory.mktemp("k") / "k.pem"
    p.write_bytes(pem)
    return str(p)


class Resp:
    def __init__(self, status=200, body=None):
        self.status_code = status
        self._body = {} if body is None else body

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(f"{self.status_code}", response=self)


class FakeSession:
    """Routes GETs through `handler(path, params) -> Resp | Exception` and records everything."""

    def __init__(self, handler):
        self.handler = handler
        self.gets = []
        self.other = []

    def get(self, url, headers=None, params=None, timeout=None):
        path = url[len(BASE):]
        self.gets.append({"path": path, "params": dict(params or {}), "timeout": timeout, "headers": headers})
        r = self.handler(path, dict(params or {}))
        if isinstance(r, Exception):
            raise r
        return r

    def post(self, *a, **k):
        self.other.append(("post", a, k))
        raise AssertionError("POST issued")

    def delete(self, *a, **k):
        self.other.append(("delete", a, k))
        raise AssertionError("DELETE issued")

    put = patch = delete


def make(key_path, handler):
    c = KalshiClient(BASE, "kid", key_path, timeout=7)
    c.session = FakeSession(handler)
    return c


# --- fixtures: documented shapes -----------------------------------------------

def order_new(**over):
    o = {"order_id": "ord-1", "user_id": "u", "client_order_id": "cid-1", "ticker": "KXNBAGAME-X-BOS",
         "type": "limit", "status": "executed", "outcome_side": "yes", "book_side": "bid",
         "yes_price_dollars": "0.9300", "no_price_dollars": "0.0700",
         "initial_count_fp": "285.00", "fill_count_fp": "285.00", "remaining_count_fp": "0.00",
         "taker_fees_dollars": "0.0000", "maker_fees_dollars": "0.0000",
         "taker_fill_cost_dollars": "265.0500", "maker_fill_cost_dollars": "0.0000",
         "created_time": "2026-10-29T00:00:00Z", "last_update_time": "2026-10-29T00:00:01Z",
         "expiration_time": None, "some_future_field": {"x": 1}}
    o.update(over)
    return o


def order_legacy(**over):
    o = {"order_id": "ord-2", "client_order_id": "cid-2", "ticker": "T", "status": "RESTING",
         "side": "yes", "action": "buy", "yes_price": 93, "no_price": 7,
         "initial_count": 10, "fill_count": 4, "remaining_count": 6,
         "taker_fill_cost": 372, "maker_fill_cost": 0, "created_time": "2026-10-29T00:00:00Z"}
    o.update(over)
    return o


# --- get_order -----------------------------------------------------------------

def test_get_order_new_shape(key_path):
    c = make(key_path, lambda p, q: Resp(body={"order": order_new()}))
    o = c.get_order("ord-1")
    assert o["order_id"] == "ord-1" and o["client_order_id"] == "cid-1"
    assert o["status"] == "executed" and o["filled"] == 285 and o["remaining"] == 0
    assert o["limit_cents"] == 93 and o["avg_fill_cents"] == 93.0
    assert o["created_time"] == "2026-10-29T00:00:00Z" and o["expiration_time"] is None
    assert c.session.gets[0]["path"] == "/portfolio/orders/ord-1"
    assert c.session.gets[0]["timeout"] == 7


def test_get_order_legacy_shape(key_path):
    c = make(key_path, lambda p, q: Resp(body={"order": order_legacy()}))
    o = c.get_order("ord-2")
    assert o["status"] == "resting" and o["filled"] == 4 and o["remaining"] == 6
    assert o["limit_cents"] == 93 and o["avg_fill_cents"] == 93.0


def test_get_order_limit_from_no_price_only(key_path):
    c = make(key_path, lambda p, q: Resp(body={"order": {"order_id": "a", "no_price_dollars": "0.0700"}}))
    assert c.get_order("a")["limit_cents"] == 93


def test_get_order_missing_fields_tolerated(key_path):
    c = make(key_path, lambda p, q: Resp(body={"order": {"order_id": "a", "ticker": "T", "status": "resting"}}))
    o = c.get_order("a")
    assert o["filled"] == 0 and o["remaining"] is None and o["limit_cents"] is None
    assert o["avg_fill_cents"] is None and o["client_order_id"] is None


def test_get_order_partial_fill_average(key_path):
    o = order_new(fill_count_fp="3.00", taker_fill_cost_dollars="1.0000", maker_fill_cost_dollars="0.9300")
    c = make(key_path, lambda p, q: Resp(body={"order": o}))
    assert c.get_order("x")["avg_fill_cents"] == pytest.approx(64.3333, abs=1e-4)


def test_get_order_404_is_none(key_path):
    c = make(key_path, lambda p, q: Resp(404, {"code": "not_found"}))
    assert c.get_order("nope") is None


@pytest.mark.parametrize("bad", [Resp(500), Resp(401), Resp(403), requests.exceptions.ConnectTimeout("t")])
def test_get_order_errors_raise(key_path, bad):
    c = make(key_path, lambda p, q: bad)
    with pytest.raises((requests.exceptions.RequestException,)):
        c.get_order("x")


def test_get_order_empty_body_is_none(key_path):
    c = make(key_path, lambda p, q: Resp(body={}))
    assert c.get_order("x") is None


# --- get_order_by_client_id ----------------------------------------------------

def orders_handler(pages_by_status, historical=None, hist_exc=None):
    """pages_by_status: {status: [page_orders, ...]}; cursor "c<n>" points at page n."""
    def h(path, q):
        if path == "/historical/orders":
            if hist_exc:
                return hist_exc
            return Resp(body={"orders": historical or [], "cursor": ""})
        assert path == "/portfolio/orders"
        pages = pages_by_status.get(q["status"], [[]])
        i = int(q["cursor"][1:]) if q.get("cursor") else 0
        nxt = f"c{i + 1}" if i + 1 < len(pages) else ""
        return Resp(body={"orders": pages[i], "cursor": nxt})
    return h


def test_by_client_id_found_first_status_stops_early(key_path):
    c = make(key_path, orders_handler({"resting": [[order_new(client_order_id="me")]]}))
    o = c.get_order_by_client_id("me", ticker="TK")
    assert o["client_order_id"] == "me"
    assert len(c.session.gets) == 1
    assert c.session.gets[0]["params"]["ticker"] == "TK" and c.session.gets[0]["params"]["status"] == "resting"


def test_by_client_id_found_on_page_two_of_executed(key_path):
    pages = {"executed": [[order_new(client_order_id="a")], [order_new(client_order_id="b", order_id="ord-b")]]}
    c = make(key_path, orders_handler(pages))
    o = c.get_order_by_client_id("b")
    assert o["order_id"] == "ord-b"
    executed = [g for g in c.session.gets if g["params"].get("status") == "executed"]
    assert len(executed) == 2 and executed[1]["params"]["cursor"] == "c1"


def test_by_client_id_found_in_historical(key_path):
    c = make(key_path, orders_handler({}, historical=[order_new(client_order_id="old")]))
    assert c.get_order_by_client_id("old")["client_order_id"] == "old"
    assert c.session.gets[-1]["path"] == "/historical/orders"


def test_by_client_id_not_found_scans_everything(key_path):
    c = make(key_path, orders_handler({"resting": [[order_new(client_order_id="x")]]}))
    assert c.get_order_by_client_id("missing") is None
    statuses = {g["params"].get("status") for g in c.session.gets if g["path"] == "/portfolio/orders"}
    assert statuses == {"resting", "executed", "canceled"}
    assert any(g["path"] == "/historical/orders" for g in c.session.gets)


def test_by_client_id_historical_can_be_disabled(key_path):
    c = make(key_path, orders_handler({}))
    assert c.get_order_by_client_id("m", include_historical=False) is None
    assert all(g["path"] == "/portfolio/orders" for g in c.session.gets)


def test_by_client_id_error_in_any_scan_raises_not_none(key_path):
    # The last scan (historical) fails: must NOT be reported as "not found".
    c = make(key_path, orders_handler({}, hist_exc=requests.exceptions.ReadTimeout("t")))
    with pytest.raises(requests.exceptions.ReadTimeout):
        c.get_order_by_client_id("m")


def test_by_client_id_http_error_raises(key_path):
    c = make(key_path, lambda p, q: Resp(500))
    with pytest.raises(requests.exceptions.HTTPError):
        c.get_order_by_client_id("m")


def test_by_client_id_requires_id(key_path):
    c = make(key_path, orders_handler({}))
    with pytest.raises(ValueError):
        c.get_order_by_client_id("")
    assert c.session.gets == []


# --- list_fills ----------------------------------------------------------------

def fill_new(**over):
    f = {"fill_id": "f1", "trade_id": "f1", "order_id": "ord-1", "ticker": "T", "market_ticker": "T",
         "outcome_side": "yes", "book_side": "bid", "side": "yes", "action": "buy",
         "yes_price_dollars": "0.9300", "no_price_dollars": "0.0700", "count_fp": "285.00",
         "fee_cost": "0.0174", "is_taker": True, "created_time": "2026-10-29T01:00:00Z", "ts": 1793235600}
    f.update(over)
    return f


def test_fills_new_shape(key_path):
    c = make(key_path, lambda p, q: Resp(body={"fills": [fill_new()], "cursor": ""}))
    [f] = c.list_fills(ticker="T", order_id="ord-1", min_ts=1793000000)
    assert f == {"fill_id": "f1", "order_id": "ord-1", "ticker": "T", "count": 285, "price_cents": 93,
                 "side": "yes", "action": "buy", "fee_cents": 2, "fee_dollars": "0.0174",
                 "ts": "2026-10-29T01:00:00Z"}
    g = c.session.gets[0]
    assert g["path"] == "/portfolio/fills"
    assert g["params"]["ticker"] == "T" and g["params"]["order_id"] == "ord-1" and g["params"]["min_ts"] == 1793000000


def test_fills_legacy_shape(key_path):
    legacy = {"trade_id": "t7", "order_id": "o", "market_ticker": "T", "side": "NO", "action": "buy",
              "yes_price": 40, "no_price": 60, "count": 3, "ts": 1793235600}
    c = make(key_path, lambda p, q: Resp(body={"fills": [legacy]}))
    [f] = c.list_fills()
    assert f["fill_id"] == "t7" and f["ticker"] == "T" and f["count"] == 3 and f["price_cents"] == 40
    assert f["side"] == "no" and f["fee_cents"] is None and f["ts"] == "1793235600"


def test_fills_missing_fields(key_path):
    c = make(key_path, lambda p, q: Resp(body={"fills": [{"fill_id": "z"}]}))
    [f] = c.list_fills()
    assert f["count"] == 0 and f["price_cents"] is None and f["order_id"] is None and f["action"] is None


def test_fills_paginate_three_pages(key_path):
    pages = {"": ([fill_new(fill_id="a")], "p2"), "p2": ([fill_new(fill_id="b")], "p3"),
             "p3": ([fill_new(fill_id="c")], "")}

    def h(path, q):
        rows, nxt = pages[q.get("cursor", "")]
        return Resp(body={"fills": rows, "cursor": nxt})
    c = make(key_path, h)
    assert [f["fill_id"] for f in c.list_fills()] == ["a", "b", "c"]
    assert len(c.session.gets) == 3 and "cursor" not in c.session.gets[0]["params"]


def test_fills_empty(key_path):
    c = make(key_path, lambda p, q: Resp(body={"fills": [], "cursor": ""}))
    assert c.list_fills() == []
    c2 = make(key_path, lambda p, q: Resp(body={}))
    assert c2.list_fills() == []


def test_fills_error_raises(key_path):
    with pytest.raises(requests.exceptions.HTTPError):
        make(key_path, lambda p, q: Resp(503)).list_fills()
    with pytest.raises(requests.exceptions.ConnectionError):
        make(key_path, lambda p, q: requests.exceptions.ConnectionError("x")).list_fills()


def test_fills_error_on_second_page_raises_not_partial(key_path):
    def h(path, q):
        return Resp(body={"fills": [fill_new()], "cursor": "p2"}) if not q.get("cursor") else Resp(500)
    with pytest.raises(requests.exceptions.HTTPError):
        make(key_path, h).list_fills()


def test_page_guard_raises_instead_of_truncating(key_path, monkeypatch):
    monkeypatch.setattr(kc, "MAX_PAGES", 3)
    n = {"i": 0}

    def h(path, q):
        n["i"] += 1
        return Resp(body={"fills": [fill_new()], "cursor": f"cur{n['i']}"})
    c = make(key_path, h)
    with pytest.raises(RuntimeError, match="truncated"):
        c.list_fills()
    assert len(c.session.gets) == 3


def test_repeated_cursor_raises(key_path):
    c = make(key_path, lambda p, q: Resp(body={"fills": [fill_new()], "cursor": "same"}))
    with pytest.raises(RuntimeError):
        c.list_fills()


# --- list_positions_detailed ---------------------------------------------------

def test_positions_new_shape(key_path):
    body = {"market_positions": [
        {"ticker": "A", "position_fp": "285.00", "market_exposure_dollars": "264.9000",
         "realized_pnl_dollars": "-1.2500", "fees_paid_dollars": "0.5000", "total_traded_dollars": "300.00",
         "last_updated_ts": "x", "exchange_index": 0},
        {"ticker": "B", "position_fp": "-3.00", "market_exposure_dollars": "1.2000"}],
        "event_positions": [{"event_ticker": "E"}], "cursor": ""}
    c = make(key_path, lambda p, q: Resp(body=body))
    a, b = c.list_positions_detailed()
    assert a == {"ticker": "A", "position": 285, "exposure_cents": 26490, "realized_pnl_cents": -125,
                 "fees_paid_cents": 50}
    assert b["position"] == -3 and b["exposure_cents"] == 120 and b["realized_pnl_cents"] is None
    g = c.session.gets[0]
    assert g["path"] == "/portfolio/positions" and g["params"]["count_filter"] == "position"


def test_positions_legacy_shape(key_path):
    body = {"market_positions": [{"ticker": "A", "position": -4, "market_exposure": 160,
                                  "realized_pnl": 25, "fees_paid": 3}]}
    [p] = make(key_path, lambda pa, q: Resp(body=body)).list_positions_detailed()
    assert p == {"ticker": "A", "position": -4, "exposure_cents": 160, "realized_pnl_cents": 25, "fees_paid_cents": 3}


def test_positions_missing_fields_and_no_filter(key_path):
    c = make(key_path, lambda p, q: Resp(body={"market_positions": [{"ticker": "A"}]}))
    [p] = c.list_positions_detailed(count_filter=None)
    assert p["position"] == 0 and p["exposure_cents"] is None and p["fees_paid_cents"] is None
    assert "count_filter" not in c.session.gets[0]["params"]


def test_positions_paginate_and_empty(key_path):
    def h(path, q):
        if not q.get("cursor"):
            return Resp(body={"market_positions": [{"ticker": "A", "position_fp": "1.00"}], "cursor": "n"})
        return Resp(body={"market_positions": [{"ticker": "B", "position_fp": "2.00"}], "cursor": ""})
    assert [p["ticker"] for p in make(key_path, h).list_positions_detailed()] == ["A", "B"]
    assert make(key_path, lambda p, q: Resp(body={"market_positions": []})).list_positions_detailed() == []


def test_positions_error_raises(key_path):
    with pytest.raises(requests.exceptions.HTTPError):
        make(key_path, lambda p, q: Resp(500)).list_positions_detailed()


# --- list_settlements ----------------------------------------------------------

def test_settlements_new_shape(key_path):
    body = {"settlements": [{"ticker": "A", "event_ticker": "E", "market_result": "YES",
                             "yes_count_fp": "285.00", "yes_total_cost_dollars": "265.05",
                             "no_count_fp": "0.00", "no_total_cost_dollars": "0.00",
                             "revenue": 28500, "fee_cost": "0.0000", "settled_time": "2026-10-29T05:00:00Z",
                             "value": 100}], "cursor": ""}
    c = make(key_path, lambda p, q: Resp(body=body))
    [s] = c.list_settlements(min_ts=1793000000)
    assert s["ticker"] == "A" and s["result"] == "yes" and s["yes_count"] == 285 and s["no_count"] == 0
    assert s["revenue_cents"] == 28500 and s["settled_ts"] == "2026-10-29T05:00:00Z"
    assert s["fee_cents"] == 0 and s["yes_total_cost_cents"] == 26505
    g = c.session.gets[0]
    assert g["path"] == "/portfolio/settlements" and g["params"]["min_ts"] == 1793000000


def test_settlements_legacy_and_missing(key_path):
    body = {"settlements": [{"ticker": "A", "market_result": "no", "yes_count": 0, "no_count": 5, "revenue": 500},
                            {"ticker": "B", "market_result": "scalar"}, {"ticker": "C"}]}
    a, b, cc = make(key_path, lambda p, q: Resp(body=body)).list_settlements()
    assert a["result"] == "no" and a["no_count"] == 5 and a["revenue_cents"] == 500
    assert b["result"] is None and cc["result"] is None and cc["revenue_cents"] is None and cc["settled_ts"] is None


def test_settlements_paginate_empty_error(key_path):
    def h(path, q):
        if not q.get("cursor"):
            return Resp(body={"settlements": [{"ticker": "A"}], "cursor": "n"})
        return Resp(body={"settlements": [{"ticker": "B"}]})
    assert [s["ticker"] for s in make(key_path, h).list_settlements()] == ["A", "B"]
    assert make(key_path, lambda p, q: Resp(body={"settlements": []})).list_settlements() == []
    with pytest.raises(requests.exceptions.HTTPError):
        make(key_path, lambda p, q: Resp(429)).list_settlements()


# --- numeric parsing -----------------------------------------------------------

@pytest.mark.parametrize("value,cents", [("0.9300", 93), ("0.93", 93), ("0.935", 94), ("0.9349", 93), ("1", 100),
                                         ("0.0000", 0), ("285.00", 28500), ("-0.0150", -2), (0.07, 7), (None, None),
                                         ("", None), ("abc", None), ("NaN", None), (True, None)])
def test_dollar_string_to_cents_uses_decimal(value, cents):
    assert kc._cents_from_dollars(value) == cents


@pytest.mark.parametrize("value,n", [("285.00", 285), ("2.5", 3), (7, 7), ("-3.00", -3), (None, None), ("x", None)])
def test_count_parsing(value, n):
    assert kc._count({"c": value}, "c", None) == n


def test_float_binary_noise_does_not_misround():
    # 0.29 * 100 == 28.999999999999996 in float; Decimal must give 29.
    assert kc._cents_from_dollars("0.29") == 29 and kc._cents_from_dollars(0.29) == 29


# --- safety: GET only, path-only signatures ------------------------------------

def test_no_non_get_requests_and_signature_excludes_query(key_path, monkeypatch):
    signed = []
    c = make(key_path, lambda p, q: Resp(body={"orders": [], "fills": [], "market_positions": [],
                                               "settlements": [], "order": order_new()}))
    orig = c._sign_request
    monkeypatch.setattr(c, "_sign_request", lambda m, p: (signed.append((m, p)), orig(m, p))[1])
    c.get_order("o1")
    c.get_order_by_client_id("x", ticker="T")
    c.list_fills(ticker="T", order_id="o", min_ts=5)
    c.list_positions_detailed()
    c.list_settlements(min_ts=5)
    assert c.session.other == []
    assert signed and all(m == "GET" and "?" not in p for m, p in signed)
    assert all(g["timeout"] == 7 for g in c.session.gets)
    assert all(p.startswith("/portfolio/") or p.startswith("/historical/") for _, p in signed)

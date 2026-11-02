"""Order lifecycle + Kalshi-fill settlement in the ledger (docs/design-g4-reconciliation.md s3/s5)."""

from __future__ import annotations

import threading

import pytest

from nba_trading.ledger import Ledger, kalshi_fee_cents, settlement_pnl_cents


@pytest.fixture()
def led(tmp_path):
    l = Ledger(str(tmp_path / "l.sqlite"))
    yield l
    l.close()


def _decision(led):
    return led.record_decision(game_id="g", action="buy", reason="in_band", dry_run=False)


def _begin(led, cid="c1", ticker="T1", count=5, price=92):
    pk, created = led.begin_order(_decision(led), ticker, cid, count, price)
    return pk


def _placed(led, cid="c1", oid="o1", **kw):
    pk = _begin(led, cid, **kw)
    led.mark_placed(pk, oid, "resting")
    return pk


def _fill(fid, oid="o1", count=5, price=92, fee=None, ts="2026-01-01T00:00:00Z", side="yes",
          action="buy", ticker="T1"):
    return {"fill_id": fid, "order_id": oid, "ticker": ticker, "count": count,
            "price_cents": price, "side": side, "action": action, "fee_cents": fee, "ts": ts}


def _settlement(led, pk):
    r = led.conn.execute("SELECT * FROM settlements WHERE order_pk=?", (pk,)).fetchone()
    return None if r is None else dict(r)


# --- begin_order -------------------------------------------------------------

def test_begin_order_inserts_submitting_row(led):
    d = _decision(led)
    pk, created = led.begin_order(d, "T1", "c1", 5, 92)
    assert created is True
    row = led.get_order_row(pk)
    assert (row["outcome"], row["ticker"], row["client_order_id"], row["count"],
            row["limit_price"], row["decision_id"]) == ("submitting", "T1", "c1", 5, 92, d)
    assert row["order_id"] is None and row["filled"] is None and row["status"] is None
    assert row["updated_ts"] is not None and row["ts"] is not None


def test_begin_order_is_idempotent_per_client_order_id(led):
    pk1, c1 = led.begin_order(None, "T1", "dup", 5, 92)
    pk2, c2 = led.begin_order(None, "T1", "dup", 99, 50)       # different args: still the same row
    assert (c1, c2) == (True, False) and pk1 == pk2
    assert led.conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 1
    assert led.get_order_row(pk1)["count"] == 5                  # original untouched
    other, created = led.begin_order(None, "T1", "other", 5, 92)
    assert created and other != pk1


def test_begin_order_returns_existing_row_in_any_state(led):
    pk = _placed(led, "c1", "o1")
    again, created = led.begin_order(None, "T1", "c1", 5, 92)
    assert again == pk and created is False
    assert led.get_order_row(pk)["outcome"] == "placed"


def test_begin_order_finds_legacy_record_order_row(led):
    old = led.record_order(decision_id=None, ticker="T1", client_order_id="legacy", order_id="x",
                           count=1, limit_price=90, outcome="failed")
    assert led.begin_order(None, "T1", "legacy", 1, 90) == (old, False)


# --- mark_placed -------------------------------------------------------------

def test_mark_placed_from_submitting(led):
    pk = _begin(led)
    led.mark_placed(pk, "o1", "resting")
    r = led.get_order_row(pk)
    assert (r["outcome"], r["order_id"], r["status"], r["resolved_via"]) == ("placed", "o1", "resting", "response")
    assert [x["id"] for x in led.open_orders()] == [pk]


def test_mark_placed_without_status_and_idempotent(led):
    pk = _begin(led)
    led.mark_placed(pk, "o1")
    led.mark_placed(pk, "o1", "executed")
    r = led.get_order_row(pk)
    assert r["outcome"] == "placed" and r["status"] == "executed"
    led.mark_placed(pk, "o1")                                    # status None keeps the stored one
    assert led.get_order_row(pk)["status"] == "executed"


def test_mark_placed_from_unconfirmed(led):
    pk = _begin(led)
    led.set_order_outcome(pk, "unconfirmed")
    led.mark_placed(pk, "o1")
    assert led.get_order_row(pk)["outcome"] == "placed"


@pytest.mark.parametrize("outcome", ["insufficient_funds", "blocked_location", "not_found",
                                     "failed", "duplicate"])
def test_mark_placed_rejects_terminal_rows(led, outcome):
    pk = _begin(led)
    led.set_order_outcome(pk, outcome)
    with pytest.raises(ValueError):
        led.mark_placed(pk, "o1")
    assert led.get_order_row(pk)["outcome"] == outcome
    assert led.get_order_row(pk)["order_id"] is None


def test_mark_placed_rejects_adopted_settled_and_missing(led):
    ad = led.adopt_order("T1", "oa", 2, 90.0)
    with pytest.raises(ValueError):
        led.mark_placed(ad, "oa")
    pk = _placed(led)
    led.record_settlement_from_fills(pk, "yes")
    with pytest.raises(ValueError):
        led.mark_placed(pk, "o1")
    with pytest.raises(ValueError):
        led.mark_placed(9999, "o1")


# --- set_order_outcome -------------------------------------------------------

@pytest.mark.parametrize("outcome", ["unconfirmed", "not_found", "insufficient_funds",
                                     "blocked_location", "duplicate", "failed"])
def test_set_order_outcome_from_submitting(led, outcome):
    pk = _begin(led)
    led.set_order_outcome(pk, outcome)
    r = led.get_order_row(pk)
    assert r["outcome"] == outcome and r["updated_ts"] is not None
    assert led.open_orders() == []


def test_set_order_outcome_unconfirmed_resolution(led):
    pk = _begin(led)
    led.set_order_outcome(pk, "unconfirmed")
    led.set_order_outcome(pk, "unconfirmed")                     # same -> same: no-op
    led.set_order_outcome(pk, "not_found")
    assert led.get_order_row(pk)["outcome"] == "not_found"
    led.set_order_outcome(pk, "not_found")                       # terminal, same: no-op
    with pytest.raises(ValueError):
        led.set_order_outcome(pk, "unconfirmed")                 # terminal never reopens
    with pytest.raises(ValueError):
        led.set_order_outcome(pk, "insufficient_funds")


def test_set_order_outcome_cannot_reject_a_live_order(led):
    pk = _placed(led)
    for bad in ("failed", "not_found", "insufficient_funds", "unconfirmed", "duplicate"):
        with pytest.raises(ValueError):
            led.set_order_outcome(pk, bad)
    assert led.get_order_row(pk)["outcome"] == "placed"
    ad = led.adopt_order("T2", "oa", 1, 90.0)
    with pytest.raises(ValueError):
        led.set_order_outcome(ad, "failed")
    assert led.get_order_row(ad)["outcome"] == "adopted"


def test_set_order_outcome_rejects_unknown_and_non_settable_targets(led):
    pk = _begin(led)
    for bad in ("placed", "adopted", "submitting", "bogus", ""):
        with pytest.raises(ValueError):
            led.set_order_outcome(pk, bad)
    with pytest.raises(ValueError):
        led.set_order_outcome(424242, "failed")
    assert led.get_order_row(pk)["outcome"] == "submitting"


def test_set_order_outcome_does_not_touch_legacy_terminal_rows(led):
    old = led.record_order(decision_id=None, ticker="T1", client_order_id="c", order_id=None,
                           count=1, limit_price=90, outcome="insufficient_funds")
    with pytest.raises(ValueError):
        led.set_order_outcome(old, "not_found")


# --- apply_order_state -------------------------------------------------------

def test_apply_order_state_resolves_unconfirmed_to_placed(led):
    pk = _begin(led)
    led.set_order_outcome(pk, "unconfirmed")
    led.apply_order_state(pk, "o1", "executed", 5, 91.4, "lookup")
    r = led.get_order_row(pk)
    assert (r["outcome"], r["order_id"], r["status"], r["filled"], r["avg_fill_cents"],
            r["resolved_via"]) == ("placed", "o1", "executed", 5, 91.4, "lookup")
    assert [x["id"] for x in led.orders_needing_resolution()] == []
    assert [x["id"] for x in led.open_orders()] == [pk]


def test_apply_order_state_from_submitting_and_startup(led):
    pk = _begin(led)
    led.apply_order_state(pk, "o1", "resting", 0, None, "startup")
    r = led.get_order_row(pk)
    assert r["outcome"] == "placed" and r["filled"] == 0 and r["avg_fill_cents"] is None
    assert r["resolved_via"] == "startup"


def test_apply_order_state_updates_a_placed_order_and_keeps_missing_values(led):
    pk = _placed(led)
    led.apply_order_state(pk, "o1", "executed", 5, 91.0, "lookup")
    led.apply_order_state(pk, "o1", "executed", None, None, "lookup")
    r = led.get_order_row(pk)
    assert r["filled"] == 5 and r["avg_fill_cents"] == 91.0


def test_apply_order_state_corrects_not_found_and_rejections(led):
    """Kalshi's truth wins: an order we thought missing/rejected that exists becomes placed."""
    for i, outcome in enumerate(["not_found", "failed", "duplicate"]):
        pk = _begin(led, f"c{i}")
        led.set_order_outcome(pk, outcome)
        led.apply_order_state(pk, f"o{i}", "resting", 1, 90.0, "lookup")
        assert led.get_order_row(pk)["outcome"] == "placed"


def test_apply_order_state_keeps_adopted_provenance(led):
    pk = led.adopt_order("T1", "oa", 2, 90.0)
    led.apply_order_state(pk, "oa", "executed", 3, 89.0, "adopt")
    r = led.get_order_row(pk)
    assert r["outcome"] == "adopted" and r["filled"] == 3 and r["avg_fill_cents"] == 89.0


def test_apply_order_state_refuses_settled_order(led):
    pk = _placed(led)
    led.apply_order_state(pk, "o1", "executed", 5, 92.0, "lookup")
    led.record_settlement_from_fills(pk, "yes")
    with pytest.raises(ValueError):
        led.apply_order_state(pk, "o1", "resting", 0, None, "lookup")
    r = led.get_order_row(pk)
    assert r["outcome"] == "placed" and r["status"] == "executed" and r["filled"] == 5


def test_apply_order_state_validates_resolved_via_and_row(led):
    pk = _begin(led)
    with pytest.raises(ValueError):
        led.apply_order_state(pk, "o1", "resting", 0, None, "bogus")
    with pytest.raises(ValueError):
        led.apply_order_state(31337, "o1", "resting", 0, None, "lookup")
    assert led.get_order_row(pk)["outcome"] == "submitting"


# --- adopt_order -------------------------------------------------------------

def test_adopt_order_creates_adopted_row(led):
    pk = led.adopt_order("T7", "oX", 4, 88.5, client_order_id="cX", limit_price=90)
    r = led.get_order_row(pk)
    assert (r["outcome"], r["ticker"], r["order_id"], r["client_order_id"], r["count"], r["filled"],
            r["avg_fill_cents"], r["limit_price"], r["resolved_via"], r["decision_id"]) == (
        "adopted", "T7", "oX", "cX", 4, 4, 88.5, 90, "adopt", None)


def test_adopt_order_defaults(led):
    pk = led.adopt_order("T7", "oX", 4, 88.6)
    r = led.get_order_row(pk)
    assert r["client_order_id"] == "adopted:oX" and r["limit_price"] == 89
    pk2 = led.adopt_order("T8", None, 2, None)                   # position-only adoption
    r2 = led.get_order_row(pk2)
    assert r2["order_id"] is None and r2["limit_price"] == 0
    assert r2["client_order_id"].startswith("adopted:")


def test_adopt_order_idempotent_on_order_id(led):
    a = led.adopt_order("T7", "oX", 4, 88.0)
    b = led.adopt_order("T7", "oX", 9, 50.0)
    assert a == b and led.conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 1
    assert led.get_order_row(a)["filled"] == 4                   # unchanged
    p = _placed(led, "c1", "o1")
    assert led.adopt_order("T1", "o1", 5, 92.0) == p             # known order is not re-adopted
    assert led.get_order_row(p)["outcome"] == "placed"


def test_adopt_order_resolves_existing_unconfirmed_row_by_client_id(led):
    pk = _begin(led, "cu")
    led.set_order_outcome(pk, "unconfirmed")
    got = led.adopt_order("T1", "oU", 5, 92.0, client_order_id="cu")
    assert got == pk
    r = led.get_order_row(pk)
    assert r["outcome"] == "placed" and r["order_id"] == "oU" and r["filled"] == 5
    assert r["resolved_via"] == "adopt"
    assert led.conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 1


def test_adopted_orders_are_unsettled_and_get_settled(led):
    pk = led.adopt_order("T7", "oX", 4, 90.0)
    assert [r["id"] for r in led.unsettled_orders()] == [pk]
    assert [r["id"] for r in led.open_orders()] == [pk]
    led.upsert_fills([_fill("f1", oid="oX", count=4, price=90, fee=2, ticker="T7")])
    assert led.record_settlement_from_fills(pk, "yes") == 4 * 10 - 2
    assert led.unsettled_orders() == [] and led.open_orders() == []
    assert _settlement(led, pk)["source"] == "kalshi_fills"


# --- queries -----------------------------------------------------------------

def test_orders_needing_resolution_and_open_orders_sets(led):
    sub = _begin(led, "a")
    unc = _begin(led, "b")
    led.set_order_outcome(unc, "unconfirmed")
    nf = _begin(led, "c")
    led.set_order_outcome(nf, "not_found")
    rej = _begin(led, "d")
    led.set_order_outcome(rej, "insufficient_funds")
    pl = _placed(led, "e", "oe")
    ad = led.adopt_order("T9", "oa", 1, 90.0)
    assert [r["id"] for r in led.orders_needing_resolution()] == [sub, unc]
    assert [r["id"] for r in led.open_orders()] == [pl, ad]
    assert [r["id"] for r in led.unsettled_orders()] == [pl, ad]


def test_unsettled_orders_excludes_settled_but_keeps_legacy_placed(led):
    legacy = led.record_order(decision_id=None, ticker="T1", client_order_id="l", order_id="ol",
                              count=2, limit_price=90, outcome="placed")
    done = _placed(led, "x", "ox")
    led.record_settlement_from_fills(done, "no")
    assert [r["id"] for r in led.unsettled_orders()] == [legacy]


def test_get_order_row_and_order_by_client_id(led):
    pk = _begin(led, "cid")
    assert led.get_order_row(pk)["client_order_id"] == "cid"
    assert led.get_order_row(12345) is None
    assert led.order_by_client_id("cid")["id"] == pk
    assert led.order_by_client_id("nope") is None


# --- fills -------------------------------------------------------------------

def test_upsert_fills_inserts_and_is_idempotent(led):
    fills = [_fill("f1", fee=1), _fill("f2", count=2, price=93, fee=None, ts="2026-01-01T00:01:00Z")]
    assert led.upsert_fills(fills) == 2
    assert led.upsert_fills(fills) == 0
    assert led.conn.execute("SELECT COUNT(*) FROM kalshi_fills").fetchone()[0] == 2
    rows = led.fills_for_order("o1")
    assert [r["fill_id"] for r in rows] == ["f1", "f2"]
    assert dict(rows[1]) == {"fill_id": "f2", "order_id": "o1", "ticker": "T1", "count": 2,
                             "price_cents": 93, "side": "yes", "action": "buy", "fee_cents": None,
                             "ts": "2026-01-01T00:01:00Z"}


def test_upsert_fills_mixed_and_duplicates_within_batch(led):
    led.upsert_fills([_fill("f1")])
    assert led.upsert_fills([_fill("f1"), _fill("f2"), _fill("f2")]) == 1
    assert led.upsert_fills([]) == 0 and led.upsert_fills(None) == 0


def test_upsert_fills_backfills_missing_fee_but_never_duplicates_or_overwrites(led):
    led.upsert_fills([_fill("f1", fee=None)])
    assert led.upsert_fills([_fill("f1", fee=3, price=50, count=99)]) == 0
    r = led.fills_for_order("o1")[0]
    assert r["fee_cents"] == 3                                   # missing fee filled in
    assert r["price_cents"] == 92 and r["count"] == 5            # immutable fields untouched
    led.upsert_fills([_fill("f1", fee=9)])
    assert led.fills_for_order("o1")[0]["fee_cents"] == 3


def test_upsert_fills_skips_unusable_records(led, caplog):
    bad = [{"order_id": "o1", "count": 1, "price_cents": 90},          # no fill_id
           {"fill_id": "f9", "order_id": "o1", "price_cents": 90},     # no count
           {"fill_id": "f8", "order_id": "o1", "count": 1}]            # no price
    assert led.upsert_fills(bad + [_fill("ok")]) == 1
    assert [r["fill_id"] for r in led.fills_for_order("o1")] == ["ok"]


def test_fills_for_order_filters_by_order(led):
    led.upsert_fills([_fill("a", oid="o1"), _fill("b", oid="o2"), _fill("c", oid=None)])
    assert [r["fill_id"] for r in led.fills_for_order("o2")] == ["b"]
    assert led.fills_for_order("missing") == [] and led.fills_for_order(None) == []


# --- record_settlement_from_fills --------------------------------------------

def test_settlement_from_fills_win(led):
    pk = _placed(led)
    led.upsert_fills([_fill("f1", count=5, price=92, fee=2)])
    assert led.record_settlement_from_fills(pk, "yes") == 5 * 8 - 2
    s = _settlement(led, pk)
    assert (s["result"], s["filled"], s["fee_cents"], s["pnl_cents"], s["source"],
            s["avg_entry_cents"]) == ("yes", 5, 2, 38, "kalshi_fills", 92.0)


def test_settlement_from_fills_loss(led):
    pk = _placed(led)
    led.upsert_fills([_fill("f1", count=5, price=92, fee=2)])
    assert led.record_settlement_from_fills(pk, "no") == -5 * 92 - 2
    assert _settlement(led, pk)["pnl_cents"] == -462


def test_settlement_uses_actual_fill_price_not_limit(led):
    pk = _placed(led, price=95)                                   # limit 95, filled at 90
    led.upsert_fills([_fill("f1", count=4, price=90, fee=1)])
    assert led.record_settlement_from_fills(pk, "yes") == 4 * 10 - 1


def test_settlement_partial_fill_uses_filled_count_not_order_count(led):
    pk = _placed(led, count=10)
    led.upsert_fills([_fill("f1", count=3, price=92, fee=1)])
    assert led.record_settlement_from_fills(pk, "yes") == 3 * 8 - 1
    assert _settlement(led, pk)["filled"] == 3


def test_settlement_multiple_fills_weighted_average(led):
    pk = _placed(led, count=10)
    led.upsert_fills([_fill("f1", count=4, price=90, fee=1, ts="1"),
                      _fill("f2", count=6, price=93, fee=2, ts="2"),
                      _fill("other", oid="zzz", count=100, price=10, fee=50)])   # other order ignored
    assert led.record_settlement_from_fills(pk, "yes") == 4 * 10 + 6 * 7 - 3
    s = _settlement(led, pk)
    assert s["filled"] == 10 and s["fee_cents"] == 3
    assert s["avg_entry_cents"] == pytest.approx((4 * 90 + 6 * 93) / 10)
    pk2 = _placed(led, "c2", "o2", count=10)
    led.upsert_fills([_fill("g1", oid="o2", count=4, price=90, fee=1),
                      _fill("g2", oid="o2", count=6, price=93, fee=2)])
    assert led.record_settlement_from_fills(pk2, "no") == -(4 * 90 + 6 * 93) - 3


def test_settlement_null_fee_uses_formula_fee_per_fill(led):
    pk = _placed(led, count=9)
    led.upsert_fills([_fill("f1", count=5, price=92, fee=None), _fill("f2", count=4, price=90, fee=7)])
    expected_fee = kalshi_fee_cents(5, 92) + 7
    assert led.record_settlement_from_fills(pk, "yes") == 5 * 8 + 4 * 10 - expected_fee
    s = _settlement(led, pk)
    assert s["fee_cents"] == expected_fee and s["source"] == "kalshi_fills"


def test_settlement_zero_fee_is_not_replaced_by_formula(led):
    pk = _placed(led)
    led.upsert_fills([_fill("f1", count=5, price=92, fee=0)])
    assert led.record_settlement_from_fills(pk, "yes") == 40
    assert _settlement(led, pk)["fee_cents"] == 0


def test_settlement_no_fills_falls_back_to_computed(led):
    pk = _placed(led, count=5, price=92)
    led.apply_order_state(pk, "o1", "executed", 4, 91.0, "lookup")
    expected, fee = settlement_pnl_cents(4, 92, True)
    assert led.record_settlement_from_fills(pk, "yes") == expected
    s = _settlement(led, pk)
    assert (s["source"], s["fee_cents"], s["filled"], s["avg_entry_cents"]) == ("computed", fee, 4, 92)
    pk2 = _placed(led, "c2", "o2", count=5, price=92)
    led.apply_order_state(pk2, "o2", "executed", 4, 91.0, "lookup")
    assert led.record_settlement_from_fills(pk2, "no") == settlement_pnl_cents(4, 92, False)[0]


def test_settlement_no_fills_and_nothing_filled_is_zero(led):
    pk = _placed(led)                                             # filled is NULL
    assert led.record_settlement_from_fills(pk, "yes") == 0
    assert _settlement(led, pk)["pnl_cents"] == 0 and _settlement(led, pk)["source"] == "computed"


def test_settlement_from_fills_is_idempotent_and_keeps_timestamp(led):
    pk = _placed(led)
    led.upsert_fills([_fill("f1", count=5, price=92, fee=2)])
    first = led.record_settlement_from_fills(pk, "yes")
    ts = _settlement(led, pk)["ts"]
    assert led.record_settlement_from_fills(pk, "yes") == first
    assert led.conn.execute("SELECT COUNT(*) FROM settlements").fetchone()[0] == 1
    assert _settlement(led, pk)["ts"] == ts


def test_settlement_upgrades_from_computed_to_fills_when_fills_arrive(led):
    pk = _placed(led)
    led.apply_order_state(pk, "o1", "executed", 5, 92.0, "lookup")
    led.record_settlement_from_fills(pk, "yes")
    assert _settlement(led, pk)["source"] == "computed"
    led.upsert_fills([_fill("f1", count=5, price=90, fee=1)])
    assert led.record_settlement_from_fills(pk, "yes") == 5 * 10 - 1
    assert _settlement(led, pk)["source"] == "kalshi_fills"


def test_settlement_handles_sell_and_no_side_fills(led):
    pk = _placed(led, count=10)
    # buy 10 YES @ 90, sell 4 YES @ 95 (closing), YES wins:
    led.upsert_fills([_fill("b", count=10, price=90, fee=0), _fill("s", count=4, price=95, action="sell", fee=0)])
    assert led.record_settlement_from_fills(pk, "yes") == 10 * 10 - 4 * 5
    pk2 = _placed(led, "c2", "o2")
    led.upsert_fills([_fill("n", oid="o2", count=2, price=30, side="no", action="buy", fee=0)])
    # NO buy at yes-leg 30 == short YES: YES loses (result no) -> +2*30
    assert led.record_settlement_from_fills(pk2, "no") == 60
    assert led.record_settlement_from_fills(pk2, "yes") == -2 * 70


def test_settlement_result_is_validated_and_case_insensitive(led):
    pk = _placed(led)
    led.upsert_fills([_fill("f1", fee=0)])
    for bad in ("", None, "maybe", "void"):
        with pytest.raises(ValueError):
            led.record_settlement_from_fills(pk, bad)
    assert _settlement(led, pk) is None
    assert led.record_settlement_from_fills(pk, "YES") == 40


def test_settlement_returns_none_for_missing_or_non_open_orders(led):
    assert led.record_settlement_from_fills(999, "yes") is None
    for outcome in ("unconfirmed", "not_found", "insufficient_funds"):
        pk = _begin(led, f"x-{outcome}")
        led.set_order_outcome(pk, outcome)
        assert led.record_settlement_from_fills(pk, "yes") is None
        assert _settlement(led, pk) is None
    sub = _begin(led, "sub")
    assert led.record_settlement_from_fills(sub, "yes") is None


def test_legacy_record_settlement_marks_source_computed(led):
    pk = _placed(led)
    pnl = led.record_settlement(pk, "yes", 5, 92)
    s = _settlement(led, pk)
    assert s["pnl_cents"] == pnl and s["source"] == "computed" and s["avg_entry_cents"] == 92


# --- thread safety -----------------------------------------------------------

def _run_threads(fns):
    errors = []

    def wrap(fn):
        def run():
            try:
                fn()
            except BaseException as e:      # noqa: BLE001
                errors.append(e)
        return run

    threads = [threading.Thread(target=wrap(f)) for f in fns]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []


def test_concurrent_begin_order_distinct_ids(led):
    results = {}

    def work(i):
        def run():
            results[i] = led.begin_order(None, "T1", f"cid-{i}", 1, 90)
        return run

    _run_threads([work(i) for i in range(16)])
    assert all(created for _, created in results.values())
    assert len({pk for pk, _ in results.values()}) == 16
    assert led.conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 16


def test_concurrent_begin_order_same_id_creates_exactly_one_row(led):
    results = []
    barrier = threading.Barrier(12)

    def run():
        barrier.wait()
        results.append(led.begin_order(None, "T1", "same", 1, 90))

    _run_threads([run] * 12)
    assert len({pk for pk, _ in results}) == 1
    assert sum(1 for _, created in results if created) == 1
    assert led.conn.execute("SELECT COUNT(*) FROM orders WHERE client_order_id='same'").fetchone()[0] == 1


def test_concurrent_upsert_fills_and_adopt_are_idempotent(led):
    barrier = threading.Barrier(8)
    inserted, adopted = [], []

    def run():
        barrier.wait()
        inserted.append(led.upsert_fills([_fill("f1"), _fill("f2")]))
        adopted.append(led.adopt_order("T1", "oSame", 1, 90.0))

    _run_threads([run] * 8)
    assert sum(inserted) == 2
    assert len(set(adopted)) == 1
    assert led.conn.execute("SELECT COUNT(*) FROM kalshi_fills").fetchone()[0] == 2

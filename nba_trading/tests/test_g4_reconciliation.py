"""G4 / G5 end to end: an order the bot lost track of must be found, tracked and settled - and
must never be sent twice."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import pytest

from nba_trading import config, main
from nba_trading.ledger import Ledger
from nba_trading.tests.fakes import order_dict
from nba_trading.tests.test_main import FakeClient, FakeScheduler, env  # noqa: F401  (fixtures)

TICKER = "KXNBA-G1-BOS"


def coid(env):
    return f"nba-{env['game']['game_id']}-{TICKER}"


@pytest.fixture()
def led(monkeypatch):
    ledger = Ledger(":memory:")
    monkeypatch.setattr(main, "LEDGER", ledger)
    monkeypatch.setattr(main, "SCHEDULER", None)
    return ledger


def run(env, client, attempt=0):
    main.pre_game_job(client, env["portfolio"], env["high_water"], env["scheduler"], env["game"],
                      False, attempt)


def rows(led, table):
    return [dict(r) for r in led.conn.execute(f"SELECT * FROM {table}")]


def kinds(led):
    return [r["kind"] for r in led.conn.execute("SELECT kind FROM events")]


class Client(FakeClient):
    """FakeClient + market results."""
    market_result = ""

    def get_market(self, ticker):
        return {"result": self.market_result}


def fill_checks(env):
    return [kw for a, kw in env["scheduler"].jobs if str(kw.get("id", "")).startswith("confirm-")]


# ============================================================ 1. write-ahead intent

def test_the_intent_is_on_disk_before_the_order_is_sent(env, led):  # noqa: F811
    seen = {}

    class Spy(Client):
        def place_limit_order(self, **kw):
            seen["rows"] = rows(led, "orders")               # what the ledger knows AT POST TIME
            return super().place_limit_order(**kw)

    run(env, Spy())
    assert [(r["outcome"], r["client_order_id"], r["order_id"]) for r in seen["rows"]] == [
        ("submitting", coid(env), None)]
    final = rows(led, "orders")
    assert len(final) == 1 and final[0]["outcome"] == "placed" and final[0]["order_id"] == "oid-1"


def test_a_ledger_that_cannot_write_ahead_does_not_block_the_trade(env, monkeypatch):  # noqa: F811
    class Broken:
        def __getattr__(self, name):
            raise RuntimeError("disk full")
    monkeypatch.setattr(main, "LEDGER", Broken())
    client = Client()
    run(env, client)
    assert len(client.order_calls) == 1


# ============================================================ 2. timeout, but the order DID land

def test_timeout_after_the_order_landed_is_found_tracked_and_not_resent(env, led, caplog):  # noqa: F811
    client = Client(order_result=None)                        # reply never arrived
    client.by_client_id = {coid(env): order_dict("oid-77", coid(env), TICKER, status="executed",
                                                 filled=285, avg_fill_cents=70)}
    with caplog.at_level(logging.CRITICAL):
        run(env, client)
    assert len(client.order_calls) == 1                       # exactly one POST, ever
    (o,) = rows(led, "orders")
    assert (o["outcome"], o["order_id"], o["filled"], o["resolved_via"]) == ("placed", "oid-77", 285, "lookup")
    assert o["avg_fill_cents"] == 70
    assert env["portfolio"].has_position(TICKER)              # exposure is tracked
    assert len(fill_checks(env)) == 1 and fill_checks(env)[0]["id"] == "confirm-oid-77"
    assert "order_recovered" in kinds(led) and "ORDER RECOVERED" in caplog.text


def test_recovered_order_settles_from_kalshi_fills_and_enters_the_pnl(env, led):  # noqa: F811
    client = Client(order_result=None)
    client.by_client_id = {coid(env): order_dict("oid-77", coid(env), TICKER, filled=10, avg_fill_cents=70)}
    run(env, client)
    client.fills = [{"fill_id": "f1", "order_id": "oid-77", "ticker": TICKER, "count": 10,
                     "price_cents": 70, "side": "yes", "action": "buy", "fee_cents": 15, "ts": None}]
    client.market_result = "yes"
    assert main.settle_open_orders(client) == 1
    (s,) = rows(led, "settlements")
    assert s["source"] == "kalshi_fills" and s["pnl_cents"] == 10 * 30 - 15   # actual price and fee
    # the trade that used to vanish from the live-edge stats is now a settled trade
    assert len(led.unsettled_orders()) == 0


def test_duplicate_reply_409_is_resolved_the_same_way(env, led):  # noqa: F811
    client = Client(order_result="DUPLICATE_ORDER")
    client.by_client_id = {coid(env): order_dict("oid-5", coid(env), TICKER, status="resting",
                                                 filled=0, remaining=285)}
    run(env, client)
    assert len(client.order_calls) == 1
    assert rows(led, "orders")[0]["outcome"] == "placed"
    assert env["portfolio"].has_position(TICKER)


# ============================================================ 3. timeout, order did NOT land

def test_order_that_never_landed_is_recorded_not_found_and_never_retried(env, led):  # noqa: F811
    client = Client(order_result=None)                        # no such order on Kalshi
    run(env, client)
    assert rows(led, "orders")[0]["outcome"] == "not_found"
    assert not env["portfolio"].has_position(TICKER)
    assert "order_not_found" in kinds(led)
    assert fill_checks(env) == []
    # And nothing - not the game job, not a manual re-run - may send it now.
    run(env, client)
    assert len(client.order_calls) == 1


def test_a_failed_lookup_leaves_the_order_unconfirmed_and_alerts(env, led):  # noqa: F811
    client = Client(order_result=None)
    client.lookup_error = RuntimeError("kalshi 503")
    run(env, client)
    assert rows(led, "orders")[0]["outcome"] == "unconfirmed"
    assert "order_lookup_failed" in kinds(led)
    assert len(client.order_calls) == 1
    assert env["fired"] == [env["game"]["game_id"]]           # an order attempt is final: no retry job


def test_an_unconfirmed_order_is_resolved_by_the_health_job_later(env, led, monkeypatch):  # noqa: F811
    client = Client(order_result=None)
    client.lookup_error = RuntimeError("kalshi 503")
    run(env, client)
    assert rows(led, "orders")[0]["outcome"] == "unconfirmed"
    client.lookup_error = None                                 # Kalshi is back
    client.by_client_id = {coid(env): order_dict("oid-3", coid(env), TICKER, filled=5, avg_fill_cents=71)}
    monkeypatch.setattr(main, "SCHEDULER", env["scheduler"])
    main.health_job(client, env["portfolio"], env["high_water"])
    assert rows(led, "orders")[0]["outcome"] == "placed"
    assert len(client.order_calls) == 1


# ============================================================ 4. crash between POST and insert

def test_restart_with_an_inflight_order_asks_kalshi_instead_of_resending(env, led):  # noqa: F811
    # Simulate the crash: the write-ahead row exists, the process died before learning the outcome.
    led.begin_order(None, TICKER, coid(env), 285, 71)
    client = Client()
    client.by_client_id = {coid(env): order_dict("oid-8", coid(env), TICKER, filled=285, avg_fill_cents=70)}
    run(env, client)                                           # the restart re-evaluates the game
    assert client.order_calls == []                            # NOT re-sent
    assert rows(led, "orders")[0]["outcome"] == "placed" and len(rows(led, "orders")) == 1
    assert "order_in_flight" in kinds(led)


def test_restart_when_the_inflight_order_never_reached_kalshi(env, led):  # noqa: F811
    led.begin_order(None, TICKER, coid(env), 285, 71)
    client = Client()                                          # Kalshi has nothing
    run(env, client)
    assert client.order_calls == []                            # still not re-sent: a miss beats a double buy
    assert rows(led, "orders")[0]["outcome"] == "not_found"


# ============================================================ 5. startup recovery

def test_startup_resolves_ambiguous_orders_and_requeues_fill_checks(env, led):  # noqa: F811
    d = led.record_decision(game_id="g", action="buy", reason="in_band", dry_run=False,
                            tip_off_utc=(datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat())
    pk, _ = led.begin_order(d, TICKER, "c-lost", 10, 71)                     # lost in flight
    pk2 = led.record_order(decision_id=d, ticker="KXNBA-G2-NYK", client_order_id="c-open",
                           order_id="oid-open", count=5, limit_price=92, outcome="placed")
    client = Client()
    client.by_client_id = {"c-lost": order_dict("oid-lost", "c-lost", TICKER, filled=10, avg_fill_cents=71)}
    resolved, queued = main.recover_orders(client, env["portfolio"], env["scheduler"])
    assert resolved == 1 and queued == 2                       # the recovered one + the already-open one
    assert {kw["id"] for kw in fill_checks(env)} == {"confirm-oid-lost", "confirm-oid-open"}
    assert led.get_order_row(pk)["outcome"] == "placed"
    assert led.orders_needing_resolution() == []


def test_a_fill_check_for_a_game_already_over_runs_promptly(env, led):  # noqa: F811
    d = led.record_decision(game_id="g", action="buy", reason="r", dry_run=False,
                            tip_off_utc=(datetime.now(timezone.utc) - timedelta(hours=5)).isoformat())
    led.record_order(decision_id=d, ticker=TICKER, client_order_id="c", order_id="oid-old",
                     count=5, limit_price=92, outcome="placed")
    main.recover_orders(Client(), env["portfolio"], env["scheduler"])
    (job,) = fill_checks(env)
    delta = job["trigger"].run_date - datetime.now(timezone.utc)
    assert timedelta(seconds=0) < delta < timedelta(seconds=15)


def test_startup_recovery_failure_does_not_stop_the_bot(env, led, monkeypatch):  # noqa: F811
    class Boom(Client):
        lookup_error = RuntimeError("down")
    led.begin_order(None, TICKER, "c", 5, 71)
    resolved, queued = main.recover_orders(Boom(), env["portfolio"], env["scheduler"])
    assert resolved == 0                                       # still unresolved, no exception


# ============================================================ 6. unknown positions & equity from Kalshi

def test_unknown_nba_position_is_adopted_with_an_alert(env, led):  # noqa: F811
    pf = env["portfolio"]
    c = Client(positions=[{"ticker": "KXNBAGAME-26OCT29ATLCLE-CLE", "position_fp": "20.00",
                           "exposure_cents": 1860}])
    positions, dropped, adopted = main._sync_positions(c, pf)
    assert adopted == ["KXNBAGAME-26OCT29ATLCLE-CLE"]
    assert pf.has_position("KXNBAGAME-26OCT29ATLCLE-CLE")
    assert pf._positions["KXNBAGAME-26OCT29ATLCLE-CLE"][0]["price"] == 93      # 1860 / 20
    (o,) = rows(led, "orders")
    assert o["outcome"] == "adopted" and o["filled"] == 20
    assert "position_adopted" in kinds(led)
    # idempotent: a second sync adopts nothing and adds no second ledger row
    assert main._sync_positions(c, pf)[2] == [] and len(rows(led, "orders")) == 1


def test_non_nba_position_is_reported_not_managed(env, led):  # noqa: F811
    main._WARNED_FOREIGN.clear()
    pf = env["portfolio"]
    c = Client(positions=[{"ticker": "KXBTC-26-T100K", "position_fp": "5.00", "exposure_cents": 250}])
    _, _, adopted = main._sync_positions(c, pf)
    assert adopted == [] and not pf.has_position("KXBTC-26-T100K") and rows(led, "orders") == []
    assert "foreign_position" in kinds(led)
    main._sync_positions(c, pf)
    assert kinds(led).count("foreign_position") == 1           # reported once, not every cycle


def test_equity_uses_kalshis_exposure_not_our_possibly_stale_lots(env):  # noqa: F811
    pf = env["portfolio"]
    pf.add_position("T1", 50, 100)                             # local lot says $50 at risk ...
    total, unknown = pf.exposure_cents({"T1": {"position": 100, "exposure_cents": 9_300}})
    assert (total, unknown) == (9_300, [])                     # ... Kalshi says $93: Kalshi wins


def test_resting_orders_kalshi_does_not_show_yet_still_count(env):  # noqa: F811
    pf = env["portfolio"]
    pf.add_position("T-RESTING", 92, 10)                       # placed, unfilled, no Kalshi position yet
    total, unknown = pf.exposure_cents({"T1": {"position": 5, "exposure_cents": 465}})
    assert total == 465 + 920 and unknown == []


def test_unknown_cost_makes_equity_degraded(env):  # noqa: F811
    total, unknown = env["portfolio"].exposure_cents({"MYSTERY": {"position": 5, "exposure_cents": None}})
    assert total == 0 and unknown == ["MYSTERY"]


def test_degraded_equity_does_not_raise_the_drawdown_peak(env, led, monkeypatch):  # noqa: F811
    env["high_water"].update(100_000, False)
    c = Client(positions=[{"ticker": "KXNBAGAME-26OCT29AAABBB-AAA", "position_fp": "5.00"}])  # no exposure
    c.get_balance = lambda: 500_000                            # would be a huge "new peak" if trusted
    # adoption gives the position a (worst-case) lot, so cost is known: peak may rise honestly ...
    main.health_job(c, env["portfolio"], env["high_water"])
    assert env["high_water"].peak() >= 100_000
    # ... but a position we can neither cost nor adopt (non-NBA, no exposure) must not move the peak.
    env["high_water"]._peak_cents = 100_000
    c2 = Client(positions=[{"ticker": "KXBTC-26-X", "position_fp": "5.00"}])
    c2.get_balance = lambda: 500_000
    main.health_job(c2, env["portfolio"], env["high_water"])
    assert env["high_water"].peak() == 100_000
    assert "equity_degraded" in kinds(led)


# ============================================================ 7. settle from Kalshi data

def test_orphan_nba_fills_are_adopted_so_the_trade_cannot_vanish(env, led):  # noqa: F811
    c = Client()
    c.fills = [{"fill_id": "f1", "order_id": "oid-orphan", "ticker": "KXNBAGAME-26OCT29ATLCLE-CLE",
                "count": 8, "price_cents": 92, "side": "yes", "action": "buy", "fee_cents": 5, "ts": None},
               {"fill_id": "f2", "order_id": "oid-orphan", "ticker": "KXNBAGAME-26OCT29ATLCLE-CLE",
                "count": 2, "price_cents": 94, "side": "yes", "action": "buy", "fee_cents": 1, "ts": None}]
    main.sync_fills(c)
    (o,) = rows(led, "orders")
    assert o["outcome"] == "adopted" and o["order_id"] == "oid-orphan" and o["filled"] == 10
    assert o["avg_fill_cents"] == pytest.approx(92.4)
    assert "order_adopted_from_fills" in kinds(led)
    main.sync_fills(c)                                         # idempotent
    assert len(rows(led, "orders")) == 1 and len(rows(led, "kalshi_fills")) == 2


def test_fills_of_an_unresolved_order_are_attributed_to_it_not_adopted_as_a_second_trade(env, led):  # noqa: F811
    led.begin_order(None, "KXNBAGAME-26OCT29ATLCLE-CLE", "c-pending", 10, 92)    # unresolved
    c = Client()
    c.fills = [{"fill_id": "f1", "order_id": "oid-x", "ticker": "KXNBAGAME-26OCT29ATLCLE-CLE",
                "count": 10, "price_cents": 92, "side": "yes", "action": "buy", "fee_cents": 5, "ts": None}]
    main.sync_fills(c)
    (o,) = rows(led, "orders")                                  # still exactly ONE row for the trade
    assert (o["outcome"], o["order_id"], o["filled"]) == ("placed", "oid-x", 10)
    assert "order_attributed_from_fills" in kinds(led)


def test_a_fill_sync_failure_pauses_settlement_instead_of_booking_a_partial_picture(env, led):  # noqa: F811
    c = Client(order_result=None)
    c.by_client_id = {coid(env): order_dict("oid-1", coid(env), TICKER, filled=20, avg_fill_cents=70)}
    run(env, c)
    # Only part of the fills were stored before the fills endpoint went down.
    led.upsert_fills([{"fill_id": "f1", "order_id": "oid-1", "ticker": TICKER, "count": 5,
                       "price_cents": 70, "side": "yes", "action": "buy", "fee_cents": 4, "ts": None}])

    def boom(**k):
        raise RuntimeError("fills endpoint down")
    c.list_fills = boom
    c.market_result = "no"
    assert main.settle_open_orders(c) == 0                      # waits; does NOT book filled=5
    assert rows(led, "settlements") == [] and "fill_sync_failed" in kinds(led)
    # once the endpoint is back and the full fills arrive, it settles on the complete picture
    del c.list_fills
    c.fills = [{"fill_id": "f1", "order_id": "oid-1", "ticker": TICKER, "count": 5, "price_cents": 70,
                "side": "yes", "action": "buy", "fee_cents": 4, "ts": None},
               {"fill_id": "f2", "order_id": "oid-1", "ticker": TICKER, "count": 15, "price_cents": 70,
                "side": "yes", "action": "buy", "fee_cents": 12, "ts": None}]
    c.orders = {"oid-1": order_dict("oid-1", ticker=TICKER, status="executed", filled=20)}
    assert main.settle_open_orders(c) == 1
    s = rows(led, "settlements")[0]
    assert s["filled"] == 20 and s["pnl_cents"] == -20 * 70 - 16


# ============================================================ 8. the scheduler itself

def test_a_missed_job_is_reported(led):
    from apscheduler.events import EVENT_JOB_MISSED, EVENT_JOB_ERROR

    class Ev:
        def __init__(self, code, job_id, exception=None):
            self.code, self.job_id, self.exception = code, job_id, exception

    main._on_scheduler_event(Ev(EVENT_JOB_MISSED, "2026-10-29-ATL-CLE"))
    main._on_scheduler_event(Ev(EVENT_JOB_ERROR, "health", RuntimeError("x")))
    assert kinds(led) == ["job_missed", "job_error"]


def test_the_daily_rollover_is_a_wall_clock_cron_job_not_a_sleep(monkeypatch, led):
    from apscheduler.triggers.cron import CronTrigger
    added = []

    class FakeSched:
        def __init__(self, **kw): pass
        def start(self): pass
        def shutdown(self): pass
        def add_listener(self, *a, **k): pass
        def add_job(self, func, trigger=None, **k): added.append((func, trigger, k))

    monkeypatch.setattr(main, "BackgroundScheduler", FakeSched)
    monkeypatch.setattr(main, "recover_orders", lambda *a, **k: (0, 0))
    monkeypatch.setattr(main, "health_job", lambda *a, **k: None)
    monkeypatch.setattr(main, "_register_health_job", lambda *a, **k: None)
    monkeypatch.setattr(main, "setup_daily_schedule", lambda *a, **k: None)
    monkeypatch.setattr(main.time, "sleep", lambda _: (_ for _ in ()).throw(KeyboardInterrupt()))

    class P:
        def has_any_positions(self): return False

    class HW:
        def update(self, *a): pass
        def peak(self): return 0
        def drawdown_pct(self, e): return 0.0

    class C:
        def get_balance(self): return 1000

    main._run_loop(C(), P(), HW(), dry_run=True)
    (func, trigger, kw), = [a for a in added if a[2].get("id") == "rollover"]
    assert func is main.setup_daily_schedule and isinstance(trigger, CronTrigger)
    assert kw["misfire_grace_time"] >= 3600 and kw["coalesce"] is True


def test_startup_runs_recovery_before_scheduling_anything(monkeypatch, led):
    order = []

    class FakeSched:
        def __init__(self, **kw): pass
        def start(self): pass
        def shutdown(self): pass
        def add_listener(self, *a, **k): pass
        def add_job(self, *a, **k): order.append("add_job")

    monkeypatch.setattr(main, "BackgroundScheduler", FakeSched)
    monkeypatch.setattr(main, "recover_orders", lambda *a, **k: order.append("recover") or (0, 0))
    monkeypatch.setattr(main, "health_job", lambda *a, **k: order.append("health"))
    monkeypatch.setattr(main, "_register_health_job", lambda *a, **k: None)
    monkeypatch.setattr(main, "setup_daily_schedule", lambda *a, **k: order.append("schedule"))
    monkeypatch.setattr(main.time, "sleep", lambda _: (_ for _ in ()).throw(KeyboardInterrupt()))

    class P:
        def has_any_positions(self): return False

    class HW:
        def update(self, *a): pass
        def peak(self): return 0
        def drawdown_pct(self, e): return 0.0

    class C:
        def get_balance(self): return 1000

    main._run_loop(C(), P(), HW(), dry_run=True)
    assert order[:3] == ["recover", "health", "schedule"]

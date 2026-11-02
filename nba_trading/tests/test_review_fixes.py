"""Regression tests for the independent review's confirmed findings (F1-F7, F9, F11-F13).

Each test replays the reviewer's failing scenario and asserts the books are right: ONE ledger row and
ONE settlement per real trade, and nothing booked from a partial or guessed picture.
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

import pytest

from nba_trading import main
from nba_trading.ledger import Ledger
from nba_trading.tests.fakes import order_dict
from nba_trading.tests.test_main import FakeClient, FakeScheduler, env  # noqa: F401  (fixtures)

NBA = "KXNBAGAME-26OCT29ATLCLE-CLE"
TICK = "KXNBA-G1-BOS"                    # the ticker FakeClient's orderbook uses


@pytest.fixture()
def led(monkeypatch):
    ledger = Ledger(":memory:")
    monkeypatch.setattr(main, "LEDGER", ledger)
    monkeypatch.setattr(main, "SCHEDULER", None)
    monkeypatch.setattr(main.config, "ORDER_LOOKUP_DELAY_SECONDS", 0, raising=False)
    for name in ("_WARNED_FOREIGN", "_SETTLE_STUCK_REPORTED", "_DUP_REPORTED"):
        getattr(main, name, set()).clear()          # tolerant: lets this file run against pre-fix code
    return ledger


class Client(FakeClient):
    market_result = ""

    def get_market(self, ticker):
        return {"result": self.market_result}


def fill(fid, oid, ticker, count, price, fee):
    return {"fill_id": fid, "order_id": oid, "ticker": ticker, "count": count, "price_cents": price,
            "side": "yes", "action": "buy", "fee_cents": fee, "ts": None}


def rows(led, table):
    return [dict(r) for r in led.conn.execute(f"SELECT * FROM {table}")]


def kinds(led):
    return [r["kind"] for r in led.conn.execute("SELECT kind FROM events")]


def coid(env, ticker=TICK):
    return f"nba-{env['game']['game_id']}-{ticker}"


def run(env, client):
    main.pre_game_job(client, env["portfolio"], env["high_water"], env["scheduler"], env["game"], False, 0)


# ====================================================================== F1: one trade, one row

def test_F1_position_and_fills_for_an_unknown_trade_make_one_row_and_one_settlement(env, led):  # noqa: F811
    c = Client(positions=[{"ticker": NBA, "position_fp": "10.00", "exposure_cents": 920}])
    c.fills = [fill("f1", "oid-X", NBA, 10, 92, 6)]
    main.health_job(c, env["portfolio"], env["high_water"])
    (o,) = rows(led, "orders")                                  # was 2 rows
    assert (o["outcome"], o["order_id"]) == ("adopted", "oid-X")     # position row got the real id
    c.market_result, c.positions = "yes", []
    main.health_job(c, env["portfolio"], env["high_water"])
    (s,) = rows(led, "settlements")                             # was 2 settlements (pnl 148 vs true 74)
    assert s["pnl_cents"] == 10 * 8 - 6 and s["source"] == "kalshi_fills"


def test_F1_double_booked_rows_are_never_settled_and_are_reported(env, led):  # noqa: F811
    led.adopt_order(NBA, None, 10, 92, limit_price=92)           # two rows for one trade (legacy damage)
    led.adopt_order(NBA, "oid-X", 10, 92, limit_price=92)
    c = Client()
    c.fills = [fill("f1", "oid-X", NBA, 10, 92, 6)]
    c.market_result = "yes"
    assert main.settle_open_orders(c) == 0
    assert rows(led, "settlements") == [] and "duplicate_ledger_rows" in kinds(led)


# ====================================================================== F2: unresolved + position

def test_F2_an_unconfirmed_order_that_fills_is_not_also_adopted(env, led, monkeypatch):  # noqa: F811
    monkeypatch.setattr(main, "NBA_SERIES_PREFIX", "KXNBA-")
    c = Client(order_result=None)
    c.lookup_error = RuntimeError("503")                        # POST timed out AND lookup failed
    run(env, c)
    assert rows(led, "orders")[0]["outcome"] == "unconfirmed"
    c.lookup_error = None                                       # Kalshi recovers; the order had filled
    c.by_client_id = {coid(env): order_dict("oid-9", coid(env), TICK, filled=10, avg_fill_cents=70)}
    c.positions = [{"ticker": TICK, "position_fp": "10.00", "exposure_cents": 700}]
    main.health_job(c, env["portfolio"], env["high_water"])
    (o,) = rows(led, "orders")                                  # was 2 rows
    assert (o["outcome"], o["order_id"]) == ("placed", "oid-9")
    c.fills, c.market_result, c.positions = [fill("f1", "oid-9", TICK, 10, 70, 15)], "no", []
    c.orders = {"oid-9": order_dict("oid-9", ticker=TICK, status="executed", filled=10)}
    main.health_job(c, env["portfolio"], env["high_water"])
    (s,) = rows(led, "settlements")                             # was -1430 vs true -715
    assert s["pnl_cents"] == -10 * 70 - 15


def test_F2_position_for_an_order_the_lookup_still_cannot_find_is_carried_not_adopted(env, led, monkeypatch):  # noqa: F811
    monkeypatch.setattr(main, "NBA_SERIES_PREFIX", "KXNBA-")
    c = Client(order_result=None)
    c.lookup_error = RuntimeError("503")
    run(env, c)
    c.positions = [{"ticker": TICK, "position_fp": "10.00", "exposure_cents": 700}]
    main.health_job(c, env["portfolio"], env["high_water"])      # lookup still failing
    assert len(rows(led, "orders")) == 1 and rows(led, "orders")[0]["outcome"] == "unconfirmed"
    assert env["portfolio"].has_position(TICK) and "position_without_order" in kinds(led)
    # when the fills sync they are attributed to that same row
    c.fills = [fill("f1", "oid-9", TICK, 10, 70, 15)]
    main.sync_fills(c)
    (o,) = rows(led, "orders")
    assert (o["outcome"], o["order_id"]) == ("placed", "oid-9")


# ====================================================================== F3: in-flight POST vs health job

def test_F3_health_job_never_resolves_an_order_whose_post_is_in_flight(env, led, monkeypatch):  # noqa: F811
    monkeypatch.setattr(main, "HEALTH_LOCK_TIMEOUT_SECONDS", 0.2)
    in_post, release = threading.Event(), threading.Event()

    class Slow(Client):
        def place_limit_order(self, **kw):
            self.order_calls.append(kw)
            in_post.set()
            release.wait(5)
            self.by_client_id = {coid(env): order_dict("oid-L", coid(env), TICK, status="resting", remaining=10)}
            return {"order_id": "oid-L", "status": "resting"}

    c = Slow()
    t = threading.Thread(target=main.pre_game_job,
                         args=(c, env["portfolio"], env["high_water"], env["scheduler"], env["game"], False, 0))
    t.start()
    assert in_post.wait(5)
    main.health_job(c, env["portfolio"], env["high_water"])      # concurrent health cycle: must stay out
    release.set()
    t.join(5)
    (o,) = rows(led, "orders")
    assert (o["outcome"], o["order_id"]) == ("placed", "oid-L")    # was not_found with no id
    assert "order_not_found" not in kinds(led)
    assert len(env["portfolio"]._positions[TICK]) == 1             # one lot, not two (R9)
    assert len(c.order_calls) == 1


def test_F3_a_reply_that_arrives_after_a_wrong_not_found_still_wins(env, led):  # noqa: F811
    pk, _ = led.begin_order(None, TICK, coid(env), 10, 71)
    led.set_order_outcome(pk, "not_found")                      # some other path wrote not_found
    # the order path must not lose the real reply to the illegal transition
    try:
        led.mark_placed(pk, "oid-1")
    except ValueError:
        led.apply_order_state(pk, "oid-1", "resting", None, None, "response")
    assert led.get_order_row(pk)["outcome"] == "placed"


# ====================================================================== F4: not_found is provisional

def test_F4_a_late_appearing_order_upgrades_not_found_and_is_never_resent(env, led):  # noqa: F811
    c = Client(order_result=None)                               # timeout; Kalshi has nothing YET
    run(env, c)
    assert rows(led, "orders")[0]["outcome"] == "not_found"
    c.by_client_id = {coid(env): order_dict("oid-late", coid(env), TICK, filled=5, avg_fill_cents=71)}
    main.health_job(c, env["portfolio"], env["high_water"])      # next health cycle re-checks
    (o,) = rows(led, "orders")
    assert (o["outcome"], o["order_id"], o["filled"]) == ("placed", "oid-late", 5)
    assert env["portfolio"].has_position(TICK)
    assert len(c.order_calls) == 1


def test_F4_not_found_is_rechecked_quietly_and_expires(env, led):  # noqa: F811
    c = Client(order_result=None)
    run(env, c)
    n_alerts = kinds(led).count("order_not_found")
    for _ in range(3):
        main.health_job(c, env["portfolio"], env["high_water"])
    assert kinds(led).count("order_not_found") == n_alerts      # no alert spam on re-checks
    led.conn.execute("UPDATE orders SET ts = '2000-01-01T00:00:00+00:00'")   # ancient
    assert led.provisional_not_found_orders(48) == []           # stops re-checking eventually


def test_F4_the_first_lookup_waits_for_a_slow_post_to_land(env, led, monkeypatch):  # noqa: F811
    monkeypatch.setattr(main.config, "ORDER_LOOKUP_DELAY_SECONDS", 3)
    slept = []
    monkeypatch.setattr(main.time, "sleep", lambda s: slept.append(s))
    run(env, Client(order_result=None))
    assert slept == [3]
    slept.clear()
    run(env, Client(order_result="DUPLICATE_ORDER"))             # a 409 means it exists: no pause needed
    assert slept == []


# ====================================================================== F5: fill check scope

def test_F5_a_fill_check_only_drops_its_own_lot_not_other_games_resting_orders(env, led):  # noqa: F811
    pf = env["portfolio"]
    pf.add_position("KXNBA-B-NYK", 92, 50)                       # game B: just placed, still resting
    pf.add_position("KXNBA-A-BOS", 92, 50)                       # game A: expired unfilled at tip
    c = Client()
    c.orders = {"oid-A": order_dict("oid-A", None, "KXNBA-A-BOS", filled=0, status="canceled")}
    main.confirm_fill_job(c, pf, "KXNBA-A-BOS", "oid-A", 50)
    assert not pf.has_position("KXNBA-A-BOS")                    # its own: dropped
    assert pf.has_position("KXNBA-B-NYK")                        # was dropped before the fix
    assert pf.exposure_cents({})[0] == 92 * 50


def test_F5_reconcile_grace_overrides_are_per_ticker(env):  # noqa: F811
    pf = env["portfolio"]
    pf.add_position("A", 90, 1)
    pf.add_position("B", 90, 1)
    assert pf.reconcile({}, grace_seconds=1800, grace_overrides={"A": 0}) == ["A"]
    assert pf.has_position("B")


# ====================================================================== F6 / F7: settlement honesty

def test_F6_settlement_waits_when_stored_fills_are_short_of_what_the_order_filled(env, led):  # noqa: F811
    pk = led.adopt_order(NBA, "oid-1", 20, 70, limit_price=70)
    led.upsert_fills([fill("f1", "oid-1", NBA, 5, 70, 4)])
    c = Client()
    c.orders = {"oid-1": order_dict("oid-1", ticker=NBA, status="executed", filled=20)}
    c.fills = [fill("f1", "oid-1", NBA, 5, 70, 4)]               # the other 15 not reported yet
    c.market_result = "yes"
    assert main.settle_open_orders(c) == 0
    assert rows(led, "settlements") == []


def test_F7_an_order_that_can_never_settle_raises_one_alert(env, led):  # noqa: F811
    pk = led.adopt_order(NBA, "oid-1", 10, 70, limit_price=70)
    led.conn.execute("DELETE FROM kalshi_fills")
    led.conn.execute("UPDATE orders SET filled = 0, ts = ? WHERE id = ?",
                     ((datetime.now(timezone.utc) - timedelta(hours=30)).isoformat(timespec="seconds"), pk))
    c = Client()
    c.orders = {"oid-1": order_dict("oid-1", ticker=NBA, status="executed", filled=0)}   # parse gap
    c.market_result = "yes"
    for _ in range(4):                                            # four health cycles
        assert main.settle_open_orders(c) == 0
    assert kinds(led).count("settlement_stuck") == 1              # visible, once


def test_F7_a_young_order_still_waiting_is_not_an_alarm(env, led):  # noqa: F811
    pk = led.adopt_order(NBA, "oid-1", 10, 70, limit_price=70)
    led.conn.execute("UPDATE orders SET filled = 0 WHERE id = ?", (pk,))
    c = Client()
    c.orders = {"oid-1": order_dict("oid-1", ticker=NBA, status="executed", filled=0)}
    c.market_result = "yes"
    main.settle_open_orders(c)
    assert "settlement_stuck" not in kinds(led)


# ====================================================================== F9, F11, F12, F13

def test_F9_retry_after_is_capped_so_a_request_cannot_stall_the_trade_lock(tmp_path):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from kalshi.client import KalshiClient, MAX_RETRY_AFTER_SECONDS
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = tmp_path / "k.pem"
    pem.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
                                      serialization.NoEncryption()))
    client = KalshiClient("https://api.example.com/trade-api/v2", "id", str(pem))
    retries = client.session.get_adapter("https://api.example.com").max_retries
    assert retries.allowed_methods == frozenset({"GET"})
    assert retries.retry_after_max <= 10 == MAX_RETRY_AFTER_SECONDS


def test_F11_a_fill_with_an_uninterpretable_side_is_refused_not_guessed(env, led):  # noqa: F811
    pk = led.adopt_order(NBA, "oid-1", 10, 70, limit_price=70)
    bad = fill("f1", "oid-1", NBA, 10, 70, 4)
    bad["side"] = "bid"                                          # a V2 leg label, not yes/no
    led.upsert_fills([bad])
    with pytest.raises(ValueError, match="unsupported side"):
        led.record_settlement_from_fills(pk, "yes")
    assert rows(led, "settlements") == []
    c = Client()
    c.fills = [bad]
    c.orders = {"oid-1": order_dict("oid-1", ticker=NBA, status="executed", filled=10)}
    c.market_result = "yes"
    assert main.settle_open_orders(c) == 0                       # reported, not crashed, not booked
    assert "settlement_unsupported_fill" in kinds(led)


def test_F12_a_dry_run_never_books_manual_or_orphan_fills_into_the_ledger(env, led):  # noqa: F811
    c = Client()
    c.fills = [fill("f1", "oid-manual", NBA, 10, 92, 6)]
    main.health_job(c, env["portfolio"], env["high_water"], dry_run=True)
    assert rows(led, "orders") == []
    assert len(rows(led, "kalshi_fills")) == 1                   # mirrored, but nothing adopted


def test_F13_one_bad_order_does_not_stop_startup_recovery_of_the_rest(env, led, monkeypatch):  # noqa: F811
    led.begin_order(None, "KXNBA-X", "c-bad", 5, 70)
    led.begin_order(None, TICK, coid(env), 5, 71)
    real = main._resolve_order
    calls = []

    def flaky(client, portfolio, scheduler, pk, via="lookup", announce_not_found=True):
        calls.append(pk)
        if len(calls) == 1:
            raise RuntimeError("boom")
        return real(client, portfolio, scheduler, pk, via, announce_not_found)

    monkeypatch.setattr(main, "_resolve_order", flaky)
    c = Client()
    c.by_client_id = {coid(env): order_dict("oid-2", coid(env), TICK, filled=5, avg_fill_cents=71)}
    resolved, _ = main.recover_orders(c, env["portfolio"], env["scheduler"])
    assert len(calls) == 2 and resolved == 1                     # the second row was still recovered


# ====================================================================== strengthened old assertions

def test_degraded_equity_really_leaves_the_peak_alone(env, led):  # noqa: F811
    """Replaces a vacuous check: with an uncostable foreign position the peak must not move at all."""
    env["high_water"].update(100_000, False)
    c = Client(positions=[{"ticker": "KXBTC-26-X", "position_fp": "5.00"}])    # no exposure, no lot
    c.get_balance = lambda: 500_000                              # would be a huge "new peak" if trusted
    main.health_job(c, env["portfolio"], env["high_water"])
    assert env["high_water"].peak() == 100_000
    assert "equity_degraded" in kinds(led)

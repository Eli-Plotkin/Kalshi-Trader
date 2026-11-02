"""The remote pause is a LATCH, fails closed until the first read (live only), is re-checked right
before an order, is acknowledged, and the bot writes a liveness beacon.
(docs/design-replication.md sections 11.1 and 11.4). Fakes and tmp dirs only: no network."""

from __future__ import annotations

import io
import json
import os
import threading
import time

import pytest

from nba_trading import config, control, main
from nba_trading.ledger import Ledger
from nba_trading.tests.test_control import FakeStore
from nba_trading.tests.test_main import FakeClient, FakeScheduler, env  # noqa: F401  (fixtures)


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "KILL_SWITCH_FILE", str(tmp_path / "STOP_TRADING"))
    monkeypatch.setattr(main, "CONTROL_POLLER", None)
    monkeypatch.setattr(main, "CONTROL_BUILD_FAILED", False)
    monkeypatch.setattr(main, "DRY_RUN", True)
    for k in list(os.environ):
        if k.startswith(("CONTROL_BUCKET", "CONTROL_LOCAL", "REPLICATION_")):
            monkeypatch.delenv(k, raising=False)


def _poller(store, events=None):
    ev = events if events is not None else []
    return control.ControlPoller(store, interval=999, on_event=lambda *a: ev.append(a)), ev


# ------------------------------------------------------------------ F1: the latch (reviewer R1)

def test_latch_file_and_ack_written_on_first_paused_read():
    s = FakeStore()
    p, _ = _poller(s)
    control.request_pause(s, by="dashboard", reason="odd\nfills")
    assert not os.path.exists(config.KILL_SWITCH_FILE)
    p.poll_once()
    note = open(config.KILL_SWITCH_FILE).read()
    assert "dashboard" in note and "requested_at" in note and "latched at" in note
    assert "odd fills" in note                                  # one line: no injection of extra fields
    ack = control.read_ack(s)
    assert ack["latched"] is True and ack["by"] == "bot" and ack["acked_at"]
    assert ack["pause_requested_at"] == control.read_pause(s)["requested_at"]
    assert not [f for f in os.listdir(os.path.dirname(config.KILL_SWITCH_FILE)) if f.endswith(".tmp")]


@pytest.mark.parametrize("tamper", [
    lambda s: s.data.__setitem__(control.KEY, b'{"paused": false}'),
    lambda s: s.data.pop(control.KEY),
    lambda s: s.data.__setitem__(control.KEY, b"{broken"),
    lambda s: s.data.__setitem__(control.KEY, b'{"paused": false, "pad": "' + b"A" * 50_000 + b'"}'),
    lambda s: s.data.__setitem__(control.KEY, b""),
], ids=["false", "deleted", "corrupt", "oversized", "empty"])
def test_nothing_remote_clears_the_latch(tamper):
    s = FakeStore()
    p, events = _poller(s)
    main.CONTROL_POLLER = p
    control.request_pause(s, by="dashboard")
    p.poll_once()
    tamper(s)
    for _ in range(3):
        p.poll_once()
    assert p.remote_paused and main.kill_switch_active() and os.path.exists(config.KILL_SWITCH_FILE)
    assert [e[1] for e in events].count("remote_pause_clear_ignored") == 1     # once per transition
    assert next(e for e in events if e[1] == "remote_pause_clear_ignored")[0] == "WARNING"


@pytest.mark.parametrize("raw", [b"{broken", b'{"paused": false, "pad": "' + b"A" * 50_000 + b'"}'], ids=["corrupt", "oversized"])
def test_unreadable_key_never_counts_as_an_admin_resume_even_without_the_local_file(raw):
    s = FakeStore()
    p, _ = _poller(s)
    main.CONTROL_POLLER = p
    control.request_pause(s, by="dashboard")
    p.poll_once()
    s.data[control.KEY] = raw
    os.remove(config.KILL_SWITCH_FILE)
    p.poll_once()
    assert p.remote_paused and main.kill_switch_active()


def test_admin_resume_lets_a_running_bot_trade_again():
    s = FakeStore()
    p, events = _poller(s)
    main.CONTROL_POLLER = p
    control.request_pause(s, by="dashboard")
    p.poll_once()
    assert main.kill_switch_active()
    out = io.StringIO()
    os.environ["CONTROL_LOCAL_DIR"] = "unused"          # build_store is patched below
    try:
        orig = control.build_store
        control.build_store = lambda: s
        assert control.main(["resume"], out=out) == 0
    finally:
        control.build_store = orig
        del os.environ["CONTROL_LOCAL_DIR"]
    assert not os.path.exists(config.KILL_SWITCH_FILE) and control.ACK_KEY not in s.data
    p.poll_once()
    assert not p.remote_paused and not main.kill_switch_active()


def test_repause_after_resume_latches_again_and_acks_again():
    s = FakeStore()
    p, _ = _poller(s)
    control.request_pause(s, by="a")
    p.poll_once()
    control.clear_pause(s)
    control.clear_ack(s)
    os.remove(config.KILL_SWITCH_FILE)
    p.poll_once()
    assert not p.remote_paused
    control.request_pause(s, by="b")
    p.poll_once()
    assert p.remote_paused and os.path.exists(config.KILL_SWITCH_FILE) and control.read_ack(s) is not None


def test_ack_failure_is_retried_on_the_next_poll():
    s = FakeStore()
    p, _ = _poller(s)
    control.request_pause(s, by="a")
    real_put = s.put_bytes

    def flaky(key, data):
        if key == control.ACK_KEY:
            raise ConnectionError("put denied")
        return real_put(key, data)
    s.put_bytes = flaky
    p.poll_once()
    assert p.remote_paused and control.ACK_KEY not in s.data     # latched even though the ack failed
    s.put_bytes = real_put
    p.poll_once()
    assert control.read_ack(s) is not None


def test_latch_file_failure_is_loud_but_pause_holds_in_memory(tmp_path, monkeypatch):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    monkeypatch.setattr(config, "KILL_SWITCH_FILE", str(blocker / "STOP_TRADING"))   # parent is a file
    s = FakeStore()
    p, events = _poller(s)
    main.CONTROL_POLLER = p
    control.request_pause(s, by="a")
    p.poll_once()
    assert main.kill_switch_active() and "kill_switch_file_failed" in [e[1] for e in events]
    assert control.read_ack(s)["latched"] is False


def test_read_ack_semantics():
    s = FakeStore()
    assert control.read_ack(s) is None
    control.write_ack(s, "2026-10-29T00:00:00+00:00")
    assert control.read_ack(s)["pause_requested_at"] == "2026-10-29T00:00:00+00:00"
    s.data[control.ACK_KEY] = b"[1]"
    with pytest.raises(ValueError):
        control.read_ack(s)
    s.down = True
    with pytest.raises(ConnectionError):
        control.read_ack(s)


# ------------------------------------------------------------------ F4: bounded reads (reviewer R11)

def test_reads_pass_the_cap_and_oversized_is_unreadable_not_unpaused():
    seen = []

    class Capped(FakeStore):
        def get_bytes(self, key, max_bytes=None):
            seen.append(max_bytes)
            return super().get_bytes(key, max_bytes=max_bytes)

    s = Capped()
    s.data[control.KEY] = b'{"paused": false, "pad": "' + b"A" * 10_000_000 + b'"}'
    p, _ = _poller(s)
    p.poll_once()
    assert seen and seen[0] == control._max_bytes() <= 65536
    assert p.verified is False and p.last_error and not p.remote_paused


def test_store_without_the_max_bytes_keyword_is_still_bounded():
    class Plain:
        def __init__(self):
            self.data = {control.KEY: b'{"paused": false, "pad": "' + b"A" * 100_000 + b'"}'}

        def get_bytes(self, key):
            return self.data.get(key)

    with pytest.raises(ValueError):
        control.read_pause(Plain())


# ------------------------------------------------------------------ F2: fail closed (reviewer R2)

def _live_source(poller):
    main.CONTROL_POLLER = poller
    return main._kill_switch_source(False)


def test_unverified_live_poller_is_remote_unverified_and_dry_run_is_not():
    s = FakeStore()
    s.down = True
    p, _ = _poller(s)
    p.poll_once()
    assert p.verified is False
    assert _live_source(p) == "remote_unverified"
    assert main.kill_switch_active(False) is True
    assert main._kill_switch_source(True) is None                 # dry run: never blocked
    assert main.kill_switch_active(True) is False


def test_module_mode_global_drives_the_default(monkeypatch):
    s = FakeStore()
    s.down = True
    p, _ = _poller(s)
    main.CONTROL_POLLER = p
    monkeypatch.setattr(main, "DRY_RUN", False)
    assert main.kill_switch_active() is True
    monkeypatch.setattr(main, "DRY_RUN", True)
    assert main.kill_switch_active() is False


def test_restart_with_latch_file_and_bucket_down_is_paused():
    open(config.KILL_SWITCH_FILE, "w").write("latched earlier")
    s = FakeStore()
    s.down = True
    p, _ = _poller(s)
    p.poll_once()
    main.CONTROL_POLLER = p
    assert main._kill_switch_source(False) == "local" and main._kill_switch_source(True) == "local"


def test_restart_without_latch_bucket_down_live_paused_dry_not():
    s = FakeStore()
    s.down = True
    p, _ = _poller(s)
    p.poll_once()
    main.CONTROL_POLLER = p
    assert main.kill_switch_active(False) is True
    assert main.kill_switch_active(True) is False


def test_first_successful_read_opens_trading_unless_paused():
    s = FakeStore()
    s.down = True
    p, _ = _poller(s)
    main.CONTROL_POLLER = p
    p.poll_once()
    assert main.kill_switch_active(False)
    s.down = False
    p.poll_once()                                                 # key missing is a SUCCESSFUL read
    assert p.verified and not main.kill_switch_active(False)
    s.down = True
    p.poll_once()                                                 # a later outage does not re-close it
    assert p.verified and not main.kill_switch_active(False)


def test_first_successful_read_with_a_pause_keeps_it_closed():
    s = FakeStore()
    control.request_pause(s, by="dash")
    p, _ = _poller(s)
    main.CONTROL_POLLER = p
    p.poll_once()
    assert main._kill_switch_source(False) in ("local", "remote") and main.kill_switch_active(False)


def test_configured_but_unbuildable_store_refuses_live_orders(monkeypatch):
    def boom():
        raise RuntimeError("bad credentials")
    monkeypatch.setattr(control, "build_store", boom)
    main._start_control_poller()
    assert main.CONTROL_POLLER is None and main.CONTROL_BUILD_FAILED
    assert main.kill_switch_active(False) is True and main.kill_switch_active(True) is False
    monkeypatch.setattr(control, "build_store", lambda: None)     # not configured at all: not blocked
    main._start_control_poller()
    assert not main.CONTROL_BUILD_FAILED and main.kill_switch_active(False) is False


def test_no_store_configured_never_blocks_live():
    assert main.CONTROL_POLLER is None and main.kill_switch_active(False) is False


def test_live_pre_game_job_with_an_unverified_poller_skips_finally(env, monkeypatch):  # noqa: F811
    led = Ledger(":memory:")
    monkeypatch.setattr(main, "LEDGER", led)
    s = FakeStore()
    s.down = True
    p, _ = _poller(s)
    p.poll_once()
    main.CONTROL_POLLER = p
    client = FakeClient()
    main.pre_game_job(client, env["portfolio"], env["high_water"], env["scheduler"],
                      env["game"], dry_run=False)
    assert client.order_calls == [] and env["scheduler"].jobs == []
    assert env["fired"] == [env["game"]["game_id"]]
    assert [(r["action"], r["reason"]) for r in led.conn.execute("SELECT action, reason FROM decisions")] \
        == [("skip", "kill_switch")]
    # the bucket comes back, no pause: a later game trades normally
    s.down = False
    p.poll_once()
    env["fired"].clear()
    main.pre_game_job(client, env["portfolio"], env["high_water"], env["scheduler"],
                      env["game"], dry_run=False)
    assert len(client.order_calls) == 1


def test_dry_run_pre_game_job_is_not_blocked_by_an_unverified_poller(env, monkeypatch):  # noqa: F811
    s = FakeStore()
    s.down = True
    p, _ = _poller(s)
    p.poll_once()
    main.CONTROL_POLLER = p
    client = FakeClient()
    main.pre_game_job(client, env["portfolio"], env["high_water"], env["scheduler"],
                      env["game"], dry_run=True)
    assert client.order_calls == []                               # dry run never orders...
    # ...but it got past the kill switch (no kill_switch skip was recorded)
    assert env["fired"] == []


def test_first_poll_is_bounded_when_the_store_hangs():
    release = threading.Event()

    class Hung:
        def get_bytes(self, key, max_bytes=None):
            release.wait(30)
            return None

    p, _ = _poller(Hung())
    t0 = time.monotonic()
    assert p.first_poll(0.2) is False
    assert time.monotonic() - t0 < 5 and p.verified is False
    release.set()


def test_first_control_read_runs_for_live_only_and_alerts_when_it_fails(monkeypatch):
    led = Ledger(":memory:")
    monkeypatch.setattr(main, "LEDGER", led)
    monkeypatch.setattr(config, "CONTROL_FIRST_READ_TIMEOUT_SECONDS", 1)
    s = FakeStore()
    s.down = True
    p, _ = _poller(s)
    main.CONTROL_POLLER = p
    main._first_control_read(dry_run=True)
    assert p.last_error is None                                   # dry run: no blocking poll at all
    main._first_control_read(dry_run=False)
    assert p.verified is False and p.last_error
    assert "control_unverified" in [r["kind"] for r in led.conn.execute("SELECT kind FROM events")]
    s.down = False
    main._first_control_read(dry_run=False)
    assert p.verified


def test_run_loop_reads_control_before_scheduling_games(monkeypatch):
    order = []
    s = FakeStore()
    p, _ = _poller(s)
    main.CONTROL_POLLER = p
    monkeypatch.setattr(main, "LEDGER", Ledger(":memory:"))
    monkeypatch.setattr(main, "recover_orders", lambda *a, **k: None)
    monkeypatch.setattr(main, "health_job", lambda *a, **k: None)
    monkeypatch.setattr(main, "_register_health_job", lambda *a, **k: None)
    monkeypatch.setattr(main, "_first_control_read", lambda dry: order.append("first_read"))
    monkeypatch.setattr(main, "setup_daily_schedule", lambda *a, **k: order.append("schedule"))

    def stop_sleep(_):
        raise KeyboardInterrupt
    monkeypatch.setattr(main.time, "sleep", stop_sleep)

    class Port:
        def has_any_positions(self):
            return False

    class HW:
        def update(self, *a):
            pass

        def peak(self):
            return 0

        def drawdown_pct(self, b):
            return 0.0

    class C:
        def get_balance(self):
            return 100

    main._run_loop(C(), Port(), HW(), dry_run=False)
    assert order == ["first_read", "schedule"]
    from nba_trading import liveness
    assert liveness.read(liveness.alive_path(config.LEDGER_FILE)) is not None   # beacon written at startup


# ------------------------------------------------------------------ re-check right before the POST

def test_pause_arriving_between_evaluation_and_post_stops_the_order(env, monkeypatch):  # noqa: F811
    led = Ledger(":memory:")
    monkeypatch.setattr(main, "LEDGER", led)
    calls = {"n": 0}

    def late_pause(*a, **k):
        calls["n"] += 1
        return True                       # the top-of-evaluation check uses _kill_switch_source, not this
    monkeypatch.setattr(main, "kill_switch_active", late_pause)
    client = FakeClient()
    main.pre_game_job(client, env["portfolio"], env["high_water"], env["scheduler"],
                      env["game"], dry_run=False)
    assert calls["n"] == 1 and client.order_calls == []
    row = led.conn.execute("SELECT outcome FROM orders").fetchall()
    assert [r["outcome"] for r in row] == ["failed"]              # no 'submitting' row left behind
    assert [(r["action"], r["reason"]) for r in led.conn.execute("SELECT action, reason FROM decisions ORDER BY rowid")] \
        == [("buy", "in_band"), ("skip", "kill_switch")]
    assert not env["portfolio"].has_any_positions()
    assert env["scheduler"].jobs == [] and env["fired"] == [env["game"]["game_id"]]
    assert "order_blocked_by_kill_switch" in [r["kind"] for r in led.conn.execute("SELECT kind FROM events")]


def test_pause_latched_by_the_poller_mid_evaluation_stops_the_order(env, monkeypatch):  # noqa: F811
    s = FakeStore()
    p, _ = _poller(s)
    p.poll_once()
    main.CONTROL_POLLER = p

    class PausingClient(FakeClient):
        def get_orderbook(self, ticker):             # the dashboard presses PAUSE while we evaluate
            control.request_pause(s, by="dash")
            p.poll_once()
            return super().get_orderbook(ticker)

    client = PausingClient()
    main.pre_game_job(client, env["portfolio"], env["high_water"], env["scheduler"],
                      env["game"], dry_run=False)
    assert client.order_calls == []


def test_unpaused_order_still_goes_through_the_recheck(env):  # noqa: F811
    client = FakeClient()
    main.pre_game_job(client, env["portfolio"], env["high_water"], env["scheduler"],
                      env["game"], dry_run=False)
    assert len(client.order_calls) == 1


# ------------------------------------------------------------------ control store from env (F6)

def test_control_store_prefers_control_vars_then_falls_back(tmp_path):
    c, r = tmp_path / "c", tmp_path / "r"
    store = control.control_store_from_env({"CONTROL_LOCAL_DIR": str(c), "REPLICATION_LOCAL_DIR": str(r)})
    store.put_bytes("control/x", b"1")
    assert (c / "control" / "x").exists() and not (r / "control").exists()
    fallback = control.control_store_from_env({"REPLICATION_LOCAL_DIR": str(r)})
    fallback.put_bytes("control/y", b"1")
    assert (r / "control" / "y").exists()
    assert control.control_store_from_env({}) is None
    blank = control.control_store_from_env({"CONTROL_LOCAL_DIR": "  ", "REPLICATION_LOCAL_DIR": str(r)})
    blank.put_bytes("control/z", b"1")
    assert (r / "control" / "z").exists()                         # blank CONTROL_* means "not set"


def test_build_store_uses_the_control_variables(tmp_path, monkeypatch):
    assert control.build_store() is None
    monkeypatch.setenv("CONTROL_LOCAL_DIR", str(tmp_path / "ctl"))
    st = control.build_store()
    st.put_bytes(control.KEY, b"{}")
    assert (tmp_path / "ctl" / "control" / "pause.json").exists()
    assert control.store_configured()


def test_control_store_s3_uses_control_credentials_and_default_prefix(monkeypatch):
    captured = {}

    def fake_store_from_env(prefix_env="REPLICATION", env=None):
        captured["prefix_env"], captured["env"] = prefix_env, env
        return object()
    import nba_trading.replication.store as store_module
    monkeypatch.setattr(store_module, "store_from_env", fake_store_from_env)
    env_ = {"CONTROL_BUCKET": "ctl", "REPLICATION_BUCKET": "data"}
    control.control_store_from_env(env_)
    assert captured["prefix_env"] == "CONTROL" and captured["env"] is env_
    control.control_store_from_env({"REPLICATION_BUCKET": "data"})
    assert captured["prefix_env"] == "REPLICATION"


# ------------------------------------------------------------------ CLI

def _cli(argv, store, monkeypatch):
    monkeypatch.setattr(control, "build_store", lambda: store)
    out = io.StringIO()
    return control.main(argv, out=out), out.getvalue()


def test_resume_clears_remote_first_and_removes_local_only_after(monkeypatch):
    s = FakeStore()
    order = []
    real_delete = s.delete

    def spy(key):
        order.append(("delete", key, os.path.exists(config.KILL_SWITCH_FILE)))
        return real_delete(key)
    s.delete = spy
    control.request_pause(s, by="dash")
    p, _ = _poller(s)
    p.poll_once()
    rc, out = _cli(["resume"], s, monkeypatch)
    assert rc == 0 and not os.path.exists(config.KILL_SWITCH_FILE)
    assert order[0] == ("delete", control.KEY, True)               # local file still present when the key goes
    assert control.ACK_KEY in [o[1] for o in order] and control.ACK_KEY not in s.data


def test_resume_leaves_the_local_file_when_the_remote_clear_fails(monkeypatch):
    s = FakeStore()
    control.request_pause(s, by="dash")
    p, _ = _poller(s)
    p.poll_once()
    s.down = True
    rc, out = _cli(["resume"], s, monkeypatch)
    assert rc != 0 and os.path.exists(config.KILL_SWITCH_FILE) and "stays paused" in out


def test_resume_survives_a_failing_ack_delete(monkeypatch):
    s = FakeStore()
    open(config.KILL_SWITCH_FILE, "w").write("x")
    real = s.delete

    def delete(key):
        if key == control.ACK_KEY:
            raise ConnectionError("denied")
        return real(key)
    s.delete = delete
    rc, out = _cli(["resume"], s, monkeypatch)
    assert rc == 0 and not os.path.exists(config.KILL_SWITCH_FILE) and "stale" in out


def test_pause_works_with_no_store_and_status_reports_latch_remote_and_ack(monkeypatch):
    rc, out = _cli(["pause", "--reason", "maintenance"], None, monkeypatch)
    assert rc == 0 and "local file only" in out
    assert "maintenance" in open(config.KILL_SWITCH_FILE).read()
    os.remove(config.KILL_SWITCH_FILE)
    s = FakeStore()
    rc, out = _cli(["pause"], s, monkeypatch)
    assert rc == 0 and os.path.exists(config.KILL_SWITCH_FILE) and control.read_pause(s)["by"] == "cli"
    control.write_ack(s, "2026-10-29T10:00:00+00:00")
    rc, out = _cli(["status"], s, monkeypatch)
    assert rc == 0 and "PRESENT" in out and "PAUSED by cli" in out and "bot acknowledged" in out
    s.data.pop(control.KEY)
    s.data.pop(control.ACK_KEY)
    rc, out = _cli(["status"], s, monkeypatch)
    assert "no key" in out and "ack: none" in out


# ------------------------------------------------------------------ liveness (11.4)

def test_ledger_touch_alive_and_get_alive():
    led = Ledger(":memory:")
    assert led.get_alive() is None
    ts = led.touch_alive()
    assert led.get_alive() == ts and ts.endswith("+00:00")
    assert led.conn.execute("SELECT COUNT(*) FROM kv WHERE key='bot_alive_at'").fetchone()[0] == 1
    led.touch_alive()
    assert led.conn.execute("SELECT COUNT(*) FROM kv WHERE key='bot_alive_at'").fetchone()[0] == 1


def test_liveness_job_writes_the_beacon_file_not_a_ledger_row(monkeypatch, tmp_path):
    from nba_trading import liveness
    led = Ledger(":memory:")
    monkeypatch.setattr(main, "LEDGER", led)
    monkeypatch.setattr(config, "LEDGER_FILE", str(tmp_path / "nba_ledger.sqlite"))
    before = led.conn.total_changes
    main.liveness_job()
    stamp = liveness.read(liveness.alive_path(config.LEDGER_FILE))
    assert stamp is not None and stamp.endswith("+00:00")
    assert led.conn.total_changes == before                      # NOT a ledger write: no snapshot churn
    assert led.get_alive() is None


def test_liveness_job_never_raises(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "LEDGER_FILE", str(tmp_path / "no" / "such" / "dir" / "x.sqlite"))
    monkeypatch.setattr("os.replace", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    main.liveness_job()                                           # swallowed and logged


def test_liveness_job_is_scheduled_every_minute_with_grace_and_coalesce():
    from apscheduler.schedulers.background import BackgroundScheduler
    sched = BackgroundScheduler(timezone="UTC")
    main._register_liveness_job(sched)
    job = sched.get_job("liveness")
    assert job is not None and job.trigger.interval.total_seconds() == 60
    assert job.coalesce is True and job.misfire_grace_time > 0
    assert config.CONTROL_FIRST_READ_TIMEOUT_SECONDS == 10

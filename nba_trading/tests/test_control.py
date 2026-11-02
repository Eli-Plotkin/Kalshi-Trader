"""Remote pause (nba_trading/control.py) and its wiring into main.kill_switch_active().

Everything uses an in-memory fake store and an injected clock: no network, no sleeping."""

from __future__ import annotations

import inspect
import io
import json
import os
import threading

import pytest

from nba_trading import config, control, main
from nba_trading.ledger import Ledger
from nba_trading.tests.test_main import FakeClient, FakeScheduler, env  # noqa: F401  (fixtures)


@pytest.fixture(autouse=True)
def _latch_in_tmp(tmp_path, monkeypatch):
    """The poller now WRITES the local kill-switch file: never let a test touch the repo's STOP_TRADING."""
    monkeypatch.setattr(config, "KILL_SWITCH_FILE", str(tmp_path / "STOP_TRADING"))
    monkeypatch.setattr(main, "CONTROL_POLLER", None)
    monkeypatch.setattr(main, "CONTROL_BUILD_FAILED", False)
    monkeypatch.setattr(main, "DRY_RUN", True)


class FakeStore:
    def __init__(self):
        self.data = {}
        self.down = False

    def _check(self):
        if self.down:
            raise ConnectionError("bucket unreachable")

    def put_bytes(self, key, data):
        self._check()
        self.data[key] = bytes(data)

    def get_bytes(self, key, max_bytes=None):
        self._check()
        raw = self.data.get(key)
        if raw is not None and max_bytes is not None and len(raw) > max_bytes:
            raise ValueError(f"{key} too large")
        return raw

    def delete(self, key):
        self._check()
        self.data.pop(key, None)


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def _poller(store, clock=None, events=None, **kw):
    ev = events if events is not None else []
    return control.ControlPoller(store, interval=30, alert_after=kw.pop("alert_after", 300),
                                 clock=clock or Clock(), on_event=lambda *a: ev.append(a), **kw), ev


# ---------------------------------------------------------------- store documents

def test_request_read_clear_round_trip():
    s = FakeStore()
    assert control.read_pause(s) is None
    control.request_pause(s, by="dash", reason="odd fills")
    doc = control.read_pause(s)
    assert doc["paused"] is True and doc["by"] == "dash" and doc["reason"] == "odd fills"
    assert doc["requested_at"].endswith("+00:00")
    assert control.KEY == "control/pause.json" and control.KEY in s.data
    control.clear_pause(s)
    assert control.read_pause(s) is None and control.KEY not in s.data
    control.clear_pause(s)                                   # clearing twice is fine


def test_read_pause_false_document_is_none():
    s = FakeStore()
    s.data[control.KEY] = json.dumps({"paused": False}).encode()
    assert control.read_pause(s) is None


@pytest.mark.parametrize("raw", [b"{not json", b"", b"\xff\xfe", b"[]", b'{"paused": "yes"}', b'{"x": 1}', b'null'])
def test_corrupt_document_raises_not_silently_unpaused(raw):
    s = FakeStore()
    s.data[control.KEY] = raw
    with pytest.raises(ValueError):
        control.read_pause(s)


def test_read_pause_raises_on_transport_error():
    s = FakeStore()
    s.down = True
    with pytest.raises(ConnectionError):
        control.read_pause(s)


# ---------------------------------------------------------------- poller state machine

def test_poller_follows_pause_and_keeps_state_through_an_outage():
    s = FakeStore()
    p, events = _poller(s)
    p.poll_once()
    assert p.remote_paused is False and p.last_ok_at is not None and p.last_error is None
    control.request_pause(s, by="dash", reason="x")
    p.poll_once()
    assert p.remote_paused is True
    s.down = True
    p.poll_once()
    p.poll_once()
    assert p.remote_paused is True                           # outage does NOT un-pause
    assert "ConnectionError" in p.last_error
    s.down = False
    p.poll_once()
    assert p.remote_paused is True and p.last_error is None
    control.clear_pause(s)                                   # remote clear alone does NOT un-pause (latch)
    p.poll_once()
    assert p.remote_paused is True and os.path.exists(config.KILL_SWITCH_FILE)
    os.remove(config.KILL_SWITCH_FILE)                       # an admin resume: remote gone AND file gone
    p.poll_once()
    assert p.remote_paused is False


def test_failure_before_any_success_stays_unpaused_but_is_recorded():
    s = FakeStore()
    s.down = True
    p, _ = _poller(s)
    p.poll_once()
    assert p.remote_paused is False and p.last_ok_at is None and "unreachable" in p.last_error


def test_corrupt_file_keeps_last_state_and_records_error():
    s = FakeStore()
    p, _ = _poller(s)
    control.request_pause(s, by="dash")
    p.poll_once()
    assert p.remote_paused is True
    s.data[control.KEY] = b"{garbage"
    p.poll_once()
    assert p.remote_paused is True and "not valid JSON" in p.last_error
    # and a corrupt file with no earlier pause is not "paused" but IS reported as an error
    p2, _ = _poller(s)
    p2.poll_once()
    assert p2.remote_paused is False and p2.last_error


def test_pause_events_once_per_transition():
    s = FakeStore()
    p, events = _poller(s)
    p.poll_once()
    assert events == []
    control.request_pause(s, by="dash", reason="because")
    for _ in range(4):
        p.poll_once()
    crit = [e for e in events if e[1] == "remote_pause_requested"]
    assert len(crit) == 1 and crit[0][0] == "CRITICAL" and "dash" in crit[0][2] and "because" in crit[0][2]
    control.clear_pause(s)                                   # tampering: visible once, never a clear
    p.poll_once()
    p.poll_once()
    assert [e[1] for e in events].count("remote_pause_clear_ignored") == 1
    assert [e[1] for e in events].count("remote_pause_cleared") == 0 and p.remote_paused
    os.remove(config.KILL_SWITCH_FILE)                       # admin resume
    p.poll_once()
    p.poll_once()
    assert [e[1] for e in events].count("remote_pause_cleared") == 1
    control.request_pause(s, by="dash")                      # a second pause is a new transition
    p.poll_once()
    assert [e[1] for e in events].count("remote_pause_requested") == 2


def test_pause_already_present_at_startup_raises_the_event():
    s = FakeStore()
    control.request_pause(s, by="dash")
    p, events = _poller(s)
    p.poll_once()
    assert p.remote_paused and [e[1] for e in events] == ["remote_pause_requested"]


def test_unreachable_alert_after_silence_once_per_cooldown():
    s = FakeStore()
    clock = Clock()
    p, events = _poller(s, clock=clock, alert_after=300)
    p.poll_once()                                            # ok at t=1000
    s.down = True
    clock.t = 1100
    p.poll_once()
    clock.t = 1299
    p.poll_once()
    assert events == []                                      # < 300 s of silence
    clock.t = 1300
    p.poll_once()
    assert [e[:2] for e in events] == [("ERROR", "control_unreachable")]
    clock.t = 1400
    p.poll_once()
    assert len(events) == 1                                  # cooldown: not every poll
    clock.t = 1300 + control.ControlPoller.ALERT_COOLDOWN_SECONDS
    p.poll_once()
    assert [e[1] for e in events] == ["control_unreachable"] * 2
    s.down = False
    p.poll_once()
    assert events[-1][1] == "control_recovered"


def test_unreachable_alert_counts_from_construction_if_never_readable():
    s = FakeStore()
    s.down = True
    clock = Clock(0)
    p, events = _poller(s, clock=clock, alert_after=300)
    clock.t = 299
    p.poll_once()
    assert events == []
    clock.t = 301
    p.poll_once()
    assert [e[1] for e in events] == ["control_unreachable"]


def test_on_event_exception_does_not_break_polling():
    s = FakeStore()
    control.request_pause(s, by="dash")
    p = control.ControlPoller(s, on_event=lambda *a: 1 / 0)
    p.poll_once()
    assert p.remote_paused is True


# ---------------------------------------------------------------- thread lifecycle

def _poller_threads():
    return [t for t in threading.enumerate() if t.name == "control-poller"]


def test_start_stop_clean_and_idempotent():
    assert _poller_threads() == []
    s = FakeStore()
    control.request_pause(s, by="dash")
    p = control.ControlPoller(s, interval=3600)
    p.stop()                                                 # stop before start is fine
    p.start()
    p.start()                                                # second start does not add a thread
    assert len(_poller_threads()) == 1
    deadline = threading.Event()
    for _ in range(200):                                     # the immediate first poll is async
        if p.remote_paused:
            break
        deadline.wait(0.01)
    assert p.remote_paused is True
    p.stop()
    p.stop()
    assert _poller_threads() == []
    p.start()                                                # restartable
    assert len(_poller_threads()) == 1
    p.stop()
    assert _poller_threads() == []


# ---------------------------------------------------------------- kill_switch_active

class FakePoller:
    def __init__(self, paused):
        self.remote_paused = paused


@pytest.mark.parametrize("local,remote,expected", [
    (False, False, False), (True, False, True), (False, True, True), (True, True, True),
])
def test_kill_switch_active_combinations(tmp_path, monkeypatch, local, remote, expected):
    f = tmp_path / "STOP_TRADING"
    monkeypatch.setattr(config, "KILL_SWITCH_FILE", str(f))
    if local:
        f.write_text("")
    monkeypatch.setattr(main, "CONTROL_POLLER", FakePoller(remote))
    assert main.kill_switch_active() is expected


def test_kill_switch_active_without_a_poller(tmp_path, monkeypatch):
    f = tmp_path / "STOP_TRADING"
    monkeypatch.setattr(config, "KILL_SWITCH_FILE", str(f))
    monkeypatch.setattr(main, "CONTROL_POLLER", None)
    assert main.kill_switch_active() is False
    f.write_text("")
    assert main.kill_switch_active() is True


# ---------------------------------------------------------------- pre_game_job under a remote pause

def test_remote_pause_blocks_order_and_is_final_in_live_mode(env, monkeypatch):  # noqa: F811
    led = Ledger(":memory:")
    monkeypatch.setattr(main, "LEDGER", led)
    monkeypatch.setattr(main, "CONTROL_POLLER", FakePoller(True))
    client = FakeClient()
    main.pre_game_job(client, env["portfolio"], env["high_water"], env["scheduler"],
                      env["game"], dry_run=False)
    assert client.order_calls == []
    assert env["scheduler"].jobs == []                       # no retry, no fill check
    assert env["fired"] == [env["game"]["game_id"]]          # final: marked fired
    rows = list(led.conn.execute("SELECT action, reason FROM decisions"))
    assert [(r["action"], r["reason"]) for r in rows] == [("skip", "kill_switch")]


def test_remote_pause_in_dry_run_records_skip_and_does_not_mark_fired(env, monkeypatch):  # noqa: F811
    led = Ledger(":memory:")
    monkeypatch.setattr(main, "LEDGER", led)
    monkeypatch.setattr(main, "CONTROL_POLLER", FakePoller(True))
    client = FakeClient()
    main.pre_game_job(client, env["portfolio"], env["high_water"], env["scheduler"],
                      env["game"], dry_run=True)
    assert client.order_calls == [] and env["scheduler"].jobs == [] and env["fired"] == []
    assert led.conn.execute("SELECT reason FROM decisions").fetchone()["reason"] == "kill_switch"


def test_not_paused_still_trades(env, monkeypatch):  # noqa: F811
    monkeypatch.setattr(main, "CONTROL_POLLER", FakePoller(False))
    client = FakeClient()
    main.pre_game_job(client, env["portfolio"], env["high_water"], env["scheduler"],
                      env["game"], dry_run=False)
    assert len(client.order_calls) == 1


# ---------------------------------------------------------------- bot wiring

def test_start_control_poller_is_noop_without_a_store(monkeypatch):
    monkeypatch.setattr(main, "CONTROL_POLLER", None)
    monkeypatch.setattr(control, "build_store", lambda: None)
    main._start_control_poller()
    assert main.CONTROL_POLLER is None and _poller_threads() == []


def test_start_control_poller_failure_is_an_error_event_not_a_crash(monkeypatch):
    led = Ledger(":memory:")
    monkeypatch.setattr(main, "LEDGER", led)
    monkeypatch.setattr(main, "CONTROL_POLLER", None)

    def boom():
        raise RuntimeError("bad credentials")
    monkeypatch.setattr(control, "build_store", boom)
    main._start_control_poller()                             # must not raise
    assert main.CONTROL_POLLER is None
    ev = led.conn.execute("SELECT level, kind FROM events").fetchall()
    assert [(e["level"], e["kind"]) for e in ev] == [("ERROR", "control_unavailable")]


def test_start_and_stop_control_poller_with_a_store(monkeypatch):
    s = FakeStore()
    control.request_pause(s, by="dash")
    monkeypatch.setattr(main, "CONTROL_POLLER", None)
    monkeypatch.setattr(control, "build_store", lambda: s)
    monkeypatch.setattr(config, "CONTROL_POLL_SECONDS", 3600)
    main._start_control_poller()
    assert isinstance(main.CONTROL_POLLER, control.ControlPoller) and len(_poller_threads()) == 1
    main._stop_control_poller()
    assert main.CONTROL_POLLER is None and _poller_threads() == []


def test_config_defaults():
    assert config.CONTROL_POLL_SECONDS == 30 and config.CONTROL_ALERT_AFTER_SECONDS == 300


# ---------------------------------------------------------------- resume is admin-only / CLI

def test_no_frontend_callable_can_resume_except_the_admin_helpers():
    public = {n for n, o in vars(control).items()
              if inspect.isfunction(o) and not n.startswith("_") and o.__module__ == control.__name__}
    resume_like = {n for n in public if any(w in n for w in ("resume", "clear", "unpause", "delete", "remove"))}
    assert resume_like == {"clear_pause", "clear_ack"}
    assert "ADMIN ONLY" in control.clear_pause.__doc__ and "ADMIN ONLY" in control.clear_ack.__doc__
    assert "ONLY call a frontend may make" in control.request_pause.__doc__


def test_frontend_credential_cannot_resume_the_bot_end_to_end():
    """The owner pauses; an attacker holding the frontend credential writes paused:false, deletes the
    key and corrupts it. The bot stays paused through all of it (the latch)."""
    s = FakeStore()
    p = control.ControlPoller(s, interval=999)
    main.CONTROL_POLLER = p
    control.request_pause(s, by="dashboard", reason="owner sees a problem")
    p.poll_once()
    assert p.remote_paused and main.kill_switch_active()
    s.data[control.KEY] = json.dumps({"paused": False}).encode()
    p.poll_once()
    assert main.kill_switch_active() and os.path.exists(config.KILL_SWITCH_FILE)
    s.data.pop(control.KEY)
    p.poll_once()
    assert main.kill_switch_active()
    s.data[control.KEY] = b"{garbage"
    p.poll_once()
    assert main.kill_switch_active()
    s.data[control.KEY] = b'{"paused": false, "pad": "' + b"A" * 100_000 + b'"}'
    p.poll_once()
    assert main.kill_switch_active() and "too large" in p.last_error
    # and a fresh process (restart) is still paused with the bucket empty or down
    p2 = control.ControlPoller(FakeStore(), interval=999)
    main.CONTROL_POLLER = p2
    p2.poll_once()
    assert main.kill_switch_active()


def _cli(argv, store, monkeypatch):
    monkeypatch.setattr(control, "build_store", lambda: store)
    out = io.StringIO()
    rc = control.main(argv, out=out)
    return rc, out.getvalue()


def test_cli_pause_status_resume(tmp_path, monkeypatch):
    f = tmp_path / "STOP_TRADING"
    monkeypatch.setattr(config, "KILL_SWITCH_FILE", str(f))
    s = FakeStore()
    rc, out = _cli(["status"], s, monkeypatch)
    assert rc == 0 and "absent" in out and "not paused" in out
    rc, _ = _cli(["pause", "--reason", "manual"], s, monkeypatch)
    assert rc == 0 and f.exists() and control.read_pause(s)["reason"] == "manual"
    rc, out = _cli(["status"], s, monkeypatch)
    assert "PRESENT" in out and "PAUSED" in out
    rc, _ = _cli(["resume"], s, monkeypatch)
    assert rc == 0 and not f.exists() and control.read_pause(s) is None
    assert f.exists() is False and control.ACK_KEY not in s.data


def test_cli_without_a_store_works_on_the_local_file_and_says_so(tmp_path, monkeypatch):
    f = tmp_path / "STOP_TRADING"
    monkeypatch.setattr(config, "KILL_SWITCH_FILE", str(f))
    rc, out = _cli(["pause"], None, monkeypatch)
    assert rc == 0 and f.exists() and "local file only" in out
    rc, out = _cli(["status"], None, monkeypatch)
    assert "not configured" in out
    rc, out = _cli(["resume"], None, monkeypatch)
    assert rc == 0 and not f.exists()


def test_cli_resume_reports_failure_when_remote_clear_fails(tmp_path, monkeypatch):
    f = tmp_path / "STOP_TRADING"
    monkeypatch.setattr(config, "KILL_SWITCH_FILE", str(f))
    s = FakeStore()
    control.request_pause(s, by="dash")
    s.down = True
    f.write_text("latched")
    rc, out = _cli(["resume"], s, monkeypatch)
    assert rc == 1 and "NOT cleared" in out and f.exists()   # remote clear failed: local latch stays


def test_cli_pause_with_unbuildable_store_still_pauses_locally(tmp_path, monkeypatch):
    f = tmp_path / "STOP_TRADING"
    monkeypatch.setattr(config, "KILL_SWITCH_FILE", str(f))

    def boom():
        raise RuntimeError("no creds")
    monkeypatch.setattr(control, "build_store", boom)
    out = io.StringIO()
    assert control.main(["pause"], out=out) == 1 and f.exists()


def test_cli_resume_with_unbuildable_store_touches_nothing(tmp_path, monkeypatch):
    f = tmp_path / "STOP_TRADING"
    f.write_text("")
    monkeypatch.setattr(config, "KILL_SWITCH_FILE", str(f))

    def boom():
        raise RuntimeError("no creds")
    monkeypatch.setattr(control, "build_store", boom)
    assert control.main(["resume"], out=io.StringIO()) == 2 and f.exists()


def test_build_store_is_none_when_unconfigured(monkeypatch):
    monkeypatch.delenv("REPLICATION_BUCKET", raising=False)
    monkeypatch.delenv("REPLICATION_LOCAL_DIR", raising=False)
    assert control.build_store() is None

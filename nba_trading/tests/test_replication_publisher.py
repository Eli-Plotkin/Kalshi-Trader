"""Publisher: publish order, change detection, heartbeat, restart seq, failures, backoff, alerting."""

import json
import os
import threading
from datetime import datetime, timedelta, timezone

import pytest

from nba_trading import alerts
from nba_trading.ledger import Ledger
from nba_trading.replication import publisher as pubmod
from nba_trading.replication import snapshot as snap
from nba_trading.replication.publisher import Publisher, publish_once
from nba_trading.replication.store import LocalDirStore

T0 = datetime(2026, 10, 29, 18, 0, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t = self.t + timedelta(seconds=seconds)


class SpyStore(LocalDirStore):
    """LocalDirStore that records every call and can be told to fail."""

    def __init__(self, root):
        super().__init__(root)
        self.ops = []
        self.fail = {}          # op name -> exception (raised before doing anything), or callable(key)

    def _hook(self, op, key):
        self.ops.append((op, key))
        exc = self.fail.get(op)
        if exc is not None:
            if callable(exc) and not isinstance(exc, BaseException):
                exc = exc(key)
            if exc is not None:
                raise exc

    def put_file(self, key, path):
        self._hook("put_file", key)
        return super().put_file(key, path)

    def put_bytes(self, key, data):
        self._hook("put_bytes", key)
        return super().put_bytes(key, data)

    def get_bytes(self, key, max_bytes=None):
        self._hook("get_bytes", key)
        return super().get_bytes(key, max_bytes)

    def get_file(self, key, dest_path):
        self._hook("get_file", key)
        return super().get_file(key, dest_path)

    def delete(self, key):
        self._hook("delete", key)
        return super().delete(key)

    def list_keys(self, prefix):
        self._hook("list_keys", prefix)
        return super().list_keys(prefix)

    def count(self, op):
        return sum(1 for o, _k in self.ops if o == op)


@pytest.fixture
def sent(monkeypatch):
    calls = []
    monkeypatch.setattr(alerts, "notify", lambda level, kind, message: calls.append((level, kind, message)))
    return calls


@pytest.fixture
def env(tmp_path):
    led = Ledger(str(tmp_path / "ledger.sqlite"))
    led.record_event("INFO", "heartbeat", "ok")
    store = SpyStore(tmp_path / "bucket")
    clock = Clock()
    yield type("E", (), {"led": led, "store": store, "clock": clock, "db": str(tmp_path / "ledger.sqlite"),
                         "tmp": tmp_path})
    led.close()


def make_pub(env, **kw):
    kw.setdefault("now", env.clock)
    return Publisher(env.store, env.db, **kw)


# --- publish_once --------------------------------------------------------------------------------

def test_publish_once_writes_snapshot_before_manifest(env):
    m = publish_once(env.store, env.db, now=env.clock)
    puts = [(op, k) for op, k in env.store.ops if op in ("put_file", "put_bytes")]
    # snapshot object first, the seq floor next, the manifest LAST
    assert puts == [("put_file", m.snapshot_key), ("put_bytes", "seq-floor.json"),
                    ("put_bytes", "latest.json")]
    assert m.seq == 1 and m.snapshot_key.startswith("snapshots/000000001-")
    assert m.generated_at == "2026-10-29T18:00:00+00:00"
    assert snap.read_manifest(env.store) == m
    obj = env.store.get_bytes(m.snapshot_key)
    assert len(obj) == m.size
    assert m.counts["events"] == 1 and m.bot_heartbeat_at


def test_snapshot_upload_failure_leaves_manifest_untouched(env):
    first = publish_once(env.store, env.db, now=env.clock)
    env.led.record_event("INFO", "x", "y")
    env.store.fail["put_file"] = ConnectionError("down")
    with pytest.raises(ConnectionError):
        publish_once(env.store, env.db, now=env.clock)
    assert snap.read_manifest(env.store) == first
    assert env.store.get_bytes(first.snapshot_key) is not None


def test_manifest_write_failure_leaves_old_manifest_and_old_snapshot(env):
    first = publish_once(env.store, env.db, now=env.clock)
    env.led.record_event("INFO", "x", "y")
    env.store.fail["put_bytes"] = TimeoutError("t/o")
    with pytest.raises(TimeoutError):
        publish_once(env.store, env.db, now=env.clock)
    assert snap.read_manifest(env.store) == first                      # readers still get a good state
    assert env.store.get_bytes(first.snapshot_key) is not None


def test_seq_continues_after_publisher_restart(env):
    a = publish_once(env.store, env.db, now=env.clock)
    b = publish_once(env.store, env.db, now=env.clock)
    assert (a.seq, b.seq) == (1, 2)
    p = make_pub(env)                              # "restart": brand-new process state, same bucket
    assert p.tick() is True
    assert p.last_manifest.seq == 3
    p2 = make_pub(env)                             # restart again, now on a corrupt manifest
    env.store.put_bytes("latest.json", b"{garbage")
    p2.tick()
    assert p2.last_manifest.seq > 3                # still strictly increasing (derived from the seq floor)


def test_seq_continues_when_restored_on_a_new_server_with_only_the_bucket(env, tmp_path):
    for _ in range(3):
        publish_once(env.store, env.db, now=env.clock)
    new_server_db = tmp_path / "new" / "ledger.sqlite"
    new_led = Ledger(str(new_server_db))
    m = publish_once(env.store, str(new_server_db), now=env.clock)
    assert m.seq == 4
    new_led.close()


def test_identical_content_reuses_uploaded_snapshot(env):
    a = publish_once(env.store, env.db, now=env.clock)
    env.clock.advance(30)
    b = publish_once(env.store, env.db, now=env.clock, previous=a)
    assert b.sha256 == a.sha256                    # an unchanged DB gives a byte-identical backup
    assert b.snapshot_key == a.snapshot_key
    assert env.store.count("put_file") == 1
    assert b.seq == 2 and b.generated_at != a.generated_at


def test_reuse_requires_previous(env):
    with pytest.raises(ValueError):
        publish_once(env.store, env.db, now=env.clock, reuse=True)


def test_prune_keeps_newest_n_and_manifest_target(env):
    for _ in range(6):
        env.led.record_event("INFO", "x", "y")
        publish_once(env.store, env.db, now=env.clock, keep=3)
    keys = env.store.list_keys("snapshots/")
    assert len(keys) == 3
    assert snap.read_manifest(env.store).snapshot_key in keys


def test_prune_failure_does_not_fail_the_publish(env):
    publish_once(env.store, env.db, now=env.clock, keep=1)
    env.led.record_event("INFO", "x", "y")
    env.store.fail["delete"] = ConnectionError("nope")
    m = publish_once(env.store, env.db, now=env.clock, keep=1)
    assert snap.read_manifest(env.store) == m


# --- change detection / heartbeat ------------------------------------------------------------------

def test_first_tick_publishes_then_idle_ticks_do_nothing(env):
    p = make_pub(env)
    assert p.tick() is True
    first = p.last_manifest
    n_put_file = env.store.count("put_file")
    n_put_bytes = env.store.count("put_bytes")
    for _ in range(1):                            # 1 tick x 5 s = 5 s < 10 s heartbeat, no DB writes
        env.clock.advance(5)
        assert p.tick() is False
    assert env.store.count("put_file") == n_put_file
    assert env.store.count("put_bytes") == n_put_bytes
    assert snap.read_manifest(env.store) == first
    p.close()


def test_db_change_publishes_new_snapshot_on_next_tick(env):
    p = make_pub(env)
    p.tick()
    env.clock.advance(5)
    env.led.record_event("INFO", "order", "placed")          # another connection commits
    assert p.tick() is True
    m = snap.read_manifest(env.store)
    assert m.seq == 2 and m.counts["events"] == 2
    assert env.store.count("put_file") == 2
    # and the published snapshot really contains the write
    dest = str(env.tmp / "check.sqlite")
    env.store.get_file(m.snapshot_key, dest)
    assert snap.inspect_db(dest)[1]["events"] == 2
    p.close()


def test_heartbeat_republishes_manifest_only_with_fresh_generated_at_and_seq(env):
    p = make_pub(env)
    p.tick()
    first = p.last_manifest
    env.clock.advance(30)                                  # nothing changed, heartbeat interval elapsed
    assert p.tick() is True
    hb = snap.read_manifest(env.store)
    assert hb.seq == first.seq + 1
    assert hb.generated_at == snap.iso(env.clock.t) and hb.generated_at != first.generated_at
    assert hb.snapshot_key == first.snapshot_key and hb.sha256 == first.sha256     # same object
    assert env.store.count("put_file") == 1                                        # NOT re-uploaded
    assert env.store.count("put_bytes") == 3               # seq-floor.json once + two manifests
    env.clock.advance(30)
    p.tick()
    assert snap.read_manifest(env.store).seq == first.seq + 2
    p.close()


def test_write_after_heartbeats_still_publishes_a_new_snapshot(env):
    p = make_pub(env)
    p.tick()
    env.clock.advance(30)
    p.tick()
    env.led.record_event("INFO", "x", "y")
    env.clock.advance(5)
    assert p.tick() is True
    assert env.store.count("put_file") == 2
    assert snap.read_manifest(env.store).counts["events"] == 2
    p.close()


def test_pending_change_is_retried_after_a_failed_publish(env):
    p = make_pub(env)
    p.tick()
    env.led.record_event("INFO", "x", "y")
    env.store.fail["put_file"] = ConnectionError("down")
    env.clock.advance(5)
    assert p.tick() is False
    assert p.consecutive_failures == 1
    env.store.fail.clear()
    env.clock.advance(60)                                  # past the backoff
    assert p.tick() is True
    assert snap.read_manifest(env.store).counts["events"] == 2     # the change was not forgotten
    assert p.consecutive_failures == 0
    p.close()


def test_replaced_db_file_is_noticed(env):
    p = make_pub(env)
    p.tick()
    env.led.close()
    os.unlink(env.db)
    for suffix in ("-wal", "-shm"):
        if os.path.exists(env.db + suffix):
            os.unlink(env.db + suffix)
    new = Ledger(env.db)
    new.record_event("INFO", "fresh", "db")
    env.clock.advance(5)
    assert p.tick() is True
    assert snap.read_manifest(env.store).counts["events"] == 1
    new.close()
    env.led = Ledger(env.db)                               # for fixture teardown
    p.close()


# --- failures, backoff, alerting ---------------------------------------------------------------------

def test_missing_db_never_raises_and_recovers_when_it_appears(tmp_path, sent):
    store = SpyStore(tmp_path / "b")
    clock = Clock()
    db = str(tmp_path / "later.sqlite")
    p = Publisher(store, db, now=clock)
    assert p.tick() is False
    assert p.consecutive_failures == 1
    assert "No such file" in p.last_error or "does not exist" in p.last_error
    assert not os.path.exists(db)                          # a read-only publisher never creates the ledger
    Ledger(db).close()
    clock.advance(60)
    assert p.tick() is True
    assert p.consecutive_failures == 0 and p.last_error is None
    p.close()


def test_backoff_doubles_and_caps_at_30s(env):
    env.store.fail["get_bytes"] = ConnectionError("down")
    env.store.fail["list_keys"] = ConnectionError("down")
    p = make_pub(env, tick_seconds=5)
    delays = []
    for _ in range(7):
        p.tick()
        delays.append((p._next_attempt_at - env.clock.t).total_seconds())
        env.clock.t = p._next_attempt_at
    assert delays == [5, 10, 20, 30, 30, 30, 30]
    assert p.consecutive_failures == 7
    p.close()


def test_no_attempt_during_backoff_window(env):
    env.store.fail["list_keys"] = ConnectionError("down")
    p = make_pub(env, tick_seconds=5)
    p.tick()
    before = len(env.store.ops)
    env.clock.advance(1)
    p.tick()
    assert len(env.store.ops) == before
    p.close()


def test_alert_after_silence_once_per_cooldown_then_recovery(env, sent):
    env.store.fail["list_keys"] = ConnectionError("bucket unreachable")
    env.store.fail["get_bytes"] = ConnectionError("bucket unreachable")
    p = make_pub(env, tick_seconds=5, alert_after_seconds=120, alert_cooldown_seconds=600)
    for _ in range(40):                                   # ~100 s of failures: below the threshold
        p.tick()
        env.clock.advance(5)
        if (env.clock.t - T0).total_seconds() >= 100:
            break
    assert sent == []
    while (env.clock.t - T0).total_seconds() <= 130:
        p.tick()
        env.clock.advance(5)
    stale = [c for c in sent if c[1] == "replication_stale"]
    assert len(stale) == 1 and stale[0][0] == "ERROR" and "bucket unreachable" in stale[0][2]
    while (env.clock.t - T0).total_seconds() <= 500:      # still within the cooldown: no repeat
        p.tick()
        env.clock.advance(5)
    assert len([c for c in sent if c[1] == "replication_stale"]) == 1
    while (env.clock.t - T0).total_seconds() <= 800:      # cooldown elapsed: one reminder
        p.tick()
        env.clock.advance(5)
    assert len([c for c in sent if c[1] == "replication_stale"]) == 2
    env.store.fail.clear()
    env.clock.advance(60)
    assert p.tick() is True
    rec = [c for c in sent if c[1] == "replication_recovered"]
    assert len(rec) == 1 and rec[0][0] in ("INFO", "WARNING")
    p.tick()
    env.clock.advance(30)
    p.tick()
    assert len([c for c in sent if c[1] == "replication_recovered"]) == 1      # only once


def test_short_blip_sends_no_recovery_notice(env, sent):
    p = make_pub(env, tick_seconds=5)
    p.tick()
    env.led.record_event("INFO", "x", "y")
    env.store.fail["put_file"] = ConnectionError("blip")
    env.clock.advance(5)
    p.tick()
    env.store.fail.clear()
    env.clock.advance(30)
    assert p.tick() is True
    assert sent == []
    p.close()


def test_notify_exceptions_are_swallowed(env, monkeypatch):
    monkeypatch.setattr(alerts, "notify", lambda *a: (_ for _ in ()).throw(RuntimeError("boom")))
    env.store.fail["list_keys"] = ConnectionError("x")
    p = make_pub(env, tick_seconds=1, alert_after_seconds=0)
    p.tick()                                               # must not raise
    p.close()


def test_status_file_tracks_success_and_failure(env):
    status = str(env.tmp / "run" / "status.json")
    p = make_pub(env, status_file=status)
    p.tick()
    st = json.load(open(status))
    assert st["consecutive_failures"] == 0 and st["last_seq"] == 1 and st["last_error"] is None
    assert st["last_success_at"] == snap.iso(T0)
    assert pubmod.status_healthy(status, 120, now=env.clock)
    env.store.fail["put_bytes"] = ConnectionError("down")
    env.led.record_event("INFO", "x", "y")
    env.clock.advance(60)
    p.tick()
    st = json.load(open(status))
    assert st["consecutive_failures"] == 1 and "down" in st["last_error"]
    assert st["last_success_at"] == snap.iso(T0)           # unchanged by the failure
    env.clock.advance(200)
    assert not pubmod.status_healthy(status, 120, now=env.clock)
    p.close()


def test_status_healthy_false_for_missing_or_garbage_file(tmp_path):
    assert not pubmod.status_healthy(str(tmp_path / "none.json"))
    (tmp_path / "bad.json").write_text("{nope")
    assert not pubmod.status_healthy(str(tmp_path / "bad.json"))
    (tmp_path / "never.json").write_text(json.dumps({"last_success_at": None}))
    assert not pubmod.status_healthy(str(tmp_path / "never.json"))


def test_unwritable_status_file_does_not_break_publishing(env):
    blocker = env.tmp / "blocker"
    blocker.write_text("file, not dir")
    p = make_pub(env, status_file=str(blocker / "status.json"))
    assert p.tick() is True
    p.close()


# --- loops / CLI -----------------------------------------------------------------------------------

def test_run_forever_never_raises_and_publishes(env):
    stop = threading.Event()
    p = Publisher(env.store, env.db, tick_seconds=0.01, heartbeat_seconds=0.01)
    t = threading.Thread(target=p.run_forever, args=(stop,))
    t.start()
    deadline = datetime.now().timestamp() + 5
    while snap.read_manifest(env.store) is None or snap.read_manifest(env.store).seq < 3:
        assert datetime.now().timestamp() < deadline, "publisher did not make progress"
        threading.Event().wait(0.02)
    stop.set()
    t.join(5)
    assert not t.is_alive()


def test_run_forever_survives_a_store_that_always_raises(env):
    for op in ("get_bytes", "list_keys", "put_file", "put_bytes"):
        env.store.fail[op] = RuntimeError("everything is broken")
    stop = threading.Event()
    result = {}

    def go():
        result["pub"] = pubmod.run_forever(env.store, env.db, stop_event=stop, tick_seconds=0.01)

    t = threading.Thread(target=go)
    t.start()
    threading.Event().wait(0.3)
    stop.set()
    t.join(5)
    assert not t.is_alive()
    assert result["pub"].consecutive_failures >= 1
    assert result["pub"].last_success_at is None


def test_cli_once_publishes_to_local_dir(env, monkeypatch, tmp_path):
    bucket = tmp_path / "cli-bucket"
    monkeypatch.setenv("REPLICATION_LOCAL_DIR", str(bucket))
    monkeypatch.delenv("REPLICATION_BUCKET", raising=False)
    assert pubmod.main(["--once", "--db", env.db]) == 0
    assert snap.read_manifest(LocalDirStore(bucket)).seq == 1


def test_cli_disabled_and_failure_exit_codes(env, monkeypatch, tmp_path):
    monkeypatch.delenv("REPLICATION_LOCAL_DIR", raising=False)
    monkeypatch.delenv("REPLICATION_BUCKET", raising=False)
    assert pubmod.main(["--once", "--db", env.db]) == 2
    monkeypatch.setenv("REPLICATION_LOCAL_DIR", str(tmp_path / "b2"))
    assert pubmod.main(["--once", "--db", str(tmp_path / "missing.sqlite")]) == 1


def test_cli_healthcheck(env, monkeypatch, tmp_path):
    status = tmp_path / "s.json"
    monkeypatch.setenv("REPLICATION_STATUS_FILE", str(status))
    assert pubmod.main(["--healthcheck"]) == 1
    status.write_text(json.dumps({"last_success_at": snap.iso(snap.utcnow())}))
    assert pubmod.main(["--healthcheck"]) == 0
    status.write_text(json.dumps({"last_success_at": "2020-01-01T00:00:00+00:00"}))
    assert pubmod.main(["--healthcheck"]) == 1

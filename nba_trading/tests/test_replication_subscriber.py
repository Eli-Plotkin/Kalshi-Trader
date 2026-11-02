"""Subscriber: manifest-first sync, verification, atomic swap, previous-good retention, helpers."""

import hashlib
import json
import os
import sqlite3
import threading
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from nba_trading.ledger import Ledger
from nba_trading.replication import snapshot as snap
from nba_trading.replication import subscriber as sub
from nba_trading.replication.publisher import Publisher, publish_once
from nba_trading.tests.test_replication_publisher import Clock, SpyStore, T0


@pytest.fixture
def env(tmp_path):
    led = Ledger(str(tmp_path / "backend" / "ledger.sqlite"))
    led.record_event("INFO", "heartbeat", "ok")
    store = SpyStore(tmp_path / "bucket")
    clock = Clock()
    dest = tmp_path / "frontend"
    yield type("E", (), {"led": led, "store": store, "clock": clock, "dest": str(dest),
                         "db": str(tmp_path / "backend" / "ledger.sqlite"), "tmp": tmp_path})
    led.close()


def events_in_synced(env):
    conn = sqlite3.connect(f"file:{sub.ledger_path(env.dest)}?mode=ro", uri=True)
    try:
        return conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    finally:
        conn.close()


def publish(env, **kw):
    return publish_once(env.store, env.db, now=env.clock, **kw)


def leftovers(env):
    return sorted(n for n in os.listdir(env.dest) if n not in (sub.LEDGER_NAME, sub.STATE_NAME))


# --- happy path --------------------------------------------------------------------------------

def test_first_sync_installs_ledger_and_state(env):
    m = publish(env)
    r = sub.sync_once(env.store, env.dest, now=lambda: T0 + timedelta(seconds=7))
    assert r.error is None and r.changed is True and r.seq == m.seq
    assert r.generated_at == T0 and r.bot_heartbeat_at is not None
    assert r.checked_at == T0 + timedelta(seconds=7)
    assert events_in_synced(env) == 1
    st = sub.read_state(env.dest)
    assert st.seq == m.seq and st.error is None and st.consecutive_errors == 0
    assert st.last_ok_at == T0 + timedelta(seconds=7) and st.generated_at == T0
    raw = json.load(open(sub.state_path(env.dest)))
    assert set(raw) >= {"checked_at", "last_ok_at", "seq", "generated_at", "bot_heartbeat_at",
                        "error", "consecutive_errors"}
    assert leftovers(env) == []


def test_unchanged_seq_does_not_download_the_snapshot(env):
    publish(env)
    sub.sync_once(env.store, env.dest, now=env.clock)
    assert env.store.count("get_file") == 1
    for i in range(3):
        env.clock.advance(30)
        r = sub.sync_once(env.store, env.dest, now=env.clock)
        assert r.changed is False and r.error is None
    assert env.store.count("get_file") == 1                    # the performance guard
    manifest_reads = sum(1 for op, k in env.store.ops if op == "get_bytes" and k == "latest.json")
    assert manifest_reads == 1 + 1 + 3                         # publish seq lookup + 4 syncs: tiny manifest only
    assert sub.read_state(env.dest).checked_at == env.clock.t


def test_publisher_heartbeat_updates_age_without_redownload(env):
    p = Publisher(env.store, env.db, now=env.clock)
    p.tick()
    sub.sync_once(env.store, env.dest, now=env.clock)
    first_state = sub.read_state(env.dest)
    env.clock.advance(30)
    assert p.tick() is True                                    # heartbeat: new seq, same snapshot sha
    env.clock.advance(1)
    r = sub.sync_once(env.store, env.dest, now=env.clock)
    st = sub.read_state(env.dest)
    assert r.changed is False and r.error is None
    assert st.seq == first_state.seq + 1
    assert st.generated_at == T0 + timedelta(seconds=30) > first_state.generated_at
    assert sub.data_age_seconds(st, env.clock) == 0            # age counts from when THIS host saw the new seq
    env.clock.advance(7)
    assert sub.data_age_seconds(st, env.clock) == 7
    assert env.store.count("get_file") == 1                    # still no second download
    p.close()


def test_new_data_is_downloaded_and_swapped(env):
    publish(env)
    sub.sync_once(env.store, env.dest, now=env.clock)
    env.led.record_event("INFO", "x", "y")
    env.clock.advance(5)
    m2 = publish(env)
    r = sub.sync_once(env.store, env.dest, now=env.clock)
    assert r.changed is True and r.seq == m2.seq
    assert events_in_synced(env) == 2
    assert env.store.count("get_file") == 2


def test_open_reader_keeps_working_across_a_swap(env):
    publish(env)
    sub.sync_once(env.store, env.dest, now=env.clock)
    reader = sqlite3.connect(f"file:{sub.ledger_path(env.dest)}?mode=ro", uri=True)
    assert reader.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1
    env.led.record_event("INFO", "x", "y")
    publish(env)
    sub.sync_once(env.store, env.dest, now=env.clock)
    assert reader.execute("SELECT COUNT(*) FROM events").fetchone()[0] in (1, 2)   # old inode, still valid
    reader.close()
    assert events_in_synced(env) == 2


def test_missing_local_file_is_redownloaded_even_if_seq_matches(env):
    publish(env)
    sub.sync_once(env.store, env.dest, now=env.clock)
    os.unlink(sub.ledger_path(env.dest))
    r = sub.sync_once(env.store, env.dest, now=env.clock)
    assert r.changed is True and events_in_synced(env) == 1


def test_same_seq_but_different_sha_is_treated_as_new(env):
    m = publish(env)
    sub.sync_once(env.store, env.dest, now=env.clock)
    env.led.record_event("INFO", "x", "y")
    m2 = publish(env, seq=m.seq)                               # pathological: seq reused with new content
    r = sub.sync_once(env.store, env.dest, now=env.clock)
    assert m2.sha256 != m.sha256 and r.changed is True and events_in_synced(env) == 2


# --- failure paths: previous good file is always retained ------------------------------------------

def synced_once(env):
    publish(env)
    sub.sync_once(env.store, env.dest, now=env.clock)
    return open(sub.ledger_path(env.dest), "rb").read()


def test_no_manifest_yet_reports_error_and_no_data(env):
    r = sub.sync_once(env.store, env.dest, now=env.clock)
    assert r.error and "manifest" in r.error and r.seq is None and r.changed is False
    assert not os.path.exists(sub.ledger_path(env.dest))
    st = sub.read_state(env.dest)
    assert st.error and st.consecutive_errors == 1 and st.last_ok_at is None


def test_store_errors_keep_previous_file_and_count(env):
    good = synced_once(env)
    env.store.fail["get_bytes"] = ConnectionError("network down")
    for n in (1, 2, 3):
        env.clock.advance(30)
        r = sub.sync_once(env.store, env.dest, now=env.clock)
        assert "network down" in r.error and r.changed is False
        assert r.seq == 1                                      # still serving the previous data
        assert sub.read_state(env.dest).consecutive_errors == n
    assert open(sub.ledger_path(env.dest), "rb").read() == good
    st = sub.read_state(env.dest)
    assert st.generated_at == T0                               # age keeps growing -> banner goes red
    assert sub.data_age_seconds(st, env.clock) == 90
    env.store.fail.clear()
    r = sub.sync_once(env.store, env.dest, now=env.clock)
    st = sub.read_state(env.dest)
    assert r.error is None and st.consecutive_errors == 0 and st.error is None


def test_download_error_keeps_previous_file_and_leaves_no_partial(env):
    good = synced_once(env)
    env.led.record_event("INFO", "x", "y")
    publish(env)
    env.store.fail["get_file"] = ConnectionError("reset by peer")
    r = sub.sync_once(env.store, env.dest, now=env.clock)
    assert "reset by peer" in r.error
    assert open(sub.ledger_path(env.dest), "rb").read() == good
    assert leftovers(env) == []


def test_truncated_snapshot_fails_checksum_and_previous_is_kept(env):
    good = synced_once(env)
    env.led.record_event("INFO", "x", "y")
    m = publish(env)
    path = os.path.join(env.store.root, *m.snapshot_key.split("/"))
    data = open(path, "rb").read()
    open(path, "wb").write(data[: len(data) // 2])
    r = sub.sync_once(env.store, env.dest, now=env.clock)
    assert r.error and "size mismatch" in r.error and r.changed is False
    assert open(sub.ledger_path(env.dest), "rb").read() == good
    assert leftovers(env) == []
    assert sub.read_state(env.dest).seq == 1                   # state still describes the good file


def test_same_size_bitflip_fails_sha256(env):
    good = synced_once(env)
    env.led.record_event("INFO", "x", "y")
    m = publish(env)
    path = os.path.join(env.store.root, *m.snapshot_key.split("/"))
    data = bytearray(open(path, "rb").read())
    data[-1] ^= 0xFF
    open(path, "wb").write(bytes(data))
    r = sub.sync_once(env.store, env.dest, now=env.clock)
    assert r.error and "sha256" in r.error
    assert open(sub.ledger_path(env.dest), "rb").read() == good


def test_valid_checksum_but_corrupt_database_fails_integrity(env):
    good = synced_once(env)
    junk = b"definitely not sqlite" * 500
    key = snap.snapshot_key_for(2, hashlib.sha256(junk).hexdigest())
    env.store.put_bytes(key, junk)
    snap.write_manifest(env.store, snap.Manifest(
        version=1, seq=2, generated_at=snap.iso(env.clock.t), snapshot_key=key,
        sha256=hashlib.sha256(junk).hexdigest(), size=len(junk), user_version=1))
    r = sub.sync_once(env.store, env.dest, now=env.clock)
    assert r.error and "verification" in r.error
    assert open(sub.ledger_path(env.dest), "rb").read() == good
    assert leftovers(env) == []


def test_manifest_pointing_at_missing_snapshot(env):
    good = synced_once(env)
    env.led.record_event("INFO", "x", "y")
    m = publish(env)
    env.store.delete(m.snapshot_key)
    r = sub.sync_once(env.store, env.dest, now=env.clock)
    assert r.error and "missing snapshot" in r.error
    assert open(sub.ledger_path(env.dest), "rb").read() == good
    assert leftovers(env) == []


def test_manifest_from_newer_user_version_is_refused_without_download(env):
    good = synced_once(env)
    m = snap.read_manifest(env.store)
    from dataclasses import replace
    snap.write_manifest(env.store, replace(m, seq=2, user_version=sub.SUPPORTED_USER_VERSION + 1))
    r = sub.sync_once(env.store, env.dest, now=env.clock)
    assert r.error and "user_version" in r.error and "upgrade" in r.error
    assert env.store.count("get_file") == 1
    assert open(sub.ledger_path(env.dest), "rb").read() == good


def test_snapshot_file_with_newer_user_version_is_refused_even_if_manifest_lies(env):
    good = synced_once(env)
    env.led.record_event("INFO", "x", "y")
    env.led.conn.execute(f"PRAGMA user_version = {sub.SUPPORTED_USER_VERSION + 5}")
    m = publish(env)
    from dataclasses import replace
    snap.write_manifest(env.store, replace(m, user_version=1))        # manifest claims it is fine
    r = sub.sync_once(env.store, env.dest, now=env.clock)
    assert r.error and "newer than this code" in r.error
    assert open(sub.ledger_path(env.dest), "rb").read() == good


def test_invalid_and_future_version_manifests(env):
    good = synced_once(env)
    env.store.put_bytes("latest.json", b"{truncated")
    r = sub.sync_once(env.store, env.dest, now=env.clock)
    assert r.error and "invalid manifest" in r.error
    m = json.loads(snap.Manifest.from_json(
        json.dumps({**json.loads(_manifest_json(env, good)), "version": 2})).to_json())
    env.store.put_bytes("latest.json", json.dumps(m).encode())
    r = sub.sync_once(env.store, env.dest, now=env.clock)
    assert r.error and "version" in r.error
    assert open(sub.ledger_path(env.dest), "rb").read() == good


def _manifest_json(env, good):
    return json.dumps({"version": 1, "seq": 9, "generated_at": snap.iso(env.clock.t),
                       "snapshot_key": "snapshots/000000001-aaaaaaaa.sqlite",
                       "sha256": hashlib.sha256(good).hexdigest(), "size": len(good),
                       "user_version": 1, "bot_heartbeat_at": None, "counts": {}})


def test_seq_going_backwards_is_refused(env):
    for _ in range(3):
        publish(env)
    sub.sync_once(env.store, env.dest, now=env.clock)
    assert sub.read_state(env.dest).seq == 3
    env.led.record_event("INFO", "x", "y")
    publish(env, seq=1)                                        # e.g. bucket restored from an old backup
    r = sub.sync_once(env.store, env.dest, now=env.clock)
    assert r.error and "backwards" in r.error
    assert events_in_synced(env) == 1 and sub.read_state(env.dest).seq == 3


def test_recovers_after_failures_and_swaps_in_new_data(env):
    synced_once(env)
    env.store.fail["get_file"] = ConnectionError("x")
    env.led.record_event("INFO", "x", "y")
    publish(env)
    assert sub.sync_once(env.store, env.dest, now=env.clock).error
    env.store.fail.clear()
    r = sub.sync_once(env.store, env.dest, now=env.clock)
    assert r.error is None and r.changed is True and events_in_synced(env) == 2


def test_unusable_dest_dir_reports_error_instead_of_raising(env, tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    r = sub.sync_once(env.store, str(blocker / "sub"), now=env.clock)
    assert r.error


def test_corrupt_state_file_is_treated_as_no_state(env):
    publish(env)
    os.makedirs(env.dest)
    open(sub.state_path(env.dest), "w").write("{nope")
    assert sub.read_state(env.dest) is None
    r = sub.sync_once(env.store, env.dest, now=env.clock)
    assert r.error is None and r.changed is True


# --- helpers ---------------------------------------------------------------------------------------

def test_read_state_none_when_nothing_synced(tmp_path):
    assert sub.read_state(str(tmp_path / "nothing")) is None
    assert sub.data_age_seconds(None) is None and sub.heartbeat_age_seconds(None) is None


def test_ages_and_thresholds(env):
    publish(env)
    sub.sync_once(env.store, env.dest, now=env.clock)
    st = sub.read_state(env.dest)
    assert sub.data_age_seconds(st, lambda: T0 + timedelta(seconds=45)) == 45
    assert sub.data_age_seconds(st, T0 + timedelta(seconds=45)) == 45          # datetime also accepted
    assert sub.data_age_seconds(st, lambda: T0 - timedelta(seconds=5)) == 0    # frontend clock stepped back
    st = replace(st, bot_heartbeat_at=T0 - timedelta(seconds=20))               # backend-clock stamps, 20 s apart
    assert sub.heartbeat_age_seconds(st, lambda: T0 + timedelta(seconds=40)) == 60   # data age 40 + gap 20
    assert (sub.FRESH_SECONDS, sub.STALE_SECONDS, sub.BOT_SILENT_SECONDS) == (60, 180, 45 * 60)
    assert [sub.freshness(a) for a in (0, 60, 61, 180, 181, None)] == [
        "fresh", "fresh", "stale", "stale", "broken", "broken"]


def test_bot_silent_detection(env):
    publish(env)
    sub.sync_once(env.store, env.dest, now=env.clock)
    st = replace(sub.read_state(env.dest), bot_heartbeat_at=T0, bot_alive_at=None)   # no bot_alive_at: 45-min rule
    assert not sub.bot_silent(st, lambda: T0 + timedelta(minutes=44))
    assert sub.bot_silent(st, lambda: T0 + timedelta(minutes=46))


def test_state_without_heartbeat_has_none_age(tmp_path):
    led = Ledger(str(tmp_path / "l.sqlite"))
    store = SpyStore(tmp_path / "b")
    publish_once(store, str(tmp_path / "l.sqlite"), now=Clock())
    sub.sync_once(store, str(tmp_path / "d"), now=Clock())
    st = sub.read_state(str(tmp_path / "d"))
    assert st.bot_heartbeat_at is None and sub.heartbeat_age_seconds(st) is None
    led.close()


# --- loops -----------------------------------------------------------------------------------------

def test_run_forever_syncs_and_stops(env):
    publish(env)
    stop = threading.Event()
    t = threading.Thread(target=sub.run_forever, args=(env.store, env.dest, 0.01, stop))
    t.start()
    for _ in range(200):
        if sub.read_state(env.dest) is not None and os.path.exists(sub.ledger_path(env.dest)):
            break
        threading.Event().wait(0.02)
    stop.set()
    t.join(5)
    assert not t.is_alive() and events_in_synced(env) == 1


def test_run_forever_never_raises_on_broken_store(env):
    env.store.fail["get_bytes"] = RuntimeError("boom")
    stop = threading.Event()
    t = threading.Thread(target=sub.run_forever, args=(env.store, env.dest, 0.01, stop))
    t.start()
    threading.Event().wait(0.2)
    stop.set()
    t.join(5)
    assert not t.is_alive()
    assert sub.read_state(env.dest).consecutive_errors >= 1


def test_start_background_is_idempotent_per_dest(env):
    publish(env)
    t1 = sub.start_background(env.store, env.dest, interval=0.01)
    t2 = sub.start_background(env.store, env.dest, interval=0.01)
    other = sub.start_background(env.store, env.dest + "-other", interval=0.01)
    try:
        assert t1 is t2 and t1.daemon and t1.is_alive()
        assert other is not t1
        for _ in range(200):
            if os.path.exists(sub.ledger_path(env.dest)):
                break
            threading.Event().wait(0.02)
        assert events_in_synced(env) == 1
    finally:
        t1.stop_event.set()
        other.stop_event.set()
        t1.join(5)
        other.join(5)
    t3 = sub.start_background(env.store, env.dest, interval=0.01)       # old thread dead: a new one starts
    assert t3 is not t1
    t3.stop_event.set()
    t3.join(5)


def test_no_credentialed_modules_are_imported():
    import subprocess
    import sys
    code = ("import sys; import nba_trading.replication.subscriber as s; "
            "bad=[m for m in sys.modules if m.startswith('kalshi') or m in ('boto3','requests')]; "
            "print(bad)")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         cwd=os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
    assert out.stdout.strip() == "[]", out.stdout + out.stderr

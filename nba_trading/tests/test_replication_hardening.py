"""Design section 11 (replication side): untrusted snapshots, bounded reads, prefix, skew-proof freshness,
bot liveness in the manifest, and the seq floor. No network: LocalDirStore / fake S3 client / tmp dirs."""

import io
import json
import os
import sqlite3
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from nba_trading.ledger import KV_PEAK, Ledger
from nba_trading.replication import restore
from nba_trading.replication import snapshot as snap
from nba_trading.replication import subscriber as sub
from nba_trading.replication.publisher import Publisher, publish_once
from nba_trading.replication.store import (
    DEFAULT_PREFIX, MAX_CONTROL_BYTES, MAX_MANIFEST_BYTES, LocalDirStore, ObjectTooLarge, S3Store,
    store_from_env)
from nba_trading.tests.test_replication_publisher import Clock, SpyStore, T0
from nba_trading.tests.test_replication_store import FakeS3Client


def iso(dt):
    return snap.iso(dt)


# --- F3: snapshots are untrusted input --------------------------------------------------------------

def _good_db(path):
    led = Ledger(str(path))
    led.record_event("INFO", "heartbeat", "ok")
    led.close()
    return str(path)


def _exec(path, script):
    c = sqlite3.connect(str(path))
    c.executescript(script)
    c.commit()
    c.close()


@pytest.mark.parametrize("script,needle", [
    ("CREATE TRIGGER t AFTER INSERT ON snapshots BEGIN UPDATE kv SET value='1' WHERE key='x'; END;", "trigger"),
    ("CREATE VIEW v AS SELECT 1 AS x;", "view"),
    ("CREATE TABLE extra(a);", "unexpected table"),
    ("CREATE INDEX idx_extra ON events(ts);", "unexpected index"),
    ("CREATE INDEX idx_orders_order_id_x ON orders(ts);", "unexpected index"),
    ("CREATE INDEX idx_orders_order_id_bad ON kv(key);", "unexpected index"),
    ("ALTER TABLE events ADD COLUMN sneaky TEXT;", "unexpected column"),
    ("ALTER TABLE events ADD COLUMN g INTEGER GENERATED ALWAYS AS (1) VIRTUAL;", "unexpected column"),
])
def test_inspect_rejects_hostile_schema(tmp_path, script, needle):
    db = _good_db(tmp_path / "l.sqlite")
    _exec(db, script)
    with pytest.raises(snap.SnapshotRejected, match=needle):
        snap.inspect_db(db)


def test_inspect_rejects_recursive_cte_view_without_running_it(tmp_path):
    db = str(tmp_path / "evil.sqlite")
    _exec(db, """PRAGMA user_version=1;
        CREATE VIEW decisions AS WITH RECURSIVE r(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM r WHERE x<3000000000)
            SELECT x AS id FROM r;
        CREATE TABLE events(ts,level,kind,message);""")
    t = time.time()
    with pytest.raises(snap.SnapshotRejected, match="view"):
        snap.inspect_db(db)
    assert time.time() - t < 5            # rejected from sqlite_master; the view was never evaluated


def test_snapshot_rejected_is_a_value_error_and_a_snapshot_error():
    assert issubclass(snap.SnapshotRejected, ValueError) and issubclass(snap.SnapshotRejected, snap.SnapshotError)


def test_current_schema_snapshot_is_accepted(tmp_path):
    uv, counts, hb = snap.inspect_db(_good_db(tmp_path / "ok.sqlite"))
    assert uv >= 1 and counts["events"] == 1


def test_older_schema_subset_is_accepted(tmp_path):
    db = str(tmp_path / "old.sqlite")
    _exec(db, """PRAGMA user_version=0;
        CREATE TABLE decisions (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, game_id TEXT);
        CREATE TABLE events (ts TEXT NOT NULL, level TEXT NOT NULL, kind TEXT NOT NULL, message TEXT NOT NULL);
        INSERT INTO events VALUES ('2026-01-01T00:00:00+00:00','INFO','heartbeat','x');""")
    uv, counts, hb = snap.inspect_db(db)
    assert uv == 0 and counts["events"] == 1 and counts["orders"] == 0 and hb


def test_oversized_database_is_refused(tmp_path):
    db = _good_db(tmp_path / "big.sqlite")
    with pytest.raises(snap.SnapshotRejected, match="limit"):
        snap.inspect_snapshot(db, max_db_bytes=1000)
    assert snap.MAX_DB_BYTES == 512 * 1024 * 1024


def test_page_count_times_page_size_is_checked_not_just_file_size(tmp_path):
    db = _good_db(tmp_path / "l.sqlite")
    size = os.path.getsize(db)
    with pytest.raises(snap.SnapshotRejected):
        snap.inspect_snapshot(db, max_db_bytes=size - 1)
    snap.inspect_snapshot(db, max_db_bytes=size)           # exactly at the cap is fine


def test_read_connections_set_trusted_schema_off_and_no_extensions(tmp_path):
    db = _good_db(tmp_path / "l.sqlite")
    conn = snap.open_readonly(db)
    try:
        assert conn.execute("PRAGMA trusted_schema").fetchone()[0] == 0
        with pytest.raises((sqlite3.OperationalError, AttributeError)):
            conn.execute("SELECT load_extension('nonexistent')")
    finally:
        conn.close()


def test_publisher_read_connection_is_hardened(tmp_path):
    db = _good_db(tmp_path / "l.sqlite")
    p = Publisher(SpyStore(tmp_path / "b"), db, now=Clock())
    p.tick()
    assert p._conn.execute("PRAGMA trusted_schema").fetchone()[0] == 0
    p.close()


def _publish_evil(tmp_path, script):
    """Bypass the publisher's own check: put a hostile snapshot + a matching manifest straight in a bucket."""
    evil = _good_db(tmp_path / "evil.sqlite")
    _exec(evil, script)
    bucket = LocalDirStore(tmp_path / "bucket")
    sha = snap.sha256_file(evil)
    key = snap.snapshot_key_for(1, sha)
    bucket.put_file(key, evil)
    m = snap.Manifest(version=1, seq=1, generated_at=iso(T0), snapshot_key=key, sha256=sha,
                      size=os.path.getsize(evil), user_version=1)
    snap.write_manifest(bucket, m)
    return bucket


def test_subscriber_refuses_a_trigger_snapshot_and_keeps_previous_good(tmp_path):
    good = _good_db(tmp_path / "good.sqlite")
    b0 = SpyStore(tmp_path / "b0")
    publish_once(b0, good, now=Clock())
    dest = str(tmp_path / "dest")
    assert sub.sync_once(b0, dest, now=Clock()).error is None
    before = open(sub.ledger_path(dest), "rb").read()

    bucket = _publish_evil(tmp_path, "CREATE TRIGGER t AFTER INSERT ON events BEGIN DELETE FROM events; END;")
    # carry the good state over so the evil manifest is a newer seq for the same dest
    bucket.put_bytes("latest.json", json.dumps(dict(json.loads(bucket.get_bytes("latest.json")), seq=5)).encode())
    r = sub.sync_once(bucket, dest, now=Clock())
    assert r.error and "trigger" in r.error and not r.changed
    assert open(sub.ledger_path(dest), "rb").read() == before


def test_restore_refuses_a_trigger_snapshot_even_with_force(tmp_path):
    bucket = _publish_evil(tmp_path, "CREATE TRIGGER t AFTER INSERT ON snapshots BEGIN "
                                     "UPDATE kv SET value='1' WHERE key='high_water_peak_cents'; END;")
    target = tmp_path / "srv" / "nba_ledger.sqlite"
    target.parent.mkdir()
    Ledger(str(target)).close()
    before = target.read_bytes()
    for force in (False, True):
        with pytest.raises(restore.RestoreError, match="trigger|already exists"):
            restore.restore(bucket, str(target), force=force)
    with pytest.raises(restore.RestoreError, match="trigger"):
        restore.restore(bucket, str(target), force=True)
    assert target.read_bytes() == before
    fresh = tmp_path / "new" / "l.sqlite"
    with pytest.raises(restore.RestoreError, match="trigger"):
        restore.restore(bucket, str(fresh))
    assert not fresh.exists()


def test_publisher_refuses_to_publish_a_ledger_with_unexpected_objects(tmp_path):
    db = _good_db(tmp_path / "l.sqlite")
    _exec(db, "CREATE TABLE sneaky(a);")
    store = SpyStore(tmp_path / "b")
    with pytest.raises(snap.SnapshotRejected):
        publish_once(store, db, now=Clock())
    assert store.list_keys("") == []


# --- F4: bounded reads ---------------------------------------------------------------------------------

def test_constants():
    assert MAX_CONTROL_BYTES == 4096 and MAX_MANIFEST_BYTES == 65536


def test_local_get_bytes_cap(tmp_path):
    s = LocalDirStore(tmp_path)
    s.put_bytes("a", b"x" * 100)
    assert s.get_bytes("a", 100) == b"x" * 100
    assert s.get_bytes("a") == b"x" * 100
    with pytest.raises(ObjectTooLarge):
        s.get_bytes("a", 99)
    assert s.get_bytes("missing", 10) is None
    assert issubclass(ObjectTooLarge, ValueError)


class _HugeBody:
    """A body that would blow memory if fully read; records the biggest single read asked of it."""

    def __init__(self, total=10 ** 12):
        self.left, self.max_read, self.closed = total, 0, False

    def read(self, n=-1):
        if n is None or n < 0:
            raise MemoryError("unbounded read of a huge object")
        self.max_read = max(self.max_read, n)
        n = min(n, self.left)
        self.left -= n
        return b"A" * n

    def close(self):
        self.closed = True


class _HugeClient(FakeS3Client):
    def __init__(self, content_length, body):
        super().__init__()
        self.cl, self.body = content_length, body

    def get_object(self, Bucket, Key):
        return {"Body": self.body, **({"ContentLength": self.cl} if self.cl is not None else {})}


def test_s3_get_bytes_checks_content_length_before_reading():
    body = _HugeBody()
    s = S3Store("b", client=_HugeClient(10 ** 12, body))
    with pytest.raises(ObjectTooLarge):
        s.get_bytes("latest.json", MAX_MANIFEST_BYTES)
    assert body.max_read == 0 and body.closed


def test_s3_get_bytes_reads_at_most_cap_plus_one_when_length_is_missing():
    body = _HugeBody()
    s = S3Store("b", client=_HugeClient(None, body))
    with pytest.raises(ObjectTooLarge):
        s.get_bytes("latest.json", MAX_MANIFEST_BYTES)
    assert body.max_read == MAX_MANIFEST_BYTES + 1 and body.closed


def test_s3_get_bytes_under_cap_and_missing_key():
    c = FakeS3Client()
    s = S3Store("b", prefix="p/", client=c)
    s.put_bytes("k", b"hello")
    assert s.get_bytes("k", 5) == b"hello"
    with pytest.raises(ObjectTooLarge):
        s.get_bytes("k", 4)
    assert s.get_bytes("nope", 5) is None


def test_oversized_manifest_is_an_error_and_keeps_previous_good_file(tmp_path):
    good = _good_db(tmp_path / "g.sqlite")
    store = SpyStore(tmp_path / "b")
    publish_once(store, good, now=Clock())
    dest = str(tmp_path / "d")
    sub.sync_once(store, dest, now=Clock())
    ledger_before = open(sub.ledger_path(dest), "rb").read()
    store.put_bytes("latest.json", b'{"pad": "' + b"A" * (MAX_MANIFEST_BYTES + 10) + b'"}')
    r = sub.sync_once(store, dest, now=Clock())
    assert r.error and "oversized" in r.error
    assert open(sub.ledger_path(dest), "rb").read() == ledger_before
    st = sub.read_state(dest)
    assert st.error and st.seq == 1 and st.consecutive_errors == 1
    with pytest.raises(ObjectTooLarge):
        snap.read_manifest(store)


# --- F6: prefix ------------------------------------------------------------------------------------------

@pytest.mark.parametrize("extra", [{}, {"REPLICATION_PREFIX": ""}, {"REPLICATION_PREFIX": "   "},
                                   {"REPLICATION_PREFIX": "/"}, {"REPLICATION_PREFIX": "kalshi-bot/"}])
def test_blank_prefix_resolves_to_the_default_everywhere(extra):
    env = dict({"REPLICATION_BUCKET": "b"}, **extra)
    # bot / replicator / frontend each call store_from_env with the same variables
    keys = set()
    for _ in range(3):
        s = _from_env_with_fake_client(env)
        keys.add(s._k("control/pause.json"))
    assert keys == {DEFAULT_PREFIX + "control/pause.json"}


def _from_env_with_fake_client(env):
    import nba_trading.replication.store as st
    orig = st.S3Store.__init__

    def init(self, bucket, prefix="", **kw):
        kw["client"] = FakeS3Client()
        orig(self, bucket, prefix, **kw)

    st.S3Store.__init__ = init
    try:
        return st.store_from_env(env=env)
    finally:
        st.S3Store.__init__ = orig


def test_custom_prefix_is_kept_and_disabled_stays_none():
    assert _from_env_with_fake_client({"REPLICATION_BUCKET": "b", "REPLICATION_PREFIX": "mine"}).prefix == "mine/"
    assert store_from_env(env={}) is None
    assert store_from_env(env={"REPLICATION_PREFIX": ""}) is None


# --- F5: skew-proof freshness ----------------------------------------------------------------------------

def _publisher_world(tmp_path, backend_offset):
    """Backend clock = true clock + offset. Returns (pub, ledger, store, dest, true_clock, backend_clock)."""
    led = Ledger(str(tmp_path / "l.sqlite"))
    led.record_event("INFO", "heartbeat", "x")
    store = SpyStore(tmp_path / "b")
    true = Clock()
    backend = lambda: true.t + timedelta(seconds=backend_offset)       # noqa: E731
    pub = Publisher(store, str(tmp_path / "l.sqlite"), now=backend)
    return pub, led, store, str(tmp_path / "d"), true


def test_backend_clock_ahead_5min_with_dead_replication_never_looks_fresh(tmp_path):
    pub, led, store, dest, true = _publisher_world(tmp_path, +300)
    assert pub.tick()
    assert sub.sync_once(store, dest, now=true).error is None
    ages = []
    for _ in range(8):                              # publisher is dead from here on
        true.advance(30)
        sub.sync_once(store, dest, now=true)
        st = sub.read_state(dest)
        ages.append(sub.data_age_seconds(st, true))
    assert ages == sorted(ages) and ages[-1] == 240                      # grows with real time
    assert sub.freshness(ages[2]) == "stale" and sub.freshness(ages[-1]) == "broken"
    assert sub.clock_skew_estimate(sub.read_state(dest)) < sub.SKEW_BEHIND_SECONDS      # flagged
    assert sub.clock_skew_suspect(sub.read_state(dest))
    pub.close(); led.close()


def test_backend_clock_behind_2min_healthy_has_small_age_and_flags_skew(tmp_path):
    pub, led, store, dest, true = _publisher_world(tmp_path, -120)
    for _ in range(4):
        pub.tick()
        true.advance(10)
        sub.sync_once(store, dest, now=true)
    st = sub.read_state(dest)
    assert sub.data_age_seconds(st, true) == 0 and sub.freshness(sub.data_age_seconds(st, true)) == "fresh"
    assert sub.clock_skew_estimate(st) > sub.SKEW_AHEAD_SECONDS and sub.clock_skew_suspect(st)
    pub.close(); led.close()


def test_healthy_replication_has_a_small_skew_estimate(tmp_path):
    pub, led, store, dest, true = _publisher_world(tmp_path, 0)
    pub.tick()
    true.advance(8)
    sub.sync_once(store, dest, now=true)
    st = sub.read_state(dest)
    assert sub.clock_skew_estimate(st) == 8 and not sub.clock_skew_suspect(st)
    pub.close(); led.close()


def test_forged_huge_seq_then_real_publisher_ages_out_with_an_error(tmp_path):
    pub, led, store, dest, true = _publisher_world(tmp_path, 0)
    pub.tick()
    sub.sync_once(store, dest, now=true)
    fake = _good_db(tmp_path / "fake.sqlite")
    publish_once(store, fake, seq=10 ** 12, now=lambda: T0 + timedelta(days=30))     # attacker, future-dated
    assert sub.sync_once(store, dest, now=true).changed
    errors, ages = [], []
    for i in range(8):                                                             # real publisher keeps going
        true.advance(30)
        led.record_event("CRITICAL", "order_failed", f"real {i}")
        pub.last_publish_at = None
        pub.tick()
        r = sub.sync_once(store, dest, now=true)
        errors.append(r.error)
        ages.append(sub.data_age_seconds(sub.read_state(dest), true))
    assert all(e and "backwards" in e for e in errors)
    st = sub.read_state(dest)
    assert st.error and st.consecutive_errors == 8
    assert ages == sorted(ages) and ages[-1] == 240 and sub.freshness(ages[-1]) == "broken"
    assert sub.freshness(ages[0]) != "fresh" or ages[0] <= sub.FRESH_SECONDS     # honest at each step
    pub.close(); led.close()


def test_future_generated_at_in_a_legacy_state_is_never_fresh():
    st = sub.SyncState(checked_at=T0, last_ok_at=T0, seq=1, generated_at=T0 + timedelta(minutes=5),
                       bot_heartbeat_at=None, error=None, consecutive_errors=0)
    age = sub.data_age_seconds(st, T0)
    assert age == -300 and sub.freshness(age) == "broken"


def test_old_state_file_without_seq_advanced_at_is_tolerated(tmp_path):
    d = tmp_path / "d"
    d.mkdir()
    (d / sub.STATE_NAME).write_text(json.dumps({
        "checked_at": iso(T0), "last_ok_at": iso(T0), "seq": 4, "generated_at": iso(T0 - timedelta(seconds=20)),
        "bot_heartbeat_at": None, "error": None, "consecutive_errors": 0, "sha256": "a" * 64, "user_version": 1}))
    st = sub.read_state(str(d))
    assert st.seq_advanced_at is None and st.bot_alive_at is None
    assert sub.data_age_seconds(st, T0) == 20            # falls back to generated_at
    assert sub.clock_skew_estimate(st) is None


def test_seq_advanced_at_only_moves_when_seq_increases(tmp_path):
    pub, led, store, dest, true = _publisher_world(tmp_path, 0)
    pub.tick()
    sub.sync_once(store, dest, now=true)
    first = sub.read_state(dest).seq_advanced_at
    assert first == T0
    true.advance(20)
    sub.sync_once(store, dest, now=true)                  # same manifest again
    assert sub.read_state(dest).seq_advanced_at == first
    true.advance(1)
    pub.last_publish_at = None
    pub.tick()
    sub.sync_once(store, dest, now=true)
    assert sub.read_state(dest).seq_advanced_at == true.t
    store.fail["get_bytes"] = ConnectionError("down")
    true.advance(50)
    sub.sync_once(store, dest, now=true)                  # a failed pass must not refresh it
    assert sub.read_state(dest).seq_advanced_at == true.t - timedelta(seconds=50)
    pub.close(); led.close()


# --- 11.4: bot liveness -------------------------------------------------------------------------------------

def _alive(led, dt):
    led.set_kv(snap.KV_BOT_ALIVE, iso(dt))


def test_snapshot_path_copies_bot_alive_at_into_the_manifest(tmp_path):
    led = Ledger(str(tmp_path / "l.sqlite"))
    _alive(led, T0 - timedelta(seconds=15))
    m = publish_once(SpyStore(tmp_path / "b"), str(tmp_path / "l.sqlite"), now=Clock())
    assert m.bot_alive_at == iso(T0 - timedelta(seconds=15))
    assert snap.Manifest.from_json(m.to_json()).bot_alive_at == m.bot_alive_at
    led.close()


def test_missing_kv_row_or_table_gives_none(tmp_path):
    led = Ledger(str(tmp_path / "l.sqlite"))
    assert publish_once(SpyStore(tmp_path / "b"), str(tmp_path / "l.sqlite"), now=Clock()).bot_alive_at is None
    led.close()
    old = str(tmp_path / "old.sqlite")
    _exec(old, "CREATE TABLE events (ts TEXT NOT NULL, level TEXT NOT NULL, kind TEXT NOT NULL, message TEXT NOT NULL);")
    assert publish_once(SpyStore(tmp_path / "b2"), old, now=Clock()).bot_alive_at is None


def test_heartbeat_manifest_reflects_the_latest_alive_stamp(tmp_path):
    led = Ledger(str(tmp_path / "l.sqlite"))
    led.record_event("INFO", "heartbeat", "x")
    _alive(led, T0)
    store = SpyStore(tmp_path / "b")
    clock = Clock()
    pub = Publisher(store, str(tmp_path / "l.sqlite"), now=clock)
    pub.tick()
    assert pub.last_manifest.bot_alive_at == iso(T0)
    clock.advance(30)
    # the touch changes data_version, so the publisher would take a new snapshot; make the stamp the ONLY
    # thing that changed by checking the heartbeat path directly with an unchanged db_version:
    m = publish_once(store, str(tmp_path / "l.sqlite"), now=clock, previous=pub.last_manifest, reuse=True,
                     bot_alive_at=iso(T0 + timedelta(seconds=30)))
    assert m.snapshot_key == pub.last_manifest.snapshot_key and m.bot_alive_at == iso(T0 + timedelta(seconds=30))
    kept = publish_once(store, str(tmp_path / "l.sqlite"), now=clock, previous=pub.last_manifest, reuse=True)
    assert kept.bot_alive_at == iso(T0)                  # no fresher value supplied: carried over
    pub.close(); led.close()


def test_publisher_heartbeat_tick_reads_alive_through_the_persistent_connection(tmp_path, monkeypatch):
    led = Ledger(str(tmp_path / "l.sqlite"))
    led.record_event("INFO", "heartbeat", "x")
    _alive(led, T0)
    store = SpyStore(tmp_path / "b")
    clock = Clock()
    pub = Publisher(store, str(tmp_path / "l.sqlite"), now=clock)
    pub.tick()
    # freeze change detection so the next tick is a heartbeat-only publish
    monkeypatch.setattr(pub, "_read_dv", lambda: pub._published_dv)
    pub._conn.execute("PRAGMA query_only=ON")
    w = sqlite3.connect(str(tmp_path / "l.sqlite"))
    w.execute("UPDATE kv SET value=? WHERE key=?", (iso(T0 + timedelta(seconds=40)), snap.KV_BOT_ALIVE))
    w.commit(); w.close()
    clock.advance(40)
    n_files = store.count("put_file")
    assert pub.tick() is True
    assert store.count("put_file") == n_files                         # heartbeat only: no snapshot upload
    assert snap.read_manifest(store).bot_alive_at == iso(T0 + timedelta(seconds=40))
    pub.close(); led.close()


def test_manifest_from_json_tolerates_bad_or_absent_bot_alive_at():
    base = json.loads(snap.Manifest(version=1, seq=1, generated_at=iso(T0), snapshot_key="snapshots/000000001-ab.sqlite",
                                    sha256="a" * 64, size=1, user_version=1).to_json())
    base.pop("bot_alive_at")
    assert snap.Manifest.from_json(json.dumps(base)).bot_alive_at is None
    for junk in ("yesterday", 12, ["x"], ""):
        assert snap.Manifest.from_json(json.dumps(dict(base, bot_alive_at=junk))).bot_alive_at is None


def _state(**kw):
    base = dict(checked_at=T0, last_ok_at=T0, seq=1, generated_at=T0 - timedelta(seconds=2), bot_heartbeat_at=None,
                error=None, consecutive_errors=0, seq_advanced_at=T0)
    base.update(kw)
    return sub.SyncState(**base)


def test_bot_alive_age_is_skew_proof_and_bot_silent_uses_it():
    # backend stamps are 2 s and 40 s before generated_at; the frontend saw the seq 5 s ago
    st = _state(bot_alive_at=T0 - timedelta(seconds=42))
    now = T0 + timedelta(seconds=5)
    assert sub.bot_alive_age_seconds(st, now) == 5 + 40
    assert sub.BOT_ALIVE_SILENT_SECONDS == 180
    assert not sub.bot_silent(st, now)
    assert sub.bot_silent(st, T0 + timedelta(seconds=200))                   # alive stamp ~4 min old
    # a heartbeat event from yesterday does not matter while bot_alive_at is present and recent
    st2 = replace(st, bot_heartbeat_at=T0 - timedelta(days=1))
    assert not sub.bot_silent(st2, now)


def test_bot_silent_falls_back_to_the_45_minute_rule_without_bot_alive_at():
    st = _state(bot_alive_at=None, bot_heartbeat_at=T0 - timedelta(seconds=10))
    assert sub.bot_alive_age_seconds(st, T0) is None
    assert not sub.bot_silent(st, T0 + timedelta(minutes=44))
    assert sub.bot_silent(st, T0 + timedelta(minutes=46))
    assert sub.bot_silent(_state(bot_alive_at=None, bot_heartbeat_at=None), T0)


def test_sync_state_carries_bot_alive_at(tmp_path):
    led = Ledger(str(tmp_path / "l.sqlite"))
    _alive(led, T0 - timedelta(seconds=9))
    store = SpyStore(tmp_path / "b")
    publish_once(store, str(tmp_path / "l.sqlite"), now=Clock())
    sub.sync_once(store, str(tmp_path / "d"), now=Clock())
    st = sub.read_state(str(tmp_path / "d"))
    assert st.bot_alive_at == T0 - timedelta(seconds=9)
    assert sub.bot_alive_age_seconds(st, T0) == 9
    led.close()


# --- 11.6: seq floor ------------------------------------------------------------------------------------------

def test_restart_over_a_corrupt_manifest_after_many_heartbeats_never_reuses_a_seq(tmp_path):
    led = Ledger(str(tmp_path / "l.sqlite"))
    led.record_event("INFO", "heartbeat", "x")
    store = SpyStore(tmp_path / "b")
    clock = Clock()
    pub = Publisher(store, str(tmp_path / "l.sqlite"), now=clock)
    seqs = []
    for _ in range(250):                                  # 1 snapshot + 249 heartbeat-only manifests
        clock.advance(11)
        assert pub.tick()
        seqs.append(pub.last_manifest.seq)
    assert seqs == list(range(1, 251)) and store.count("put_file") == 1
    assert len([k for k in store.list_keys("snapshots/")]) == 1          # snapshot keys alone say "1"
    store.put_bytes("latest.json", b"{corrupt")
    pub.close()
    p2 = Publisher(store, str(tmp_path / "l.sqlite"), now=clock)
    assert p2.tick()
    assert p2.last_manifest.seq > 250
    # same for an oversized manifest
    store.put_bytes("latest.json", b"A" * (MAX_MANIFEST_BYTES + 1))
    p3 = Publisher(store, str(tmp_path / "l.sqlite"), now=clock)
    assert p3.tick()
    assert p3.last_manifest.seq > p2.last_manifest.seq
    p2.close(); p3.close(); led.close()


def test_next_seq_is_contiguous_with_a_healthy_manifest_and_uses_floor_when_it_is_missing(tmp_path):
    led = Ledger(str(tmp_path / "l.sqlite"))
    store = SpyStore(tmp_path / "b")
    clock = Clock()
    for want in (1, 2, 3):
        assert publish_once(store, str(tmp_path / "l.sqlite"), now=clock).seq == want
    assert snap.read_seq_floor(store) == 1 + snap.SEQ_FLOOR_RESERVE
    store.delete("latest.json")
    assert snap.next_seq(store) == 1 + snap.SEQ_FLOOR_RESERVE + 1
    led.close()


def test_floor_is_written_before_the_manifest_and_only_every_reserve_publishes(tmp_path):
    led = Ledger(str(tmp_path / "l.sqlite"))
    store = SpyStore(tmp_path / "b")
    floor = snap.SeqFloor(store)
    clock = Clock()
    m = None
    for _ in range(5):
        m = publish_once(store, str(tmp_path / "l.sqlite"), now=clock, previous=m, reuse=m is not None,
                         seq_floor=floor)
    puts = [k for op, k in store.ops if op == "put_bytes"]
    assert puts.count("seq-floor.json") == 1 and puts.index("seq-floor.json") < puts.index("latest.json")
    led.close()


def test_corrupt_or_oversized_floor_is_ignored_and_a_rolled_back_manifest_is_detected(tmp_path):
    store = SpyStore(tmp_path / "b")
    store.put_bytes("seq-floor.json", b"nope")
    assert snap.read_seq_floor(store) is None
    store.put_bytes("seq-floor.json", b"1" * (MAX_CONTROL_BYTES + 1))
    assert snap.read_seq_floor(store) is None
    assert snap.next_seq(store) == 1
    # a valid but OLD manifest while the floor says we got far beyond it
    led = Ledger(str(tmp_path / "l.sqlite"))
    m = publish_once(store, str(tmp_path / "l.sqlite"), now=Clock(), seq=3)
    store.put_bytes("seq-floor.json", json.dumps({"seq": 3 + 500}).encode())
    assert snap.next_seq(store) == 3 + 500 + 1
    led.close()


def test_failed_publish_reseeds_from_the_store_and_rereads_the_floor(tmp_path):
    led = Ledger(str(tmp_path / "l.sqlite"))
    led.record_event("INFO", "heartbeat", "x")
    store = SpyStore(tmp_path / "b")
    clock = Clock()
    pub = Publisher(store, str(tmp_path / "l.sqlite"), now=clock)
    assert pub.tick()
    store.fail["put_bytes"] = ConnectionError("down")
    clock.advance(11)
    assert pub.tick() is False
    store.fail.clear()
    clock.advance(60)
    assert pub.tick() is True and pub.last_manifest.seq == 2
    pub.close(); led.close()

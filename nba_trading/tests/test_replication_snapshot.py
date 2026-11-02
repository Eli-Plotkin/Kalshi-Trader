"""Manifest, consistent snapshot (under a live writer), verification, seq derivation, pruning."""

import json
import os
import sqlite3
import threading

import pytest

from nba_trading.ledger import Ledger
from nba_trading.replication import snapshot as snap
from nba_trading.replication.store import LocalDirStore


def make_ledger(tmp_path, name="ledger.sqlite"):
    return Ledger(str(tmp_path / name))


def good_manifest(**over):
    base = dict(version=1, seq=7, generated_at="2026-10-29T18:00:30+00:00",
                snapshot_key="snapshots/000000007-ab12cd34.sqlite", sha256="a" * 64, size=10,
                user_version=1, bot_heartbeat_at=None, counts={"events": 1})
    base.update(over)
    return snap.Manifest(**base)


# --- manifest ------------------------------------------------------------------------------------

def test_manifest_roundtrip():
    m = good_manifest(bot_heartbeat_at="2026-10-29T17:58:12+00:00")
    assert snap.Manifest.from_json(m.to_json()) == m
    raw = json.loads(m.to_json())
    assert set(raw) == {"version", "seq", "generated_at", "snapshot_key", "sha256", "size",
                        "user_version", "bot_heartbeat_at", "counts", "bot_alive_at"}


@pytest.mark.parametrize("mutate", [
    lambda d: d.pop("seq"),
    lambda d: d.update(seq="x"),
    lambda d: d.update(generated_at="yesterday"),
    lambda d: d.update(sha256="short"),
    lambda d: d.update(snapshot_key="../../etc/passwd"),
    lambda d: d.update(snapshot_key="/abs"),
])
def test_manifest_rejects_invalid(mutate):
    d = json.loads(good_manifest().to_json())
    mutate(d)
    with pytest.raises(snap.ManifestError):
        snap.Manifest.from_json(json.dumps(d))


@pytest.mark.parametrize("raw", [b"", b"not json", b"[1,2]", b"\xff\xfe"])
def test_manifest_rejects_garbage(raw):
    with pytest.raises(snap.ManifestError):
        snap.Manifest.from_json(raw)


def test_read_write_manifest(tmp_path):
    store = LocalDirStore(tmp_path / "b")
    assert snap.read_manifest(store) is None
    m = good_manifest()
    snap.write_manifest(store, m)
    assert snap.read_manifest(store) == m
    store.put_bytes("latest.json", b"{broken")
    with pytest.raises(snap.ManifestError):
        snap.read_manifest(store)


# --- snapshot ------------------------------------------------------------------------------------

def test_build_snapshot_contents_counts_and_heartbeat(tmp_path):
    led = make_ledger(tmp_path)
    led.record_event("INFO", "heartbeat", "ok")
    led.record_event("INFO", "other", "x")
    led.record_decision(game_id="g1", action="skip", reason="r", dry_run=True)
    out = tmp_path / "snap.sqlite"
    s = snap.build_snapshot(str(tmp_path / "ledger.sqlite"), str(out))
    assert s.counts["events"] == 2 and s.counts["decisions"] == 1 and s.counts["orders"] == 0
    assert s.bot_heartbeat_at is not None
    assert s.user_version >= 1
    assert s.size == os.path.getsize(out) and s.sha256 == snap.sha256_file(str(out))
    # self-contained single file: journal_mode DELETE, no sidecars
    with open(out, "rb") as fh:
        header = fh.read(20)
    assert header[18] == 1 and header[19] == 1
    assert [n for n in os.listdir(tmp_path) if n.startswith("snap.sqlite")] == ["snap.sqlite"]
    # the live ledger is untouched and still writable
    led.record_event("INFO", "after", "x")
    led.close()


def test_snapshot_without_heartbeat_has_none(tmp_path):
    led = make_ledger(tmp_path)
    led.close()
    s = snap.build_snapshot(str(tmp_path / "ledger.sqlite"), str(tmp_path / "s.sqlite"))
    assert s.bot_heartbeat_at is None


def test_build_snapshot_missing_db_raises_and_never_creates_it(tmp_path):
    with pytest.raises(snap.SnapshotError):
        snap.build_snapshot(str(tmp_path / "nope.sqlite"), str(tmp_path / "s.sqlite"))
    assert not (tmp_path / "nope.sqlite").exists()
    assert not (tmp_path / "s.sqlite").exists()


def test_snapshot_uses_a_readonly_connection(tmp_path):
    make_ledger(tmp_path).close()
    conn = snap.open_readonly(str(tmp_path / "ledger.sqlite"))
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("INSERT INTO events VALUES ('t','INFO','k','m')")
    conn.close()


def test_inspect_db_rejects_corrupt_file(tmp_path):
    bad = tmp_path / "bad.sqlite"
    bad.write_bytes(b"this is not a database" * 100)
    with pytest.raises(snap.SnapshotError):
        snap.inspect_db(str(bad))


def test_inspect_db_detects_page_corruption(tmp_path):
    led = make_ledger(tmp_path)
    for i in range(300):
        led.record_event("INFO", "k", "x" * 200)
    led.close()
    out = tmp_path / "s.sqlite"
    snap.build_snapshot(str(tmp_path / "ledger.sqlite"), str(out))
    data = bytearray(out.read_bytes())
    for i in range(4096 * 3, 4096 * 6):          # trash several data pages
        data[i] = 0xFF
    out.write_bytes(bytes(data))
    with pytest.raises(snap.SnapshotError):
        snap.inspect_db(str(out))


def test_snapshots_are_consistent_while_a_writer_is_active(tmp_path):
    """Online backup from a read-only connection while another thread keeps committing: every snapshot
    passes integrity_check, is a transaction boundary (paired rows stay paired) and counts never go
    backwards."""
    led = make_ledger(tmp_path)
    db = str(tmp_path / "ledger.sqlite")
    stop = threading.Event()
    errors = []

    def writer():
        i = 0
        try:
            while not stop.is_set():
                with led._lock:                       # one transaction inserts a PAIR of rows
                    led.conn.execute("INSERT INTO events VALUES ('t','INFO','pair',?)", (str(i),))
                    led.conn.execute("INSERT INTO events VALUES ('t','INFO','pair',?)", (str(i),))
                    led.conn.commit()
                i += 1
        except Exception as exc:                      # pragma: no cover
            errors.append(exc)

    t = threading.Thread(target=writer)
    t.start()
    try:
        last = -1
        for n in range(25):
            out = str(tmp_path / f"s{n}.sqlite")
            s = snap.build_snapshot(db, out)
            assert s.counts["events"] >= last
            assert s.counts["events"] % 2 == 0, "snapshot split a transaction"
            last = s.counts["events"]
            user_version, counts, _ = snap.inspect_db(out)       # integrity_check inside
            assert counts["events"] == s.counts["events"]
            os.unlink(out)
    finally:
        stop.set()
        t.join()
    assert errors == []
    assert last > 0
    led.close()


# --- seq / keys / pruning ------------------------------------------------------------------------

def test_snapshot_key_format_and_ordering():
    assert snap.snapshot_key_for(1234, "ab12cd34ffff") == "snapshots/000001234-ab12cd34.sqlite"
    assert snap.snapshot_key_for(2, "aa") < snap.snapshot_key_for(10, "aa")


def test_next_seq_from_empty_manifest_snapshots_and_corrupt_manifest(tmp_path):
    store = LocalDirStore(tmp_path / "b")
    assert snap.next_seq(store) == 1
    snap.write_manifest(store, good_manifest(seq=41))
    assert snap.next_seq(store) == 42
    store.put_bytes("snapshots/000000050-deadbeef.sqlite", b"x")      # orphan from a failed publish
    assert snap.next_seq(store) == 51
    store.put_bytes("latest.json", b"{corrupt")                        # manifest unreadable
    assert snap.next_seq(store) == 51
    assert snap.next_seq(store, previous=good_manifest(seq=99)) == 100


def test_prune_keeps_newest_and_protects_manifest_target(tmp_path):
    store = LocalDirStore(tmp_path / "b")
    for s in range(1, 11):
        store.put_bytes(snap.snapshot_key_for(s, "ab" * 4), b"x")
    store.put_bytes("latest.json", b"{}")
    protected = snap.snapshot_key_for(2, "ab" * 4)          # manifest still points at an old object
    deleted = snap.prune_snapshots(store, keep=3, protect=(protected,))
    remaining = store.list_keys("snapshots/")
    assert remaining == [snap.snapshot_key_for(s, "ab" * 4) for s in (2, 8, 9, 10)]
    assert len(deleted) == 6
    assert store.get_bytes("latest.json") == b"{}"           # never touches non-snapshot keys


def test_prune_ignores_foreign_keys_and_noop_when_few(tmp_path):
    store = LocalDirStore(tmp_path / "b")
    store.put_bytes("snapshots/readme.txt", b"hi")
    store.put_bytes(snap.snapshot_key_for(1, "aa" * 4), b"x")
    assert snap.prune_snapshots(store, keep=60) == []
    assert snap.prune_snapshots(store, keep=1) == []
    assert store.get_bytes("snapshots/readme.txt") == b"hi"

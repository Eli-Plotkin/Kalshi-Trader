"""Disaster recovery: restoring the ledger from the latest published snapshot."""

from __future__ import annotations

import io
import os
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from nba_trading.ledger import InstanceLock, Ledger
from nba_trading.replication import publisher, restore, subscriber
from nba_trading.replication.store import LocalDirStore


def _make_ledger(path, n_events=3):
    led = Ledger(str(path))
    for i in range(n_events):
        led.record_event("INFO", "heartbeat" if i == 0 else "note", f"e{i}")
    led.record_decision(game_id="g1", action="skip", reason="outside_band:70", dry_run=True)
    led.close()


def _publish(store, db):
    return publisher.publish_once(store, str(db))


def _count(path, table):
    c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return c.execute(f"select count(*) from {table}").fetchone()[0]
    finally:
        c.close()


@pytest.fixture()
def world(tmp_path):
    store = LocalDirStore(str(tmp_path / "bucket"))
    src = tmp_path / "old-server" / "nba_ledger.sqlite"
    src.parent.mkdir()
    _make_ledger(src)
    _publish(store, src)
    return store, src, tmp_path / "new-server" / "nba_ledger.sqlite"


def test_restore_onto_a_fresh_server_reproduces_the_ledger(world):
    store, src, target = world
    info = restore.restore(store, str(target))
    assert os.path.exists(target)
    assert _count(target, "events") == _count(src, "events") == 3
    assert _count(target, "decisions") == 1
    assert info["counts"]["events"] == 3 and info["backup"] is None
    # and it is a working ledger the bot can open (schema + migrations intact)
    led = Ledger(str(target))
    led.record_event("INFO", "after-restore", "x")
    led.close()


def test_restore_refuses_to_overwrite_without_force(world):
    store, src, target = world
    target.parent.mkdir()
    target.write_bytes(b"precious")
    with pytest.raises(restore.RestoreError, match="already exists"):
        restore.restore(store, str(target))
    assert target.read_bytes() == b"precious"


def test_force_keeps_the_old_file_and_drops_stale_wal_files(world):
    store, src, target = world
    target.parent.mkdir()
    _make_ledger(target, n_events=9)                           # a different, older local ledger
    (target.parent / (target.name + "-wal")).write_bytes(b"stale wal")
    info = restore.restore(store, str(target), force=True)
    assert _count(target, "events") == 3                       # the snapshot, not the local one
    assert info["backup"] and os.path.exists(info["backup"]) and _count(info["backup"], "events") == 9
    assert not os.path.exists(str(target) + "-wal")            # a stale WAL must never be replayed onto it
    assert os.path.exists(info["backup"] + "-wal")


def test_restore_refuses_while_a_bot_holds_the_lock(world):
    store, src, target = world
    target.parent.mkdir()
    held = InstanceLock(str(target))
    held.acquire()
    try:
        with pytest.raises(restore.RestoreError, match="bot appears to be running"):
            restore.restore(store, str(target), force=True)
    finally:
        held.release()
    assert not os.path.exists(target)


def test_nothing_published_yet_is_a_clear_refusal(tmp_path):
    with pytest.raises(restore.RestoreError, match="nothing has ever been published"):
        restore.restore(LocalDirStore(str(tmp_path / "empty")), str(tmp_path / "x" / "l.sqlite"))


def test_a_corrupt_snapshot_is_never_installed(world):
    store, src, target = world
    m = restore.read_manifest(store)
    key = os.path.join(store.root, m.snapshot_key) if hasattr(store, "root") else None
    with open(key, "r+b") as f:                                # flip bytes in the stored snapshot
        f.seek(100)
        f.write(b"\x00\xff\x00\xff")
    with pytest.raises(restore.RestoreError, match="verification|download"):
        restore.restore(store, str(target))
    assert not os.path.exists(target)
    assert not [p for p in os.listdir(target.parent) if "restor" in p]   # no half-written leftovers


def test_a_storage_outage_is_reported_not_swallowed(world):
    store, src, target = world

    class Down(LocalDirStore):
        def get_bytes(self, key, max_bytes=None):
            raise ConnectionError("bucket unreachable")

    with pytest.raises(restore.RestoreError, match="could not read the manifest"):
        restore.restore(Down(store.root), str(target))


def test_restore_reports_how_old_the_snapshot_is(world):
    store, src, target = world
    later = datetime.now(timezone.utc) + timedelta(seconds=45)
    info = restore.restore(store, str(target), now=later)
    assert 40 <= info["snapshot_age_seconds"] <= 50


def test_cli_prints_a_summary_and_exit_codes(world, monkeypatch):
    store, src, target = world
    monkeypatch.setenv("REPLICATION_LOCAL_DIR", store.root)
    monkeypatch.delenv("REPLICATION_BUCKET", raising=False)
    out = io.StringIO()
    assert restore.main(["--to", str(target)], out=out) == 0
    text = out.getvalue()
    assert "Restored" in text and "OLD server is stopped" in text
    out2 = io.StringIO()
    assert restore.main(["--to", str(target)], out=out2) == 1     # now exists: refused
    assert "RESTORE REFUSED" in out2.getvalue()


def test_cli_without_replication_configured(monkeypatch, tmp_path):
    for k in ("REPLICATION_LOCAL_DIR", "REPLICATION_BUCKET"):
        monkeypatch.delenv(k, raising=False)
    out = io.StringIO()
    assert restore.main(["--to", str(tmp_path / "l.sqlite")], out=out) == 2
    assert "not configured" in out.getvalue()


def test_publish_after_restore_continues_the_sequence(world):
    """A restore onto a new server must not make seq go backwards (the frontend would refuse it)."""
    store, src, target = world
    before = restore.read_manifest(store).seq
    restore.restore(store, str(target))
    m = publisher.publish_once(store, str(target))
    assert m.seq > before

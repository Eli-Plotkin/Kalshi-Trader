"""The bot-alive beacon: a file next to the ledger, not a ledger write."""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

from nba_trading import liveness
from nba_trading.ledger import Ledger
from nba_trading.replication import publisher, subscriber
from nba_trading.replication.snapshot import read_manifest
from nba_trading.replication.store import LocalDirStore


def test_touch_and_read_round_trip(tmp_path):
    p = liveness.alive_path(str(tmp_path / "nba_ledger.sqlite"))
    assert os.path.basename(p) == "bot_alive.json" and os.path.dirname(p) == str(tmp_path)
    stamp = liveness.touch(p)
    assert liveness.read(p) == stamp
    assert not [f for f in os.listdir(tmp_path) if ".tmp-" in f]          # atomic: no temp leftovers


def test_read_tolerates_missing_garbage_and_oversized(tmp_path):
    p = str(tmp_path / "bot_alive.json")
    assert liveness.read(p) is None
    open(p, "w").write("not json")
    assert liveness.read(p) is None
    open(p, "w").write(json.dumps({"alive_at": "not a date"}))
    assert liveness.read(p) is None
    open(p, "w").write(json.dumps({"alive_at": "2026-10-29T18:00:00+00:00", "pad": "x" * 5000}))
    assert liveness.read(p) is None


def test_the_beacon_does_not_make_the_replicator_upload_snapshots(tmp_path):
    """The whole reason it is a file: refreshing liveness must not look like a data change."""
    db = tmp_path / "nba_ledger.sqlite"
    Ledger(str(db)).close()
    store = LocalDirStore(str(tmp_path / "bucket"))
    t = [datetime(2026, 10, 29, 18, 0, 0, tzinfo=timezone.utc)]
    pub = publisher.Publisher(store, str(db), tick_seconds=5, heartbeat_seconds=30, now=lambda: t[0])
    assert pub.tick() is True                                              # first publish: a snapshot
    snapshots = len([k for k in store.list_keys("snapshots/")])
    for _ in range(12):                                                    # an hour of 5-min ticks with a live bot
        t[0] += timedelta(seconds=60)
        liveness.touch(liveness.alive_path(str(db)), now=t[0])
        pub.tick()
    assert len(store.list_keys("snapshots/")) == snapshots                 # no new snapshot objects
    pub.close()


def test_manifest_carries_the_freshest_beacon_on_both_paths(tmp_path):
    db = tmp_path / "nba_ledger.sqlite"
    led = Ledger(str(db))
    store = LocalDirStore(str(tmp_path / "bucket"))
    t = [datetime(2026, 10, 29, 18, 0, 0, tzinfo=timezone.utc)]
    pub = publisher.Publisher(store, str(db), tick_seconds=5, heartbeat_seconds=30, now=lambda: t[0])
    liveness.touch(liveness.alive_path(str(db)), now=t[0])
    pub.tick()                                                             # snapshot path
    m1 = read_manifest(store)
    assert m1.bot_alive_at == t[0].isoformat(timespec="seconds")
    t[0] += timedelta(seconds=40)
    liveness.touch(liveness.alive_path(str(db)), now=t[0])
    pub.tick()                                                             # heartbeat-only path
    m2 = read_manifest(store)
    assert m2.bot_alive_at == t[0].isoformat(timespec="seconds") and m2.seq > m1.seq
    t[0] += timedelta(seconds=5)
    led.record_event("INFO", "x", "a real change")                         # snapshot path again
    liveness.touch(liveness.alive_path(str(db)), now=t[0])
    pub.tick()
    assert read_manifest(store).bot_alive_at == t[0].isoformat(timespec="seconds")
    pub.close()
    led.close()


def test_dead_bot_shows_as_silent_while_replication_stays_healthy(tmp_path):
    db = tmp_path / "nba_ledger.sqlite"
    Ledger(str(db)).close()
    store = LocalDirStore(str(tmp_path / "bucket"))
    t = [datetime(2026, 10, 29, 18, 0, 0, tzinfo=timezone.utc)]
    pub = publisher.Publisher(store, str(db), tick_seconds=5, heartbeat_seconds=10, now=lambda: t[0])
    liveness.touch(liveness.alive_path(str(db)), now=t[0])                 # the bot writes once, then dies
    pub.tick()
    for _ in range(30):                                                    # 5 minutes of healthy replication
        t[0] += timedelta(seconds=10)
        pub.tick()
    dest = tmp_path / "frontend"
    subscriber.sync_once(store, str(dest), now=lambda: t[0])
    state = subscriber.read_state(str(dest))
    assert subscriber.data_age_seconds(state, t[0]) < 5                    # replication is fresh ...
    assert subscriber.bot_alive_age_seconds(state, t[0]) > 280             # ... but the bot is not
    assert subscriber.bot_silent(state, t[0]) is True
    pub.close()

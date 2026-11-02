"""Ledger publisher (docs/design-replication.md section 5).

Runs as its own process next to the bot:  ``python -m nba_trading.replication.publisher``

* Reads the ledger READ-ONLY; never writes to it and never blocks the bot.
* Every ``tick`` seconds it publishes a fresh snapshot if the database changed
  (``PRAGMA data_version`` on a persistent read connection, which changes whenever ANY other
  connection/process commits), and ALWAYS publishes a fresh manifest if the last publish is older than
  ``heartbeat`` seconds. When nothing changed the heartbeat only rewrites the tiny manifest (new
  ``seq`` + ``generated_at``) and points it at the already-uploaded snapshot object, so the frontend's
  data age stays small without re-uploading the database.
* Publish order is the correctness rule: snapshot object (immutable key) FIRST, ``latest.json`` LAST.
* Failures never escape: log, back off (capped at 30 s), keep trying; alert after ``alert_after``
  seconds without a success and send a recovery notice.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import tempfile
import threading
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Optional

from nba_trading import alerts
from nba_trading.replication.snapshot import (
    DEFAULT_KEEP, Manifest, SeqFloor, build_snapshot, heartbeat_manifest, iso, manifest_for,
    next_seq, open_readonly, parse_iso, prune_snapshots, read_kv_alive, to_utc, utcnow, write_manifest)
from nba_trading.replication.store import Store

logger = logging.getLogger(__name__)

DEFAULT_TICK_SECONDS = 5.0
DEFAULT_HEARTBEAT_SECONDS = 10.0
DEFAULT_ALERT_AFTER_SECONDS = 120.0
DEFAULT_ALERT_COOLDOWN_SECONDS = 1800.0
MAX_BACKOFF_SECONDS = 30.0


def publish_once(store: Store, db_path: str, *, now=None, keep: int = DEFAULT_KEEP,
                 seq: Optional[int] = None, previous: Optional[Manifest] = None,
                 reuse: bool = False, workdir: Optional[str] = None,
                 bot_alive_at: Optional[str] = None, seq_floor: Optional[SeqFloor] = None) -> Manifest:
    """Publish the ledger once and return the manifest that is now live. Raises on any failure; on
    failure ``latest.json`` is untouched (readers keep the previous good state).

    ``seq``       explicit sequence number; default derives it from the store (survives restarts).
    ``previous``  the last manifest this caller published (lets an unchanged snapshot be reused).
    ``reuse``     DB known unchanged: skip the snapshot and republish ``previous``'s snapshot under a
                  new seq/generated_at (the heartbeat).
    ``bot_alive_at``  the freshest liveness stamp (the bot's beacon file), used on every path so a
                  manifest never carries an older stamp than the one just read; None keeps the previous
                  manifest's value (heartbeat) or the snapshot's own kv copy (snapshot path).
    ``seq_floor`` persisted-floor helper (the Publisher passes a long-lived one); ``seq-floor.json`` is
                  raised before ``latest.json`` is written so a restart over a corrupt manifest cannot
                  reuse a seq (design section 11.6).
    """
    clock = now or utcnow
    if reuse and previous is None:
        raise ValueError("reuse=True needs previous")
    if seq is None:
        seq = next_seq(store, previous)
    if reuse:
        manifest = heartbeat_manifest(previous, seq, clock(), bot_alive_at)
    else:
        with tempfile.TemporaryDirectory(prefix="replica-", dir=workdir) as tmp:
            taken_at = clock()
            built = build_snapshot(db_path, os.path.join(tmp, "snapshot.sqlite"))
            if previous is not None and previous.sha256 == built.sha256:
                # identical content: reuse the uploaded object, no upload
                manifest = replace(heartbeat_manifest(previous, seq, taken_at),
                                   counts=dict(built.counts), bot_heartbeat_at=built.bot_heartbeat_at,
                                   bot_alive_at=built.bot_alive_at)
            else:
                manifest = manifest_for(built, seq, taken_at)
                store.put_file(manifest.snapshot_key, built.path)         # (3) snapshot object FIRST
        if bot_alive_at is not None:                                       # the live beacon beats the snapshot's copy
            manifest = replace(manifest, bot_alive_at=bot_alive_at)
    (seq_floor or SeqFloor(store)).ensure(manifest.seq)                    # (3b) floor >= seq, before it is visible
    write_manifest(store, manifest)                                        # (4) manifest LAST
    try:                                                                   # (5) retention; never fatal
        prune_snapshots(store, keep, protect=(manifest.snapshot_key,))
    except Exception as exc:
        logger.warning("replication: pruning old snapshots failed (will retry next publish): %s", exc)
    return manifest


def _atomic_write_json(path, payload):
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    tmp = f"{path}.tmp-{os.getpid()}-{threading.get_ident()}"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh)
    os.replace(tmp, path)


class Publisher:
    """Stateful loop around ``publish_once``. ``tick()`` never raises."""

    def __init__(self, store: Store, db_path: str, *, tick_seconds: float = DEFAULT_TICK_SECONDS,
                 heartbeat_seconds: float = DEFAULT_HEARTBEAT_SECONDS, keep: int = DEFAULT_KEEP,
                 alert_after_seconds: float = DEFAULT_ALERT_AFTER_SECONDS,
                 alert_cooldown_seconds: float = DEFAULT_ALERT_COOLDOWN_SECONDS,
                 status_file: Optional[str] = None, now=None, workdir: Optional[str] = None):
        self.store = store
        self.db_path = db_path
        self.tick_seconds = float(tick_seconds)
        self.heartbeat_seconds = float(heartbeat_seconds)
        self.keep = keep
        self.alert_after = float(alert_after_seconds)
        self.alert_cooldown = float(alert_cooldown_seconds)
        self.status_file = status_file
        self.workdir = workdir
        self._now = now or utcnow
        self._conn = None
        self._conn_id = None
        self._reseed = False             # a failed publish may have half-happened: re-derive seq from the store
        self._published_dv = None        # data_version of the last SUCCESSFULLY published state
        self._floor = SeqFloor(store)    # persisted seq floor (re-read after any failure)
        self.last_manifest: Optional[Manifest] = None
        self.last_publish_at: Optional[datetime] = None
        self.last_success_at: Optional[datetime] = None
        self.last_error: Optional[str] = None
        self.consecutive_failures = 0
        self._next_attempt_at: Optional[datetime] = None
        self._started_at = None
        self._alerted_at: Optional[datetime] = None
        self._alert_active = False

    # --- change detection ---

    def _read_dv(self):
        """(data_version, file identity). Reopens the read connection if the db file was replaced."""
        st = os.stat(self.db_path)
        ident = (st.st_dev, st.st_ino)
        if self._conn is not None and ident != self._conn_id:
            self._close_conn()
        if self._conn is None:
            self._conn = open_readonly(self.db_path)
            self._conn_id = ident
            self._published_dv = None    # a fresh connection's data_version is not comparable
        return self._conn.execute("PRAGMA data_version").fetchone()[0]

    def _read_alive(self):
        """The bot's liveness stamp: the beacon file next to the ledger (nba_trading/liveness.py), else
        the legacy kv row. None if neither is readable. Never raises."""
        try:
            from nba_trading import liveness
            stamp = liveness.read(liveness.alive_path(self.db_path))
            if stamp:
                return stamp
        except Exception:
            pass
        try:
            return read_kv_alive(self._conn)
        except Exception:
            return None

    def _close_conn(self):
        if self._conn is not None:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass
        self._conn = None
        self._conn_id = None

    def close(self):
        self._close_conn()

    # --- one iteration ---

    def tick(self) -> bool:
        """One loop iteration. Returns True if a manifest was published. Never raises."""
        now = to_utc(self._now())
        if self._started_at is None:
            self._started_at = now
        try:
            if self._next_attempt_at is not None and now < self._next_attempt_at:
                self._check_staleness(now)
                return False
            published = self._tick(now)
            return published
        except Exception as exc:                       # pragma: no cover - belt and braces
            self._fail(now, exc)
            return False

    def _tick(self, now):
        try:
            dv = self._read_dv()
            changed = (self.last_manifest is None or dv != self._published_dv)
            due = (self.last_publish_at is None
                   or (now - self.last_publish_at).total_seconds() >= self.heartbeat_seconds)
            if not changed and not due:
                self._check_staleness(now)
                return False
            seq = None
            if self.last_manifest is not None and not self._reseed:
                seq = self.last_manifest.seq + 1
            manifest = publish_once(
                self.store, self.db_path, now=lambda: now, keep=self.keep, seq=seq,
                previous=self.last_manifest, reuse=(not changed), workdir=self.workdir,
                bot_alive_at=self._read_alive(), seq_floor=self._floor)
        except Exception as exc:
            self._fail(now, exc)
            return False
        self.last_manifest = manifest
        self._reseed = False
        self._published_dv = dv
        self.last_publish_at = now
        self._succeed(now, manifest)
        return True

    # --- bookkeeping / alerting ---

    def _succeed(self, now, manifest):
        was_alerting = self._alert_active
        self.last_success_at = now
        self.last_error = None
        self.consecutive_failures = 0
        self._next_attempt_at = None
        self._alert_active = False
        self._alerted_at = None
        self._write_status(now)
        if was_alerting:
            self._notify("WARNING", "replication_recovered",
                         f"ledger replication recovered (seq {manifest.seq})")

    def _fail(self, now, exc):
        self.consecutive_failures += 1
        self._reseed = True
        self._floor = SeqFloor(self.store)               # the store may have changed under us: re-read
        self.last_error = f"{type(exc).__name__}: {exc}"[:500]
        delay = min(MAX_BACKOFF_SECONDS, self.tick_seconds * (2 ** min(self.consecutive_failures - 1, 10)))
        self._next_attempt_at = now + timedelta(seconds=delay)
        logger.warning("replication publish failed (%d in a row, retry in %.0fs): %s",
                       self.consecutive_failures, delay, self.last_error)
        self._check_staleness(now)
        self._write_status(now)

    def _check_staleness(self, now):
        # "silence" runs from the last success, or from startup if there never was one.
        since = self.last_success_at or self._started_at or now
        silent = (now - since).total_seconds()
        if self.consecutive_failures <= 0 or silent < self.alert_after:
            return
        if self._alerted_at is not None and (now - self._alerted_at).total_seconds() < self.alert_cooldown:
            return
        self._alerted_at = now
        self._alert_active = True
        self._notify("ERROR", "replication_stale",
                     f"ledger replication has not succeeded for {int(silent)}s "
                     f"({self.consecutive_failures} failures); last error: {self.last_error}")

    @staticmethod
    def _notify(level, kind, message):
        try:
            alerts.notify(level, kind, message)
        except Exception as exc:                        # notify never raises, but be sure
            logger.warning("replication alert failed: %s", exc)

    def status(self):
        return {
            "last_success_at": iso(self.last_success_at) if self.last_success_at else None,
            "consecutive_failures": self.consecutive_failures,
            "last_error": self.last_error,
            "last_seq": self.last_manifest.seq if self.last_manifest else None,
        }

    def _write_status(self, now):
        if not self.status_file:
            return
        try:
            payload = dict(self.status(), updated_at=iso(now))
            _atomic_write_json(self.status_file, payload)
        except Exception as exc:
            logger.warning("could not write replication status file: %s", exc)

    # --- loop ---

    def run_forever(self, stop_event: Optional[threading.Event] = None):
        stop = stop_event or threading.Event()
        logger.info("replication publisher started: db=%s tick=%.0fs heartbeat=%.0fs",
                    self.db_path, self.tick_seconds, self.heartbeat_seconds)
        try:
            while not stop.is_set():
                self.tick()
                stop.wait(self.tick_seconds)
        finally:
            self.close()


def run_forever(store: Store, db_path: str, *, stop_event: Optional[threading.Event] = None, **kwargs):
    """Run a Publisher until ``stop_event`` is set. Never raises."""
    pub = Publisher(store, db_path, **kwargs)
    try:
        pub.run_forever(stop_event)
    except Exception:                                  # pragma: no cover - tick() never raises
        logger.exception("replication publisher loop crashed")
    return pub


# --- status file / healthcheck -------------------------------------------------------------------

def status_healthy(status_file, max_age_seconds=DEFAULT_ALERT_AFTER_SECONDS, now=None):
    """Container healthcheck: True when the status file shows a success within ``max_age_seconds``."""
    try:
        with open(status_file, encoding="utf-8") as fh:
            st = json.load(fh)
        last = parse_iso(st.get("last_success_at"))
        if last is None:
            return False
        return (to_utc((now or utcnow)()) - last).total_seconds() <= max_age_seconds
    except Exception:
        return False


# --- CLI -----------------------------------------------------------------------------------------

def _env_float(name, default):
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return float(default)


def main(argv=None):
    from nba_trading.replication.store import store_from_env

    parser = argparse.ArgumentParser(description="Publish ledger snapshots to object storage.")
    parser.add_argument("--db", default=None, help="ledger path (default: LEDGER_FILE env or data/nba_ledger.sqlite)")
    parser.add_argument("--once", action="store_true", help="publish once and exit (0 = ok)")
    parser.add_argument("--healthcheck", action="store_true",
                        help="exit 0 if the status file shows a recent success, else 1")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    status_file = os.environ.get("REPLICATION_STATUS_FILE") or None
    alert_after = _env_float("REPLICATION_ALERT_AFTER_SECONDS", DEFAULT_ALERT_AFTER_SECONDS)
    if args.healthcheck:
        if not status_file:
            return 1
        return 0 if status_healthy(status_file, max(alert_after, 60.0)) else 1

    store = store_from_env()
    if store is None:
        logger.error("replication is not configured (set REPLICATION_BUCKET or REPLICATION_LOCAL_DIR)")
        return 2
    db_path = args.db or os.environ.get("LEDGER_FILE") or os.path.join("data", "nba_ledger.sqlite")
    keep = int(_env_float("REPLICATION_KEEP_SNAPSHOTS", DEFAULT_KEEP))
    if args.once:
        try:
            m = publish_once(store, db_path, keep=keep)
        except Exception as exc:
            logger.error("publish failed: %s", exc)
            return 1
        logger.info("published seq %d", m.seq)
        return 0
    run_forever(store, db_path,
                tick_seconds=_env_float("REPLICATION_TICK_SECONDS", DEFAULT_TICK_SECONDS),
                heartbeat_seconds=_env_float("REPLICATION_HEARTBEAT_SECONDS", DEFAULT_HEARTBEAT_SECONDS),
                keep=keep, alert_after_seconds=alert_after, status_file=status_file)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

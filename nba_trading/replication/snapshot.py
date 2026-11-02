"""Manifest + consistent ledger snapshot (docs/design-replication.md section 4).

``latest.json`` (the Manifest) is the single pointer a reader trusts. Publish order is the
correctness rule: snapshot object first (immutable key), manifest last.

The snapshot is taken with SQLite's online backup API from a READ-ONLY connection, so it is a
transactionally consistent copy even while the bot is writing, and the replicator can never write to
or lock the live ledger for longer than one read transaction.
"""

from __future__ import annotations

import functools
import hashlib
import json
import logging
import os
import re
import sqlite3
import time
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple, Optional

from nba_trading.replication.store import MAX_CONTROL_BYTES, MAX_MANIFEST_BYTES, ObjectTooLarge

logger = logging.getLogger(__name__)

MANIFEST_KEY = "latest.json"
SNAPSHOT_PREFIX = "snapshots/"
MANIFEST_VERSION = 1
DEFAULT_KEEP = 60
COUNT_TABLES = ("decisions", "orders", "settlements", "paper_trades", "events")

SEQ_FLOOR_KEY = "seq-floor.json"
SEQ_FLOOR_RESERVE = 100            # the persisted floor runs this far ahead of the published seq
MAX_DB_BYTES = 512 * 1024 * 1024   # a ledger/snapshot bigger than this is refused (page_count*page_size)
INSPECT_TIMEOUT_SECONDS = 120.0    # wall-clock budget for verifying one snapshot (integrity_check etc.)
KV_BOT_ALIVE = "bot_alive_at"      # kv row the bot touches every ~60 s (Ledger.touch_alive)

_SNAP_RE = re.compile(r"^snapshots/(\d+)-([0-9a-f]+)\.sqlite$")


class SnapshotError(Exception):
    """A snapshot could not be taken or failed verification."""


class SnapshotRejected(SnapshotError, ValueError):
    """The snapshot is well-formed SQLite but is not an acceptable ledger (trigger, view, unexpected
    table/index/column, too big, ...). Snapshots are untrusted input (design section 11.2)."""


class ManifestError(ValueError):
    """latest.json exists but is not a valid manifest."""


# --- time helpers --------------------------------------------------------------------------------

def utcnow():
    return datetime.now(timezone.utc)


def to_utc(dt):
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def iso(dt):
    return to_utc(dt).isoformat(timespec="seconds")


def parse_iso(text):
    """Parse an ISO-8601 timestamp to an aware UTC datetime; None for empty/None/garbage."""
    if not text:
        return None
    try:
        return to_utc(datetime.fromisoformat(str(text).replace("Z", "+00:00")))
    except (ValueError, TypeError):
        return None


# --- manifest ------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Manifest:
    version: int
    seq: int
    generated_at: str
    snapshot_key: str
    sha256: str
    size: int
    user_version: int
    bot_heartbeat_at: Optional[str] = None
    counts: dict = field(default_factory=dict)
    bot_alive_at: Optional[str] = None       # kv 'bot_alive_at' (backend clock), design section 11.4

    def to_json(self):
        return json.dumps(asdict(self), sort_keys=True, indent=1).encode("utf-8")

    @classmethod
    def from_json(cls, data):
        try:
            raw = json.loads(data.decode("utf-8") if isinstance(data, (bytes, bytearray)) else data)
        except (ValueError, UnicodeDecodeError) as exc:
            raise ManifestError(f"manifest is not valid JSON: {exc}") from exc
        if not isinstance(raw, dict):
            raise ManifestError("manifest is not a JSON object")
        try:
            m = cls(
                version=int(raw["version"]), seq=int(raw["seq"]),
                generated_at=str(raw["generated_at"]), snapshot_key=str(raw["snapshot_key"]),
                sha256=str(raw["sha256"]), size=int(raw["size"]),
                user_version=int(raw["user_version"]),
                bot_heartbeat_at=(None if raw.get("bot_heartbeat_at") in (None, "")
                                  else str(raw["bot_heartbeat_at"])),
                counts=dict(raw.get("counts") or {}),
                bot_alive_at=_clean_ts(raw.get("bot_alive_at")),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ManifestError(f"manifest is missing/invalid field: {exc!r}") from exc
        if parse_iso(m.generated_at) is None:
            raise ManifestError("manifest generated_at is not a timestamp")
        if not _valid_key(m.snapshot_key):
            raise ManifestError(f"manifest snapshot_key is invalid: {m.snapshot_key!r}")
        if m.seq < 0 or m.size < 0 or not re.fullmatch(r"[0-9a-f]{64}", m.sha256):
            raise ManifestError("manifest has an invalid seq/size/sha256")
        return m


def _clean_ts(value):
    """A timestamp string normalised to ISO-8601 UTC, or None for absent/garbage (tolerant on purpose:
    an optional field must never make a manifest unreadable)."""
    dt = parse_iso(value) if isinstance(value, str) else None
    return iso(dt) if dt is not None else None


def _valid_key(key):
    return (bool(key) and not key.startswith("/") and "\\" not in key
            and all(p not in ("", ".", "..") for p in key.split("/")))


def read_manifest(store):
    """The current manifest, or None if the store has none. RAISES on transport errors,
    ManifestError if latest.json exists but is invalid and ObjectTooLarge if it exceeds
    MAX_MANIFEST_BYTES."""
    data = store.get_bytes(MANIFEST_KEY, MAX_MANIFEST_BYTES)     # ObjectTooLarge if oversized
    if data is None:
        return None
    return Manifest.from_json(data)


def write_manifest(store, manifest):
    store.put_bytes(MANIFEST_KEY, manifest.to_json())


def snapshot_key_for(seq, sha256):
    return f"{SNAPSHOT_PREFIX}{int(seq):09d}-{sha256[:8]}.sqlite"


def _snapshot_seq(key):
    m = _SNAP_RE.match(key)
    return int(m.group(1)) if m else None


def read_seq_floor(store):
    """The persisted seq floor, or None if absent / corrupt / oversized. RAISES on transport errors."""
    try:
        data = store.get_bytes(SEQ_FLOOR_KEY, MAX_CONTROL_BYTES)
    except ObjectTooLarge:
        logger.warning("replication: %s is oversized; ignoring it", SEQ_FLOOR_KEY)
        return None
    if data is None:
        return None
    try:
        raw = json.loads(data.decode("utf-8"))
        floor = int(raw["seq"])
    except (ValueError, KeyError, TypeError, UnicodeDecodeError):
        logger.warning("replication: %s is corrupt; ignoring it", SEQ_FLOOR_KEY)
        return None
    return floor if floor >= 0 else None


class SeqFloor:
    """Persisted upper bound of every seq this publisher has issued (``seq-floor.json``).

    Invariant: every manifest seq that was ever published is <= the stored floor. ``ensure(seq)`` is
    called BEFORE ``latest.json`` is written; it only touches the store when ``seq`` passes the stored
    floor, in which case it writes ``seq + SEQ_FLOOR_RESERVE`` (one tiny PUT every ~100 publishes, ~17 min
    at the default heartbeat). After a restart over a corrupt/oversized/deleted ``latest.json`` the next
    seq is derived from this floor, so it can never fall below a previously published seq."""

    def __init__(self, store):
        self.store = store
        self.value = None
        self._loaded = False

    def ensure(self, seq):
        if not self._loaded:
            self.value = read_seq_floor(self.store)
            self._loaded = True
        if self.value is not None and seq <= self.value:
            return
        new = int(seq) + SEQ_FLOOR_RESERVE
        self.store.put_bytes(SEQ_FLOOR_KEY, json.dumps({"seq": new, "written_at": iso(utcnow())}).encode("utf-8"))
        self.value = new


def next_seq(store, previous=None):
    """Next strictly-increasing seq, derived from what the STORE holds (so it survives a publisher
    restart or a restore onto a new server).

    * A readable manifest wins: next = max(manifest.seq, newest snapshot key seq, previous.seq) + 1,
      which keeps seqs contiguous in normal operation.
    * If latest.json is missing, corrupt or oversized, or is older than the floor can explain (a rolled
      back manifest), the persisted ``seq-floor.json`` is used too: it is >= every seq ever published,
      so heartbeat-only publishes (which leave no snapshot key behind) cannot be reused.
    Snapshot keys are always consulted as a further lower bound."""
    best = previous.seq if previous is not None else 0
    manifest = None
    try:
        manifest = read_manifest(store)
    except (ManifestError, ObjectTooLarge) as exc:
        logger.warning("replication: existing manifest is unusable (%s); deriving seq from the floor "
                       "and snapshot keys", exc)
    if manifest is not None:
        best = max(best, manifest.seq)
    floor = read_seq_floor(store)
    if floor is not None and (manifest is None or floor - SEQ_FLOOR_RESERVE > manifest.seq):
        best = max(best, floor)
    for key in store.list_keys(SNAPSHOT_PREFIX):
        s = _snapshot_seq(key)
        if s is not None:
            best = max(best, s)
    return best + 1


# --- snapshot ------------------------------------------------------------------------------------

def _ro_uri(path):
    return Path(os.path.abspath(path)).as_uri() + "?mode=ro"


def open_readonly(path, timeout=15.0):
    """A read-only connection to a SQLite file (never creates it, can never write).

    Hardened for untrusted files (design section 11.2): ``trusted_schema=OFF`` so a hostile schema
    object cannot call application-defined/unsafe SQL functions, and extension loading is disabled."""
    conn = sqlite3.connect(_ro_uri(path), uri=True, timeout=timeout)
    try:
        if hasattr(conn, "enable_load_extension"):
            conn.enable_load_extension(False)
        conn.execute("PRAGMA trusted_schema=OFF")
    except Exception:
        conn.close()
        raise
    return conn


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def take_snapshot(db_path, dest_path, attempts=3):
    """Copy ``db_path`` to ``dest_path`` with the online backup API from a read-only connection.

    The copy is made inside ONE read transaction (pages=-1) so it is consistent even while a writer
    commits concurrently. The result is a self-contained single file (journal_mode=DELETE: no -wal)."""
    last = None
    for attempt in range(max(1, attempts)):
        if os.path.exists(dest_path):
            os.unlink(dest_path)
        src = dst = None
        try:
            src = open_readonly(db_path)
            dst = sqlite3.connect(dest_path)
            src.backup(dst, pages=-1)
            dst.execute("PRAGMA journal_mode=DELETE")
            dst.commit()
            return dest_path
        except sqlite3.OperationalError as exc:      # e.g. "database is locked": brief retry
            last = exc
            time.sleep(0.2 * (attempt + 1))
        finally:
            for c in (dst, src):
                if c is not None:
                    try:
                        c.close()
                    except sqlite3.Error:
                        pass
            for suffix in ("-wal", "-shm", "-journal"):
                p = dest_path + suffix
                if os.path.exists(p):
                    try:
                        os.unlink(p)
                    except OSError:
                        pass
    raise SnapshotError(f"could not snapshot {db_path}: {last}")


class DbInfo(NamedTuple):
    user_version: int
    counts: dict
    bot_heartbeat_at: Optional[str]
    bot_alive_at: Optional[str]


@functools.lru_cache(maxsize=1)
def expected_schema():
    """``({table: frozenset(columns)}, {index: table})`` of the CURRENT ledger schema, derived from a
    fresh in-memory ``Ledger`` so it can never drift from ledger.py. A snapshot written by an older
    schema may contain a subset of these tables/indexes/columns, never anything else."""
    from nba_trading.ledger import Ledger
    ledger = Ledger(":memory:")
    try:
        conn = ledger.conn
        tables, indexes = {}, {}
        for typ, name, tbl in conn.execute("SELECT type, name, tbl_name FROM sqlite_master").fetchall():
            if name.startswith("sqlite_"):
                continue
            if typ == "table":
                tables[name] = frozenset(
                    r[1] for r in conn.execute(f'PRAGMA table_xinfo("{name}")').fetchall())
            elif typ == "index":
                indexes[name] = tbl
        return tables, indexes
    finally:
        ledger.close()


def check_schema(conn):
    """Raise SnapshotRejected unless the database holds only objects of the expected ledger schema:
    no triggers, no views, no virtual tables, no generated columns, no unknown tables/indexes/columns."""
    tables, indexes = expected_schema()
    rows = conn.execute("SELECT type, name, tbl_name, sql FROM sqlite_master").fetchall()
    for typ, name, tbl, sql in rows:
        if typ in ("trigger", "view"):
            raise SnapshotRejected(f"snapshot contains a {typ} ({name!r}); not a valid ledger")
        if typ == "table":
            if name == "sqlite_sequence":
                continue
            if name not in tables:
                raise SnapshotRejected(f"snapshot contains an unexpected table {name!r}")
            if (sql or "").lstrip()[:14].upper() == "CREATE VIRTUAL":
                raise SnapshotRejected(f"table {name!r} is a virtual table")
            for col in conn.execute(f'PRAGMA table_xinfo("{name}")').fetchall():
                if col[1] not in tables[name] or col[6] != 0:      # unknown column / hidden or generated
                    raise SnapshotRejected(f"table {name!r} has an unexpected column {col[1]!r}")
        elif typ == "index":
            if name.startswith("sqlite_autoindex_"):
                if tbl not in tables:
                    raise SnapshotRejected(f"snapshot contains an unexpected index {name!r}")
            elif indexes.get(name) != tbl:
                raise SnapshotRejected(f"snapshot contains an unexpected index {name!r}")
        else:
            raise SnapshotRejected(f"snapshot contains an unexpected schema object {typ} {name!r}")


def read_kv_alive(conn):
    """kv ``bot_alive_at`` as normalised ISO text, or None if the table/row is absent or not a timestamp."""
    try:
        row = conn.execute("SELECT value FROM kv WHERE key = ?", (KV_BOT_ALIVE,)).fetchone()
    except sqlite3.Error:
        return None
    return _clean_ts(str(row[0])) if row is not None and row[0] is not None else None


def inspect_snapshot(path, max_db_bytes=MAX_DB_BYTES, timeout_seconds=INSPECT_TIMEOUT_SECONDS):
    """Verify a SQLite file as UNTRUSTED input and summarise it.

    Raises SnapshotRejected for a database that is too large or whose schema is not the ledger's
    (triggers, views, unknown tables/indexes, ...), and SnapshotError if ``PRAGMA integrity_check`` is not
    'ok', the file is unreadable or verification exceeds ``timeout_seconds``."""
    try:
        if os.path.getsize(path) > max_db_bytes:
            raise SnapshotRejected(f"database file is {os.path.getsize(path)} bytes (limit {max_db_bytes})")
        conn = open_readonly(path)
    except (sqlite3.Error, OSError) as exc:
        raise SnapshotError(f"cannot open {path}: {exc}") from exc
    deadline = time.monotonic() + timeout_seconds
    conn.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 10000)
    try:
        try:
            page_count = conn.execute("PRAGMA page_count").fetchone()[0]
            page_size = conn.execute("PRAGMA page_size").fetchone()[0]
            if page_count * page_size > max_db_bytes:
                raise SnapshotRejected(f"database is {page_count * page_size} bytes (limit {max_db_bytes})")
            check_schema(conn)
            rows = conn.execute("PRAGMA integrity_check").fetchall()
        except sqlite3.DatabaseError as exc:
            raise SnapshotError(f"snapshot verification failed: {exc}") from exc
        if [r[0] for r in rows] != ["ok"]:
            raise SnapshotError(f"integrity_check: {[r[0] for r in rows][:3]}")
        try:
            user_version = conn.execute("PRAGMA user_version").fetchone()[0]
            counts = {}
            for table in COUNT_TABLES:
                try:
                    counts[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                except sqlite3.OperationalError as exc:
                    if "interrupted" in str(exc):
                        raise
                    counts[table] = 0                  # table absent in this (older) schema
            try:
                hb = conn.execute("SELECT MAX(ts) FROM events WHERE kind='heartbeat'").fetchone()[0]
            except sqlite3.OperationalError as exc:
                if "interrupted" in str(exc):
                    raise
                hb = None
            alive = read_kv_alive(conn)
        except sqlite3.DatabaseError as exc:
            raise SnapshotError(f"snapshot verification failed: {exc}") from exc
        return DbInfo(int(user_version), counts, hb, alive)
    finally:
        conn.close()


def inspect_db(path):
    """``(user_version, counts, bot_heartbeat_at)`` of a verified snapshot (see ``inspect_snapshot``)."""
    info = inspect_snapshot(path)
    return info.user_version, info.counts, info.bot_heartbeat_at


@dataclass(frozen=True)
class Snapshot:
    path: str
    sha256: str
    size: int
    user_version: int
    counts: dict
    bot_heartbeat_at: Optional[str]
    bot_alive_at: Optional[str] = None


def build_snapshot(db_path, dest_path):
    """Take + verify a snapshot. Raises SnapshotError (and leaves nothing behind) on any problem."""
    if not os.path.exists(db_path):
        raise SnapshotError(f"ledger {db_path} does not exist")
    try:
        take_snapshot(db_path, dest_path)
        info = inspect_snapshot(dest_path)
        return Snapshot(dest_path, sha256_file(dest_path), os.path.getsize(dest_path),
                        info.user_version, info.counts, info.bot_heartbeat_at, info.bot_alive_at)
    except SnapshotError:
        if os.path.exists(dest_path):
            os.unlink(dest_path)
        raise
    except sqlite3.Error as exc:
        if os.path.exists(dest_path):
            os.unlink(dest_path)
        raise SnapshotError(f"snapshot failed: {exc}") from exc


def manifest_for(snap, seq, generated_at):
    return Manifest(
        version=MANIFEST_VERSION, seq=seq, generated_at=iso(generated_at),
        snapshot_key=snapshot_key_for(seq, snap.sha256), sha256=snap.sha256, size=snap.size,
        user_version=snap.user_version, bot_heartbeat_at=snap.bot_heartbeat_at,
        counts=dict(snap.counts), bot_alive_at=snap.bot_alive_at)


def heartbeat_manifest(previous, seq, generated_at, bot_alive_at=None):
    """Same snapshot object as ``previous``, fresh seq/generated_at (the DB did not change).
    ``bot_alive_at`` (a fresher liveness stamp read from the live ledger) replaces the previous one when
    given; otherwise the previous manifest's value is carried over."""
    m = replace(previous, seq=seq, generated_at=iso(generated_at))
    if bot_alive_at is not None:
        m = replace(m, bot_alive_at=_clean_ts(bot_alive_at))
    return m


# --- retention -----------------------------------------------------------------------------------

def prune_snapshots(store, keep=DEFAULT_KEEP, protect=()):
    """Delete all but the newest ``keep`` snapshot objects. Keys in ``protect`` (the manifest's
    target) are never deleted. Returns the list of deleted keys."""
    keep = max(1, int(keep))
    protect = set(protect)
    keys = []
    for key in store.list_keys(SNAPSHOT_PREFIX):
        s = _snapshot_seq(key)
        if s is not None:
            keys.append((s, key))
    keys.sort(reverse=True)
    deleted = []
    for _s, key in keys[keep:]:
        if key in protect:
            continue
        store.delete(key)
        deleted.append(key)
    return deleted

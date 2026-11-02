"""Ledger subscriber, used by the frontend (docs/design-replication.md section 6).

``sync_once`` fetches the tiny manifest first and only downloads the snapshot when it is new. The
synced ledger lives at ``<dest_dir>/ledger.sqlite`` and is replaced atomically (``os.replace``), so a
reader opening it never sees a partial file. On ANY failure the previous good file keeps being served,
the error is recorded in ``<dest_dir>/sync_state.json`` and nothing partial is left in place.

This module imports nothing that needs Kalshi credentials.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from nba_trading.replication.snapshot import (
    MANIFEST_KEY, MANIFEST_VERSION, ManifestError, SnapshotError, Manifest, inspect_db, iso,
    parse_iso, sha256_file, to_utc, utcnow)
from nba_trading.replication.store import MAX_MANIFEST_BYTES, ObjectTooLarge, Store

logger = logging.getLogger(__name__)

LEDGER_NAME = "ledger.sqlite"
STATE_NAME = "sync_state.json"

# Freshness thresholds (seconds) -- section 6 of the design. These are what the dashboard colours by.
FRESH_SECONDS = 60          # age <= 60 s: green (normal worst case is ~30 s)
STALE_SECONDS = 180         # 60 s < age <= 180 s: amber; above: broken (red)
BOT_SILENT_SECONDS = 45 * 60   # fallback rule (no bot_alive_at in the data): newest heartbeat event this old
BOT_ALIVE_SILENT_SECONDS = 180  # kv bot_alive_at (touched every ~60 s) older than this: "bot silent"
SKEW_AHEAD_SECONDS = 60        # clock_skew_estimate above this: backend clock is behind / frontend ahead
SKEW_BEHIND_SECONDS = -10      # below this: backend clock is AHEAD of the frontend (manifest "from the future")

# The newest ledger schema this code understands.
try:
    from nba_trading.ledger import SCHEMA_VERSION as SUPPORTED_USER_VERSION
except Exception:                                  # pragma: no cover - ledger import never needs creds
    SUPPORTED_USER_VERSION = 1


@dataclass(frozen=True)
class SyncResult:
    changed: bool                         # a NEW snapshot file was installed by this call
    seq: Optional[int]                    # seq of the data currently being served (None: nothing yet)
    generated_at: Optional[datetime]
    bot_heartbeat_at: Optional[datetime]
    error: Optional[str]
    checked_at: datetime


@dataclass(frozen=True)
class SyncState:
    checked_at: Optional[datetime]
    last_ok_at: Optional[datetime]
    seq: Optional[int]
    generated_at: Optional[datetime]
    bot_heartbeat_at: Optional[datetime]
    error: Optional[str]
    consecutive_errors: int
    sha256: Optional[str] = None
    user_version: Optional[int] = None
    seq_advanced_at: Optional[datetime] = None   # FRONTEND clock: when we last saw a higher manifest seq
    bot_alive_at: Optional[datetime] = None      # BACKEND clock: kv bot_alive_at in the served snapshot


def ledger_path(dest_dir):
    return os.path.join(str(dest_dir), LEDGER_NAME)


def state_path(dest_dir):
    return os.path.join(str(dest_dir), STATE_NAME)


def _clock(now):
    if now is None:
        return utcnow()
    if isinstance(now, datetime):
        return to_utc(now)
    return to_utc(now())


# --- state file ----------------------------------------------------------------------------------

def read_state(dest_dir) -> Optional[SyncState]:
    """What the dashboard shows. None if nothing has been synced/checked yet (or the file is unreadable)."""
    try:
        with open(state_path(dest_dir), encoding="utf-8") as fh:
            raw = json.load(fh)
        seq = raw.get("seq")
        return SyncState(
            checked_at=parse_iso(raw.get("checked_at")), last_ok_at=parse_iso(raw.get("last_ok_at")),
            seq=int(seq) if seq is not None else None,
            generated_at=parse_iso(raw.get("generated_at")),
            bot_heartbeat_at=parse_iso(raw.get("bot_heartbeat_at")),
            error=raw.get("error"), consecutive_errors=int(raw.get("consecutive_errors") or 0),
            sha256=raw.get("sha256"), user_version=raw.get("user_version"),
            seq_advanced_at=parse_iso(raw.get("seq_advanced_at")),      # absent in old state files
            bot_alive_at=parse_iso(raw.get("bot_alive_at")))
    except (OSError, ValueError, TypeError, AttributeError):
        return None


def _write_state(dest_dir, st):
    payload = {
        "checked_at": iso(st.checked_at) if st.checked_at else None,
        "last_ok_at": iso(st.last_ok_at) if st.last_ok_at else None,
        "seq": st.seq,
        "generated_at": iso(st.generated_at) if st.generated_at else None,
        "bot_heartbeat_at": iso(st.bot_heartbeat_at) if st.bot_heartbeat_at else None,
        "error": st.error, "consecutive_errors": st.consecutive_errors,
        "sha256": st.sha256, "user_version": st.user_version,
        "seq_advanced_at": iso(st.seq_advanced_at) if st.seq_advanced_at else None,
        "bot_alive_at": iso(st.bot_alive_at) if st.bot_alive_at else None,
    }
    path = state_path(dest_dir)
    tmp = f"{path}.tmp-{uuid.uuid4().hex}"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


# --- freshness helpers ---------------------------------------------------------------------------

def data_age_seconds(state, now=None) -> Optional[float]:
    """Seconds since the served data last ADVANCED, measured on the FRONTEND clock only: ``now -
    seq_advanced_at`` (the moment this host saw a higher manifest seq). Immune to backend clock skew and
    to a forged manifest with an absurd seq (a frozen seq ages out). Falls back to ``now - generated_at``
    for a state file written before ``seq_advanced_at`` existed; that fallback is not clamped, so a
    manifest dated in the future yields a negative age, which ``freshness`` never reports as fresh.
    None if never synced."""
    if state is None or state.generated_at is None:
        return None
    advanced = getattr(state, "seq_advanced_at", None)
    if advanced is not None:
        return max(0.0, (_clock(now) - advanced).total_seconds())     # same clock both sides: >= 0 unless it stepped back
    return (_clock(now) - state.generated_at).total_seconds()


def _backend_gap(state, stamp):
    """generated_at - stamp, both on the BACKEND clock (so skew cancels); never negative."""
    return max(0.0, (state.generated_at - stamp).total_seconds())


def heartbeat_age_seconds(state, now=None) -> Optional[float]:
    """Seconds since the newest bot heartbeat event in the served data: data age on the frontend clock
    plus the backend-clock gap ``generated_at - bot_heartbeat_at``. None if there is none."""
    if state is None or state.bot_heartbeat_at is None:
        return None
    age = data_age_seconds(state, now)
    if age is None:
        return None
    return age + _backend_gap(state, state.bot_heartbeat_at)


def bot_alive_age_seconds(state, now=None) -> Optional[float]:
    """Seconds since the bot last touched kv ``bot_alive_at`` (skew-proof, same construction as
    ``heartbeat_age_seconds``). None if the served data carries no bot_alive_at."""
    stamp = getattr(state, "bot_alive_at", None) if state is not None else None
    if stamp is None:
        return None
    age = data_age_seconds(state, now)
    if age is None:
        return None
    return age + _backend_gap(state, stamp)


def clock_skew_estimate(state) -> Optional[float]:
    """``seq_advanced_at - generated_at`` in seconds at the time of observation: frontend clock minus
    backend clock, plus the legitimate publish/sync latency (about 0-25 s). Strongly positive: the
    backend clock is behind; negative: the backend clock is ahead (or the manifest is forged).
    None when unknown (no seq_advanced_at yet)."""
    if state is None:
        return None
    adv = getattr(state, "seq_advanced_at", None)
    if adv is None or state.generated_at is None:
        return None
    return (adv - state.generated_at).total_seconds()


def clock_skew_suspect(state) -> bool:
    """True when the skew estimate is outside [SKEW_BEHIND_SECONDS, SKEW_AHEAD_SECONDS]."""
    est = clock_skew_estimate(state)
    return est is not None and (est > SKEW_AHEAD_SECONDS or est < SKEW_BEHIND_SECONDS)


def freshness(age_seconds) -> str:
    """'fresh' (<= FRESH_SECONDS), 'stale' (<= STALE_SECONDS) or 'broken' (older / unknown / an age that
    is meaningfully negative, i.e. the data claims to come from the future)."""
    if age_seconds is None or age_seconds > STALE_SECONDS or age_seconds < SKEW_BEHIND_SECONDS:
        return "broken"
    return "fresh" if age_seconds <= FRESH_SECONDS else "stale"


def bot_silent(state, now=None) -> bool:
    """True when the bot looks dead although replication may be fine. Uses kv ``bot_alive_at``
    (> BOT_ALIVE_SILENT_SECONDS) when the data carries it, else the newest heartbeat event
    (> BOT_SILENT_SECONDS, or none at all)."""
    if state is not None and getattr(state, "bot_alive_at", None) is not None:
        age = bot_alive_age_seconds(state, now)
        return age is None or age > BOT_ALIVE_SILENT_SECONDS
    age = heartbeat_age_seconds(state, now)
    return age is None or age > BOT_SILENT_SECONDS


# --- sync ----------------------------------------------------------------------------------------

_locks_guard = threading.Lock()
_locks = {}


def _dest_lock(dest_dir):
    key = os.path.abspath(str(dest_dir))
    with _locks_guard:
        return _locks.setdefault(key, threading.Lock())


class _SyncFailure(Exception):
    pass


def sync_once(store: Store, dest_dir, *, now=None) -> SyncResult:
    """One sync pass. Never raises: failures are returned in ``SyncResult.error`` and recorded in
    sync_state.json while the previous good ledger keeps being served."""
    dest_dir = str(dest_dir)
    checked_at = _clock(now)
    with _dest_lock(dest_dir):
        try:
            os.makedirs(dest_dir, exist_ok=True)
        except OSError as exc:
            return SyncResult(False, None, None, None, f"cannot create {dest_dir}: {exc}", checked_at)
        prev = read_state(dest_dir)
        have_file = os.path.isfile(ledger_path(dest_dir))
        try:
            changed, new_state = _sync(store, dest_dir, prev, have_file, checked_at)
            error = None
        except _SyncFailure as exc:
            changed, error = False, str(exc)
            new_state = _failed_state(prev, checked_at, error)
        except Exception as exc:                                  # transport errors etc.
            changed, error = False, f"{type(exc).__name__}: {exc}"[:500]
            new_state = _failed_state(prev, checked_at, error)
        try:
            _write_state(dest_dir, new_state)
        except OSError as exc:
            logger.warning("could not write %s: %s", STATE_NAME, exc)
        if error:
            logger.warning("replication sync failed (%d in a row): %s", new_state.consecutive_errors, error)
        served = new_state if (have_file or changed) else None
        return SyncResult(
            changed=changed,
            seq=served.seq if served else None,
            generated_at=served.generated_at if served else None,
            bot_heartbeat_at=served.bot_heartbeat_at if served else None,
            error=error, checked_at=checked_at)


def _failed_state(prev, checked_at, error):
    if prev is None:
        return SyncState(checked_at, None, None, None, None, error, 1)
    return SyncState(checked_at, prev.last_ok_at, prev.seq, prev.generated_at, prev.bot_heartbeat_at,
                     error, prev.consecutive_errors + 1, prev.sha256, prev.user_version,
                     prev.seq_advanced_at, prev.bot_alive_at)


def _ok_state(checked_at, m: Manifest, prev=None):
    """State after a successful pass. ``seq_advanced_at`` moves to ``checked_at`` only when the manifest
    seq is higher than the stored one; otherwise the previous value is kept (a frozen seq must age)."""
    advanced = prev is None or prev.seq is None or m.seq > prev.seq
    seq_advanced_at = checked_at if advanced else prev.seq_advanced_at
    return SyncState(checked_at, checked_at, m.seq, parse_iso(m.generated_at),
                     parse_iso(m.bot_heartbeat_at), None, 0, m.sha256, m.user_version,
                     seq_advanced_at, parse_iso(m.bot_alive_at))


def _sync(store, dest_dir, prev, have_file, checked_at):
    try:
        raw = store.get_bytes(MANIFEST_KEY, MAX_MANIFEST_BYTES)   # tiny; raises on transport errors
    except ObjectTooLarge as exc:
        raise _SyncFailure(f"manifest is oversized and was ignored: {exc}") from exc
    if raw is None:
        raise _SyncFailure("no manifest in the store yet (the publisher has not published)")
    try:
        m = Manifest.from_json(raw)
    except ManifestError as exc:
        raise _SyncFailure(f"invalid manifest: {exc}") from exc
    if m.version > MANIFEST_VERSION:
        raise _SyncFailure(f"manifest version {m.version} is newer than supported ({MANIFEST_VERSION})")
    if m.user_version > SUPPORTED_USER_VERSION:
        raise _SyncFailure(f"snapshot user_version {m.user_version} is newer than this code "
                           f"understands ({SUPPORTED_USER_VERSION}); upgrade the frontend")

    base = prev if have_file else None                  # no local file: everything counts as new
    local_seq = prev.seq if (prev is not None and have_file) else None
    if local_seq is not None:
        if m.seq == local_seq and m.sha256 == prev.sha256:
            return False, _ok_state(checked_at, m, base)           # nothing new: NO snapshot download
        if m.seq < local_seq:
            raise _SyncFailure(f"manifest seq went backwards ({m.seq} < {local_seq}); "
                               f"keeping the data already synced (delete {dest_dir} to resync)")
        if m.sha256 == prev.sha256:
            return False, _ok_state(checked_at, m, base)           # heartbeat: same snapshot, fresh clock

    tmp = os.path.join(dest_dir, f".download-{uuid.uuid4().hex}.tmp")
    try:
        if not store.get_file(m.snapshot_key, tmp):
            raise _SyncFailure(f"manifest seq {m.seq} points at a missing snapshot {m.snapshot_key}")
        size = os.path.getsize(tmp)
        if size != m.size:
            raise _SyncFailure(f"snapshot size mismatch (got {size}, manifest says {m.size})")
        if sha256_file(tmp) != m.sha256:
            raise _SyncFailure("snapshot sha256 mismatch (corrupt or truncated download)")
        try:
            user_version, _counts, _hb = inspect_db(tmp)
        except SnapshotError as exc:
            raise _SyncFailure(f"snapshot failed verification: {exc}") from exc
        if user_version > SUPPORTED_USER_VERSION:
            raise _SyncFailure(f"snapshot user_version {user_version} is newer than this code "
                               f"understands ({SUPPORTED_USER_VERSION})")
        os.replace(tmp, ledger_path(dest_dir))               # atomic swap
    finally:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass
    return True, _ok_state(checked_at, m, base)


# --- loops ---------------------------------------------------------------------------------------

def run_forever(store: Store, dest_dir, interval=10, stop_event=None, now=None):
    """Sync every ``interval`` seconds until ``stop_event`` is set. Never raises."""
    stop = stop_event or threading.Event()
    while not stop.is_set():
        try:
            sync_once(store, dest_dir, now=now)
        except Exception:                                    # pragma: no cover - sync_once never raises
            logger.exception("replication sync loop error")
        stop.wait(interval)


_threads_guard = threading.Lock()
_threads = {}


def start_background(store: Store, dest_dir, interval=10) -> threading.Thread:
    """Start the sync loop on a daemon thread; idempotent per ``dest_dir`` (a live thread is reused).
    The returned thread has a ``stop_event`` attribute (set it to stop the loop)."""
    key = os.path.abspath(str(dest_dir))
    with _threads_guard:
        t = _threads.get(key)
        if t is not None and t.is_alive():
            return t
        stop = threading.Event()
        t = threading.Thread(target=run_forever, args=(store, dest_dir, interval, stop),
                             name=f"replication-sync:{key}", daemon=True)
        t.stop_event = stop
        _threads[key] = t
        t.start()
        return t

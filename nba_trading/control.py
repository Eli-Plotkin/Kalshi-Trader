"""Remote pause: the dashboard (frontend) can PAUSE the bot, only the backend can RESUME it.

The frontend cannot touch the backend's STOP_TRADING file, so it writes ``control/pause.json`` to
the control bucket. The bot's ``ControlPoller`` reads it every CONTROL_POLL_SECONDS and
``main.kill_switch_active()`` treats a remote pause like the local kill-switch file.

THE PAUSE IS A LATCH (docs/design-replication.md section 11.1). The first time the poller sees
``paused: true`` it creates the local kill-switch file (``config.KILL_SWITCH_FILE``) and writes
``control/ack.json``. After that NOTHING REMOTE can clear it: ``{"paused": false}``, a deleted or
missing key, a corrupt or oversized file all leave the latch in place (a WARNING event
``remote_pause_clear_ignored`` is raised once per transition so tampering is visible). Only an action
on the backend server clears it: ``python -m nba_trading.control resume`` clears the remote key (and
ack) FIRST and removes the local file only if that succeeded. The latch is the local file, so it
survives restarts, crashes and a down bucket. A running bot lets go of an in-memory latch only when
the remote key is no longer paused AND the local file is gone, i.e. after an admin ``resume``.

``store`` below is duck-typed: any object with ``put_bytes(key, data)``, ``get_bytes(key)``
(None only when the key does not exist) and ``delete(key)``, all of which RAISE on transport
errors. Reads pass ``max_bytes=MAX_CONTROL_BYTES`` when the store supports it; an oversized object
counts as unreadable. nba_trading.replication is imported lazily.

Failure policy (fail-safe for money):
- A transport error, an unparseable or an oversized control file is never read as "not paused": the
  poller keeps the last known state, records ``last_error`` and raises an ERROR event after
  ``alert_after`` seconds.
- ``ControlPoller.verified`` is True only after a SUCCESSFUL read in this process. main treats a LIVE
  bot with a control store and an unverified poller as paused (source ``remote_unverified``).
"""

from __future__ import annotations

import argparse
import inspect
import json
import logging
import os
import sys
import threading
import time
from datetime import datetime, timezone

from nba_trading import config

KEY = "control/pause.json"
ACK_KEY = "control/ack.json"
MAX_CONTROL_BYTES = 4096          # fallback; the store module's constant wins when it exists

log = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _max_bytes() -> int:
    try:
        from nba_trading.replication import store as store_module
        return int(getattr(store_module, "MAX_CONTROL_BYTES", MAX_CONTROL_BYTES))
    except Exception:
        return MAX_CONTROL_BYTES


def _get(store, key):
    """Bounded read. Raises ValueError (ObjectTooLarge is one) for an oversized object, transport
    errors propagate, None means the key does not exist."""
    limit = _max_bytes()
    try:
        params = inspect.signature(store.get_bytes).parameters
        bounded = "max_bytes" in params or any(p.kind is p.VAR_KEYWORD for p in params.values())
    except (TypeError, ValueError):
        bounded = False
    raw = store.get_bytes(key, max_bytes=limit) if bounded else store.get_bytes(key)
    if raw is not None and len(raw) > limit:
        raise ValueError(f"{key} is larger than {limit} bytes")
    return raw


def _parse(raw, key):
    try:
        return json.loads(raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else raw)
    except (ValueError, UnicodeDecodeError) as e:
        raise ValueError(f"{key} is not valid JSON: {e}") from e


# ------------------------------------------------------------------ store documents

def request_pause(store, by, reason="") -> None:
    """Ask the bot to stop opening new positions. This is the ONLY call a frontend may make."""
    doc = {
        "paused": True,
        "requested_at": _now_iso(),
        "by": by,
        "reason": reason,
    }
    store.put_bytes(KEY, json.dumps(doc).encode("utf-8"))


def read_state(store):
    """-> ("missing", None) | ("false", doc) | ("paused", doc).

    RAISES on transport errors, on an oversized object and on a corrupt / malformed document
    (ValueError), so the caller can tell "not paused" from "could not tell"."""
    raw = _get(store, KEY)
    if raw is None:
        return "missing", None
    doc = _parse(raw, KEY)
    if not isinstance(doc, dict) or not isinstance(doc.get("paused"), bool):
        raise ValueError(f"{KEY} is malformed (need a JSON object with boolean 'paused')")
    return ("paused" if doc["paused"] else "false"), doc


def read_pause(store):
    """Return the pause document if a pause is requested in the bucket, else None.

    None means: the key does not exist, or it holds ``{"paused": false}``. NOTE this is a view of the
    bucket only: for the BOT a None never un-pauses anything (the poller latches).
    RAISES on transport errors AND on a corrupt / oversized / malformed document (ValueError)."""
    state, doc = read_state(store)
    return doc if state == "paused" else None


def clear_pause(store) -> None:
    """ADMIN ONLY: delete the remote pause. Used by ``python -m nba_trading.control resume`` on the
    backend server. The frontend must never call this (its credential cannot, by design)."""
    store.delete(KEY)


def write_ack(store, pause_requested_at=None, latched=True) -> None:
    """Bot side: tell the dashboard the pause was seen and latched (control/ack.json)."""
    doc = {"acked_at": _now_iso(), "pause_requested_at": pause_requested_at,
           "latched": bool(latched), "by": "bot"}
    store.put_bytes(ACK_KEY, json.dumps(doc).encode("utf-8"))


def read_ack(store):
    """-> the ack document, or None if there is none. Raises on transport errors and on a corrupt,
    oversized or malformed document (ValueError)."""
    raw = _get(store, ACK_KEY)
    if raw is None:
        return None
    doc = _parse(raw, ACK_KEY)
    if not isinstance(doc, dict):
        raise ValueError(f"{ACK_KEY} is malformed (need a JSON object)")
    return doc


def clear_ack(store) -> None:
    """ADMIN ONLY (run by ``resume``): delete control/ack.json."""
    store.delete(ACK_KEY)


# ------------------------------------------------------------------ local latch file

def _one_line(value, limit=200) -> str:
    return " ".join(str(value).split())[:limit]


def write_latch_file(doc=None, source="remote pause") -> bool:
    """Create the local kill-switch file atomically (temp file + rename). An existing file is left
    alone (its original note is the better evidence). Returns True when the file exists afterwards."""
    path = config.KILL_SWITCH_FILE
    if os.path.exists(path):
        return True
    doc = doc or {}
    note = (f"{source} latched at {_now_iso()}\n"
            f"requested_by: {_one_line(doc.get('by', '?'))}\n"
            f"requested_at: {_one_line(doc.get('requested_at', '?'))}\n"
            f"reason: {_one_line(doc.get('reason', ''))}\n"
            f"Remove with: python -m nba_trading.control resume\n")
    tmp = f"{path}.{os.getpid()}.tmp"
    try:
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(note)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        return True
    except OSError:
        log.exception("could not create the local kill-switch file %s", path)
        try:
            os.remove(tmp)
        except OSError:
            pass
        return os.path.exists(path)


# ------------------------------------------------------------------ poller

class ControlPoller:
    """Daemon thread that reads ``control/pause.json`` and LATCHES a pause (see module docstring)."""

    ALERT_COOLDOWN_SECONDS = 1800   # re-raise control_unreachable at most this often

    def __init__(self, store, interval=30, alert_after=300, clock=time.time, on_event=None):
        self._store = store
        self._interval = interval
        self._alert_after = alert_after
        self._clock = clock
        self._on_event = on_event
        self._lock = threading.Lock()
        self._remote_paused = False
        self._verified = False
        self._last_ok_at = None
        self._last_error = None
        self._created_at = clock()
        self._last_alert_at = None
        self._alerted = False
        self._last_state = None          # "paused" | "false" | "missing" | "corrupt" (for once-per-transition)
        self._acked_for = None           # pause.json requested_at the ack was last written for
        self._thread = None
        self._stop = threading.Event()

    # -- state
    @property
    def remote_paused(self) -> bool:
        """True once a remote pause was latched (stays True until an admin resume)."""
        return self._remote_paused

    @property
    def verified(self) -> bool:
        """True after the first SUCCESSFUL control read of this process."""
        return self._verified

    @property
    def last_ok_at(self):
        return self._last_ok_at

    @property
    def last_error(self):
        return self._last_error

    def _emit(self, level, kind, message):
        if self._on_event is None:
            return
        try:
            self._on_event(level, kind, message)
        except Exception:
            log.exception("control on_event callback failed")

    # -- one poll
    def poll_once(self) -> None:
        """Read the control key once and update state. Never raises."""
        try:
            state, doc = read_state(self._store)
        except Exception as e:                       # transport error, corrupt or oversized document
            self._on_failure(e)
            return
        try:
            self._on_success(state, doc)
        except Exception:
            log.exception("control poll bookkeeping failed")

    def first_poll(self, timeout) -> bool:
        """One poll in a helper thread, waiting at most ``timeout`` seconds (a hung store call cannot
        block startup). Returns ``verified``."""
        t = threading.Thread(target=self.poll_once, name="control-first-poll", daemon=True)
        t.start()
        t.join(timeout)
        return self._verified

    def _on_success(self, state, doc):
        now = self._clock()
        with self._lock:
            was_paused = self._remote_paused
            recovered = self._alerted
            self._verified = True
            self._last_ok_at = now
            self._last_error = None
            self._alerted = False
            self._last_alert_at = None
            previous = self._last_state
            self._last_state = state
            if state == "paused":
                self._remote_paused = True
        if recovered:
            log.info("control channel readable again")
            self._emit("INFO", "control_recovered", "remote pause channel readable again")
        if state == "paused":
            if not was_paused:
                msg = (f"remote pause requested by {_one_line(doc.get('by', '?'))} at "
                       f"{_one_line(doc.get('requested_at', '?'))}"
                       f"{': ' + _one_line(doc['reason']) if doc.get('reason') else ''}; no new orders "
                       f"until 'python -m nba_trading.control resume' is run on the server")
                log.critical(msg)
                self._emit("CRITICAL", "remote_pause_requested", msg)
            self._latch(doc)
            return
        # The bucket says "not paused" (key missing or paused:false).
        if not was_paused:
            return
        if os.path.exists(config.KILL_SWITCH_FILE):
            # Latched and the file is still there: the remote key can never clear it.
            if previous != state:
                msg = (f"remote pause key is now {'paused:false' if state == 'false' else 'missing'} but the "
                       f"pause is LATCHED locally and stays in force; only "
                       f"'python -m nba_trading.control resume' on the server clears it")
                log.warning(msg)
                self._emit("WARNING", "remote_pause_clear_ignored", msg)
            return
        # Remote key gone AND the local file gone: an admin ran resume on this server.
        with self._lock:
            self._remote_paused = False
            self._acked_for = None
        log.warning("remote pause cleared by an admin resume")
        self._emit("INFO", "remote_pause_cleared", "remote pause cleared (admin resume)")

    def _latch(self, doc):
        """Make the pause durable: local file first, then the acknowledgement."""
        file_ok = write_latch_file(doc)
        if not file_ok:
            self._emit("ERROR", "kill_switch_file_failed",
                       f"could not create {config.KILL_SWITCH_FILE}; the pause holds in memory only "
                       f"and will NOT survive a restart")
        requested_at = doc.get("requested_at")
        if self._acked_for == requested_at:
            return
        try:
            write_ack(self._store, pause_requested_at=requested_at, latched=file_ok)
            self._acked_for = requested_at
        except Exception as e:                        # retried on the next poll
            log.warning("could not write %s: %s: %s", ACK_KEY, type(e).__name__, e)

    def _on_failure(self, exc):
        now = self._clock()
        err = f"{type(exc).__name__}: {exc}"
        with self._lock:
            self._last_error = err                    # remote_paused deliberately NOT changed
            since = self._last_ok_at if self._last_ok_at is not None else self._created_at
            silent_for = now - since
            due = silent_for >= self._alert_after and (
                self._last_alert_at is None
                or now - self._last_alert_at >= self.ALERT_COOLDOWN_SECONDS)
            if due:
                self._last_alert_at = now
                self._alerted = True
            paused = self._remote_paused
            tampered = (paused and isinstance(exc, ValueError) and self._last_state != "corrupt")
            if isinstance(exc, ValueError):
                self._last_state = "corrupt"
        log.warning(f"control poll failed ({err}); keeping remote_paused={paused}")
        if tampered:
            msg = (f"{KEY} is corrupt or oversized ({err}); the pause is LATCHED locally and stays in force")
            log.warning(msg)
            self._emit("WARNING", "remote_pause_clear_ignored", msg)
        if due:
            self._emit("ERROR", "control_unreachable",
                       f"cannot read {KEY} for {silent_for:.0f}s ({err}); "
                       f"remote pause state is stale (currently {'PAUSED' if paused else 'not paused'})")

    # -- thread
    def start(self) -> None:
        """Start the daemon polling thread (idempotent). Polls immediately, then every interval."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="control-poller", daemon=True)
            thread = self._thread
        thread.start()

    def _run(self):
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception:                         # poll_once should not raise; belt and braces
                log.exception("control poll crashed")
            if self._stop.wait(self._interval):
                break

    def stop(self, timeout=5.0) -> None:
        """Stop the thread and wait for it (idempotent; safe if never started)."""
        self._stop.set()
        with self._lock:
            thread, self._thread = self._thread, None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)


# ------------------------------------------------------------------ store from env

def _env_set(env, *names) -> bool:
    return any((env.get(n) or "").strip() for n in names)


def control_store_from_env(env=None):
    """The control-channel store (docs/design-replication.md section 11.1): the ``CONTROL_*`` variables
    (CONTROL_BUCKET, CONTROL_PREFIX, CONTROL_ENDPOINT, CONTROL_REGION, CONTROL_ACCESS_KEY_ID,
    CONTROL_SECRET_ACCESS_KEY, CONTROL_LOCAL_DIR) when CONTROL_BUCKET or CONTROL_LOCAL_DIR is set, else
    the ``REPLICATION_*`` store. None when neither is configured. Lazily imports the replication store."""
    env = os.environ if env is None else env
    from nba_trading.replication.store import store_from_env
    if _env_set(env, "CONTROL_BUCKET", "CONTROL_LOCAL_DIR"):
        return store_from_env("CONTROL", env)
    return store_from_env("REPLICATION", env)


def store_configured(env=None) -> bool:
    env = os.environ if env is None else env
    return _env_set(env, "CONTROL_BUCKET", "CONTROL_LOCAL_DIR", "REPLICATION_BUCKET", "REPLICATION_LOCAL_DIR")


def build_store():
    """Return the control store described by the environment, or None if none is configured.
    Exceptions from building the store propagate."""
    if not store_configured():
        return None
    return control_store_from_env()


# ------------------------------------------------------------------ CLI

def _local_state() -> str:
    return "PRESENT (paused)" if os.path.exists(config.KILL_SWITCH_FILE) else "absent"


def _cmd_status(store, out) -> int:
    print(f"local latch {config.KILL_SWITCH_FILE}: {_local_state()}", file=out)
    if store is None:
        print("remote pause: not configured (CONTROL_* / REPLICATION_* bucket unset); "
              "local file only", file=out)
        return 0
    rc = 0
    try:
        state, doc = read_state(store)
    except Exception as e:
        print(f"remote pause: UNKNOWN, could not read {KEY}: {type(e).__name__}: {e}", file=out)
        rc = 1
    else:
        if state == "paused":
            print(f"remote pause: PAUSED by {_one_line(doc.get('by', '?'))} at "
                  f"{_one_line(doc.get('requested_at', '?'))} reason={_one_line(doc.get('reason', ''))!r}", file=out)
        elif state == "false":
            print('remote pause: not paused (key holds {"paused": false})', file=out)
        else:
            print("remote pause: not paused (no key)", file=out)
    try:
        ack = read_ack(store)
    except Exception as e:
        print(f"ack: UNKNOWN, could not read {ACK_KEY}: {type(e).__name__}: {e}", file=out)
        return 1
    if ack is None:
        print("ack: none", file=out)
    else:
        print(f"ack: bot acknowledged at {_one_line(ack.get('acked_at', '?'))} "
              f"(pause requested {_one_line(ack.get('pause_requested_at', '?'))}, "
              f"latched={ack.get('latched')})", file=out)
    return rc


def _cmd_pause(store, reason, out) -> int:
    write_latch_file({"by": "cli", "requested_at": _now_iso(), "reason": reason}, source="manual pause")
    if not os.path.exists(config.KILL_SWITCH_FILE):
        print(f"could NOT create the local kill switch {config.KILL_SWITCH_FILE}", file=out)
        return 1
    print(f"local kill switch in place: {config.KILL_SWITCH_FILE}", file=out)
    if store is None:
        print("remote pause: not configured; local file only", file=out)
        return 0
    try:
        request_pause(store, by="cli", reason=reason)
    except Exception as e:
        print(f"remote pause NOT written ({type(e).__name__}: {e}); the local file is in place", file=out)
        return 1
    print("remote pause written", file=out)
    return 0


def _cmd_resume(store, out) -> int:
    """Clear the REMOTE key first (and the ack); remove the local file ONLY if that succeeded."""
    if store is not None:
        try:
            clear_pause(store)
        except Exception as e:
            print(f"remote pause NOT cleared ({type(e).__name__}: {e}); the local latch "
                  f"{config.KILL_SWITCH_FILE} is left in place and the bot stays paused", file=out)
            return 1
        print("remote pause cleared", file=out)
        try:
            clear_ack(store)
        except Exception as e:
            print(f"warning: could not delete {ACK_KEY} ({type(e).__name__}: {e}); harmless but stale", file=out)
    try:
        os.remove(config.KILL_SWITCH_FILE)
        print(f"local kill switch removed: {config.KILL_SWITCH_FILE}", file=out)
    except FileNotFoundError:
        print("local kill switch was not present", file=out)
    except OSError as e:
        print(f"could NOT remove {config.KILL_SWITCH_FILE} ({type(e).__name__}: {e}); the bot stays paused", file=out)
        return 1
    if store is None:
        print("remote pause: not configured; local file only", file=out)
    else:
        print("the bot notices within one poll interval", file=out)
    return 0


def main(argv=None, out=None) -> int:
    out = out or sys.stdout
    parser = argparse.ArgumentParser(prog="python -m nba_trading.control",
                                     description="Local + remote trading pause (run on the backend server)")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="show the local latch, the remote flag and the bot's ack")
    p = sub.add_parser("pause", help="create the local kill switch and write the remote flag")
    p.add_argument("--reason", default="")
    sub.add_parser("resume", help="clear the remote flag, then remove the local kill switch (admin)")
    args = parser.parse_args(argv)
    try:
        store = build_store()
    except Exception as e:
        print(f"cannot build the control store: {type(e).__name__}: {e}", file=out)
        store = None
        if args.cmd == "resume":              # do not half-resume: leave local and remote untouched
            print("refusing to resume: the remote flag could not be cleared", file=out)
            return 2
        if args.cmd == "pause":               # pausing locally is still right; report the failure
            rc = _cmd_pause(None, args.reason, out)
            print("remote pause NOT written (store unavailable)", file=out)
            return rc or 1
    if args.cmd == "status":
        return _cmd_status(store, out)
    if args.cmd == "pause":
        return _cmd_pause(store, args.reason, out)
    return _cmd_resume(store, out)


if __name__ == "__main__":
    sys.exit(main())

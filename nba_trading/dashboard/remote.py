"""Remote-mode plumbing for the dashboard: env, background sync, remote pause.

The dashboard host has no Kalshi credentials and no access to the backend's disk. In remote mode
a subscriber thread copies the latest ledger snapshot from the bucket into DASHBOARD_DATA_DIR and
the dashboard reads `<DASHBOARD_DATA_DIR>/ledger.sqlite` read-only, exactly like the local ledger.

The replication / control modules are written separately and are imported LAZILY inside the
functions below (never at import time), so this module imports cleanly without them and without
boto3. Names used from them:
    nba_trading.replication.store.store_from_env()
    nba_trading.replication.subscriber.start_background(store, dest_dir, interval)
    nba_trading.replication.subscriber.read_state(dest_dir)
    nba_trading.control.control_store_from_env()      # the CONTROL bucket (falls back to REPLICATION_*)
    nba_trading.control.request_pause(store, by=..., reason=...)
    nba_trading.control.read_pause(store)
    nba_trading.control.read_ack(store)               # the bot's acknowledgement (dict or None)
The DATA store (replication) is only ever READ here; the pause goes through the separate CONTROL
store, so the frontend's key for the data bucket can be read-only (docs/design-replication.md 11.1).
NOT used, on purpose: control.clear_pause (resume is admin-only, on the backend server).
"""
from __future__ import annotations

import os
import re
from datetime import datetime, timezone

import streamlit as st

LEDGER_NAME = "ledger.sqlite"
DEFAULT_DATA_DIR = "/tmp/kalshi-dashboard"
DEFAULT_SYNC_SECONDS = 10
MIN_SYNC_SECONDS = 5
PAUSE_BY = "dashboard"
CONTROL_POLL_SECONDS = 30                      # how often the bot reads control/pause.json
ACK_WARN_SECONDS = 2 * CONTROL_POLL_SECONDS    # a requested pause with no ack after this long is a warning
MAX_BY_CHARS = 60
MAX_REASON_CHARS = 200
MAX_TIME_CHARS = 40

RESUME_HELP = ("Resuming trading is intentionally not possible from this dashboard, so a compromised "
               "dashboard cannot start the bot. To resume, run `python -m nba_trading.control resume` "
               "on the backend server (it clears the remote pause flag and the local STOP_TRADING file).")


def get_mode() -> str:
    """'local' (default) or 'remote'. Any other value is an error, never silently 'local'."""
    raw = os.environ.get("DASHBOARD_MODE", "").strip().lower()
    if raw in ("", "local"):
        return "local"
    if raw == "remote":
        return "remote"
    raise ValueError(f"DASHBOARD_MODE must be 'local' or 'remote', got {raw!r}.")


def data_dir() -> str:
    return os.environ.get("DASHBOARD_DATA_DIR", "").strip() or DEFAULT_DATA_DIR


def ledger_path() -> str:
    return os.path.join(data_dir(), LEDGER_NAME)


def sync_seconds() -> int:
    try:
        v = int(float(os.environ.get("DASHBOARD_SYNC_SECONDS", "") or DEFAULT_SYNC_SECONDS))
    except ValueError:
        v = DEFAULT_SYNC_SECONDS
    return max(MIN_SYNC_SECONDS, v)


@st.cache_resource(show_spinner=False)
def _store():
    """One store per process, built from the environment. Raising here means it is not cached."""
    from nba_trading.replication.store import store_from_env
    store = store_from_env()
    if store is None:
        raise RuntimeError("replication is not configured (set REPLICATION_BUCKET, or "
                           "REPLICATION_LOCAL_DIR for a local test)")
    return store


@st.cache_resource(show_spinner=False)
def _start_subscriber(dest_dir: str, interval: int) -> bool:
    """Start the sync thread once per process (per dir/interval). Raising means not cached."""
    from nba_trading.replication.subscriber import start_background
    os.makedirs(dest_dir, exist_ok=True)
    start_background(_store(), dest_dir, interval)
    return True


def ensure_sync_started():
    """Start the background sync if it is not running. Returns None, or an error message.

    Cheap to call on every rerun. A failure (bad config, missing module) is returned, not raised,
    so the page can show it instead of crashing, and is retried on the next rerun.
    """
    try:
        _start_subscriber(data_dir(), sync_seconds())
        return None
    except Exception as exc:  # noqa: BLE001 - shown to the (authenticated) operator
        return f"Could not start the background sync: {type(exc).__name__}: {exc}"


def load_sync_state():
    """The subscriber's last recorded state (see subscriber.read_state), or None."""
    try:
        from nba_trading.replication.subscriber import read_state
        return read_state(data_dir())
    except Exception:  # noqa: BLE001 - treated as "no state yet"
        return None


@st.cache_resource(show_spinner=False)
def _control_store():
    """One CONTROL store per process (CONTROL_* variables, else the REPLICATION_* store)."""
    from nba_trading import control
    store = control.control_store_from_env()
    if store is None:
        raise RuntimeError("remote pause is not configured (set CONTROL_BUCKET / CONTROL_LOCAL_DIR, "
                           "or the REPLICATION_* variables)")
    return store


def plain(value, limit=MAX_REASON_CHARS) -> str:
    """Untrusted text (from pause.json / ack.json) made safe to show: one line, no control
    characters, truncated. Show the result ONLY with st.text / st.code, never st.markdown."""
    text = re.sub(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]+", " ", str(value if value is not None else ""))
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: max(0, limit - 1)] + "\u2026"


def current_pause():
    """(pause_dict_or_None, error_or_None) for the remote pause flag in the CONTROL bucket."""
    try:
        from nba_trading import control
        return control.read_pause(_control_store()), None
    except Exception as exc:  # noqa: BLE001
        return None, plain(f"{type(exc).__name__}: {exc}", 200)


def current_ack():
    """(ack_dict_or_None, error_or_None): the bot's acknowledgement of a pause (control/ack.json)."""
    try:
        from nba_trading import control
        ack = control.read_ack(_control_store())
        if ack is not None and not isinstance(ack, dict):
            return None, "acknowledgement file is malformed"
        return ack, None
    except Exception as exc:  # noqa: BLE001
        return None, plain(f"{type(exc).__name__}: {exc}", 200)


def _parse_time(value):
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def pause_status(pause, pause_error, ack, ack_error, now=None) -> dict:
    """Pure: what the pause panel should say. All text fields are PLAIN TEXT built from untrusted
    documents (already truncated by `plain`); show them with st.text only.

    Returns {"state": unknown | not_requested | requested | latched_only,
             "requested_by", "requested_at", "reason"   (plain text or ""),
             "acked_at", "ack_for_request"               (plain text or ""),
             "ack": unknown | acked | pending | missing | none,
             "warn_no_ack": bool, "lines": [plain text lines]}.
    `latched_only`: the bucket flag is gone/false but the bot still reports a latched pause (only a
    `resume` on the backend clears it).
    """
    now = now or datetime.now(timezone.utc)
    out = {"state": "unknown", "requested_by": "", "requested_at": "", "reason": "", "acked_at": "",
           "ack_for_request": "", "ack": "unknown", "warn_no_ack": False, "lines": []}
    if pause_error:
        out["lines"].append(f"Current remote pause state could not be read ({pause_error}).")
        return out
    pause = pause if isinstance(pause, dict) else None
    requested = bool(pause and pause.get("paused"))
    acked = isinstance(ack, dict) and bool(ack.get("acked_at"))
    if acked:
        out["acked_at"] = plain(ack.get("acked_at"), MAX_TIME_CHARS)
        out["ack_for_request"] = plain(ack.get("pause_requested_at"), MAX_TIME_CHARS)
    if requested:
        out["state"] = "requested"
        out["requested_by"] = plain(pause.get("by") or "?", MAX_BY_CHARS)
        out["requested_at"] = plain(pause.get("requested_at") or "?", MAX_TIME_CHARS)
        out["reason"] = plain(pause.get("reason") or "", MAX_REASON_CHARS)
    elif acked and ack.get("latched"):
        out["state"] = "latched_only"
    else:
        out["state"] = "not_requested"

    if ack_error:
        out["ack"] = "unknown"
        out["lines"].append(f"Bot acknowledgement: unknown ({ack_error}).")
    elif acked:
        out["ack"] = "acked"
    elif requested:
        out["ack"] = "pending"
    else:
        out["ack"] = "none"

    if out["state"] == "requested":
        out["lines"].append(f"requested by {out['requested_by']} at {out['requested_at']}"
                            + (f": {out['reason']}" if out["reason"] else ""))
        if out["ack"] == "acked":
            out["lines"].append(f"acknowledged by bot at {out['acked_at']}"
                                + (f" (for the request at {out['ack_for_request']})"
                                   if out["ack_for_request"] and out["ack_for_request"] != out["requested_at"]
                                   else ""))
        elif out["ack"] == "pending":
            when = _parse_time(pause.get("requested_at"))
            waited = (now - when).total_seconds() if when else None
            out["lines"].append("not yet acknowledged by the bot")
            if waited is None or waited > ACK_WARN_SECONDS:
                out["warn_no_ack"] = True
    elif out["state"] == "latched_only":
        out["lines"].append(f"The bot still holds a pause (acknowledged at {out['acked_at']}"
                            + (f", requested at {out['ack_for_request']}" if out["ack_for_request"] else "")
                            + "); only `resume` on the backend clears it.")
    return out


def request_pause(reason: str = ""):
    """Ask the backend to stop placing new orders. Returns (ok, message)."""
    try:
        from nba_trading import control
        control.request_pause(_control_store(), by=PAUSE_BY, reason=(reason or "").strip()[:MAX_REASON_CHARS])
    except Exception as exc:  # noqa: BLE001
        return False, plain(f"Could not request the pause: {type(exc).__name__}: {exc}", 300)
    return True, ("Pause requested. The bot picks it up within ~30 s and it applies to games it has "
                  "not evaluated yet; resting orders are not cancelled.")

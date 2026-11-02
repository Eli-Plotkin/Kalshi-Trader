"""Push alerts + dead-man's-switch pings for the NBA bot.

HOW IT IS USED (nba_trading/main.py)
------------------------------------
``main._event(level, kind, message)`` calls ``alerts.notify(level, kind, message)`` for every
event after the ledger write (heartbeats excepted), and ``health_job`` calls
``alerts.ping_healthcheck("ok")`` when the balance fetch worked and ``("fail")`` when it did not or
the job raised. ``notify`` filters by ``ALERT_MIN_LEVEL`` (default ERROR, set in
``nba_trading/config.py``), de-duplicates per ``kind`` and never raises or blocks. If the process
dies or hangs the pings stop and Healthchecks.io alerts you (dead-man's switch).
``alerts.flush()`` is called on shutdown so a last alert is not lost.

Configuration (env, empty = disabled; see nba_trading/config.py):
ALERT_NTFY_URL, ALERT_WEBHOOK_URL, ALERT_MIN_LEVEL, ALERT_COOLDOWN_MINUTES,
HEALTHCHECK_URL. With nothing configured every function is a silent no-op.

Safety: network I/O happens on daemon threads with a short timeout, all
exceptions are swallowed and logged, and URLs (which may embed secret topics or
tokens) are never logged in full.
"""

import json
import logging
import threading
import time
from datetime import datetime, timezone

import requests

from nba_trading import config

logger = logging.getLogger(__name__)

LEVELS = {"INFO": 10, "WARNING": 20, "ERROR": 30, "CRITICAL": 40}
_NTFY_PRIORITY = {"INFO": "2", "WARNING": "3", "ERROR": "4", "CRITICAL": "5"}
_NTFY_TAGS = {
    "INFO": "information_source",
    "WARNING": "warning",
    "ERROR": "x",
    "CRITICAL": "rotating_light",
}

TIMEOUT_SECONDS = 5

# Injectable clock (seconds, monotonic-like) for cooldown logic; tests replace it.
_clock = time.monotonic

_lock = threading.Lock()
_last_sent = {}      # kind -> clock() value when last accepted
_threads = []        # live background sender threads (for flush())


# --- helpers ---------------------------------------------------------------

def _level_value(level):
    return LEVELS.get(str(level).strip().upper(), LEVELS["INFO"])


def _norm_level(level):
    name = str(level).strip().upper()
    return name if name in LEVELS else "INFO"


def _redact(url):
    """scheme://host/... -- drops path/query where topics and tokens live."""
    try:
        scheme, rest = str(url).split("://", 1)
        return f"{scheme}://{rest.split('/', 1)[0]}/<redacted>"
    except Exception:
        return "<redacted-url>"


def _safe_exc(exc, *urls):
    text = f"{type(exc).__name__}: {exc}"
    for u in urls:
        if u:
            text = text.replace(u, _redact(u))
    return text


def _cfg(name, default=""):
    value = getattr(config, name, default)
    return value.strip() if isinstance(value, str) else value


def _cooldown_seconds():
    try:
        return max(0.0, float(_cfg("ALERT_COOLDOWN_MINUTES", 15)) * 60.0)
    except (TypeError, ValueError):
        return 15 * 60.0


def _ascii_header(value):
    return str(value).encode("ascii", "replace").decode("ascii").replace("\n", " ").replace("\r", " ")


def _spawn(target, *args):
    """Run target(*args) on a daemon thread; never raises."""
    def runner():
        try:
            target(*args)
        except Exception as exc:  # pragma: no cover - targets already guard
            logger.warning("alert thread failed: %s", type(exc).__name__)

    try:
        t = threading.Thread(target=runner, name="alerts-sender", daemon=True)
        with _lock:
            _threads[:] = [x for x in _threads if x.is_alive()]
            _threads.append(t)
        t.start()
    except Exception as exc:
        logger.warning("could not start alert thread: %s", type(exc).__name__)


def flush(timeout=5.0):
    """Wait (up to `timeout` seconds total) for pending alert threads. For tests/shutdown."""
    deadline = time.monotonic() + timeout
    with _lock:
        pending = list(_threads)
    for t in pending:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        t.join(remaining)


def reset():
    """Clear de-dup state (tests)."""
    with _lock:
        _last_sent.clear()


# --- senders (run on background threads) -----------------------------------

def _send_ntfy(url, level, kind, message):
    try:
        resp = requests.post(
            url,
            data=str(message).encode("utf-8"),
            headers={
                "Title": _ascii_header(f"[{level}] {kind}"),
                "Priority": _NTFY_PRIORITY[level],
                "Tags": _NTFY_TAGS[level],
            },
            timeout=TIMEOUT_SECONDS,
        )
        status = getattr(resp, "status_code", None)
        if isinstance(status, int) and status >= 400:
            logger.warning("ntfy alert rejected (%s) at %s", status, _redact(url))
        else:
            logger.info("ntfy alert sent kind=%s level=%s to %s", kind, level, _redact(url))
    except Exception as exc:
        logger.warning("ntfy alert failed: %s", _safe_exc(exc, url))


def _send_webhook(url, level, kind, message):
    try:
        payload = {
            "level": level,
            "kind": kind,
            "message": str(message),
            "text": f"[{level}] {kind}: {message}",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        resp = requests.post(
            url,
            data=json.dumps(payload),
            headers={"Content-Type": "application/json"},
            timeout=TIMEOUT_SECONDS,
        )
        status = getattr(resp, "status_code", None)
        if isinstance(status, int) and status >= 400:
            logger.warning("webhook alert rejected (%s) at %s", status, _redact(url))
        else:
            logger.info("webhook alert sent kind=%s level=%s to %s", kind, level, _redact(url))
    except Exception as exc:
        logger.warning("webhook alert failed: %s", _safe_exc(exc, url))


def _do_ping(url):
    try:
        requests.get(url, timeout=TIMEOUT_SECONDS)
    except Exception as exc:
        logger.warning("healthcheck ping failed: %s", _safe_exc(exc, url))


# --- public API --------------------------------------------------------------

def notify(level, kind, message):
    """Send a push for an event at/above ALERT_MIN_LEVEL. Never raises, never blocks."""
    try:
        ntfy_url = _cfg("ALERT_NTFY_URL")
        webhook_url = _cfg("ALERT_WEBHOOK_URL")
        if not ntfy_url and not webhook_url:
            return False

        lvl = _norm_level(level)
        if LEVELS[lvl] < _level_value(_cfg("ALERT_MIN_LEVEL", "CRITICAL") or "CRITICAL"):
            return False

        kind = str(kind)
        now = _clock()
        with _lock:
            last = _last_sent.get(kind)
            if last is not None and now - last < _cooldown_seconds():
                return False
            _last_sent[kind] = now

        if ntfy_url:
            _spawn(_send_ntfy, ntfy_url, lvl, kind, message)
        if webhook_url:
            _spawn(_send_webhook, webhook_url, lvl, kind, message)
        return True
    except Exception as exc:
        try:
            logger.warning("notify failed: %s", type(exc).__name__)
        except Exception:
            pass
        return False


def ping_healthcheck(status="ok", url=None):
    """GET the Healthchecks.io-style URL ('/fail' appended for status='fail'). No-op if unset."""
    try:
        base = (url if url is not None else _cfg("HEALTHCHECK_URL")) or ""
        base = str(base).strip()
        if not base:
            return False
        target = base.rstrip("/") + "/fail" if str(status).lower() == "fail" else base
        _spawn(_do_ping, target)
        return True
    except Exception as exc:
        try:
            logger.warning("ping_healthcheck failed: %s", type(exc).__name__)
        except Exception:
            pass
        return False

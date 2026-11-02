"""Pure freshness logic for the remote dashboard: replication state -> banner content.

No Streamlit, no I/O, no imports from the replication package, so it is unit-tested at the
boundaries. `state` is whatever `nba_trading.replication.subscriber.read_state` returns: a
dataclass/object with attributes or a plain dict with the `sync_state.json` fields
    generated_at       BACKEND clock: when the publisher built the snapshot
    seq_advanced_at    FRONTEND clock: when this host last saw the manifest seq go up
    bot_heartbeat_at   BACKEND clock: newest 30-minute heartbeat event in the snapshot
    bot_alive_at       BACKEND clock: the bot's 60-second liveness stamp in the snapshot
    checked_at, last_ok_at, error, consecutive_errors   subscriber bookkeeping (FRONTEND clock)
Timestamps may be datetimes or ISO-8601 strings; naive values are taken as UTC.

The age is SKEW-PROOF (docs/design-replication.md section 11.3): it is measured on the frontend's
own clock from the moment the sequence number last ADVANCED, never as frontend-now minus the
backend's wall clock, so two hosts whose clocks disagree cannot make a dead replication look
healthy. A frozen seq (a forged or stuck manifest) therefore ages out to amber and then red even
when its `generated_at` looks fresh. Only an old state file with no `seq_advanced_at` falls back to
now - generated_at. A negative age is never clamped to hide a problem: a `generated_at` more than
FUTURE_TOLERANCE_SECONDS ahead of the frontend clock, or a skew estimate (seq_advanced_at -
generated_at) above SKEW_HIGH_SECONDS or below SKEW_LOW_SECONDS, forces the level to at least
`stale` with a message saying so.

Thresholds (identical everywhere; docs/design-replication.md section 11.5):
  data age <= 60 s           fresh   (green)
  60 s < age <= 180 s        stale   (amber)
  age > 180 s                broken  (red)
Bot status: `bot_alive_at` (written every 60 s) is preferred: its skew-proof age is
data age + (generated_at - bot_alive_at); above BOT_ALIVE_SILENT_SECONDS (180) the bot is silent.
Only when `bot_alive_at` is absent (an older snapshot) the 45-minute `bot_heartbeat_at` rule is used.
"""
from __future__ import annotations

from datetime import datetime, timezone

FRESH_MAX_SECONDS = 60
STALE_MAX_SECONDS = 180
HEARTBEAT_SILENT_SECONDS = 45 * 60
BOT_ALIVE_SILENT_SECONDS = 180

FUTURE_TOLERANCE_SECONDS = 10     # generated_at / bot_alive_at may lead the reference clock by this much
SKEW_HIGH_SECONDS = 60            # seq_advanced_at - generated_at above this: skew (or a very late publish)
SKEW_LOW_SECONDS = -10            # ... below this: the backend clock is ahead (or generated_at is forged)

LEVEL_FRESH = "fresh"
LEVEL_STALE = "stale"
LEVEL_BROKEN = "broken"
LEVEL_WAITING = "waiting"

HB_OK = "ok"
HB_SILENT = "silent"
HB_UNKNOWN = "unknown"

_MAX_ERROR_CHARS = 300
_LEVEL_ORDER = {LEVEL_FRESH: 0, LEVEL_STALE: 1, LEVEL_BROKEN: 2}


def _field(state, name):
    if state is None:
        return None
    if isinstance(state, dict):
        return state.get(name)
    return getattr(state, name, None)


def _as_dt(value):
    if value is None or value == "":
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _seconds(later, earlier):
    return (later - earlier).total_seconds()


def _fmt_age(seconds):
    s = int(seconds)
    return f"{s} s ago" if s < 120 else f"{s} s ago (~{round(s / 60)} min)"


def _fmt_utc(dt):
    return f"{dt.astimezone(timezone.utc):%Y-%m-%d %H:%M:%S} UTC"


def classify_age(age_seconds):
    """Map a data age in seconds to fresh / stale / broken (inclusive upper bounds)."""
    if age_seconds <= FRESH_MAX_SECONDS:
        return LEVEL_FRESH
    if age_seconds <= STALE_MAX_SECONDS:
        return LEVEL_STALE
    return LEVEL_BROKEN


def _worse(a, b):
    return a if _LEVEL_ORDER[a] >= _LEVEL_ORDER[b] else b


def assess(state, now=None):
    """Turn the replication state into what the banner shows.

    Returns a dict:
      level            fresh | stale | broken | waiting
      message          "Data as of YYYY-MM-DD HH:MM:SS UTC (N s ago)" (+ suffix), or the waiting text
      data_age_seconds skew-proof age in seconds (raw, may be < 0 only when the frontend's own
                       clock is behind the state it recorded -- that is flagged, not hidden), or None
      skew_seconds     seq_advanced_at - generated_at in seconds, or None when not knowable
      skew_warning     None, or the text of the clock-skew / untrusted-timestamp warning
      heartbeat        {"level": ok|silent|unknown, "message": "...", "age_seconds": float|None,
                        "source": "bot_alive_at"|"bot_heartbeat_at"|None}
      error            None, or a message about the last failed sync attempt
    """
    now = _as_dt(now) or datetime.now(timezone.utc)
    generated = _as_dt(_field(state, "generated_at"))
    advanced = _as_dt(_field(state, "seq_advanced_at"))
    last_ok = _as_dt(_field(state, "last_ok_at"))
    hb_at = _as_dt(_field(state, "bot_heartbeat_at"))
    alive_at = _as_dt(_field(state, "bot_alive_at"))
    raw_error = _field(state, "error")
    n_errors = _field(state, "consecutive_errors") or 0

    error = None
    if raw_error:
        text = " ".join(str(raw_error).split())[:_MAX_ERROR_CHARS]
        since = (f" Last good sync {_fmt_age(max(0.0, _seconds(now, last_ok)))}." if last_ok
                 else " There has been no successful sync yet.")
        repeat = f" ({n_errors} failures in a row)" if n_errors > 1 else ""
        error = f"Last sync attempt failed{repeat}: {text}.{since}"

    if generated is None:
        return {
            "level": LEVEL_WAITING,
            "message": "Waiting for the first data from the backend...",
            "data_age_seconds": None,
            "skew_seconds": None,
            "skew_warning": None,
            "heartbeat": {"level": HB_UNKNOWN, "message": "Bot status: unknown (no data yet)",
                          "age_seconds": None, "source": None},
            "error": error,
        }

    # ---- data age: measured on the frontend clock from the last seq advance -----------------------
    reference = advanced if advanced is not None else generated
    age = _seconds(now, reference)
    level = classify_age(age)
    skew = _seconds(advanced, generated) if advanced is not None else None

    problems = []          # reasons the timestamps cannot be trusted
    skew_n = None
    if age < -FUTURE_TOLERANCE_SECONDS:
        # the recorded observation / manifest time is ahead of this host's clock
        problems.append(f"the recorded update time is {int(-age)} s ahead of this host's clock")
        skew_n = int(round(-age))
    if _seconds(generated, now) > FUTURE_TOLERANCE_SECONDS:
        problems.append("the snapshot's timestamp is in the future")
        skew_n = skew_n if skew_n is not None else int(round(_seconds(generated, now)))
    if skew is not None and (skew > SKEW_HIGH_SECONDS or skew < SKEW_LOW_SECONDS):
        skew_n = int(round(skew))
    skew_warning = None
    if problems or (skew is not None and (skew > SKEW_HIGH_SECONDS or skew < SKEW_LOW_SECONDS)):
        skew_warning = (f"clock skew between hosts (about {skew_n} s): age is measured from observed updates")
        level = _worse(level, LEVEL_STALE)

    if age >= -FUTURE_TOLERANCE_SECONDS:
        shown = _fmt_age(max(0, int(age)))           # within the tolerance: rounding noise, shown as 0
    else:
        shown = f"{int(-age)} s in the future"        # never shown as a normal age
    message = f"Data as of {_fmt_utc(generated)} ({shown})"
    if level == LEVEL_BROKEN:
        message += " - DATA IS STALE, do not rely on it"
    elif level == LEVEL_STALE:
        message += " - replication is lagging" if classify_age(age) != LEVEL_FRESH else ""
    if skew_warning:
        message += f" - {skew_warning}"

    # ---- bot status -------------------------------------------------------------------------------
    replication_ok = classify_age(age) == LEVEL_FRESH
    if alive_at is not None:
        lead = _seconds(generated, alive_at)          # backend clock minus backend clock: skew-proof
        if lead < -FUTURE_TOLERANCE_SECONDS:
            heartbeat = {"level": HB_SILENT, "age_seconds": None, "source": "bot_alive_at",
                         "message": "Bot liveness stamp is later than the snapshot it came from - "
                                    "cannot confirm the bot is alive"}
        else:
            alive_age = max(0.0, age) + max(0.0, lead)
            text = f"Bot alive {_fmt_age(alive_age)}"
            if alive_age <= BOT_ALIVE_SILENT_SECONDS:
                heartbeat = {"level": HB_OK, "message": text, "age_seconds": alive_age,
                             "source": "bot_alive_at"}
            else:
                text += (" - replication OK, bot silent" if replication_ok else
                         " - the bot may have stopped (replication is also behind)")
                heartbeat = {"level": HB_SILENT, "message": text, "age_seconds": alive_age,
                             "source": "bot_alive_at"}
    elif hb_at is not None:
        lead = _seconds(generated, hb_at)
        hb_age = max(0.0, age) + max(0.0, lead)
        minutes = int(hb_age // 60)
        if hb_age <= HEARTBEAT_SILENT_SECONDS:
            heartbeat = {"level": HB_OK, "message": f"Bot heartbeat {minutes} min ago",
                         "age_seconds": hb_age, "source": "bot_heartbeat_at"}
        elif replication_ok:
            heartbeat = {"level": HB_SILENT, "age_seconds": hb_age, "source": "bot_heartbeat_at",
                         "message": f"Bot heartbeat {minutes} min ago - replication OK, bot silent"}
        else:
            heartbeat = {"level": HB_SILENT, "age_seconds": hb_age, "source": "bot_heartbeat_at",
                         "message": f"Bot heartbeat {minutes} min ago - the bot may have stopped "
                                    "(replication is also behind)"}
    else:
        heartbeat = {"level": HB_UNKNOWN, "age_seconds": None, "source": None,
                     "message": "Bot status: no liveness stamp or heartbeat in this snapshot"}

    return {"level": level, "message": message, "data_age_seconds": age, "skew_seconds": skew,
            "skew_warning": skew_warning, "heartbeat": heartbeat, "error": error}

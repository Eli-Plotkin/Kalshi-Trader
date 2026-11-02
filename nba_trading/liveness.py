"""Bot liveness beacon, kept OUT of the ledger on purpose.

The bot refreshes a tiny file next to the ledger every ~60 s; the replicator copies the timestamp into
each manifest, so the dashboard can say "bot alive N s ago" / "bot silent". It is a file and not a ledger
row because every ledger commit counts as a data change: writing a row each minute would make the
replicator upload a full snapshot each minute (pointless churn that also shrinks the snapshot history
the 60-snapshot retention window covers to about an hour).

Writes are atomic (temp file + os.replace) so a reader never sees a half-written file.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone

ALIVE_FILENAME = "bot_alive.json"


def alive_path(ledger_path):
    """The beacon lives in the same directory as the ledger (the shared data volume)."""
    return os.path.join(os.path.dirname(os.path.abspath(ledger_path)), ALIVE_FILENAME)


def touch(path, now=None):
    """Record 'the bot is alive right now'. Returns the ISO timestamp written."""
    stamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat(timespec="seconds")
    tmp = f"{path}.tmp-{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"alive_at": stamp}, fh)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    return stamp


def read(path):
    """The last recorded timestamp (ISO string), or None if missing or unreadable. Never raises."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            if os.fstat(fh.fileno()).st_size > 1024:
                return None
            value = json.load(fh).get("alive_at")
        datetime.fromisoformat(value)                      # reject garbage
        return value
    except Exception:
        return None

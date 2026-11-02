"""Restore the ledger from the latest published snapshot (disaster recovery).

    python -m nba_trading.replication.restore --to /app/data/nba_ledger.sqlite [--force]

Use it on a fresh or replacement server before starting the bot. It downloads the snapshot the manifest
points to, VERIFIES it (size, sha256, SQLite integrity_check, schema version), and only then puts it in
place atomically. It will not:
  * overwrite an existing ledger unless --force (and with --force the old file is kept as *.bak-<time>);
  * run while a bot holds the instance lock on that path;
  * install a snapshot that fails verification.

What you lose: ledger rows written after the snapshot (see "snapshot age" in the output; normally a few
seconds). Money state is not at risk: balance, positions, orders and fills live on Kalshi, and the bot
asks Kalshi about every order it has no final answer for when it starts.

IMPORTANT: stop the old server first. Two bots against one Kalshi account is the one thing the
file lock cannot prevent across machines.
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
from datetime import datetime, timezone

from nba_trading.replication import subscriber
from nba_trading.replication.snapshot import read_manifest
from nba_trading.replication.store import store_from_env


class RestoreError(RuntimeError):
    """Restore refused or failed; nothing was installed."""


def restore(store, target, *, force=False, now=None):
    """Install the latest verified snapshot at `target`. Returns a dict describing what was restored."""
    target = os.path.abspath(target)
    try:
        manifest = read_manifest(store)
    except Exception as e:
        raise RestoreError(f"could not read the manifest from the bucket: {e}") from e
    if manifest is None:
        raise RestoreError("the bucket has no latest.json: nothing has ever been published here")

    if os.path.exists(target) and not force:
        raise RestoreError(f"{target} already exists. Refusing to overwrite; pass --force to replace it "
                           f"(the old file is kept as a .bak-<time> copy).")

    from nba_trading.ledger import InstanceLock, InstanceLockError
    lock = InstanceLock(target)
    try:
        lock.acquire()
    except InstanceLockError as e:
        raise RestoreError(f"a bot appears to be running against {target}: {e}") from e
    try:
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="restore-", dir=os.path.dirname(target)) as tmp:
            result = subscriber.sync_once(store, tmp, now=now)       # downloads + verifies, or reports why not
            if result.error or not os.path.exists(subscriber.ledger_path(tmp)):
                raise RestoreError(f"snapshot failed verification or could not be downloaded: "
                                   f"{result.error or 'no file produced'}")
            staged = target + ".restoring"
            shutil.copyfile(subscriber.ledger_path(tmp), staged)
            backup = None
            if os.path.exists(target):
                backup = f"{target}.bak-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
                os.replace(target, backup)
                for ext in ("-wal", "-shm"):                         # stale WAL files must not be replayed onto it
                    if os.path.exists(target + ext):
                        os.replace(target + ext, backup + ext)
            os.replace(staged, target)
    finally:
        lock.release()

    when = result.generated_at
    age = (( (now() if callable(now) else now) or datetime.now(timezone.utc)) - when).total_seconds() if when else None
    return {"target": target, "seq": result.seq, "generated_at": when, "snapshot_age_seconds": age,
            "counts": dict(manifest.counts), "backup": backup, "bot_heartbeat_at": result.bot_heartbeat_at}


def main(argv=None, out=None):
    out = out or sys.stdout
    ap = argparse.ArgumentParser(description="Restore the ledger from the latest published snapshot.")
    ap.add_argument("--to", required=True, help="path of the ledger to create, e.g. /app/data/nba_ledger.sqlite")
    ap.add_argument("--force", action="store_true", help="replace an existing ledger (it is kept as .bak-<time>)")
    args = ap.parse_args(argv)

    store = store_from_env()
    if store is None:
        print("Replication is not configured: set REPLICATION_BUCKET (and keys) or REPLICATION_LOCAL_DIR.", file=out)
        return 2
    try:
        info = restore(store, args.to, force=args.force)
    except RestoreError as e:
        print(f"RESTORE REFUSED: {e}", file=out)
        return 1
    age = info["snapshot_age_seconds"]
    print(f"Restored {info['target']}", file=out)
    print(f"  snapshot #{info['seq']} taken {info['generated_at']}"
          + (f" ({age:.0f} s old: rows written since are lost)" if age is not None else ""), file=out)
    print(f"  rows: {info['counts']}", file=out)
    if info["backup"]:
        print(f"  previous ledger kept as {info['backup']}", file=out)
    print("Next: make sure the OLD server is stopped, then start the bot. It will ask Kalshi about any "
          "order whose outcome the restored ledger does not know.", file=out)
    return 0


if __name__ == "__main__":
    sys.exit(main())

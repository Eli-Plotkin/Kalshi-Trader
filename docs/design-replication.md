# Design: backend/frontend split, ledger replication, remote pause

Status: IMPLEMENTED (2026-10-29); section 11 (revision after an independent review) overrides earlier sections. Everything below is a *contract*; the replication,
control-channel and frontend agents all build against these exact names, shapes and rules.

## 1. Goals (the owner's requirements)

1. The **backend** (bot + ledger) runs on its own server.
2. The **frontend** (Streamlit dashboard) is deployable on a *different* host, with no Kalshi credentials
   and no access to the backend's disk.
3. The data the frontend shows is **stale only on a predictable basis**: never more than ~30 s behind the
   backend while replication is healthy, and the age is always shown. When it is not healthy, the page
   says so loudly.
4. **No lost data** that matters: a backend loss costs at most a few seconds of ledger writes (money
   state lives on Kalshi and is rebuilt on restart; see `design-g4-reconciliation.md`).

## 2. Moving parts

```text
 BACKEND SERVER                                   OBJECT STORAGE (S3-compatible bucket)         FRONTEND HOST
 ┌────────────────────────────┐                  ┌────────────────────────────────────┐        ┌──────────────────────────┐
 │ bot  ──writes──> ledger.db │                  │ snapshots/<seq>-<sha8>.sqlite      │        │ Streamlit dashboard      │
 │        (SQLite, WAL)       │                  │ latest.json   (manifest, the pointer)│       │  + subscriber thread     │
 │ replicator (sidecar) ──────┼── publish ─────> │ control/pause.json (frontend->bot) │ <───── │  syncs every 30 s        │
 │  reads a consistent copy   │                  └────────────────────────────────────┘ sync   │  reads synced ledger     │
 │ bot control poller <───────┼── reads control/pause.json every 30 s                          │  writes control/pause    │
 └────────────────────────────┘                                                                └──────────────────────────┘
```

## 3. Storage interface (`nba_trading/replication/store.py`)

```python
class Store:                                   # abstract
    def put_file(self, key: str, path: str) -> None
    def put_bytes(self, key: str, data: bytes) -> None
    def get_bytes(self, key: str) -> bytes | None          # None only if the key does not exist
    def get_file(self, key: str, dest_path: str) -> bool   # False only if the key does not exist
    def delete(self, key: str) -> None
    def list_keys(self, prefix: str) -> list[str]
class LocalDirStore(Store): ...               # a directory standing in for a bucket (tests, local dev)
class S3Store(Store):                         # boto3, any S3-compatible endpoint (AWS S3, R2, B2, MinIO)
    def __init__(self, bucket, prefix="", endpoint_url=None, region=None,
                 access_key_id=None, secret_access_key=None, client=None)   # `client` injectable for tests
def store_from_env(prefix_env="REPLICATION") -> Store    # see section 8
```
Every method **raises** on transport errors (so callers can tell "not found" from "could not ask").
`put_*` must be atomic from a reader's point of view (LocalDirStore: write temp + `os.replace`).

## 4. Manifest and snapshot (`snapshot.py`)

`latest.json` is the single pointer a reader trusts:

```json
{"version": 1, "seq": 1234, "generated_at": "2026-10-29T18:00:30+00:00",
 "snapshot_key": "snapshots/000001234-ab12cd34.sqlite", "sha256": "<hex>", "size": 123456,
 "user_version": 1, "bot_heartbeat_at": "2026-10-29T17:58:12+00:00",
 "counts": {"decisions": 12, "orders": 0, "settlements": 0, "paper_trades": 3, "events": 77}}
```

- `seq` is strictly increasing across publishes (persisted by the publisher, e.g. derived from the newest
  manifest it can read plus one; it must survive a publisher restart and a restore on a new server).
- `generated_at` is when the snapshot was TAKEN (UTC, from the publisher's clock). `bot_heartbeat_at` is
  the newest `events` row with `kind='heartbeat'` in that snapshot (None if none).
- **Publish order (the correctness rule):** (1) take a consistent copy with the SQLite online backup API
  from a read-only connection, (2) verify it (`PRAGMA integrity_check`), (3) upload the snapshot object
  under its immutable key, (4) only then overwrite `latest.json`. A reader following the manifest can
  therefore never see a missing or half-written snapshot. (5) prune: keep the newest N snapshots
  (default 60) and always the one the manifest points to.
- A snapshot contains the whole ledger EXCEPT nothing is redacted: the bucket holds trade history, so it
  must be private and the frontend's key read-only (section 8).

## 5. Publisher (`publisher.py`, CLI `python -m nba_trading.replication.publisher`)

- Runs as its own process (compose service `replicator`), reading the ledger **read-only** (`mode=ro`); it
  never writes to the ledger and never blocks the bot.
- Loop every `REPLICATION_TICK_SECONDS` (default 5): publish when the database CHANGED since the last
  publish (compare `PRAGMA data_version` on a persistent read connection and/or file mtime/size of the
  `-wal`), at most once per tick; and ALWAYS publish if the last publish is older than
  `REPLICATION_HEARTBEAT_SECONDS` (default 10; see section 11). So: loss window ~5 s while the bot is writing, and the
  frontend's data is never older than ~30 s + upload time even when nothing changes.
- Failures never crash it: log, back off (cap 30 s), keep trying. Track `last_success_at` and
  `consecutive_failures`. After `REPLICATION_ALERT_AFTER_SECONDS` (default 120) without a success, emit an
  alert through the existing alert channel (`nba_trading.alerts.notify("ERROR","replication_stale",...)`)
  at most once per cooldown; send a recovery notice (`INFO`/`WARNING` "replication_recovered").
- Exposes `publish_once(store, db_path, ...) -> Manifest` (pure, testable) and `run_forever(...)`.
- Also writes a tiny status file `REPLICATION_STATUS_FILE` (JSON: last_success_at, consecutive_failures,
  last_error, last_seq) for the container healthcheck.

## 6. Subscriber (`subscriber.py`, used by the frontend)

```python
sync_once(store, dest_dir, *, now=None) -> SyncResult
#   SyncResult: changed: bool, seq: int|None, generated_at: datetime|None, bot_heartbeat_at: datetime|None,
#               error: str|None, checked_at: datetime
run_forever(store, dest_dir, interval=30, stop_event=None)   # never raises; sleeps `interval` between syncs
start_background(store, dest_dir, interval=30) -> threading.Thread   # daemon thread, idempotent per dest_dir
read_state(dest_dir) -> SyncState | None      # what the dashboard shows
```
- Fetch `latest.json` (tiny) first. If its `seq` equals the local one: do NOT download the snapshot
  (this is what keeps transfer cost near zero). If newer: download to a temp file, verify size and
  sha256 from the manifest, `PRAGMA integrity_check`, and that `user_version` is not newer than this
  code understands; then swap into place atomically (`os.replace`) as `<dest_dir>/ledger.sqlite`.
- On ANY failure (network, checksum mismatch, corrupt file, missing snapshot) keep serving the previous
  good file, record the error in `<dest_dir>/sync_state.json`, and never leave a partial file in place.
- `sync_state.json`: `{checked_at, last_ok_at, seq, generated_at, bot_heartbeat_at, error, consecutive_errors}`.
- `data_age_seconds(state, now)` = now - `generated_at`; `heartbeat_age_seconds` = now - `bot_heartbeat_at`.
  These two numbers are what the dashboard shows. Thresholds (also constants): fresh <= 60 s;
  stale 60 s-180 s (amber); broken > 180 s (red) (see section 11.3 for how the age is measured). A fresh snapshot with an old `bot_heartbeat_at`
  (> 45 min) means "replication fine, **bot silent**".

## 7. Remote pause (`nba_trading/control.py`)

The frontend cannot touch the backend's `STOP_TRADING` file, so it writes `control/pause.json` to the
bucket: `{"paused": true, "requested_at": "<iso>", "by": "<label>", "reason": "<text>"}`.

- **Asymmetric on purpose.** The frontend can only PAUSE. Resuming requires an action on the backend
  server (`python -m nba_trading.control resume`, which removes the local file AND clears the remote
  flag), so a compromised dashboard cannot start trading.
- Backend side: `ControlPoller` (daemon thread started by `run_bot`) reads `control/pause.json` every
  `CONTROL_POLL_SECONDS` (default 30) and keeps an in-memory `remote_paused` flag. The bot's existing
  kill-switch check becomes `kill_switch_active()` = local `STOP_TRADING` file exists OR `remote_paused`.
  If the bucket is unreachable the last known state is kept (and an ERROR event is raised if it cannot
  be read for `CONTROL_ALERT_AFTER_SECONDS`, default 300).
- CLI: `python -m nba_trading.control status|pause|resume`.
- The frontend's credential for this is PUT-only on the `control/` prefix; its credential for the data
  is GET-only. They may be the same key if the provider cannot scope them, but the docs must say what to
  scope.

## 8. Configuration (environment variables)

| Variable | Used by | Meaning |
|---|---|---|
| `REPLICATION_BUCKET` | replicator, frontend, bot | bucket name; if unset, replication is disabled |
| `REPLICATION_PREFIX` | all | key prefix inside the bucket (default `kalshi-bot/`) |
| `REPLICATION_ENDPOINT` | all | S3-compatible endpoint (empty = AWS) |
| `REPLICATION_REGION` | all | region (`auto` for R2) |
| `REPLICATION_ACCESS_KEY_ID`, `REPLICATION_SECRET_ACCESS_KEY` | all | credentials (backend: read+write snapshots and read control; frontend: read snapshots + write control) |
| `REPLICATION_LOCAL_DIR` | tests/dev | use a `LocalDirStore` rooted here instead of S3 |
| `REPLICATION_TICK_SECONDS` / `REPLICATION_HEARTBEAT_SECONDS` | replicator | 5 / 10 |
| `REPLICATION_KEEP_SNAPSHOTS` | replicator | 60 |
| `REPLICATION_ALERT_AFTER_SECONDS` | replicator | 120 |
| `REPLICATION_STATUS_FILE` | replicator | path for the status JSON |
| `CONTROL_POLL_SECONDS` / `CONTROL_ALERT_AFTER_SECONDS` | bot | 30 / 300 |
| `DASHBOARD_MODE` | frontend | `local` (read the ledger file directly; default) or `remote` (sync from the bucket) |
| `DASHBOARD_DATA_DIR` | frontend | where the synced ledger lives in remote mode (default `/tmp/kalshi-dashboard`) |
| `DASHBOARD_SYNC_SECONDS` | frontend | 10 |
| `DASHBOARD_PASSWORD` | frontend | required in remote mode; the page shows nothing until it is entered |

## 9. Frontend rules

- Remote mode needs **no** Kalshi key and imports nothing that requires one (`kalshi.config` raises if
  credentials are missing: the dashboard must never import it).
- A password gate (`DASHBOARD_PASSWORD`, constant-time compare, small delay on failure) in front of
  EVERYTHING in remote mode. No password configured in remote mode => refuse to render data.
- A freshness banner at the top: "Data as of HH:MM:SS UTC (N s ago)", colour by the thresholds in section 6,
  and a separate "Bot heartbeat N min ago" status.
- The pause toggle in remote mode writes the control file (section 7) and shows "pause requested, takes
  effect within ~30 s + the next game evaluation"; there is no resume toggle (it explains how to resume on
  the server). In local mode the existing file toggle is unchanged.

## 10. Non-goals / honest limits

- Not synchronous replication: up to ~5 s of ledger writes can be lost if the server dies mid-burst.
- A pause only affects games not yet evaluated; resting orders are not cancelled.
- Real S3/R2/B2 behaviour (consistency, permissions, pricing) and Docker are not testable on the
  development machine; tests use `LocalDirStore` and a fake S3 client and the docs must say so.

---

## 11. Revision after the independent review (2026-10-29) - THIS SECTION OVERRIDES EARLIER SECTIONS

An independent review reproduced several flaws. The corrected rules are below; where they conflict with
sections 3-9, **this section wins**.

### 11.1 The pause is a LATCH, not a flag that mirrors the bucket (fixes F1, F2)
- When the bot's `ControlPoller` first reads `paused: true`, it **latches locally**: it writes the local
  kill-switch file (`config.KILL_SWITCH_FILE`, content = a short note incl. the requester and time).
  From then on ONLY an action on the backend server can clear it (`python -m nba_trading.control resume`,
  which clears the remote key FIRST, and only if that succeeded removes the local file).
- A later `{"paused": false}`, a missing key, a deleted key, a corrupt file or an oversized file NEVER
  clears the latch. (Emit a WARNING event `remote_pause_clear_ignored` the first time a latched pause sees
  the remote key vanish or flip to false, once per transition, so tampering is visible.)
- Restart-safe: the latch is the local file, so it survives restarts, crashes and a down bucket.
- **Fail closed until the first read, in LIVE mode only:** if a control store is configured and the bot is
  live, `kill_switch_active()` is true (source `remote_unverified`) until the first SUCCESSFUL control read
  of this process; a bounded synchronous first poll (<= `CONTROL_FIRST_READ_TIMEOUT_SECONDS`, default 10)
  runs before any game is scheduled/fired. Dry runs are never blocked by this. If the first read keeps
  failing, raise the existing `control_unreachable` ERROR event; do not trade meanwhile.
- The kill switch must be re-checked immediately before `place_limit_order` (not only at the top of the
  evaluation), so a pause that arrives mid-evaluation stops that order.
- **Acknowledgement:** when the poller latches a pause it writes `control/ack.json`
  `{"acked_at": "<iso>", "pause_requested_at": "<iso from pause.json>", "latched": true, "by": "bot"}`.
  `control.read_ack(store) -> dict | None` (None if missing; raises on transport errors). The dashboard
  shows "requested at X / acknowledged by bot at Y" and warns when there is no ack after 2 poll intervals.
  `resume` also deletes `control/ack.json`.
- **Separate control bucket (so the frontend's data key can be read-only):** `control.control_store_from_env()`
  uses `CONTROL_*` variables (`CONTROL_BUCKET`, `CONTROL_PREFIX`, `CONTROL_ENDPOINT`, `CONTROL_REGION`,
  `CONTROL_ACCESS_KEY_ID`, `CONTROL_SECRET_ACCESS_KEY`, `CONTROL_LOCAL_DIR`) when `CONTROL_BUCKET` or
  `CONTROL_LOCAL_DIR` is set, else falls back to the `REPLICATION_*` store. Backend: bot reads/writes it
  (read pause, write ack, delete on resume). Frontend: PUT `control/pause.json`, GET `pause.json`+`ack.json`.
  The DATA bucket's frontend key is then READ-ONLY, which removes the "frontend can overwrite snapshots"
  hole on providers (R2/B2) that cannot scope a key by prefix.
- One prefix everywhere: `store_from_env` treats an empty/whitespace `*_PREFIX` as the default
  (`kalshi-bot/`), never as "no prefix".
- Bounded reads: `Store.get_bytes(key, max_bytes=None)` raises `ObjectTooLarge` (a `ValueError` subclass in
  store.py) when the object is larger than `max_bytes` (S3: check `ContentLength` before reading, then read
  at most `max_bytes + 1`). Caps: `MAX_CONTROL_BYTES = 4096`, `MAX_MANIFEST_BYTES = 65536`. An oversized
  control file is "unreadable" (keeps state, never clears a latch).

### 11.2 Snapshots are untrusted input (fixes F3)
- `inspect_db` (used by the subscriber AND restore) rejects (raises `SnapshotRejected`, a `ValueError`)
  any snapshot whose `sqlite_master` contains a **trigger or view**, or any table/index whose name is not
  in the expected schema (derive the allow-list from a fresh `Ledger(":memory:")`'s `sqlite_master`, plus
  SQLite internals `sqlite_sequence` and `sqlite_autoindex_*`). A snapshot from an OLDER schema version may
  contain a SUBSET of the expected tables/indexes. `user_version` newer than this code understands is
  still rejected.
- Every read-only open of a downloaded/restored/replicated ledger sets `PRAGMA trusted_schema=OFF`
  (and never loads extensions): the publisher's read connection, the subscriber's checks, and
  `dashboard.data.connect_ro`.
- Restore additionally refuses a snapshot that fails the allow-list even with `--force`.

### 11.3 Skew-proof freshness (fixes F5)
- The subscriber records `seq_advanced_at` (FRONTEND clock) in `sync_state.json` whenever it sees a
  manifest `seq` greater than the stored one. `SyncState.seq_advanced_at: datetime | None`.
- `data_age_seconds(state, now)` = `now - seq_advanced_at` when known (else falls back to
  `now - generated_at`). Not clamped to 0 as a way of hiding a problem: it is already >= 0 by construction.
- `heartbeat_age_seconds(state, now)` = `data_age_seconds + (generated_at - bot_heartbeat_at)` (both
  backend-clock timestamps, so the difference is skew-proof).
- New `clock_skew_estimate(state) -> float | None` = `(seq_advanced_at - generated_at)` in seconds at the
  time of observation (legitimately about 0-25 s because of publish/sync intervals). The banner flags
  `> 60` or `< -10` as "clock skew between hosts (about N s)": never green, level at least `stale`.
- The banner shows the DATE as well as the time ("Data as of 2026-10-29 18:00:18 UTC").
- A manifest whose `generated_at` is more than 10 s in the future relative to `seq_advanced_at` is flagged
  as untrusted (same rule via the skew estimate). A manifest with an absurd `seq` cannot keep the banner
  green: freshness follows when `seq` last ADVANCED, and a frozen seq ages out to amber then red.

### 11.4 Bot liveness (fixes F7, dead-bot visibility)
- The bot writes a kv row `bot_alive_at` (UTC ISO) every 60 s (`Ledger.touch_alive()`, scheduled job
  `liveness`), independent of the 30-minute heartbeat event. The publisher copies it into the manifest as
  `bot_alive_at`. The dashboard shows "Bot alive N s ago" (skew-proof: `generated_at - bot_alive_at` plus
  the data age) and says "replication OK, **bot silent**" when that exceeds `BOT_ALIVE_SILENT_SECONDS = 180`.
  The older 45-minute `bot_heartbeat_at` rule remains as a secondary signal.

### 11.5 Honest bounds
- Loss window: about 5 s in steady state; longer while the bucket is unreachable (alert after 120 s).
- Frontend data age: about 10-15 s typical, about 30 s worst case with the shipped defaults
  (heartbeat 10, sync 10, refresh 10), which are `DASHBOARD_SYNC_SECONDS=10` EVERYWHERE including the
  Dockerfile. Thresholds are 60 s (green) / 180 s (amber) everywhere.

### 11.6 Smaller fixes
- `next_seq` with a corrupt/oversized `latest.json` must not fall back to a value below previously
  published heartbeat seqs: derive from the newest `snapshots/` key AND persist/recover the highest seq
  seen (e.g. also write `seq-floor.json` next to the manifest, or probe the manifest history); document
  the choice and test a corrupt-manifest restart.
- Dashboard: pause `by`/`reason` shown as plain text (no markdown/HTML interpretation); logged-out sessions
  cannot re-run fragments (check auth inside EVERY fragment and clear session state on logout);
  process-wide failed-login budget (reject, not just delay, once exceeded within a window).

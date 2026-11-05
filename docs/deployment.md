# Deployment guide: backend server + separate frontend

The system is two deployments joined by a private storage bucket:

* **Backend** (this guide): the trading bot and the ledger replicator on one small always-on server.
  It holds the Kalshi key. It publishes **no ports**.
* **Frontend** ([frontend-deployment.md](frontend-deployment.md)): the dashboard, on any host. It has **no
  Kalshi credentials**; it only reads ledger snapshots from the bucket and can only *request a pause*.

Design details: [design-replication.md](design-replication.md) (the data path),
[system-architecture.md](system-architecture.md) (components, write policies, failure modes),
[design-g4-reconciliation.md](design-g4-reconciliation.md) (how lost orders are recovered).

## Contents
1. [Architecture and guarantees](#1-architecture-and-guarantees)
2. [Compliance](#2-compliance)
3. [Costs](#3-costs)
4. [Provision the backend server](#4-provision-the-backend-server)
5. [Bucket and keys](#5-bucket-and-keys)
6. [Secrets and configuration](#6-secrets-and-configuration)
7. [First run in DRY mode, and verification](#7-first-run-in-dry-mode-and-verification)
8. [Going live](#8-going-live)
9. [Backups, restore and the data-loss guarantee](#9-backups-restore-and-the-data-loss-guarantee)
10. [Pause, kill switch and resume](#10-pause-kill-switch-and-resume)
11. [Monitoring and alerts](#11-monitoring-and-alerts)
12. [Update, redeploy, roll back](#12-update-redeploy-roll-back)
13. [Environment variable reference](#13-environment-variable-reference)
14. [What has and has not been verified](#14-what-has-and-has-not-been-verified)

## 1. Architecture and guarantees

```text
 BACKEND SERVER  (no inbound ports except SSH)       DATA BUCKET (private)                    FRONTEND HOST
 ┌────────────────────────────────────────┐        ┌──────────────────────────────┐        ┌──────────────────────┐
 │ bot ──writes──▶ nba_ledger.sqlite (WAL)│        │ snapshots/<seq>-<sha>.sqlite │        │ dashboard (Streamlit)│
 │   └─ refreshes bot_alive.json every 60s│        │ latest.json   (the pointer)  │◀─READ──│  pulls every 10 s    │
 │ replicator ──reads read-only, publishes│───────▶│ seq-floor.json               │  ONLY  │  password-gated      │
 │   within ~5 s of a change; manifest    │        └──────────────────────────────┘        │                      │
 │   heartbeat every 10 s                 │        CONTROL BUCKET (private, separate)      │                      │
 │ bot polls control/pause.json every 30s │◀───────│ control/pause.json  (a REQUEST)│◀─PUT──│ "Request pause"      │
 │   LATCHES a pause locally; writes ack  │───────▶│ control/ack.json               │─GET──▶│  shows the ack       │
 └────────────────────────────────────────┘        └──────────────────────────────┘        └──────────────────────┘
 Kalshi is the source of truth for balance, positions, orders and fills; the bot rebuilds from it.
```

| Guarantee | Value | How |
|---|---|---|
| Frontend data age (healthy) | typically under 10-15 s; worst case about 30 s | manifest heartbeat 10 s + frontend sync 10 s + page refresh 10 s |
| Banner colours | green <= 60 s, amber <= 180 s, red beyond | age = time since the frontend last SAW a newer manifest, measured on the frontend's own clock, so a skewed backend clock cannot make it look fresher than it is |
| Ledger rows lost if the whole server is lost | about 5 s of writes in steady state | a snapshot is published within ~5 s of any change. During a bucket outage the window grows until it recovers (an alert fires after 2 minutes) |
| Money lost to a ledger gap | none by itself | balance, orders, positions and fills live on Kalshi; an order with no final answer is looked up by its client id on restart, and never re-sent |
| Dead bot visible | within about 3 minutes | the bot refreshes a beacon file every 60 s; the banner says "replication OK, bot silent" when it is older than 180 s |
| A pause cannot be undone by the frontend | enforced | the bot latches a requested pause in a local file; only `control resume` on the server clears it |

Measured in a multi-process drill (writer, replicator and subscriber as separate processes, production timings, a pause attacked from the "frontend" side, then a hard `SIGKILL` of everything and a restore): data age max 8.5 s,
the pause held through an attacker's `paused:false`, a deleted key and a restart with the bucket unreachable, and
0-6 rows (under 2 s of writes) were lost. This is a local-folder drill, not a real bucket; see section 14.

## 2. Compliance

Read this before trading real money.

* Kalshi API trading requires the account holder to be in an **eligible location** and to
  **complete Kalshi's own location/eligibility verification**. This repository does not and
  cannot do that for you.
* Moving the bot to a hosted server changes where API requests originate. **You** must confirm
  with Kalshi (support / their terms and API documentation) how their eligibility and
  location rules apply to API trading from a hosted server, and proceed only if that is
  permitted for your account.
* Do **not** use VPNs, proxies, residential-IP services, or any hosting trick to make traffic
  appear to come from a different location than where you actually are. That is out of scope
  here and would likely violate Kalshi's terms.
* The account must be operated by its owner. Do not run this bot on someone else's account or
  share the API key.
* This software is not financial advice and the strategy is not a proven edge. Dry-run first.

## 3. Costs

Estimates from list prices; **check your provider's current pricing**.

| Item | Rough monthly cost |
|---|---|
| Small VPS (1 vCPU / 1-2 GB) for the backend | about $4-6 |
| Bucket requests: a manifest write every 10 s (~260k PUTs) | about $1-1.5 on S3 or R2; B2 is cheaper |
| Frontend polling the manifest every 10 s (~260k small GETs) | about $0.10 |
| Snapshot uploads and downloads | pennies (the ledger is well under a few MB; downloads happen only when the data changed) |
| Storage (60 snapshots kept) | pennies |
| Frontend host | free tier to about $5 |

The expensive mistake would be downloading a large database every few seconds on a provider that charges
for egress (S3 is roughly $0.09/GB). The subscriber avoids it: it fetches the tiny manifest first and downloads
the snapshot only when it changed. Cloudflare R2 has no egress fee.

## 4. Provision the backend server

Any small Ubuntu 22.04/24.04 VPS. On the new server, as root:

```bash
git clone <your repo> /opt/kalshi/app && cd /opt/kalshi/app
sudo bash deploy/bootstrap-vps.sh
```

`deploy/bootstrap-vps.sh` is idempotent: it patches the OS and enables automatic security updates, installs
Docker and the compose plugin, installs **chrony** and sets UTC (Kalshi request signatures are timestamped and the bot
refuses to start live if the clock is more than 30 s off), turns the firewall on with **only SSH open**, and creates
`/opt/kalshi/secrets` (mode 700, owned by uid 10001, the container user). It never touches secrets. Note it runs
`ufw --force reset`: run it on a fresh server, and keep your SSH session open until you confirm you can reconnect.

## 5. Buckets and keys

Create **two private** S3-compatible buckets (Cloudflare R2 is a good default: no egress fees; AWS S3, Backblaze B2 and
MinIO also work). They hold your trade history and your kill switch, so never make either public.

Why two: R2 and B2 scope an API token to a whole **bucket**, not to a key prefix. Separate buckets let the frontend's
data key be genuinely read-only, so a leaked frontend credential can never overwrite a snapshot (and, through a restore,
your ledger).

| Bucket | Backend key (bot, replicator) | Frontend key |
|---|---|---|
| **Data** (`REPLICATION_*`): snapshots, `latest.json` | read + write + delete | **read-only** |
| **Control** (`CONTROL_*`): `pause.json`, `ack.json` | read + write + delete | PUT `pause.json`; GET `pause.json` and `ack.json`; nothing else |

Leaving `CONTROL_BUCKET` blank reuses the data bucket; that only works safely if your provider can scope the frontend key by
prefix and operation. Whatever you choose, restore and the dashboard both **validate every snapshot** before using it
(size, checksum, integrity, schema allow-list: a snapshot carrying triggers, views or unexpected tables is rejected),
but a read-only data key is the real protection.

What a leaked **frontend** key or dashboard password can do: read the ledger and request a pause. It cannot resume
trading, place or cancel orders, or read your Kalshi key.

## 6. Secrets and configuration

```bash
cd /opt/kalshi/app
# Kalshi private key: outside the image, mounted read-only, owned by the container user.
install -m 600 -o 10001 -g 10001 /path/to/your/key.pem /opt/kalshi/secrets/kalshi_private_key.pem
cp .env.example .env && chmod 600 .env && $EDITOR .env
```

In `.env` set at least: `API_KEY_ID`, `KALSHI_ENV` (start with `demo` if you can), `BOT_MODE=dry`, the
`STRATEGY_*` values, `KALSHI_PRIVATE_KEY_FILE=/opt/kalshi/secrets/kalshi_private_key.pem`, and the
`REPLICATION_*` (data) and `CONTROL_*` (control) bucket settings. Optional: `HEALTHCHECK_URL`, `ALERT_NTFY_URL` (section 11).

Secrets never go in the image or in git. `.env`, `.env.*` (except the two templates), `*.pem` and `*.key` are git-ignored.

## 7. First run in DRY mode, and verification

```bash
docker compose up -d --build        # bot + replicator; BOT_MODE=dry means no orders can be placed
docker compose ps                   # every service should become "healthy"
docker compose logs -f bot replicator
```

Then verify, in this order:

1. **Kalshi reads** (read-only, places nothing):
   `docker compose run --rm --no-deps --entrypoint python bot -m kalshi.preflight`
2. **Replicator is publishing**: `docker compose logs replicator` shows successful publishes, and the bucket
   contains `latest.json` and `snapshots/`. `docker compose ps` shows the replicator healthy.
3. **Frontend sees it**: deploy the frontend ([frontend-deployment.md](frontend-deployment.md)); within about
   10-15 s the banner turns green with "Data as of ... UTC".
4. **A pause round trip**: press *Request pause* on the frontend; within about 30 s the dashboard shows the bot's
   acknowledgement and `docker compose exec bot python -m nba_trading.control status` shows the latch; then resume it on the
   server (section 10). If no acknowledgement appears after a minute, the pause is NOT being delivered: check the control
   bucket settings.
5. **A restore drill** (section 9) onto a scratch path, before you rely on backups.

## 8. Going live

Only proceed when **all** of these are true:

- [ ] You, the account holder, are in an eligible location and have **completed Kalshi's
      location/eligibility verification** ([Compliance](#2-compliance)), and have confirmed with
      Kalshi how this applies to API trading from a hosted server.
- [ ] Preflight passes against the environment you will trade on, ideally including a demo-environment
      test order (`KALSHI_ENV=demo python -m kalshi.preflight --place-test-order`).
- [ ] Several nights of DRY results look sane in the dashboard.
- [ ] A restore drill succeeded, a pause round trip worked, and alerts have fired a test notification.
- [ ] `STRATEGY_*` limits (sizing, drawdown, exposure) are values you accept losing. Consider starting at 1-2%
      sizing: the first live fills are also the first time the real field names in Kalshi's fill/order
      responses are exercised.

Then in `.env`:

```ini
KALSHI_ENV=prod
BOT_MODE=live
BOT_LIVE_CONFIRM=LIVE
```

```bash
docker compose up -d bot          # recreates only the bot
docker compose logs -f bot        # "LIVE — REAL MONEY"; the startup event says mode=LIVE
```

`--live` normally asks you to type `LIVE`. A container has no terminal, so the bot accepts
`BOT_LIVE_CONFIRM=LIVE` from the environment as the explicit equivalent. `deploy/entrypoint-bot.sh` refuses to start
live unless both `BOT_MODE=live` and `BOT_LIVE_CONFIRM=LIVE` are set. To go back to dry mode set `BOT_MODE=dry`,
clear `BOT_LIVE_CONFIRM`, and `docker compose up -d bot`.

Tip: for the first live night, pause first (section 10), confirm the dashboard shows the pause, then resume while you watch.

## 9. Backups, restore and the data-loss guarantee

**The replicator is the backup.** Every change to the ledger is published to the bucket within about 5 s, and the
newest 60 snapshots are kept. Each snapshot is a complete, verified SQLite file (taken with SQLite's online backup,
integrity-checked before upload, and the pointer `latest.json` is only written after its snapshot).

What is lost if the server dies: ledger rows written after the last snapshot (about 5 s). What is **not** lost:
balance, positions, orders and fills (Kalshi has them). On restart the bot asks Kalshi about every order whose
outcome the restored ledger does not know, using the fixed client order id, and never re-sends.

### Restore onto a new (or wiped) server

1. **Stop the old server first.** Two bots on one Kalshi account is the one thing the file lock cannot prevent
   across machines.
2. Provision the new server (section 4), clone the repo, restore `.env` and the key file (section 6).
3. Restore the ledger from the bucket:

   ```bash
   docker compose run --rm --no-deps --entrypoint python bot \
     -m nba_trading.replication.restore --to /app/data/nba_ledger.sqlite
   ```

   It verifies the snapshot (size, sha256, integrity, schema version) before installing it, refuses to overwrite an
   existing ledger unless `--force` (the old one is kept as `.bak-<time>`), refuses while a bot holds the lock, and
   prints how old the snapshot is.
4. `docker compose up -d` (start in `BOT_MODE=dry` first if in any doubt), then check the bot log for lookups of any
   in-flight order and confirm positions and balance on the dashboard.

Do a restore drill onto a scratch path (`--to /tmp/ledger-test.sqlite`) before you need it for real.

### Optional second layer: Litestream

`docker compose --profile litestream up -d litestream` adds continuous replication of the raw database file (about a
1-second window) to a bucket path of your choice (`LITESTREAM_*` variables). It is optional, untested here, and not
needed to meet the ~5 s figure above.

## 10. Pause, kill switch and resume

| Action | How | Takes effect |
|---|---|---|
| Pause from the dashboard | *Request pause* (writes `control/pause.json` in the control bucket) | the bot polls it every 30 s and **latches** it; applies to every game **not yet evaluated** (and is re-checked right before an order is sent) |
| Pause on the server | `docker compose exec bot python -m nba_trading.control pause --reason "..."` or `touch` the file `STOP_TRADING` on the data volume | the next game evaluation |
| Check state | `docker compose exec bot python -m nba_trading.control status` (local latch, remote key, acknowledgement) | |
| **Resume** | **only on the server:** `docker compose exec bot python -m nba_trading.control resume` (clears the remote key first, and only if that worked removes the local latch file) | the next game evaluation |

How the latch protects you: when the bot first reads a pause it writes the local `STOP_TRADING` file and an
acknowledgement. From then on nothing remote can clear it: a later `{"paused": false}`, a deleted key, a corrupt or
oversized file are all ignored (a `remote_pause_clear_ignored` warning is logged so tampering is visible). Because the latch
is a file it survives restarts and a bucket outage. A stolen dashboard password or frontend key can stop trading but never start it.

**Live mode fails closed until the control channel has been read once.** If a control bucket is configured and
`BOT_MODE=live`, the bot refuses to place orders until its first successful read of the control file after each start
(it waits up to 10 s at startup, then keeps trying; a successful read reopens trading by itself). A broken control bucket
configuration therefore blocks live trading rather than silently disabling the pause. Dry runs are never blocked.

A pause does not cancel resting orders (they expire at tip-off) and does not stop reconciliation or settlement. If the bot
cannot read the control file after it has verified once, it keeps the last known state.

## 11. Monitoring and alerts

Set in `.env` (unset = disabled): see the reference table. The alert module is called for every ledger event at or above
`ALERT_MIN_LEVEL` (default `ERROR`); the same alert kind is de-duplicated for `ALERT_COOLDOWN_MINUTES`.

* **Dead-man's switch**: create a Healthchecks.io check with **Period 30 minutes, Grace 20 minutes** and put its ping URL
  in `HEALTHCHECK_URL`. The health job (every 30 min) pings `ok` when the balance fetch worked and `/fail` when it did not
  or the job raised; if pings stop, you are told the bot or host is dead.
* **Replication**: the replicator alerts `replication_stale` (ERROR) after 2 minutes without a successful publish, repeating
  every 30 minutes, and `replication_recovered` (WARNING, so set `ALERT_MIN_LEVEL=WARNING` if you want recovery notices).
  The frontend banner independently turns amber/red.
* **Push**: `ALERT_NTFY_URL` (treat the topic name as a secret) and/or `ALERT_WEBHOOK_URL`.
* Also: `docker compose ps` health, disk space (`df -h`), and `docker compose logs -f bot replicator`.

Test alerts once by setting `ALERT_MIN_LEVEL=INFO` briefly, or `docker compose stop replicator` and wait for the
`replication_stale` alert and the amber banner.

## 12. Update, redeploy, roll back

```bash
cd /opt/kalshi/app
git fetch && git log --oneline HEAD..origin/main     # review what is coming
git pull --ff-only
export IMAGE_TAG=$(git rev-parse --short HEAD)       # also put IMAGE_TAG=... in .env to persist
docker compose build && docker compose up -d         # recreates changed services only
docker compose ps && docker compose logs --tail=50 bot replicator
```

Prefer deploying between game nights. A restart mid-night recovers the saved schedule (which games already fired),
asks Kalshi about any order whose outcome it never learned, and re-queues the post-tip fill checks. Positions are
reconciled against Kalshi inside each live game evaluation and every 30 minutes by the health job, not at startup.
Avoid restarting close to tip-off. An OS lock stops two bots on the **same** host and volume only; it cannot stop
a second copy on another server, so stop the old one first.

**Roll back**: images are tagged by commit, so `IMAGE_TAG=<previous-sha> docker compose up -d --no-build`.
The ledger schema is versioned (`PRAGMA user_version`) and migrates forward in place when the bot starts; an older
image opens a newer database but ignores columns it does not know. If a release ever changes a column in a way old
code cannot read, restore the snapshot taken before upgrading (section 9) instead of rolling code back alone.

**Stop everything**: `docker compose down` (keeps the volume). `docker compose down -v` deletes the volume and local
ledger; never run it unless you have verified a restore.

## 13. Environment variable reference

Backend `.env` (see `.env.example` for comments):

| Variable | Purpose |
|---|---|
| `API_KEY_ID`, `KALSHI_PRIVATE_KEY_FILE`, `KALSHI_ENV` | Kalshi credentials and environment (`demo` or `prod`) |
| `BOT_MODE` (`dry`/`live`), `BOT_LIVE_CONFIRM` | live trading needs both `live` and `LIVE` |
| `STRATEGY_PRICE_MIN/MAX`, `STRATEGY_BANKROLL_FRACTION`, `STRATEGY_LIMIT_BUFFER_CENTS`, `STRATEGY_MAX_DRAWDOWN_PCT`, `STRATEGY_MAX_EXPOSURE_PCT` | strategy and risk limits |
| `REPLICATION_BUCKET`, `_PREFIX`, `_ENDPOINT`, `_REGION`, `_ACCESS_KEY_ID`, `_SECRET_ACCESS_KEY` | the private bucket (required by `replicator`) |
| `CONTROL_BUCKET`, `_PREFIX`, `_ENDPOINT`, `_REGION`, `_ACCESS_KEY_ID`, `_SECRET_ACCESS_KEY` | the separate control bucket (blank bucket = reuse the data bucket) |
| `REPLICATION_TICK_SECONDS` (5), `REPLICATION_HEARTBEAT_SECONDS` (10), `REPLICATION_KEEP_SNAPSHOTS` (60), `REPLICATION_ALERT_AFTER_SECONDS` (120) | replicator tuning |
| `CONTROL_POLL_SECONDS` (30), `CONTROL_ALERT_AFTER_SECONDS` (300), `CONTROL_FIRST_READ_TIMEOUT_SECONDS` (10) | how often the bot reads the remote pause, when it alerts if it cannot, and how long live startup waits for the first read |
| `HEALTHCHECK_URL`, `ALERT_NTFY_URL`, `ALERT_WEBHOOK_URL`, `ALERT_MIN_LEVEL`, `ALERT_COOLDOWN_MINUTES` | monitoring |
| `IMAGE_TAG` | tag for rollbacks |
| `LITESTREAM_*` | only if you enable the optional Litestream profile |

Frontend variables are listed in [frontend-deployment.md](frontend-deployment.md) and `.env.frontend.example`.

## 14. What has and has not been verified

| Verified here | Not verified here (do these on the real server) |
|---|---|
| Publisher, subscriber, restore, control poller and dashboard against a local-folder "bucket" and a fake S3 client; about 1,420 automated tests | **Docker**: no image has ever been built (the dev Mac has no container runtime). The first `docker compose up --build` on the server, or the CI `docker-build` job, is the first real build. Image behavior is not covered by the simulation below: the non-root uid 10001, volume ownership via `init`, `cap_drop`, `mem_limit`, the compose healthchecks as run by Docker, and the `curl` HEALTHCHECK |
| Two independent security/reliability reviews, with every confirmed defect reproduced, fixed and covered by a regression test (the frontend credential could resume trading; a restart could forget a pause; a restored snapshot could carry triggers; clock skew could make the banner lie) | A real S3/R2/B2 bucket: token scoping, consistency, error codes, request costs |
| A multi-process drill with a hard `SIGKILL`: data age, loss window, restore, and the pause latch under attack and restart | `bootstrap-vps.sh` has never been run; `shellcheck` is not installed here |
| Compose YAML parses; bootstrap script passes `bash -n`; CI workflow YAML parses | Real order, fill and position payloads from Kalshi: field names are from documentation; the first real fills must be checked |
| Image contents simulated in clean Python 3.13 venvs: `requirements-backend.txt` / `requirements-frontend.txt` alone, copying only what each Dockerfile copies. The real `entrypoint-bot.sh` (including the `BOT_MODE=live` refusal without `BOT_LIVE_CONFIRM=LIVE`), the bot (dry), the replicator and the Streamlit dashboard (remote mode, login, render, `/_stcore/health`) all ran together against a local-folder bucket | |
| Kalshi read endpoints against the real (empty) account | Kalshi's location verification for API trading from a hosted server |

Known limits, accepted for now (none lets the frontend trade):

* The data-loss figure is ~5 s only while the bucket is reachable.
* A dashboard restarted with an empty data directory can show green for up to a minute on an old snapshot before aging to amber/red.
* The login budget is process-wide, so repeated wrong passwords can lock the owner out for a few minutes (the owner can restart the dashboard).
* The bot container receives the backend bucket keys through `.env`; it only strictly needs the control bucket.
* The replicator uploads a full snapshot each time the ledger really changes (a few times a minute in a busy game window, rarely otherwise); the bot-alive beacon is a separate file precisely so it does not cause uploads.

# Deployment guide

Run the NBA bot, the dashboard, and continuous ledger backups on one small always-on VPS
(about $5/month: Hetzner, DigitalOcean, Lightsail, Fly.io machines, etc.) using Docker Compose.

> The bot is **dry-run by default**. Real orders require two deliberate settings
> (`BOT_MODE=live` and `BOT_LIVE_CONFIRM=LIVE`) and should only be turned on after the
> steps in [Compliance](#compliance) and [Going live](#6-going-live).

## Contents

1. [Architecture](#architecture)
2. [Compliance](#compliance)
3. [Provision the VPS](#1-provision-the-vps)
4. [Install Docker and time sync](#2-install-docker-and-time-sync)
5. [Place secrets and configuration](#3-place-secrets-and-configuration)
6. [First run in DRY mode](#4-first-run-in-dry-mode)
7. [Verify with the preflight](#5-verify-with-the-read-only-preflight)
8. [Going live](#6-going-live)
9. [Reaching the dashboard](#7-reaching-the-dashboard-privately)
10. [Backups with Litestream](#8-backups-with-litestream)
11. [Restore runbook](#9-restore-runbook)
12. [Monitoring and alerts](#10-monitoring-and-alerts)
13. [Update, redeploy, roll back](#11-update-redeploy-roll-back)
14. [Kill switch](#12-kill-switch)
15. [Environment variable reference](#environment-variable-reference)

## Architecture

```
                         Internet
                             |
        outbound HTTPS only  |   (no inbound ports except SSH / Tailscale)
   +-------------------------+----------------------------------------+
   | VPS (Docker Compose, project "kalshi")                           |
   |                                                                  |
   |  +-----------+   writes    +--------------------------------+    |
   |  |   bot     |------------>|  named volume  kalshi_data     |    |
   |  | (uid10001)|   reads     |  /app/data                     |    |
   |  | dry|live  |<----------- |   nba_ledger.sqlite (+ -wal)   |    |
   |  +-----+-----+  kill file  |   STOP_TRADING (kill switch)   |    |
   |        |                   |   (positions, schedule, peak   |    |
   |        |                   |    live INSIDE the sqlite db)  |    |
   |        |                   |   trade_bot.log                |    |
   |        |                   +--^----------^-------------^----+    |
   |        |                      |ro read   |rw toggle    | WAL      |
   |        |               +------+----------+---+  +------+------+   |
   |        |               |   dashboard         |  | litestream  |   |
   |        |               |   streamlit :8501   |  | replicate   |   |
   |        |               +----------+----------+  +------+------+   |
   |        |                          |                    |          |
   +--------|--------------------------|--------------------|----------+
            |                          | 127.0.0.1:8501     |
            v                          v  (SSH tunnel /     v
   Kalshi API (signed requests)     Tailscale / CF Access)  S3-compatible
   + ntfy / webhook / Healthchecks.io                       object storage
```

* `bot` is the only application writer of the ledger; `dashboard` opens it read-only and only writes the
  kill-switch file. `litestream` reads the database file and streams changes to object storage with
  roughly a one-second lag (asynchronous, not synchronous).
* The private key is a read-only bind mount (`/run/secrets/kalshi_private_key.pem`) from the
  host, outside the image. The dashboard container has no Kalshi credentials at all.
* All three services use `restart: unless-stopped`. A one-shot `init` service fixes volume
  ownership (uid 10001) on every `up`.

## Compliance

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

## 1. Provision the VPS

1. Create a small instance: 1 vCPU, 1-2 GB RAM, 20 GB disk, Ubuntu 24.04 LTS (or Debian 12).
   The Streamlit/pandas stack is the memory hog; 1 GB works with a 1-2 GB swapfile, 2 GB is
   comfortable. Choose a region in the same legal jurisdiction you are eligible in.
2. Add your SSH public key at creation; disable password auth.
3. Firewall (provider firewall and/or `ufw`): allow inbound **22/tcp only** (or none if you
   use Tailscale SSH). Do **not** open 8501.
   ```bash
   sudo ufw default deny incoming && sudo ufw default allow outgoing
   sudo ufw allow OpenSSH && sudo ufw enable
   ```
   Note: Docker-published ports bypass `ufw`. This is why the dashboard is published on
   `127.0.0.1` only in `docker-compose.yml`; do not change that.
4. Create a non-root sudo user (`adduser deploy && usermod -aG sudo deploy`) and use it.
5. Enable automatic security updates: `sudo apt install -y unattended-upgrades`.

## 2. Install Docker and time sync

```bash
# Docker Engine + compose plugin (official convenience script; or follow docs.docker.com)
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker deploy && newgrp docker
docker compose version

# Git to fetch the code
sudo apt install -y git
git clone <your-repo-url> ~/Kalshi-Trader && cd ~/Kalshi-Trader
```

**Time sync (required).** Kalshi request signatures include a timestamp, so a drifting clock
makes every request fail with auth errors. Keep the host synced with NTP:

```bash
sudo apt install -y chrony          # on Ubuntu 24.04 systemd-timesyncd also works
sudo systemctl enable --now chrony
chronyc tracking                    # "System time" offset should be well under 1 second
timedatectl                         # "System clock synchronized: yes"
```

Containers share the host clock, so no per-container setup is needed.

## 3. Place secrets and configuration

Nothing secret goes in the image or the git repo.

```bash
cd ~/Kalshi-Trader

# 1) Kalshi private key: outside the image, read-only mount, owned by the container user.
mkdir -p secrets
scp ./my-kalshi-key.pem deploy@<host>:~/Kalshi-Trader/secrets/kalshi_private_key.pem   # from your laptop
sudo chown 10001:10001 secrets/kalshi_private_key.pem
sudo chmod 600 secrets/kalshi_private_key.pem
sudo chmod 700 secrets

# 2) Environment file
cp .env.example .env
chmod 600 .env
$EDITOR .env
```

Minimum `.env` additions for the compose deployment (the full list is in the table at the
end; strategy vars from `.env.example` still apply):

```ini
API_KEY_ID=<your kalshi key id>
KALSHI_ENV=demo            # start on demo if you have a demo account; prod needs real funds
BOT_MODE=dry

LITESTREAM_BUCKET=my-kalshi-ledger
LITESTREAM_PATH=kalshi-ledger
LITESTREAM_ENDPOINT=https://<account>.r2.cloudflarestorage.com   # empty for AWS S3
LITESTREAM_REGION=auto                                           # us-east-1 etc. for AWS
LITESTREAM_ACCESS_KEY_ID=...
LITESTREAM_SECRET_ACCESS_KEY=...

HEALTHCHECK_URL=https://hc-ping.com/<uuid>
ALERT_NTFY_URL=https://ntfy.sh/<long-random-topic>
ALERT_MIN_LEVEL=ERROR
```

`PRIVATE_KEY_PATH` in `.env` is ignored in compose (it is forced to the mounted path). Use a
bucket-scoped S3 credential (read/write on that one bucket only), not an account-wide key.
The key file path on the host can be changed with `KALSHI_PRIVATE_KEY_FILE`.

## 4. First run in DRY mode

```bash
docker compose up -d --build
docker compose ps                 # init: exited 0; bot, dashboard, litestream: running
docker compose logs -f bot        # expect "DRY RUN (no orders)" and a "startup" event
docker compose logs litestream    # expect "initialized db" / "replicating to" lines
```

Dry mode records every decision (buy or skip, with the reason) to the ledger and settles
hypothetical one-contract paper trades, so you can measure behaviour with no money at risk.
Let it run through several game nights and review the dashboard before considering live.

Health: `docker compose ps` shows `healthy` for the dashboard immediately and for the bot after
its first heartbeat (up to about 5 minutes after start). The bot check passes while the newest
`heartbeat` event in the ledger is under 50 minutes old (the bot writes one every 30 minutes).
Docker only labels a container unhealthy; the restart policy reacts to the process exiting. The
dead-man's switch below covers a hung-but-running bot.

## 5. Verify with the read-only preflight

`kalshi.preflight` checks auth signing, balance units, orderbook shape and order response
fields. Default mode is **read-only**:

```bash
docker compose run --rm --no-deps --entrypoint python bot -m kalshi.preflight
```

Every line should say OK. Auth failures usually mean a wrong `API_KEY_ID`, a key file that is
not readable by uid 10001, a mismatched `KALSHI_ENV`, or clock drift (see time sync).

Do **not** pass `--place-test-order` against prod; the tool itself refuses unless
`KALSHI_ENV=demo`.

## 6. Going live

Only proceed when **all** of these are true:

- [ ] You, the account holder, are in an eligible location and have **completed Kalshi's
      location/eligibility verification** (see [Compliance](#compliance)), and have confirmed with
      Kalshi how this applies to API trading from a hosted server.
- [ ] Preflight passes against the environment you will trade on.
- [ ] Several nights of DRY results look sane in the dashboard.
- [ ] Backups verified (section 8) and kill switch tested (section 12).
- [ ] Alerts and the dead-man's switch have fired a test notification.
- [ ] `STRATEGY_*` limits (sizing, drawdown, exposure) are values you accept losing.

Then in `.env`:

```ini
KALSHI_ENV=prod
BOT_MODE=live
BOT_LIVE_CONFIRM=LIVE
```

```bash
docker compose up -d bot          # recreates only the bot
docker compose logs -f bot        # "LIVE — REAL MONEY"; startup event says mode=LIVE
```

The bot's `--live` flag normally makes you type `LIVE` on a terminal. A restarting container
has no terminal, so `deploy/entrypoint-bot.sh` supplies that confirmation only when both
`BOT_MODE=live` and `BOT_LIVE_CONFIRM=LIVE` are set. Any other value refuses to start.
To go back to dry mode, set `BOT_MODE=dry` (and clear `BOT_LIVE_CONFIRM`) and `docker compose up -d bot`.

Tip: for the first live night, start with `touch` on the kill switch (section 12), confirm the
dashboard shows "paused", then remove it when you are watching.

## 7. Reaching the dashboard privately

The dashboard contains a toggle that can resume trading, so it must never be public.
Compose publishes it on `127.0.0.1:8501` only. Pick one:

* **SSH tunnel (simplest, nothing to install):**
  ```bash
  ssh -N -L 8501:127.0.0.1:8501 deploy@<host>
  # open http://localhost:8501
  ```
* **Tailscale:** install Tailscale on the VPS and your devices, then forward the loopback port
  to your tailnet only: `sudo tailscale serve --bg 8501` (see Tailscale docs; the service is
  then reachable at `https://<machine>.<tailnet>.ts.net` from your devices, not the internet).
  Do **not** use `tailscale funnel` (that is public).
* **Cloudflare Tunnel + Cloudflare Access:** run `cloudflared` on the host pointing at
  `http://127.0.0.1:8501`, and put an Access policy (specific emails + MFA) in front of the
  hostname. A tunnel without an Access policy is a public dashboard; do not do that.

Verify exposure from another machine: `nc -vz <public-ip> 8501` must fail.

## 8. Backups with Litestream

The ledger (decisions, orders, fills, settlements, paper trades, snapshots, events, plus the open-position lots, drawdown peak and schedule) is the dashboard's source of truth and the
bot's record of what it did. It is not a perfect audit trail: an order can be missing from it for a
moment after a crash between sending and recording, which the bot repairs on the next start by asking
Kalshi (the write-ahead `submitting` row is the evidence). Litestream replicates it asynchronously
(about 1 s behind, daily snapshots, 7 days retention) to your bucket, so a crash can lose the last second or so of writes.

Setup: create a private bucket and a bucket-scoped access key at any S3-compatible
provider (Cloudflare R2, Backblaze B2, AWS S3, Hetzner Object Storage, ...), fill the
`LITESTREAM_*` vars, `docker compose up -d`. If the bucket is on another provider than the VPS,
that is fine and preferable (a provider outage should not take both down).

**Verify backups** (do this once at setup and after any config change):

```bash
docker compose logs --tail=50 litestream            # no repeated errors
docker compose run --rm --no-deps --entrypoint litestream litestream \
  snapshots -config /etc/litestream.yml /app/data/nba_ledger.sqlite
docker compose run --rm --no-deps --entrypoint litestream litestream \
  generations -config /etc/litestream.yml /app/data/nba_ledger.sqlite
```

The strongest check is a real restore to a scratch file on the shared volume, then a row-count
comparison from the bot image (the litestream image has no `sqlite3`):

```bash
docker compose run --rm --no-deps --entrypoint litestream litestream \
  restore -config /etc/litestream.yml -o /app/data/verify.sqlite /app/data/nba_ledger.sqlite
docker compose run --rm --no-deps --entrypoint python bot -c "
import sqlite3
for p in ('/app/data/nba_ledger.sqlite', '/app/data/verify.sqlite'):
    c = sqlite3.connect('file:%s?mode=ro' % p, uri=True)
    print(p, {t: c.execute('select count(*) from %s' % t).fetchone()[0] for t in ('decisions','orders','settlements','events')})
"
docker compose run --rm --no-deps --entrypoint rm bot -f /app/data/verify.sqlite
```

Counts in the restored copy may trail the live DB by a few seconds of writes; they should be
very close and never wildly lower. Schedule this monthly.

### What is and is not backed up

| State | Where | Backed up? | If lost |
|---|---|---|---|
| Ledger (`nba_ledger.sqlite`) | volume | Yes, continuous | History and dashboard lost |
| Open-position lots, drawdown peak, today's schedule + fired flags | **inside** `nba_ledger.sqlite` | Yes, continuous (same file) | Restored with the ledger. Positions are also reconciled from Kalshi on every game |
| Legacy `portfolio_data.json` / `high_water_mark.json` / `schedule_data.json` | volume (old installs only) | No | Read once on first run to import into the ledger, never written again; safe to archive |
| `STOP_TRADING` | volume | No | Re-create it if you want to stay paused |
| Kalshi key, `.env` | host | No (by design) | Keep in a password manager. Revoke/reissue the key from Kalshi if needed |

All durable state is in the one SQLite file, so a restore needs nothing else.

## 9. Restore runbook

Use this to move to a new server or recover from a lost volume/disk. You need: this repo,
the Kalshi key file, your `.env` (especially the `LITESTREAM_*` values), and bucket access.

1. Provision a new VPS and do sections 1-3 (Docker, chrony, clone repo, `secrets/`, `.env`).
   **Stop the old bot first** (`docker compose down` on the old host, or revoke its access)
   so two bots never trade the same account.
2. Create the volume with the right ownership; **do not start the bot yet**:
   ```bash
   docker compose build
   docker compose run --rm init            # creates volume kalshi_data owned by uid 10001
   ```
3. Restore the ledger from the bucket:
   ```bash
   docker compose run --rm --no-deps --entrypoint litestream litestream \
     restore -config /etc/litestream.yml -if-db-not-exists -if-replica-exists \
     /app/data/nba_ledger.sqlite
   ```
   `-if-db-not-exists` makes this a no-op if a ledger is already present, so it cannot clobber
   live data. For point-in-time recovery add `-timestamp 2026-01-31T03:00:00Z` (or `-generation`
   and `-index` from `litestream generations/snapshots`). Check the output reports a restore
   and that the file exists:
   ```bash
   docker compose run --rm --no-deps --entrypoint ls bot -l /app/data
   ```
4. Nothing else to restore: positions, the drawdown peak and the schedule come back with the ledger.
5. Run the read-only preflight (section 5), then start in **dry** mode first if there is any
   doubt: `BOT_MODE=dry` in `.env`, then `docker compose up -d`.
6. The bot **reconciles its positions against Kalshi before each game**, so a stale or partly
   restored position table is corrected from the exchange rather than trusted. Watch `docker compose logs -f bot` for the
   reconcile output, then confirm in the dashboard that open positions, balance, and the
   heartbeat look right before switching back to `BOT_MODE=live`.
7. Litestream resumes replicating to the same bucket path. If the restored DB diverged from
   what is in the bucket (for example you restored to an earlier point in time), Litestream
   starts a new generation automatically; old generations expire per retention.

Practice this once on a throwaway server or a laptop before you need it.

## 10. Monitoring and alerts

Configured through env vars (implemented in `nba_trading/alerts.py` and called from `_event()` / `health_job` in `main.py`; unset = disabled):

| Variable | Purpose |
|---|---|
| `HEALTHCHECK_URL` | Healthchecks.io-style ping URL. The health job (every 30 min) pings `ok` when the balance fetch worked and `/fail` when it did not or the job raised; if pings stop altogether, Healthchecks alerts you. This is the dead-man's switch that also catches a hung or dead host. |
| `ALERT_NTFY_URL` | ntfy topic URL, e.g. `https://ntfy.sh/<long-random-topic>`; push to your phone. Treat the topic name as a secret. |
| `ALERT_WEBHOOK_URL` | Generic JSON webhook (Slack/Discord-compatible relays, etc.). |
| `ALERT_MIN_LEVEL` | `INFO`, `WARNING`, `ERROR` or `CRITICAL` (default `ERROR`). |
| `ALERT_COOLDOWN_MINUTES` | De-duplicate the same alert kind (default 15). |

Healthchecks.io setup: create a check with **Period 30 minutes, Grace 20 minutes**, copy its ping
URL into `HEALTHCHECK_URL`, and add your email/ntfy/phone integration. Alert on the dashboard side
too: it shows "STALE" when no heartbeat has been seen for 45 minutes.

Also worth watching: `docker compose ps` health, free disk (`df -h`), and the Litestream log.
Test alerts once by temporarily setting `ALERT_MIN_LEVEL=INFO` or by stopping the bot
(`docker compose stop bot`) and waiting for Healthchecks to fire.

Logs: `docker compose logs -f bot` (json-file driver, capped 10 MB x 5 per service). The bot also
writes `/app/data/trade_bot.log` (rotating) on the volume.

## 11. Update, redeploy, roll back

**Update**

```bash
cd ~/Kalshi-Trader
git fetch && git log --oneline HEAD..origin/main     # review what is coming
git pull --ff-only
export IMAGE_TAG=$(git rev-parse --short HEAD)       # also put IMAGE_TAG=... in .env to persist
docker compose build
docker compose up -d                                 # recreates changed services only
docker compose ps && docker compose logs --tail=50 bot
```

Prefer deploying between game nights: the bot re-plans at 00:05 UTC. A restart mid-night
recovers the saved schedule (including which games already fired) and, at startup, asks Kalshi about
any order whose outcome it never learned and re-queues the post-tip fill checks. Positions are
reconciled against Kalshi inside each live game evaluation and every 30 minutes by the health job,
not at startup itself. Avoid restarting close to tip-off. An OS lock stops two bots on the SAME
host and volume only; it cannot stop a second copy on another server (for example a restore while
the old server is still running), so stop the old one first. Run `python -m pytest` (or let CI run) before pulling to a live host.

**Roll back**

Images are tagged by commit (`kalshi-trader:<sha>`), so the previous build is still on disk:

```bash
docker image ls kalshi-trader
IMAGE_TAG=<previous-sha> docker compose up -d --no-build
```

If the old image is gone: `git checkout <previous-sha> && IMAGE_TAG=<previous-sha> docker compose up -d --build`
(then `git switch main` when you are ready to move forward again). The ledger schema is versioned
(`PRAGMA user_version`) and migrates forward in place when the bot starts, adding columns and tables. An
older image will still open a newer database but ignores the columns it does not know; if a
release ever changes a column in a way that old code cannot read, restore the Litestream backup taken
before upgrading instead of rolling the code back alone.

**Stop everything:** `docker compose down` (keeps the volume). `docker compose down -v` deletes the
volume and the local ledger; do not run it unless you have verified a backup.

## 12. Kill switch

Presence of the kill-switch file stops **new orders** without killing the process: it is checked at the
start of each game evaluation (about 15 minutes before a game), so it takes effect for every game
that has not yet been evaluated. An evaluation already in progress is not interrupted, and resting
orders already on Kalshi are not cancelled (they expire at tip-off). Existing positions are unaffected and still settle; the bot keeps
writing heartbeats.

* **Dashboard:** use the Kill switch toggle (reach it via section 7).
* **Shell, on the shared volume:**
  ```bash
  docker compose exec dashboard touch /app/data/STOP_TRADING     # pause
  docker compose exec dashboard rm -f /app/data/STOP_TRADING     # resume
  docker compose exec dashboard ls -l /app/data/STOP_TRADING     # check
  ```
  (From the host without exec: `docker run --rm -v kalshi_data:/d alpine touch /d/STOP_TRADING`;
  then `chown 10001:10001` it, or just prefer `exec` above so ownership is right and the dashboard
  can still remove it.)
* **Hard stop:** `docker compose stop bot`. Note `unless-stopped` keeps it stopped across
  reboots until you `docker compose start bot`.

Both containers use `KILL_SWITCH_FILE=/app/data/STOP_TRADING` on the same named volume, which is
what makes the dashboard toggle work across containers. To also revoke trading ability entirely,
delete the API key at Kalshi.

## Environment variable reference

| Variable | Required | Default | Used by | Notes |
|---|---|---|---|---|
| `API_KEY_ID` | yes | none | bot | Kalshi API key id |
| `KALSHI_PRIVATE_KEY_FILE` | no | `./secrets/kalshi_private_key.pem` | compose | Host path of the key; mounted read-only at `/run/secrets/kalshi_private_key.pem` |
| `KALSHI_ENV` | no | `prod` | bot | `prod` or `demo` |
| `BOT_MODE` | no | `dry` | bot | `dry` or `live` |
| `BOT_LIVE_CONFIRM` | for live | empty | bot | Must be exactly `LIVE` when `BOT_MODE=live` |
| `STRATEGY_PRICE_MIN`, `STRATEGY_PRICE_MAX`, `STRATEGY_BANKROLL_FRACTION`, `STRATEGY_LIMIT_BUFFER_CENTS`, `STRATEGY_MAX_DRAWDOWN_PCT`, `STRATEGY_MAX_EXPOSURE_PCT` | no | see `.env.example` | bot | Strategy parameters |
| `LITESTREAM_BUCKET` | yes | none | litestream | Bucket name (compose refuses to start without it) |
| `LITESTREAM_PATH` | no | `kalshi-ledger` | litestream | Prefix inside the bucket |
| `LITESTREAM_ENDPOINT` | no | empty (AWS) | litestream | S3-compatible endpoint URL for R2/B2/Hetzner/MinIO |
| `LITESTREAM_REGION` | no | `us-east-1` | litestream | `auto` for R2 |
| `LITESTREAM_ACCESS_KEY_ID` | yes | none | litestream | Bucket-scoped key |
| `LITESTREAM_SECRET_ACCESS_KEY` | yes | none | litestream | Bucket-scoped secret |
| `HEALTHCHECK_URL` | recommended | empty | bot | Dead-man's switch ping URL |
| `ALERT_NTFY_URL` | recommended | empty | bot | ntfy topic URL |
| `ALERT_WEBHOOK_URL` | no | empty | bot | JSON webhook |
| `ALERT_MIN_LEVEL` | no | `ERROR` | bot | `INFO`/`WARNING`/`ERROR`/`CRITICAL` |
| `ALERT_COOLDOWN_MINUTES` | no | `15` | bot | Alert de-duplication window |
| `IMAGE_TAG` | no | `local` | compose | Image tag; set to the git sha for rollbacks |
| `ANTHROPIC_API_KEY` | no | empty | none | Only for `agent_trader`, which is not part of this deployment |

Set automatically by `docker-compose.yml` (do not set in `.env`): `PRIVATE_KEY_PATH`,
`LEDGER_FILE`, `KILL_SWITCH_FILE`, `SCHEDULE_FILE`, `PORTFOLIO_FILE`, `HIGH_WATER_FILE`.

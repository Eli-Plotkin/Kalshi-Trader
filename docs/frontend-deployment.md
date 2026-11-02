# Deploying the dashboard (frontend) on its own host

The dashboard can run on a different machine from the bot. In this mode ("remote mode") it has **no
Kalshi credentials, no access to the bot's disk, and cannot trade**. It copies a recent snapshot of
the bot's ledger out of an S3-compatible bucket, shows it behind a password, and can only *request a
pause*. The design and contracts are in [design-replication.md](design-replication.md); the backend
side (bot, replicator, bucket) is in [deployment.md](deployment.md).

```text
 backend server                         bucket                         this dashboard host
 bot -> ledger -> replicator ---->  snapshots/ + latest.json  ---->  subscriber thread (every 10 s)
 bot control poller <---------- CONTROL bucket: pause.json  <----  "Request pause" button
 bot (writes ack)  ----------->  CONTROL bucket: ack.json   ---->  "acknowledged by bot at ..."
```

## 1. What you deploy

| File | Purpose |
|---|---|
| `Dockerfile.frontend` | python:3.13-slim, non-root (uid 10001), installs **only** `requirements-frontend.txt`, copies only `nba_trading/`, `strategy_config.py`, `.streamlit/`. `DASHBOARD_MODE=remote` by default. HEALTHCHECK on `/_stcore/health`. |
| `requirements-frontend.txt` | streamlit, pandas, boto3, python-dotenv. Nothing that can trade. |
| `.streamlit/config.toml` | headless, no usage stats, XSRF protection on, minimal toolbar (no Deploy button), no stack traces in the UI. It does not set the bind address, so a local `streamlit run` stays on localhost. |

The image never contains `.env`, `*.pem`, `data/` or a ledger (the root `.dockerignore` keeps them out
of the build context, and `Dockerfile.frontend` copies explicit paths only, never `COPY . .`).

## 2. Environment variables

| Variable | Required | Default | Meaning |
|---|---|---|---|
| `DASHBOARD_MODE` | yes (set by the image) | `local` | `remote` for this deployment. Any other value than `local`/`remote` is an error, not a silent `local`. |
| `DASHBOARD_PASSWORD` | **yes in remote mode** | none | The page shows nothing until this is entered. If it is unset or blank in remote mode the app refuses to render data and says so. Use a long random value (e.g. `openssl rand -base64 24`). |
| `REPLICATION_BUCKET` | yes (or `REPLICATION_LOCAL_DIR`) | none | Bucket name. |
| `REPLICATION_PREFIX` | no | `kalshi-bot/` | Key prefix; must match the backend's. |
| `REPLICATION_ENDPOINT` | for non-AWS | empty (AWS) | S3-compatible endpoint (R2, B2, MinIO). |
| `REPLICATION_REGION` | no | none | `auto` for Cloudflare R2. |
| `REPLICATION_ACCESS_KEY_ID`, `REPLICATION_SECRET_ACCESS_KEY` | yes | none | The frontend's own **read-only** key for the DATA bucket (section 3). |
| `CONTROL_BUCKET`, `CONTROL_PREFIX`, `CONTROL_ENDPOINT`, `CONTROL_REGION`, `CONTROL_ACCESS_KEY_ID`, `CONTROL_SECRET_ACCESS_KEY` (or `CONTROL_LOCAL_DIR` for dev) | recommended | falls back to the `REPLICATION_*` store | The separate CONTROL bucket: the frontend PUTs `pause.json` and GETs `pause.json` and `ack.json` there (section 3). When `CONTROL_BUCKET`/`CONTROL_LOCAL_DIR` is not set the pause uses the `REPLICATION_*` store, which then needs write access to `control/*` in the data bucket (weaker; see section 3). |
| `REPLICATION_LOCAL_DIR` | dev only | none | Use a folder instead of a bucket (section 6). |
| `DASHBOARD_DATA_DIR` | no | `/data` in the image, `/tmp/kalshi-dashboard` otherwise | Where the synced `ledger.sqlite` and `sync_state.json` live. Disposable. |
| `DASHBOARD_SYNC_SECONDS` | no | `10` everywhere: code, `Dockerfile.frontend`, `.env.frontend.example` (minimum 5) | How often the background thread checks `latest.json`. The snapshot is only downloaded when it changed. |
| `STRATEGY_*`, `LEDGER_FILE`, `KILL_SWITCH_FILE` | no | | Not used in remote mode (the displayed limits come from the ledger, not from this host). Never set `API_KEY_ID` or `PRIVATE_KEY_PATH` here. |

## 3. Bucket key scoping

Use **two buckets** and give the frontend a **separate key** for each; do not reuse the backend's keys.

| Bucket / prefix | Backend key | Frontend key |
|---|---|---|
| DATA: `<prefix>latest.json`, `<prefix>snapshots/*` | read + write + delete (prune) | **read only** (GET) |
| CONTROL: `<prefix>control/pause.json` | read (the bot polls it), delete (`resume`) | **PUT** (request a pause) and GET (show the state) |
| CONTROL: `<prefix>control/ack.json` | write (the bot acknowledges), delete (`resume`) | GET only |

The data key being read-only is the point of the split: on providers that cannot scope a key by prefix (R2,
B2) a frontend key that could write to the data bucket could also overwrite snapshots and `latest.json`. With a
separate control bucket the worst the frontend key can write is `pause.json`.

Example AWS IAM policies for the frontend (replace the bucket names and prefixes):

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {"Effect": "Allow", "Action": ["s3:GetObject"],
     "Resource": ["arn:aws:s3:::DATA-BUCKET/kalshi-bot/latest.json",
                  "arn:aws:s3:::DATA-BUCKET/kalshi-bot/snapshots/*"]},
    {"Effect": "Allow", "Action": ["s3:GetObject"],
     "Resource": ["arn:aws:s3:::CONTROL-BUCKET/kalshi-bot/control/pause.json",
                  "arn:aws:s3:::CONTROL-BUCKET/kalshi-bot/control/ack.json"]},
    {"Effect": "Allow", "Action": ["s3:PutObject"],
     "Resource": ["arn:aws:s3:::CONTROL-BUCKET/kalshi-bot/control/pause.json"]}
  ]
}
```

Notes:

- The frontend never lists or deletes. If the logs show `AccessDenied` on a list call, add
  `s3:ListBucket` limited to the prefix with a condition, rather than widening `GetObject`.
- Treat both buckets as private: the data bucket holds your full trade history.
- Resume is deliberately **not** possible from the frontend. Even with the frontend's keys, nothing in the
  UI can clear the pause; that needs `python -m nba_trading.control resume` on the backend server. The bot
  also latches a pause locally once it has seen it, so deleting or overwriting `pause.json` does not resume trading.

### What a leaked frontend key or password can do

- **Dashboard password leaked:** the holder can read your ledger (trades, decisions, equity, events) through the
  page and press **Request pause**. They cannot resume, change settings, place or cancel orders, or reach Kalshi.
- **Frontend bucket keys leaked:** the holder can read the ledger snapshots and write `control/pause.json` (so they can
  pause the bot, which costs you missed trades, not money). With the data key read-only they cannot alter or delete
  snapshots. They cannot resume (the bot latches a pause locally; only the backend can clear it) and they cannot
  trade; there is no Kalshi credential on the frontend host at all. If you chose the single-bucket fallback
  (no `CONTROL_*`), the frontend key must also write to the data bucket, and then a leaked key could overwrite
  snapshots and make the dashboard show false data: the banner still ages a frozen sequence out to amber and red, but it
  cannot prove content is genuine. Rotate the key and the password if either leaks.

## 4. Deploy

Build once, anywhere with Docker:

```bash
docker build -f Dockerfile.frontend -t kalshi-dashboard .
```

### Any Docker host / small VPS

```bash
docker run -d --name kalshi-dashboard --restart unless-stopped \
  -p 127.0.0.1:8501:8501 \
  -e DASHBOARD_PASSWORD='...' \
  -e REPLICATION_BUCKET=my-bucket -e REPLICATION_PREFIX=kalshi-bot/ \
  -e REPLICATION_ENDPOINT=https://<account>.r2.cloudflarestorage.com -e REPLICATION_REGION=auto \
  -e REPLICATION_ACCESS_KEY_ID=... -e REPLICATION_SECRET_ACCESS_KEY=... \
  --read-only --tmpfs /tmp --tmpfs /data:uid=10001,gid=10001 \
  --cap-drop ALL --security-opt no-new-privileges \
  kalshi-dashboard
```

Bind to loopback and put a TLS-terminating reverse proxy in front (Caddy: `dash.example.com { reverse_proxy 127.0.0.1:8501 }`
gets a certificate automatically). Prefer `--env-file` (mode 600) over `-e` so secrets stay out of shell history.
Streamlit uses WebSockets; the proxy must pass them (Caddy and nginx with `Upgrade` headers do).

### Fly.io

`fly launch --no-deploy --dockerfile Dockerfile.frontend`, then in `fly.toml` set `[http_service] internal_port = 8501`
and `force_https = true`, store secrets with
`fly secrets set DASHBOARD_PASSWORD=... REPLICATION_ACCESS_KEY_ID=... REPLICATION_SECRET_ACCESS_KEY=...`
(non-secret values under `[env]`), keep at least one machine running (`min_machines_running = 1`,
`auto_stop_machines = false`) and `fly deploy`.

### Render

New Web Service from the repo with Docker, Dockerfile path `Dockerfile.frontend`. Set the env vars above in the
dashboard and add `PORT=8501` so Render routes to the container port. Render terminates TLS. Use an instance type that
does not sleep, or the banner will show a gap every time it wakes.

### Streamlit Community Cloud (optional)

Possible but the weakest fit:

- The repo must be on GitHub (a private repo is fine, but your strategy code lives there). Main file:
  `nba_trading/dashboard/app.py`. In *Advanced settings -> Secrets* paste top-level keys; they are exposed as
  environment variables:

  ```toml
  DASHBOARD_MODE = "remote"
  DASHBOARD_PASSWORD = "..."
  REPLICATION_BUCKET = "my-bucket"
  REPLICATION_ACCESS_KEY_ID = "..."
  REPLICATION_SECRET_ACCESS_KEY = "..."
  ```

- Community Cloud installs `requirements.txt` from the repo root, which here is the full **trading** list
  (cryptography, apscheduler, anthropic ...). To keep trading dependencies off that host, deploy from a branch or
  small repo whose `requirements.txt` is a copy of `requirements-frontend.txt`.
- Apps sleep when idle and storage is ephemeral: after waking, the page shows "waiting for first data" until the first
  sync (seconds). Use the platform's viewer restrictions in addition to `DASHBOARD_PASSWORD`.

## 5. TLS and password guidance

- The password gate is a single shared password, not accounts. Serve it **only over HTTPS**; over plain HTTP the
  password and the session cookie are readable on the network. Never expose port 8501 directly.
- Use a long random password, keep it in the host's secret store, and rotate it by changing the variable and
  restarting (this also ends all sessions).
- Wrong attempts are slowed per session (0.5 s per failure, capped at 5 s) and there is a **process-wide budget**: after
  10 failed attempts within 5 minutes (across all sessions) further attempts are rejected, even with the right password,
  until old failures age out of the window. The message is the same generic one as for a wrong password. The budget is per
  process and has no per-IP notion, so it also means an attacker can keep you locked out; if the host is internet-facing,
  add rate limiting or an IP allow-list at the proxy, or put it behind a VPN (Tailscale/WireGuard) and skip public
  exposure entirely.
- Logging out clears the whole session state, and every part of the page re-checks the login itself, so a logged-out
  browser tab cannot make the page render data or start the sync.
- The password is never logged or shown, and is compared in constant time (`hmac.compare_digest`).

## 6. What the banner colours mean

The banner at the very top (after login) says `Data as of 2026-10-29 18:00:18 UTC (N s ago)`. The date and time are when the
backend **took the snapshot**; the age `N` is **not** computed from that backend timestamp. It is measured on this host's own
clock from the moment the dashboard last saw the snapshot sequence number go up (`seq_advanced_at`), so two hosts whose clocks
disagree cannot make a dead replication look healthy. A snapshot whose sequence number stops advancing (stuck publisher or a
forged `latest.json`) ages out to amber and then red even if its timestamp looks recent.

| Colour | Data age | Meaning |
|---|---|---|
| Green | up to 60 s | Healthy. Normal lag is about 10-15 s, about 30 s worst case with the shipped defaults (publisher heartbeat 10 s, sync 10 s, page refresh 10 s). |
| Amber | 60 - 180 s | Replication is lagging (slow upload or sync, or a short outage). Numbers are probably right but old. |
| Red | over 180 s | Replication is broken or the backend is down. Do not rely on the numbers. |
| Blue | none yet | "Waiting for the first data": nothing has synced yet (first start, wrong bucket/prefix/credentials). |

**Clock skew.** If the snapshot's timestamp is more than 10 s in this host's future, or the difference between when this host saw
the update and the backend's timestamp is above 60 s or below -10 s, the banner is never green: it is at least amber and says
`clock skew between hosts (about N s): age is measured from observed updates`. Fix NTP on both hosts. (Right after the dashboard
restarts with an empty data directory the first sync of an already old snapshot can show this too; it clears once new snapshots arrive.)

**Bot status.** Under the banner a line shows `Bot alive N s ago`. The bot stamps `bot_alive_at` every 60 s and the stamp travels in
the snapshot; its age is the data age plus the (backend-clock) gap between the snapshot time and the stamp, so it is also
skew-proof. Above 180 s with a green banner you get **"replication OK, bot silent"**: the pipeline works but the bot has stopped
(crash, hang) - look at the backend. Only for older snapshots that carry no `bot_alive_at` the page falls back to
`Bot heartbeat N min ago` with the 45-minute rule. If the last sync attempt failed, the error text and the time since the last good
sync are shown; the page keeps serving the previous good snapshot meanwhile.

The main panels and the banner both refresh every 10 s, so the tables can trail the banner by up to 10 s.

## 7. The pause button

- Remote mode replaces the kill-switch toggle with **Request pause**. It writes `control/pause.json` in the CONTROL bucket
  (`by: dashboard`, your optional reason). The bot polls it every `CONTROL_POLL_SECONDS` (default 30), so allow ~30 s plus
  the next game evaluation. Orders already placed are not cancelled.
- The panel shows the current state as plain text (who, when, reason are never interpreted as markdown or HTML and are
  truncated) and the bot's **acknowledgement** from `control/ack.json`: `requested by ... at X` / `acknowledged by bot at Y`.
  If a pause has been requested for more than 60 s (2 poll intervals) with no acknowledgement you get a warning: the bot
  may be down or unable to read the control bucket, so do not assume it has stopped trading. If the bucket cannot be read the
  state is shown as "unknown".
- There is **no resume button**. To resume, on the backend server run `python -m nba_trading.control resume` (clears the
  remote flag, the acknowledgement and the local `STOP_TRADING` file). The page says so next to the button.
- Local mode (default) is unchanged: the toggle creates/removes the `STOP_TRADING` file.

## 8. Test locally against a folder before touching a bucket

No cloud needed. `REPLICATION_LOCAL_DIR` makes both sides use a directory as the "bucket".

1. Put a **copy** of a ledger somewhere (never point tests at the live `data/nba_ledger.sqlite`), and publish it once
   (the publisher opens its source read-only):

   ```bash
   cp data/nba_ledger.sqlite /tmp/ledger-copy.sqlite
   python - <<'EOF'
   from nba_trading.replication.store import LocalDirStore
   from nba_trading.replication.publisher import publish_once
   publish_once(LocalDirStore("/tmp/fake-bucket"), "/tmp/ledger-copy.sqlite")
   EOF
   ```

   (To keep it fresh, re-run that, or run the replicator with the same `REPLICATION_LOCAL_DIR`; see deployment.md.)

2. Start the dashboard in remote mode against that folder, on localhost only:

   ```bash
   DASHBOARD_MODE=remote DASHBOARD_PASSWORD=dev-only DASHBOARD_DATA_DIR=/tmp/dash-data \
   REPLICATION_LOCAL_DIR=/tmp/fake-bucket DASHBOARD_SYNC_SECONDS=5 \
   streamlit run nba_trading/dashboard/app.py
   ```

3. Check: the login form appears and nothing else; a wrong password is refused; the right one shows the green banner;
   stop re-publishing and watch it turn amber after 60 s and red after 180 s; delete `/tmp/dash-data` and
   `/tmp/fake-bucket/latest.json` to see "waiting for first data"; click **Request pause** and check
   `/tmp/fake-bucket/kalshi-bot/control/pause.json` (or wherever the store puts it).
   Unset `DASHBOARD_PASSWORD` to see the refusal message.

The automated tests (`nba_trading/tests/test_dashboard_remote.py`, `test_freshness.py`) cover the same paths with fakes.

## 9. Not verified here

The development machine had no Docker, no cloud account and no real bucket, so these are **untested** and need a first-run
check by you:

- `docker build -f Dockerfile.frontend` and the container itself (the Dockerfile is only statically checked in tests: non-root
  USER, HEALTHCHECK, no secrets copied, no trading requirements). The `curl` HEALTHCHECK, the `/data` ownership and `--read-only`
  flags have not been run.
- A real S3/R2/B2 endpoint: permissions and the IAM policy above, endpoint/region settings, eventual-consistency behaviour,
  and whether your provider can scope keys by prefix.
- Fly.io, Render and Streamlit Community Cloud specifics (settings and their UIs change; treat the steps as a guide).
- TLS, reverse proxy WebSocket passthrough, and password-attempt rate limiting at the proxy.
- The modules `nba_trading.replication.*` and `nba_trading.control` were built separately; most dashboard tests use fakes for
  them (with the exact function names the dashboard calls), so the real subscriber/control behaviour against the dashboard
  has only been checked by the integration step.
- Behaviour of a Streamlit session that stays open for days (browser reconnects, the app idling) was not soak-tested.

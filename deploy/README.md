# deploy/

Deployment assets for running the bot on a single small VPS. Full guide: [`docs/deployment.md`](../docs/deployment.md).

| File | Purpose |
|---|---|
| `../Dockerfile` | One image (python:3.13-slim, non-root uid 10001) used by bot and dashboard |
| `../docker-compose.yml` | Services: `init` (volume ownership), `bot`, `dashboard` (127.0.0.1 only), `litestream` |
| `litestream.yml` | Continuous replication of the SQLite ledger to S3-compatible storage |
| `entrypoint-bot.sh` | Chooses dry vs live from `BOT_MODE`; live also needs `BOT_LIVE_CONFIRM=LIVE` |
| `../.github/workflows/ci.yml` | Tests + docker build smoke test; disabled manual deploy stub |

Quick start (details and caveats in the guide):

```bash
cp .env.example .env            # fill in API_KEY_ID, LITESTREAM_*, alert vars
mkdir -p secrets && cp /path/to/key.pem secrets/kalshi_private_key.pem
sudo chown 10001:10001 secrets/kalshi_private_key.pem && sudo chmod 600 secrets/kalshi_private_key.pem
docker compose up -d --build    # DRY mode by default
```

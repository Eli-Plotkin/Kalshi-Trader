# BACKEND image: the trading bot and the ledger replicator (compose picks the command).
# The dashboard is a separate image (Dockerfile.frontend) deployed on its own host.
# No secrets are baked in: credentials come from env vars and a mounted key file.
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app \
    TZ=UTC

# tzdata: zoneinfo (America/New_York) is used by the scheduler.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Fixed uid/gid so the host key file and the shared volume can be chowned to it.
RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --home-dir /home/app --create-home --shell /usr/sbin/nologin app

WORKDIR /app

# Dependencies first (cached layer unless the requirements change).
COPY requirements-backend.txt ./
RUN pip install -r requirements-backend.txt

# Application code: only what the bot and replicator import. Research code (misprice_discovery,
# sportsbook_sourced, agent_trader), docs and tests are not shipped. (.dockerignore keeps secrets out.)
COPY strategy_config.py ./
COPY kalshi ./kalshi
COPY nba_trading ./nba_trading
COPY deploy ./deploy
RUN rm -rf nba_trading/tests

# /app/data is the shared named volume (the SQLite ledger, which holds all durable state; the
# kill-switch file; the bot log).
# Creating it here with the right owner lets a fresh named volume inherit it.
RUN mkdir -p /app/data && chown app:app /app/data && chmod +x /app/deploy/entrypoint-bot.sh

USER app

# Default command = the bot (dry mode); compose overrides the entrypoint/command per service.
CMD ["python", "-m", "nba_trading.main"]

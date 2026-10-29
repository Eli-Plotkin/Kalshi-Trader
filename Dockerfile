# One image for both the bot and the dashboard (compose picks the command).
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
COPY requirements.txt requirements-dashboard.txt ./
RUN pip install -r requirements.txt -r requirements-dashboard.txt

# Application code (see .dockerignore for what is excluded).
COPY . .

# /app/data is the shared named volume (the SQLite ledger, which holds all durable state; the
# kill-switch file; the bot log).
# Creating it here with the right owner lets a fresh named volume inherit it.
RUN mkdir -p /app/data && chown app:app /app/data && chmod +x /app/deploy/entrypoint-bot.sh

USER app

# Default command = dashboard; compose overrides the entrypoint for the bot.
# Bound to 0.0.0.0 INSIDE the container only; compose publishes it on 127.0.0.1.
EXPOSE 8501
CMD ["streamlit", "run", "nba_trading/dashboard/app.py", \
     "--server.address=0.0.0.0", "--server.port=8501", "--server.headless=true", \
     "--browser.gatherUsageStats=false"]

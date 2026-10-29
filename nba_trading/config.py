"""NBA price-segment bot configuration.

Strategy parameters (price band, sizing, drawdown limits) are loaded from
`strategy_config.py` at the repo root, which itself reads them from `.env`.
This file only owns the bot's local state file paths.

To configure the strategy, set these env vars in `.env`:
    STRATEGY_PRICE_MIN
    STRATEGY_PRICE_MAX
    STRATEGY_BANKROLL_FRACTION
    STRATEGY_LIMIT_BUFFER_CENTS
    STRATEGY_MAX_DRAWDOWN_PCT

See `strategy_config.py` and `.env.example` for details.
"""
import os

from dotenv import load_dotenv

import strategy_config

load_dotenv()


# --- Strategy (re-exported from strategy_config for convenience) ---
BANKROLL_FRACTION = strategy_config.BANKROLL_FRACTION
LIMIT_PRICE_BUFFER_CENTS = strategy_config.LIMIT_BUFFER_CENTS
FAVORITE_PRICE_MIN = strategy_config.PRICE_MIN
FAVORITE_PRICE_MAX = strategy_config.PRICE_MAX
MAX_DRAWDOWN_PCT = strategy_config.MAX_DRAWDOWN_PCT
MAX_EXPOSURE_PCT = strategy_config.MAX_EXPOSURE_PCT


# --- Legacy JSON state files ---
# All durable state (portfolio lots, high-water mark, daily schedule) now lives in the
# SQLite database at LEDGER_FILE below. These paths are only read ONCE, to import old
# state into an empty database; the files are never modified or deleted.
SCHEDULE_FILE = os.getenv("SCHEDULE_FILE", "schedule_data.json")
PORTFOLIO_FILE = os.getenv("PORTFOLIO_FILE", "portfolio_data.json")
HIGH_WATER_FILE = os.getenv("HIGH_WATER_FILE", "high_water_mark.json")

# Create this file (e.g. `touch STOP_TRADING`) to stop new orders immediately,
# without killing the process. Checked before every order.
# Anchored at the repo root so the bot and the dashboard always use the same file,
# whatever directory each was started from.
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
KILL_SWITCH_FILE = os.getenv("KILL_SWITCH_FILE", os.path.join(_REPO_ROOT, "STOP_TRADING"))

# SQLite ledger of decisions / orders / fills / settlements (read by the dashboard).
LEDGER_FILE = os.getenv("LEDGER_FILE", "data/nba_ledger.sqlite")

# --- Alerting / dead-man's switch (see nba_trading/alerts.py). Empty = disabled. ---
ALERT_NTFY_URL = os.getenv("ALERT_NTFY_URL", "")          # e.g. https://ntfy.sh/<secret-topic>
ALERT_WEBHOOK_URL = os.getenv("ALERT_WEBHOOK_URL", "")    # generic JSON webhook
ALERT_MIN_LEVEL = os.getenv("ALERT_MIN_LEVEL", "ERROR")  # INFO|WARNING|ERROR|CRITICAL


def _float_env(name, default):
    """A malformed value must not crash the bot at import time."""
    try:
        return float(os.getenv(name, "") or default)
    except ValueError:
        return float(default)


ALERT_COOLDOWN_MINUTES = _float_env("ALERT_COOLDOWN_MINUTES", 15)
HEALTHCHECK_URL = os.getenv("HEALTHCHECK_URL", "")        # Healthchecks.io-style ping URL

# --- Scheduler misfire tolerance (APScheduler's default grace is 1 second, which
# silently drops a job that is delayed by a busy thread pool or a brief stall) ---
PRE_GAME_MISFIRE_GRACE_SECONDS = 300      # T-15 job: still 10 min before tip-off at +300s
CONFIRM_FILL_MISFIRE_GRACE_SECONDS = 3600  # fill check is useful any time after tip-off
HEALTH_MISFIRE_GRACE_SECONDS = 300

# Transient failures (we could not READ balance/positions/markets/orderbook, so no decision
# was made) are retried a few times; then the game is skipped. Orders are NEVER retried:
# once an order call has been made the outcome is handled by reconcile, not by retrying.
PRE_GAME_MAX_RETRIES = 3                     # retries after the first attempt (4 attempts max)
PRE_GAME_RETRY_DELAY_SECONDS = 60
PRE_GAME_RETRY_STOP_BEFORE_TIP_SECONDS = 180  # no retry that would run closer than this to tip-off

# Games that tip off together are evaluated one at a time (see main._TRADE_LOCK) so each sees
# the previous game's order when it checks cash and the exposure cap. A game that cannot get
# the lock in this long is treated as a transient failure (retried, then skipped).
TRADE_LOCK_TIMEOUT_SECONDS = 90

# Pause before the first lookup of an order whose POST timed out (it may still be processing).
ORDER_LOOKUP_DELAY_SECONDS = 3
SCHEDULE_RETRY_MISFIRE_GRACE_SECONDS = 600

# --- Clock skew vs. Kalshi (request signatures are timestamped) ---
CLOCK_SKEW_WARN_SECONDS = 2.0     # log a WARNING above this
CLOCK_SKEW_MAX_LIVE_SECONDS = 30.0  # refuse to start in LIVE mode above this

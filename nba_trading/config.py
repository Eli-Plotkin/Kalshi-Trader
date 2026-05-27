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


# --- Local state files (these are gitignored; defaults are fine) ---
SCHEDULE_FILE = os.getenv("SCHEDULE_FILE", "schedule_data.json")
PORTFOLIO_FILE = os.getenv("PORTFOLIO_FILE", "portfolio_data.json")
HIGH_WATER_FILE = os.getenv("HIGH_WATER_FILE", "high_water_mark.json")

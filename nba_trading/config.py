"""NBA price-segment bot configuration.

Strategy + state knobs specific to nba_trading. Shared Kalshi credentials
live in kalshi/config.py; this module re-exports them for backwards
compatibility with existing `from kalshi import config` callers.
"""
import os

from dotenv import load_dotenv

load_dotenv()

# --- Strategy ---
SHARES_TO_BUY = int(os.getenv("SHARES_TO_BUY", 10))
FAVORITE_PRICE_MIN = int(os.getenv("FAVORITE_PRICE_MIN", 0))  # cents
FAVORITE_PRICE_MAX = int(os.getenv("FAVORITE_PRICE_MAX", 0))  # cents

# --- State ---
SCHEDULE_FILE = os.getenv("SCHEDULE_FILE", "schedule_data.json")

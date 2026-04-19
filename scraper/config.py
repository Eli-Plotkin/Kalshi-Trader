import os
from dotenv import load_dotenv

# Look for .env in trading/ (where the existing bot keeps it), then project root
_project_root = os.path.dirname(os.path.dirname(__file__))
_env_path = os.path.join(_project_root, "trading", ".env")
if not os.path.exists(_env_path):
    _env_path = os.path.join(_project_root, ".env")
load_dotenv(_env_path)

# --- Kalshi API ---
BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"
API_KEY_ID = os.getenv("API_KEY_ID")
PRIVATE_KEY_PATH = os.getenv("PRIVATE_KEY_PATH")

if not API_KEY_ID or not PRIVATE_KEY_PATH:
    raise ValueError("API_KEY_ID and PRIVATE_KEY_PATH must be set in .env")

# Resolve PRIVATE_KEY_PATH relative to the trading/ directory if not absolute
if not os.path.isabs(PRIVATE_KEY_PATH):
    PRIVATE_KEY_PATH = os.path.join(_project_root, "trading", PRIVATE_KEY_PATH)

# --- Series ---
SERIES_TICKER = "KXNBAGAME"

# --- Poll intervals (seconds) ---
TICK_INTERVAL = 5
MARKET_REFRESH_TICKS = 12        # every 60s
SETTLEMENT_CHECK_TICKS = 60     # every 5 min

# --- API parameters ---
ORDERBOOK_DEPTH = 5
TRADES_PAGE_LIMIT = 1000
POLL_WORKERS = 4

# --- Database ---
DB_PATH = os.path.join(os.path.dirname(__file__), "nba_scraper.db")

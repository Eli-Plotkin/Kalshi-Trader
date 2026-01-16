import os
from dotenv import load_dotenv

# 1. Load variables from .env file into the environment
load_dotenv()

# --- API Endpoints ---
BASE_URL = "https://api.elections.kalshi.com/trade-api/v2" 

# --- Secrets (Loaded from .env) ---
# We raise an error if these are missing to prevent the bot from running insecurely
API_KEY_ID = os.getenv("API_KEY_ID")
PRIVATE_KEY_PATH = os.getenv("PRIVATE_KEY_PATH")

if not API_KEY_ID or not PRIVATE_KEY_PATH:
    raise ValueError("CRITICAL ERROR: API Key or Key Path missing from .env file.")

# --- Strategy Settings ---
# We use 'int()' to convert the string from .env. 
# The second value (e.g., 1000) is a default fallback if the .env var isn't found.

INVESTMENT_PER_BET = int(os.getenv("INVESTMENT_PER_BET", 200))   # Cents
BUY_PRICE_MIN = int(os.getenv("BUY_PRICE_MIN", 20))               # Cents
BUY_PRICE_MAX = int(os.getenv("BUY_PRICE_MAX", 35))               # Cents
SELL_PROFIT_TARGET = int(os.getenv("SELL_PROFIT_TARGET", 45))     # Cents

# --- Time Settings ---
SCAN_INTERVAL = int(os.getenv("SCAN_INTERVAL", 60))
WEEKLY_SCHEDULE = {
    0: (22, 4),  # Monday
    1: (22, 4),  # Tuesday
    2: (22, 4),  # Wednesday
    3: (22, 4),  # Thursday
    4: (22, 4),  # Friday
    5: (17, 4),  # Saturday (Start Noon EST)
    6: (16, 4),  # Sunday   (Start 11 AM EST)
}
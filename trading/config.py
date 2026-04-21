import os
from dotenv import load_dotenv

load_dotenv()

BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"

API_KEY_ID = os.getenv("API_KEY_ID")
PRIVATE_KEY_PATH = os.getenv("PRIVATE_KEY_PATH")

if not API_KEY_ID or not PRIVATE_KEY_PATH:
    raise ValueError("CRITICAL ERROR: API Key or Key Path missing from .env file.")

# --- Strategy Settings ---
SHARES_TO_BUY = int(os.getenv("SHARES_TO_BUY", 10))
FAVORITE_PRICE_MIN = int(os.getenv("FAVORITE_PRICE_MIN", 0))  # Cents
FAVORITE_PRICE_MAX = int(os.getenv("FAVORITE_PRICE_MAX", 0))  # Cents

# --- State ---
SCHEDULE_FILE = os.getenv("SCHEDULE_FILE", "schedule_data.json")
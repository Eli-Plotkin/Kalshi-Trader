import os
from dotenv import load_dotenv

load_dotenv()

BASE_URLS = {
    "prod": "https://api.elections.kalshi.com/trade-api/v2",
    "demo": "https://demo-api.kalshi.co/trade-api/v2",
}
KALSHI_ENV = os.getenv("KALSHI_ENV", "prod").lower()
if KALSHI_ENV not in BASE_URLS:
    raise ValueError(f"KALSHI_ENV must be one of {sorted(BASE_URLS)}, got {KALSHI_ENV!r}")
BASE_URL = BASE_URLS[KALSHI_ENV]

API_KEY_ID = os.getenv("API_KEY_ID")
PRIVATE_KEY_PATH = os.getenv("PRIVATE_KEY_PATH")

if not API_KEY_ID or not PRIVATE_KEY_PATH:
    raise ValueError("CRITICAL ERROR: API Key or Key Path missing from .env file.")
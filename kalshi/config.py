import os
from dotenv import load_dotenv

load_dotenv()

BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"

API_KEY_ID = os.getenv("API_KEY_ID")
PRIVATE_KEY_PATH = os.getenv("PRIVATE_KEY_PATH")

if not API_KEY_ID or not PRIVATE_KEY_PATH:
    raise ValueError("CRITICAL ERROR: API Key or Key Path missing from .env file.")
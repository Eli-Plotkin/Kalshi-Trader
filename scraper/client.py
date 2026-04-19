import time
import base64
import logging
import threading
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from cryptography.hazmat.primitives import serialization, hashes
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.backends import default_backend

from . import config

logger = logging.getLogger(__name__)

_thread_local = threading.local()


class ScraperClient:
    def __init__(self):
        self.base_url = config.BASE_URL.rstrip("/")
        self.key_id = config.API_KEY_ID

        with open(config.PRIVATE_KEY_PATH, "rb") as f:
            self.private_key = serialization.load_pem_private_key(
                f.read(), password=None, backend=default_backend()
            )

    def _get_session(self):
        """Return a per-thread requests.Session (requests.Session is not thread-safe)."""
        if not hasattr(_thread_local, "session"):
            session = requests.Session()
            retry = Retry(
                total=3,
                backoff_factor=1.0,
                status_forcelist=[429, 500, 502, 503, 504],
                allowed_methods=["GET"],
            )
            adapter = HTTPAdapter(max_retries=retry)
            session.mount("https://", adapter)
            session.mount("http://", adapter)
            _thread_local.session = session
        return _thread_local.session

    def _sign_request(self, method, endpoint_path):
        parsed_base = urlparse(self.base_url)
        full_path = parsed_base.path + endpoint_path
        timestamp = str(int(time.time() * 1000))
        msg = f"{timestamp}{method}{full_path}"

        signature = self.private_key.sign(
            msg.encode("utf-8"),
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.MAX_LENGTH,
            ),
            hashes.SHA256(),
        )

        return {
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode("utf-8"),
            "KALSHI-ACCESS-TIMESTAMP": timestamp,
            "Content-Type": "application/json",
        }

    def _get(self, path, params=None):
        headers = self._sign_request("GET", path)
        session = self._get_session()
        resp = session.get(
            f"{self.base_url}{path}", headers=headers, params=params, timeout=15
        )
        resp.raise_for_status()
        return resp.json()

    # ---- Markets ----

    def fetch_markets(self, status="open"):
        all_markets = []
        cursor = None
        while True:
            params = {
                "series_ticker": config.SERIES_TICKER,
                "status": status,
                "limit": 200,
            }
            if cursor:
                params["cursor"] = cursor
            data = self._get("/markets", params)
            batch = data.get("markets", [])
            all_markets.extend(batch)
            cursor = data.get("cursor")
            if not cursor or not batch:
                break
        return all_markets

    # ---- Orderbook ----

    def fetch_orderbook(self, ticker, depth=None):
        if depth is None:
            depth = config.ORDERBOOK_DEPTH
        params = {"depth": depth}
        data = self._get(f"/markets/{ticker}/orderbook", params)
        book = data.get("orderbook_fp") or data.get("orderbook", {})
        return {
            "yes": book.get("yes_dollars", book.get("yes", [])),
            "no": book.get("no_dollars", book.get("no", [])),
        }

    # ---- Trades ----

    def fetch_trades(self, ticker, min_ts=None):
        all_trades = []
        cursor = None
        first_page = True
        while True:
            params = {"ticker": ticker, "limit": config.TRADES_PAGE_LIMIT}
            # Only pass min_ts on the first page; subsequent pages use cursor only
            if first_page and min_ts:
                params["min_ts"] = min_ts
            if cursor:
                params["cursor"] = cursor
            data = self._get("/markets/trades", params)
            batch = data.get("trades", [])
            all_trades.extend(batch)
            cursor = data.get("cursor")
            first_page = False
            if not cursor or not batch:
                break
        return all_trades

    # ---- Market details (one-time) ----

    def fetch_market_details(self, ticker):
        return self._get(f"/markets/{ticker}")

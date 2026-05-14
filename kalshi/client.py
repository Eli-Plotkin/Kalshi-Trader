import time
import requests
import base64
import uuid  
import logging
from urllib.parse import urlparse
from cryptography.hazmat.primitives import serialization, hashes
from cryptography.hazmat.primitives.asymmetric import padding as asym_padding
from cryptography.hazmat.backends import default_backend

class KalshiClient:
    def __init__(self, base_url, key_id, key_file_path):
        self.base_url = base_url.rstrip('/') 
        self.key_id = key_id
        self.session = requests.Session()
        
        # Load the RSA Private Key
        with open(key_file_path, "rb") as key_file:
            self.private_key = serialization.load_pem_private_key(
                key_file.read(),
                password=None,
                backend=default_backend()
            )

    def _sign_request(self, method, endpoint_path):
        """
        method: GET, POST, DELETE
        endpoint_path: The relative path starting with / (e.g., "/markets")
        """
        # 1. Parse the base URL to get the V2 prefix (e.g., "/trade-api/v2")
        parsed_base = urlparse(self.base_url)
        base_path = parsed_base.path 
        
        # 2. Combine to get the full path for the signature
        # Result: "/trade-api/v2/markets"
        full_relative_path = base_path + endpoint_path

        timestamp = str(int(time.time() * 1000))
        
        # 3. Create the Signature Message
        msg = f"{timestamp}{method}{full_relative_path}"
        
        signature = self.private_key.sign(
            msg.encode('utf-8'),
            asym_padding.PSS(mgf=asym_padding.MGF1(hashes.SHA256()), salt_length=asym_padding.PSS.DIGEST_LENGTH),
            hashes.SHA256()
        )
        
        return {
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode('utf-8'),
            "KALSHI-ACCESS-TIMESTAMP": timestamp,
            "Content-Type": "application/json"
        }

    def fetch_nba_markets(self):
        """Fetches active NBA Game Winner markets."""
        path = "/markets"
        
        # CORRECT: Pass only "/markets" to the signer
        headers = self._sign_request("GET", path)
        
        # Kalshi V2 Filter: "series_ticker" is the most efficient way
        params = {"limit": 100, "status": "open", "series_ticker": "KXNBAGAME"}
        
        try:
            resp = self.session.get(f"{self.base_url}{path}", headers=headers, params=params)
            resp.raise_for_status()
            return resp.json().get("markets", [])
        except Exception as e:
            logging.error(f"Error fetching markets: {e}")
            return []

    def list_markets(self, status="open", limit=200, cursor=None, **extra_params):
        """Generic paginated market list. Returns (markets, next_cursor)."""
        path = "/markets"
        headers = self._sign_request("GET", path)
        params = {"limit": limit, "status": status, **extra_params}
        if cursor:
            params["cursor"] = cursor
        try:
            resp = self.session.get(f"{self.base_url}{path}", headers=headers, params=params)
            resp.raise_for_status()
            body = resp.json()
            return body.get("markets", []), body.get("cursor") or None
        except Exception as e:
            logging.error(f"list_markets failed: {e}")
            return [], None

    def get_balance(self):
        """Returns available cash in cents, or None on error."""
        path = "/portfolio/balance"
        headers = self._sign_request("GET", path)
        try:
            resp = self.session.get(f"{self.base_url}{path}", headers=headers)
            resp.raise_for_status()
            return resp.json().get("balance")
        except Exception as e:
            logging.error(f"get_balance failed: {e}")
            return None

    def list_positions(self):
        """Returns list of current market positions."""
        path = "/portfolio/positions"
        headers = self._sign_request("GET", path)
        try:
            resp = self.session.get(f"{self.base_url}{path}", headers=headers)
            resp.raise_for_status()
            return resp.json().get("market_positions", [])
        except Exception as e:
            logging.error(f"list_positions failed: {e}")
            return []

    def get_market(self, ticker):
        """Single-market fetch. Returns the market dict (with prices, status,
        result on settled markets) or None on error."""
        path = f"/markets/{ticker}"
        headers = self._sign_request("GET", path)
        try:
            resp = self.session.get(f"{self.base_url}{path}", headers=headers)
            resp.raise_for_status()
            return resp.json().get("market")
        except Exception as e:
            logging.error(f"get_market({ticker}) failed: {e}")
            return None

    def get_orderbook(self, ticker):
        """Gets the orderbook to calculate the spread."""
        path = f"/markets/{ticker}/orderbook"
        
        # CORRECT: Pass only the relative path
        headers = self._sign_request("GET", path)
        
        try:
            resp = self.session.get(f"{self.base_url}{path}", headers=headers)
            # Orderbooks for new markets might be empty, so handle 404 gracefully
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            return resp.json().get("orderbook_fp", {})
        except Exception as e:
            return None

    def place_limit_order(
        self,
        ticker,
        count,
        price,
        action="buy",
        side="yes",
        expiration_ts=None,
        client_order_id=None,
    ):
        path = "/portfolio/orders"
        client_order_id = client_order_id or str(uuid.uuid4())
        safe_price = int(price)

        payload = {
            "ticker": ticker,
            "action": action,
            "side": side,
            "count": count,
            "client_order_id": client_order_id,
        }
        # Kalshi requires yes_price for side=yes, no_price for side=no
        if side == "yes":
            payload["yes_price"] = safe_price
        else:
            payload["no_price"] = safe_price

        # expiration_ts: unix seconds (Kalshi v2). Order auto-cancels at this time if unfilled.
        if expiration_ts is not None:
            payload["expiration_ts"] = int(expiration_ts)
        
        headers = self._sign_request("POST", path)
        
        try:
            resp = self.session.post(f"{self.base_url}{path}", json=payload, headers=headers)
            resp.raise_for_status()
            return resp.json().get("order", {})
            
        except requests.exceptions.HTTPError as e:
            # --- NEW: SMART ERROR HANDLING ---
            error_msg = resp.text.lower()
            if "insufficient" in error_msg or "balance" in error_msg:
                logging.warning("ORDER REJECTED: INSUFFICIENT FUNDS")
                return "INSUFFICIENT_FUNDS"  # Special signal
            
            logging.error(f"Order Failed: {e}")
            logging.error(f"Server Response: {resp.text}") 
            return None
            
        except Exception as e:
            logging.error(f"Connection Error: {e}")
            return None

    def get_order_status(self, order_id):
        path = f"/portfolio/orders/{order_id}"
        headers = self._sign_request("GET", path)
        try:
            resp = self.session.get(f"{self.base_url}{path}", headers=headers)
            resp.raise_for_status()
            return resp.json().get("order", {})
        except:
            return None

    def cancel_order(self, order_id):
        path = f"/portfolio/orders/{order_id}"
        headers = self._sign_request("DELETE", path)
        try:
            resp = self.session.delete(f"{self.base_url}{path}", headers=headers)
            
            # If it's 404, the order is already gone (filled or cancelled).
            # This is NOT a crash-worthy error.
            if resp.status_code == 404:
                return False 
                
            resp.raise_for_status()
            return True
        except requests.exceptions.HTTPError as e:
            # If it's a 400 (e.g. "Cannot cancel filled order"), we also return False
            # so the main logic proceeds to check the status.
            return False
        except Exception:
            return False
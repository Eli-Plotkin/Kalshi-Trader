import time
from email.utils import parsedate_to_datetime
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import base64
import uuid  
import logging
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from urllib.parse import urlparse
from cryptography.hazmat.primitives import serialization, hashes
from cryptography.hazmat.primitives.asymmetric import padding as asym_padding
from cryptography.hazmat.backends import default_backend

MAX_RETRY_AFTER_SECONDS = 10
MAX_SKEW_ROUND_TRIP_SECONDS = 5.0
SKEW_SAMPLES = 3


def estimate_clock_skew(session, url, timeout=5, now=time.time,
                        samples=SKEW_SAMPLES, max_round_trip=MAX_SKEW_ROUND_TRIP_SECONDS):
    """Estimate local clock skew (seconds) from an HTTP `Date` response header.

    Returns local - server: positive means the local clock is AHEAD. Any response
    carries a Date header (even an error status), so the status is not checked.
    The header has 1-second resolution, so the server time is taken as the middle
    of that second, and local time as the midpoint of the request round trip.

    The midpoint is only as good as the round trip is short: a request that took 60s
    (laptop slept mid-request, network stall) leaves the midpoint ~30s off. So, as NTP
    does, take up to `samples` readings, keep the one with the SHORTEST round trip, and
    raise if even that exceeds `max_round_trip` rather than report a bogus number.
    Accuracy is about +-1s for a normal round trip. Raises on network errors, a
    missing/unparseable Date header, or no trustworthy sample; `session` and `now` are
    injectable for tests.
    """
    best = None                      # (round_trip, skew)
    for _ in range(max(1, samples)):
        sent = now()
        resp = session.get(url, timeout=timeout)
        received = now()
        date_header = resp.headers.get("Date")
        if not date_header:
            raise ValueError("response has no Date header")
        server = parsedate_to_datetime(date_header).timestamp() + 0.5
        round_trip = received - sent
        skew = (sent + received) / 2.0 - server
        if best is None or round_trip < best[0]:
            best = (round_trip, skew)
        if round_trip <= max_round_trip:
            break                    # a fast sample is trustworthy; no need for more
    if best[0] > max_round_trip:
        raise ValueError(f"clock-skew measurement unreliable: best round trip took "
                         f"{best[0]:.1f}s (limit {max_round_trip:.0f}s); machine asleep or network stalled?")
    return best[1]


# ----------------------------------------------------------------------------
# Tolerant parsers for the reconciliation read endpoints.
#
# Kalshi is mid-migration from integer-cent / integer-count fields (`yes_price`,
# `fill_count`, `market_exposure`, ...) to fixed-point strings (`*_dollars`,
# `*_fp`). Every parser prefers the new name, falls back to the legacy one, and
# returns None for anything missing or unparseable. Money is parsed with Decimal
# (never float) and rounded half-up to whole cents.
# ----------------------------------------------------------------------------

# Hard cap on pages followed per call. 50 pages x 1000 rows is far beyond anything this
# bot produces; hitting it raises (a silently truncated list could fake a "not found").
MAX_PAGES = 50
PAGE_LIMIT = 1000
ORDER_STATUSES = ("resting", "executed", "canceled")


def _dec(value):
    """Decimal from an int / numeric string / float; None if absent or unparseable."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str) and not value.strip():
        return None
    try:
        d = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None
    return d if d.is_finite() else None


def _round_int(d):
    return None if d is None else int(d.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def _first(d, *keys):
    """First present (non-None) value among `keys` of dict d."""
    for k in keys:
        if d.get(k) is not None:
            return d[k]
    return None


def _cents_from_dollars(value):
    """Fixed-point dollar string ("0.9300") -> integer cents (93)."""
    d = _dec(value)
    return None if d is None else _round_int(d * 100)


def _cents_dec(d, dollars_key, legacy_key):
    """Unrounded cents as Decimal: `<x>_dollars` field preferred, else legacy cents."""
    v = _dec(d.get(dollars_key)) if dollars_key else None
    if v is not None:
        return v * 100
    return _dec(d.get(legacy_key)) if legacy_key else None


def _cents(d, dollars_key, legacy_key):
    return _round_int(_cents_dec(d, dollars_key, legacy_key))


def _count(d, fp_key, legacy_key):
    """Contract count: `<x>_fp` string preferred, else legacy int. Rounded to whole contracts."""
    v = _dec(d.get(fp_key))
    if v is None and legacy_key:
        v = _dec(d.get(legacy_key))
    return _round_int(v)


def _lower(value):
    return str(value).strip().lower() if value not in (None, "") else None


def _ts_str(value):
    return None if value in (None, "") else str(value)


def _unix_ts(value):
    """min_ts argument -> int unix seconds (accepts int/float/numeric str/datetime)."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return int(value.timestamp())
    return int(float(value))


def _yes_price_cents(d):
    """YES-leg price in cents from yes_price_dollars / yes_price, else 100 - no price."""
    yes = _cents(d, "yes_price_dollars", "yes_price")
    if yes is not None:
        return yes
    no = _cents(d, "no_price_dollars", "no_price")
    return None if no is None else 100 - no


def normalize_order(o):
    """Kalshi order object -> contract dict (see docs/design-g4-reconciliation.md s4).

    `avg_fill_cents` is total fill cost / filled contracts, in cents (float, 4 dp).
    The bot only buys YES, where that equals the YES-leg price; `side` is included so a
    caller can tell if it ever sees a NO order.
    """
    filled = _count(o, "fill_count_fp", "fill_count")
    remaining = _count(o, "remaining_count_fp", "remaining_count")
    cost = None
    for dk, lk in (("taker_fill_cost_dollars", "taker_fill_cost"),
                   ("maker_fill_cost_dollars", "maker_fill_cost")):
        part = _cents_dec(o, dk, lk)
        if part is not None:
            cost = part if cost is None else cost + part
    avg = None
    if cost is not None and filled:
        avg = round(float(cost / filled), 4)
    return {
        "order_id": _first(o, "order_id", "id"),
        "client_order_id": o.get("client_order_id") or None,
        "ticker": o.get("ticker"),
        "status": _lower(o.get("status")),
        "filled": filled if filled is not None else 0,
        "remaining": remaining,
        "limit_cents": _yes_price_cents(o),
        "avg_fill_cents": avg,
        "created_time": _ts_str(o.get("created_time")),
        "expiration_time": _ts_str(o.get("expiration_time")),
        "side": _lower(_first(o, "outcome_side", "side")),
    }


def normalize_fill(f):
    return {
        "fill_id": _first(f, "fill_id", "trade_id"),
        "order_id": f.get("order_id") or None,
        "ticker": _first(f, "ticker", "market_ticker"),
        "count": _count(f, "count_fp", "count") or 0,
        "price_cents": _yes_price_cents(f),
        "side": _lower(_first(f, "outcome_side", "side")),
        "action": _lower(f.get("action")),          # deprecated by Kalshi; None once removed
        "fee_cents": _cents_from_dollars(f.get("fee_cost")),
        "fee_dollars": None if _dec(f.get("fee_cost")) is None else str(_dec(f.get("fee_cost"))),
        "ts": _ts_str(_first(f, "created_time", "ts")),
    }


def normalize_position(p):
    pos = _count(p, "position_fp", "position")
    return {
        "ticker": p.get("ticker"),
        "position": pos if pos is not None else 0,
        "exposure_cents": _cents(p, "market_exposure_dollars", "market_exposure"),
        "realized_pnl_cents": _cents(p, "realized_pnl_dollars", "realized_pnl"),
        "fees_paid_cents": _cents(p, "fees_paid_dollars", "fees_paid"),
    }


def normalize_settlement(s):
    result = _lower(s.get("market_result"))
    return {
        "ticker": s.get("ticker"),
        "result": result if result in ("yes", "no") else None,
        "yes_count": _count(s, "yes_count_fp", "yes_count") or 0,
        "no_count": _count(s, "no_count_fp", "no_count") or 0,
        "revenue_cents": _cents(s, "revenue_dollars", "revenue"),
        "settled_ts": _ts_str(_first(s, "settled_time", "settled_ts")),
        "fee_cents": _cents_from_dollars(s.get("fee_cost")),
        "yes_total_cost_cents": _cents(s, "yes_total_cost_dollars", "yes_total_cost"),
        "no_total_cost_cents": _cents(s, "no_total_cost_dollars", "no_total_cost"),
    }


class KalshiClient:
    def __init__(self, base_url, key_id, key_file_path, timeout=10):
        self.base_url = base_url.rstrip('/') 
        self.timeout = timeout
        self.key_id = key_id
        self.session = requests.Session()
        # Retry READS only on rate-limit / transient server errors (honours
        # Retry-After). POST is never retried here: a retried order could double-buy;
        # order safety is handled by client_order_id + reconciliation instead.
        retry_kwargs = dict(total=3, backoff_factor=1.0, status_forcelist=(429, 500, 502, 503, 504),
                            allowed_methods=frozenset({"GET"}), respect_retry_after_header=True)
        try:
            # urllib3 defaults to honouring a Retry-After of up to 6 HOURS. A request that sleeps that
            # long while the bot holds its trade lock would stall every game, so cap it.
            retry = Retry(retry_after_max=MAX_RETRY_AFTER_SECONDS, **retry_kwargs)
        except TypeError:                 # older urllib3 without the parameter
            retry_kwargs["respect_retry_after_header"] = False
            retry = Retry(**retry_kwargs)
        self.session.mount("https://", HTTPAdapter(max_retries=retry))
        self.session.mount("http://", HTTPAdapter(max_retries=retry))
        
        # Load the RSA Private Key
        with open(key_file_path, "rb") as key_file:
            self.private_key = serialization.load_pem_private_key(
                key_file.read(),
                password=None,
                backend=default_backend()
            )

    def clock_skew_seconds(self):
        """Local clock minus Kalshi's (seconds), via a cheap unauthenticated GET."""
        return estimate_clock_skew(self.session, f"{self.base_url}/exchange/status",
                                   timeout=self.timeout)

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

    def fetch_nba_markets(self, raise_on_error=False):
        """Fetches active NBA Game Winner markets.

        Errors are swallowed to [] by default; pass raise_on_error=True when an empty
        list must be distinguishable from a failed fetch (e.g. building the schedule).
        """
        path = "/markets"
        
        # CORRECT: Pass only "/markets" to the signer
        headers = self._sign_request("GET", path)
        
        # Kalshi V2 Filter: "series_ticker" is the most efficient way
        params = {"limit": 100, "status": "open", "series_ticker": "KXNBAGAME"}
        
        try:
            resp = self.session.get(f"{self.base_url}{path}", headers=headers, params=params, timeout=self.timeout)
            resp.raise_for_status()
            return resp.json().get("markets", [])
        except Exception as e:
            logging.error(f"Error fetching markets: {e}")
            if raise_on_error:
                raise
            return []

    def list_markets(self, status="open", limit=200, cursor=None, **extra_params):
        """Generic paginated market list. Returns (markets, next_cursor)."""
        path = "/markets"
        headers = self._sign_request("GET", path)
        params = {"limit": limit, "status": status, **extra_params}
        if cursor:
            params["cursor"] = cursor
        try:
            resp = self.session.get(f"{self.base_url}{path}", headers=headers, params=params, timeout=self.timeout)
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
            resp = self.session.get(f"{self.base_url}{path}", headers=headers, timeout=self.timeout)
            resp.raise_for_status()
            return resp.json().get("balance")
        except Exception as e:
            logging.error(f"get_balance failed: {e}")
            return None

    def list_positions(self, raise_on_error=False):
        """Returns list of current market positions.

        By default errors are swallowed and an empty list returned, which is
        indistinguishable from "no positions". Callers that act on emptiness
        (e.g. clearing local state) must pass raise_on_error=True.
        """
        path = "/portfolio/positions"
        headers = self._sign_request("GET", path)
        try:
            resp = self.session.get(f"{self.base_url}{path}", headers=headers, timeout=self.timeout)
            resp.raise_for_status()
            return resp.json().get("market_positions", [])
        except Exception as e:
            logging.error(f"list_positions failed: {e}")
            if raise_on_error:
                raise
            return []

    def get_market(self, ticker):
        """Single-market fetch. Returns the market dict (with prices, status,
        result on settled markets) or None on error."""
        path = f"/markets/{ticker}"
        headers = self._sign_request("GET", path)
        try:
            resp = self.session.get(f"{self.base_url}{path}", headers=headers, timeout=self.timeout)
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
            resp = self.session.get(f"{self.base_url}{path}", headers=headers, timeout=self.timeout)
            # Orderbooks for new markets might be empty, so handle 404 gracefully
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            return resp.json().get("orderbook_fp", {})
        except Exception as e:
            return None

    @staticmethod
    def _to_v2_leg(action, side, price_cents):
        """Map (buy/sell, yes/no, price) onto V2's single YES-leg book.

        V2 only speaks bid/ask on the YES leg. Buying NO at p is an ask on YES
        at 100-p; selling NO at p is a bid on YES at 100-p.
        """
        if side == "yes":
            return ("bid" if action == "buy" else "ask"), price_cents
        return ("ask" if action == "buy" else "bid"), 100 - price_cents

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
        """Place a limit order via the V2 endpoint (POST /portfolio/events/orders).

        `price` is in cents for the contract named by `side`. Returns the V2
        response dict (order_id, fill_count, remaining_count, ...) on success,
        "INSUFFICIENT_FUNDS" if rejected for balance, "LOCATION_ATTESTATION" if Kalshi
        requires the user to re-verify location (HTTP 403), "DUPLICATE_ORDER" if
        Kalshi already has an order with this client_order_id (HTTP 409), or
        None on any other failure.
        """
        path = "/portfolio/events/orders"
        client_order_id = client_order_id or str(uuid.uuid4())
        v2_side, yes_cents = self._to_v2_leg(action, side, int(price))

        payload = {
            "ticker": ticker,
            "side": v2_side,
            "count": str(int(count)),
            "price": f"{yes_cents / 100:.4f}",
            "time_in_force": "good_till_canceled",
            "self_trade_prevention_type": "taker_at_cross",
            "client_order_id": client_order_id,
        }
        # expiration_time: unix seconds; only valid with good_till_canceled.
        if expiration_ts is not None:
            payload["expiration_time"] = int(expiration_ts)

        headers = self._sign_request("POST", path)

        try:
            resp = self.session.post(f"{self.base_url}{path}", json=payload, headers=headers, timeout=self.timeout)
            resp.raise_for_status()
            return resp.json()

        except requests.exceptions.HTTPError as e:
            if resp.status_code == 409:
                logging.warning(
                    f"DUPLICATE ORDER: client_order_id {client_order_id} already exists"
                )
                return "DUPLICATE_ORDER"

            error_msg = resp.text.lower()
            if resp.status_code == 403 and "location_attestation" in error_msg.replace(" ", "_"):
                logging.critical(
                    "ORDER REJECTED: Kalshi location attestation missing/expired -- "
                    "re-verify location in the Kalshi app or website. No orders will go through."
                )
                return "LOCATION_ATTESTATION"
            if "insufficient" in error_msg or "balance" in error_msg:
                logging.warning("ORDER REJECTED: INSUFFICIENT FUNDS")
                return "INSUFFICIENT_FUNDS"

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
            resp = self.session.get(f"{self.base_url}{path}", headers=headers, timeout=self.timeout)
            resp.raise_for_status()
            return resp.json().get("order", {})
        except Exception:
            return None

    def cancel_order(self, order_id, market_ticker=None):
        """Cancel via V2 (DELETE /portfolio/events/orders/{order_id}).

        `market_ticker` lets Kalshi auto-route to the right shard.
        """
        path = f"/portfolio/events/orders/{order_id}"
        headers = self._sign_request("DELETE", path)
        kwargs = {"params": {"market_ticker": market_ticker}} if market_ticker else {}
        try:
            resp = self.session.delete(f"{self.base_url}{path}", headers=headers, **kwargs, timeout=self.timeout)
            
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

    # ------------------------------------------------------------------
    # Read-only reconciliation endpoints (docs/design-g4-reconciliation.md s4).
    # These RAISE on any network / HTTP error: None / [] mean a CONFIRMED
    # not-found / empty, never "could not ask". GET only; nothing here can
    # place, amend or cancel an order.
    # ------------------------------------------------------------------

    def _get_json(self, path, params=None, not_found_ok=False):
        """Signed GET. The signature covers `path` only (no query string). Raises on
        errors; returns None on HTTP 404 only when `not_found_ok`."""
        headers = self._sign_request("GET", path)
        kwargs = {"params": params} if params else {}
        resp = self.session.get(f"{self.base_url}{path}", headers=headers,
                                timeout=self.timeout, **kwargs)
        if not_found_ok and resp.status_code == 404:
            return None
        resp.raise_for_status()
        body = resp.json()
        if not isinstance(body, dict):
            raise ValueError(f"GET {path}: expected a JSON object, got {type(body).__name__}")
        return body

    def _get_paginated(self, path, key, params=None, max_pages=None):
        """Follow `cursor` until exhausted; returns the concatenated `key` rows (dicts only).
        Raises if more than `max_pages` pages are needed or the cursor repeats."""
        max_pages = max_pages or MAX_PAGES
        params = dict(params or {})
        params.setdefault("limit", PAGE_LIMIT)
        rows, seen = [], set()
        for _ in range(max_pages):
            body = self._get_json(path, params)
            rows.extend(r for r in (body.get(key) or []) if isinstance(r, dict))
            cursor = body.get("cursor") or None
            if not cursor:
                return rows
            if cursor in seen:
                raise RuntimeError(f"GET {path}: pagination cursor repeated ({cursor!r})")
            seen.add(cursor)
            params["cursor"] = cursor
        raise RuntimeError(f"GET {path}: more than {max_pages} pages; refusing to return a truncated list")

    def get_order(self, order_id):
        """GET /portfolio/orders/{order_id} -> normalized order dict, or None on HTTP 404."""
        body = self._get_json(f"/portfolio/orders/{order_id}", not_found_ok=True)
        if body is None:
            return None
        order = body.get("order")
        if not isinstance(order, dict) or not order:
            return None
        return normalize_order(order)

    def get_order_by_client_id(self, client_order_id, ticker=None, include_historical=True):
        """Find an order by our client_order_id. Returns the normalized order or None.

        The orders endpoint has NO client_order_id filter, so list orders (filtered by
        `ticker` when given, once per status: resting/executed/canceled), following
        pagination, and match locally; if still unmatched and `include_historical`,
        also scan GET /historical/orders. None is returned only after every listing
        completed without error; any failure raises so a lookup problem is never
        mistaken for "the order does not exist".
        """
        if not client_order_id:
            raise ValueError("client_order_id is required")
        base = {"ticker": ticker} if ticker else {}
        scans = [("/portfolio/orders", {**base, "status": s}) for s in ORDER_STATUSES]
        if include_historical:
            scans.append(("/historical/orders", dict(base)))
        for path, params in scans:
            for o in self._get_paginated(path, "orders", params):
                if o.get("client_order_id") == client_order_id:
                    return normalize_order(o)
        return None

    def list_fills(self, ticker=None, order_id=None, min_ts=None):
        """GET /portfolio/fills (paginated) -> list of normalized fills."""
        params = {}
        if ticker:
            params["ticker"] = ticker
        if order_id:
            params["order_id"] = order_id
        if min_ts is not None:
            params["min_ts"] = _unix_ts(min_ts)
        return [normalize_fill(f) for f in self._get_paginated("/portfolio/fills", "fills", params)]

    def list_positions_detailed(self, count_filter="position"):
        """GET /portfolio/positions (paginated) -> normalized market positions.

        `count_filter="position"` asks Kalshi for markets with a non-zero position only;
        pass None to omit the filter."""
        params = {"count_filter": count_filter} if count_filter else {}
        rows = self._get_paginated("/portfolio/positions", "market_positions", params)
        return [normalize_position(p) for p in rows]

    def list_settlements(self, min_ts=None):
        """GET /portfolio/settlements (paginated) -> normalized settlement records."""
        params = {"min_ts": _unix_ts(min_ts)} if min_ts is not None else {}
        return [normalize_settlement(s)
                for s in self._get_paginated("/portfolio/settlements", "settlements", params)]

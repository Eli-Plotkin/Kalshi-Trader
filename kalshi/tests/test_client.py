"""Direct unit tests for kalshi.client.KalshiClient.

This is the only module in the repo that signs and sends live orders to
Kalshi, so these tests focus on:
  - Correct request signing (PSS/SHA256 signature over the right message).
  - Correct base_url + path concatenation (a past source of bugs).
  - place_limit_order success/error handling, including the
    insufficient-funds string-matching classification.
  - get_order_status / cancel_order success and error paths.
  - That private key material / API key never leak into exceptions,
    logs, or returned error values.
"""

from __future__ import annotations

import base64
import logging
import uuid

import pytest
import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding as asym_padding
from cryptography.hazmat.primitives.asymmetric import rsa

from kalshi.client import KalshiClient


# ----------------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------------

SECRET_KEY_ID = "SECRET-KEY-ID-DO-NOT-LEAK"


@pytest.fixture(scope="session")
def rsa_keypair():
    """Generate one deterministic-enough RSA keypair for the whole test
    session (key generation is somewhat expensive, so we do it once)."""
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return private_key, pem


@pytest.fixture()
def key_file_path(tmp_path, rsa_keypair):
    _, pem = rsa_keypair
    path = tmp_path / "test_key.pem"
    path.write_bytes(pem)
    return str(path)


@pytest.fixture()
def client(key_file_path):
    return KalshiClient(
        base_url="https://api.elections.kalshi.com/trade-api/v2",
        key_id=SECRET_KEY_ID,
        key_file_path=key_file_path,
    )


class FakeResponse:
    """Minimal stand-in for requests.Response."""

    def __init__(self, status_code=200, json_data=None, text=""):
        self.status_code = status_code
        self._json_data = json_data if json_data is not None else {}
        self.text = text

    def json(self):
        return self._json_data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(
                f"{self.status_code} Error", response=self
            )


# ----------------------------------------------------------------------------
# __init__ / key loading
# ----------------------------------------------------------------------------


class TestInit:
    def test_loads_rsa_private_key(self, client, rsa_keypair):
        private_key, _ = rsa_keypair
        assert client.private_key.private_numbers().d == private_key.private_numbers().d

    def test_strips_trailing_slash_from_base_url(self, key_file_path):
        c = KalshiClient(
            base_url="https://example.com/trade-api/v2/",
            key_id="k",
            key_file_path=key_file_path,
        )
        assert c.base_url == "https://example.com/trade-api/v2"

    def test_stores_key_id(self, client):
        assert client.key_id == SECRET_KEY_ID


# ----------------------------------------------------------------------------
# _sign_request
# ----------------------------------------------------------------------------


class TestSignRequest:
    def test_headers_have_expected_keys(self, client):
        headers = client._sign_request("GET", "/markets")
        assert set(headers.keys()) == {
            "KALSHI-ACCESS-KEY",
            "KALSHI-ACCESS-SIGNATURE",
            "KALSHI-ACCESS-TIMESTAMP",
            "Content-Type",
        }
        assert headers["KALSHI-ACCESS-KEY"] == SECRET_KEY_ID
        assert headers["Content-Type"] == "application/json"

    def test_timestamp_is_milliseconds_int_string(self, client, monkeypatch):
        monkeypatch.setattr("kalshi.client.time.time", lambda: 1700000000.123456)
        headers = client._sign_request("GET", "/markets")
        assert headers["KALSHI-ACCESS-TIMESTAMP"] == "1700000000123"

    def test_signature_verifies_against_expected_message(self, client, rsa_keypair):
        """The signature must be over f"{timestamp}{method}{full_relative_path}"
        using PSS/SHA256, matching Kalshi's documented scheme."""
        private_key, _ = rsa_keypair
        public_key = private_key.public_key()

        headers = client._sign_request("POST", "/portfolio/orders")
        timestamp = headers["KALSHI-ACCESS-TIMESTAMP"]
        signature = base64.b64decode(headers["KALSHI-ACCESS-SIGNATURE"])

        expected_msg = f"{timestamp}POST/trade-api/v2/portfolio/orders".encode("utf-8")

        # Should not raise if the signature is over the expected message.
        public_key.verify(
            signature,
            expected_msg,
            asym_padding.PSS(
                mgf=asym_padding.MGF1(hashes.SHA256()),
                salt_length=asym_padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )

    def test_signature_does_not_verify_against_wrong_path(self, client, rsa_keypair):
        private_key, _ = rsa_keypair
        public_key = private_key.public_key()

        headers = client._sign_request("POST", "/portfolio/orders")
        timestamp = headers["KALSHI-ACCESS-TIMESTAMP"]
        signature = base64.b64decode(headers["KALSHI-ACCESS-SIGNATURE"])

        wrong_msg = f"{timestamp}POST/trade-api/v2/markets".encode("utf-8")

        from cryptography.exceptions import InvalidSignature

        with pytest.raises(InvalidSignature):
            public_key.verify(
                signature,
                wrong_msg,
                asym_padding.PSS(
                    mgf=asym_padding.MGF1(hashes.SHA256()),
                    salt_length=asym_padding.PSS.DIGEST_LENGTH,
                ),
                hashes.SHA256(),
            )

    def test_full_relative_path_includes_base_path_prefix_once(self, client):
        """base_url is https://.../trade-api/v2 — the signed path must be
        '/trade-api/v2/markets', with no double slash and no missing slash."""
        captured = {}
        real_private_key = client.private_key

        class SpyKey:
            def sign(self, data, *args, **kwargs):
                captured["msg"] = data
                return real_private_key.sign(data, *args, **kwargs)

        client.private_key = SpyKey()
        client._sign_request("GET", "/markets")

        msg = captured["msg"].decode("utf-8")
        assert "/trade-api/v2/markets" in msg
        assert "/trade-api/v2//markets" not in msg
        assert "trade-api/v2markets" not in msg

    def test_path_concatenation_when_base_url_has_no_path(self, key_file_path):
        c = KalshiClient(
            base_url="https://example.com",
            key_id="k",
            key_file_path=key_file_path,
        )
        real_private_key = c.private_key
        captured = {}

        class SpyKey:
            def sign(self, data, *args, **kwargs):
                captured["msg"] = data
                return real_private_key.sign(data, *args, **kwargs)

        c.private_key = SpyKey()
        c._sign_request("GET", "/markets")

        msg = captured["msg"].decode("utf-8")
        assert msg.endswith("GET/markets")


# ----------------------------------------------------------------------------
# place_limit_order
# ----------------------------------------------------------------------------


class TestPlaceLimitOrder:
    def test_success_posts_v2_payload_and_returns_body(self, client):
        v2_body = {"order_id": "abc123", "fill_count": "0.00", "remaining_count": "5.00"}
        captured = {}

        def fake_post(url, json=None, headers=None, **kwargs):
            captured["url"] = url
            captured["json"] = json
            return FakeResponse(201, json_data=v2_body)

        client.session.post = fake_post

        result = client.place_limit_order(
            ticker="KX-TEST", count=5, price=56, action="buy", side="yes",
            expiration_ts=1700000000,
        )

        assert result == v2_body
        assert captured["url"] == "https://api.elections.kalshi.com/trade-api/v2/portfolio/events/orders"
        body = captured["json"]
        assert body["ticker"] == "KX-TEST"
        assert body["side"] == "bid"
        assert body["count"] == "5"
        assert body["price"] == "0.5600"
        assert body["time_in_force"] == "good_till_canceled"
        assert body["self_trade_prevention_type"] == "taker_at_cross"
        assert body["expiration_time"] == 1700000000
        assert "expiration_ts" not in body
        assert "yes_price" not in body and "no_price" not in body

    def test_expiration_omitted_when_not_given(self, client):
        captured = {}

        def fake_post(url, json=None, headers=None, **kwargs):
            captured["json"] = json
            return FakeResponse(201, json_data={"order_id": "x"})

        client.session.post = fake_post
        client.place_limit_order(ticker="KX-TEST", count=1, price=50)
        assert "expiration_time" not in captured["json"]

    @pytest.mark.parametrize(
        "action,side,price,expected_side,expected_price",
        [
            ("buy", "yes", 35, "bid", "0.3500"),
            ("sell", "yes", 35, "ask", "0.3500"),
            ("buy", "no", 35, "ask", "0.6500"),
            ("sell", "no", 35, "bid", "0.6500"),
        ],
    )
    def test_action_side_maps_to_v2_yes_leg(
        self, client, action, side, price, expected_side, expected_price
    ):
        captured = {}

        def fake_post(url, json=None, headers=None, **kwargs):
            captured["json"] = json
            return FakeResponse(201, json_data={"order_id": "x"})

        client.session.post = fake_post
        client.place_limit_order(
            ticker="KX-TEST", count=1, price=price, action=action, side=side
        )
        assert captured["json"]["side"] == expected_side
        assert captured["json"]["price"] == expected_price

    def test_duplicate_client_order_id_409_returns_sentinel(self, client):
        def fake_post(url, json=None, headers=None, **kwargs):
            return FakeResponse(409, text='{"code":"conflict","message":"order already exists"}')

        client.session.post = fake_post
        result = client.place_limit_order(
            ticker="KX-TEST", count=1, price=50, client_order_id="dup-1"
        )
        assert result == "DUPLICATE_ORDER"

    def test_client_order_id_defaults_to_fresh_uuid(self, client):
        seen_ids = []

        def fake_post(url, json=None, headers=None, **kwargs):
            seen_ids.append(json["client_order_id"])
            return FakeResponse(201, json_data={"order_id": "x"})

        client.session.post = fake_post

        client.place_limit_order(ticker="KX-TEST", count=1, price=50)
        client.place_limit_order(ticker="KX-TEST", count=1, price=50)

        assert len(seen_ids) == 2
        assert seen_ids[0] != seen_ids[1]
        # Both should be valid UUIDs.
        uuid.UUID(seen_ids[0])
        uuid.UUID(seen_ids[1])

    def test_caller_supplied_client_order_id_is_used_verbatim(self, client):
        captured = {}

        def fake_post(url, json=None, headers=None, **kwargs):
            captured["id"] = json["client_order_id"]
            return FakeResponse(201, json_data={"order_id": "x"})

        client.session.post = fake_post
        client.place_limit_order(
            ticker="KX-TEST", count=1, price=50, client_order_id="my-custom-id-1"
        )
        assert captured["id"] == "my-custom-id-1"

    @pytest.mark.parametrize(
        "error_text",
        [
            "Order rejected: insufficient balance",
            "INSUFFICIENT FUNDS FOR ORDER",
            "Your balance is too low",
        ],
    )
    def test_insufficient_funds_response_returns_sentinel(self, client, error_text):
        def fake_post(url, json=None, headers=None, **kwargs):
            return FakeResponse(400, text=error_text)

        client.session.post = fake_post
        result = client.place_limit_order(ticker="KX-TEST", count=1, price=50)
        assert result == "INSUFFICIENT_FUNDS"

    def test_generic_http_error_returns_none(self, client, caplog):
        def fake_post(url, json=None, headers=None, **kwargs):
            return FakeResponse(422, text="Unprocessable: bad ticker format")

        client.session.post = fake_post
        with caplog.at_level(logging.ERROR):
            result = client.place_limit_order(ticker="KX-TEST", count=1, price=50)
        assert result is None

    def test_connection_error_returns_none(self, client):
        def fake_post(url, json=None, headers=None, **kwargs):
            raise requests.exceptions.ConnectionError("network down")

        client.session.post = fake_post
        result = client.place_limit_order(ticker="KX-TEST", count=1, price=50)
        assert result is None

    def test_no_key_material_leaks_on_error(self, client, rsa_keypair, caplog):
        """Ensure neither the API key id nor the PEM-encoded private key
        ever appear in logged output or in the return value on error."""
        _, pem = rsa_keypair
        pem_text = pem.decode("utf-8")

        def fake_post(url, json=None, headers=None, **kwargs):
            return FakeResponse(400, text="insufficient funds in account")

        client.session.post = fake_post
        with caplog.at_level(logging.DEBUG):
            result = client.place_limit_order(ticker="KX-TEST", count=1, price=50)

        assert result == "INSUFFICIENT_FUNDS"
        log_text = caplog.text
        assert SECRET_KEY_ID not in log_text
        assert pem_text not in log_text
        assert "BEGIN RSA PRIVATE KEY" not in log_text
        assert SECRET_KEY_ID not in str(result)


# ----------------------------------------------------------------------------
# get_order_status
# ----------------------------------------------------------------------------


class TestGetOrderStatus:
    def test_success_returns_order_dict(self, client):
        fake_order = {"order_id": "xyz", "status": "filled"}

        def fake_get(url, headers=None, **kwargs):
            assert url == "https://api.elections.kalshi.com/trade-api/v2/portfolio/orders/xyz"
            return FakeResponse(200, json_data={"order": fake_order})

        client.session.get = fake_get
        assert client.get_order_status("xyz") == fake_order

    def test_http_error_returns_none(self, client):
        def fake_get(url, headers=None, **kwargs):
            return FakeResponse(404, text="not found")

        client.session.get = fake_get
        assert client.get_order_status("missing-id") is None

    def test_unexpected_exception_is_caught(self, client):
        """Regression test for the former bare `except:` — verifies that a
        non-HTTPError exception (e.g. malformed JSON) is still handled
        gracefully rather than propagating or being silently swallowed by
        something broader than Exception."""

        def fake_get(url, headers=None, **kwargs):
            class BadResponse(FakeResponse):
                def json(self):
                    raise ValueError("malformed json")

            r = BadResponse(200)
            return r

        client.session.get = fake_get
        assert client.get_order_status("some-id") is None


# ----------------------------------------------------------------------------
# cancel_order
# ----------------------------------------------------------------------------


class TestCancelOrder:
    def test_success_returns_true(self, client):
        def fake_delete(url, headers=None, **kwargs):
            assert url == "https://api.elections.kalshi.com/trade-api/v2/portfolio/events/orders/abc"
            return FakeResponse(200)

        client.session.delete = fake_delete
        assert client.cancel_order("abc") is True

    def test_market_ticker_passed_as_query_param_not_signed_path(self, client):
        captured = {}

        def fake_delete(url, headers=None, params=None, **kwargs):
            captured["url"] = url
            captured["params"] = params
            return FakeResponse(200)

        client.session.delete = fake_delete
        assert client.cancel_order("abc", market_ticker="KX-TEST") is True
        assert captured["params"] == {"market_ticker": "KX-TEST"}
        assert "?" not in captured["url"]

    def test_404_returns_false_without_raising(self, client):
        def fake_delete(url, headers=None, **kwargs):
            return FakeResponse(404)

        client.session.delete = fake_delete
        assert client.cancel_order("already-gone") is False

    def test_400_http_error_returns_false(self, client):
        def fake_delete(url, headers=None, **kwargs):
            return FakeResponse(400, text="Cannot cancel filled order")

        client.session.delete = fake_delete
        assert client.cancel_order("filled-order") is False

    def test_generic_exception_returns_false(self, client):
        def fake_delete(url, headers=None, **kwargs):
            raise requests.exceptions.ConnectionError("network down")

        client.session.delete = fake_delete
        assert client.cancel_order("some-id") is False


class TestResilience:
    def test_session_retries_reads_but_never_posts(self, client):
        adapter = client.session.get_adapter("https://api.elections.kalshi.com")
        r = adapter.max_retries
        assert r.total == 3 and 429 in r.status_forcelist
        assert r.allowed_methods == frozenset({"GET"})          # POST excluded

    def test_location_attestation_403_is_distinguished(self, client):
        class Resp:
            status_code = 403
            text = ('{"error":{"code":"Your_location_attestation_for_API_trading_is_missing_or_'
                    'expired."}}')

            def raise_for_status(self):
                raise requests.exceptions.HTTPError("403")

        client.session.post = lambda *a, **k: Resp()
        assert client.place_limit_order("T", 1, 92) == "LOCATION_ATTESTATION"


class TestFetchNbaMarketsErrors:
    def test_swallows_by_default_but_can_raise(self, client):
        def boom(*a, **k):
            raise requests.exceptions.ConnectionError("down")
        client.session.get = boom
        assert client.fetch_nba_markets() == []
        with pytest.raises(requests.exceptions.ConnectionError):
            client.fetch_nba_markets(raise_on_error=True)


class TestClockSkew:
    """estimate_clock_skew: local - server, from an HTTP Date header."""

    @staticmethod
    def _session(date_header, seen=None):
        class Resp:
            headers = {"Date": date_header} if date_header else {}

        class Session:
            def get(self, url, timeout=None):
                if seen is not None:
                    seen.append((url, timeout))
                return Resp()
        return Session()

    # Server says 12:00:00 GMT -> estimate 12:00:00.5 after the 1s-truncation correction.
    SERVER = 1_767_268_800.0  # 2026-01-01 12:00:00 UTC
    DATE = "Thu, 01 Jan 2026 12:00:00 GMT"

    def test_in_sync(self):
        from kalshi.client import estimate_clock_skew
        skew = estimate_clock_skew(self._session(self.DATE), "http://x/exchange/status",
                                   now=lambda: self.SERVER + 0.5)
        assert skew == pytest.approx(0.0, abs=1e-6)

    def test_local_clock_ahead_is_positive(self):
        from kalshi.client import estimate_clock_skew
        skew = estimate_clock_skew(self._session(self.DATE), "http://x", now=lambda: self.SERVER + 10.5)
        assert skew == pytest.approx(10.0)

    def test_local_clock_behind_is_negative(self):
        from kalshi.client import estimate_clock_skew
        skew = estimate_clock_skew(self._session(self.DATE), "http://x", now=lambda: self.SERVER - 59.5)
        assert skew == pytest.approx(-60.0)

    def test_uses_round_trip_midpoint(self):
        from kalshi.client import estimate_clock_skew
        times = iter([self.SERVER - 1.0, self.SERVER + 2.0])    # 3s round trip, midpoint +0.5
        skew = estimate_clock_skew(self._session(self.DATE), "http://x", now=lambda: next(times))
        assert skew == pytest.approx(0.0)

    def test_missing_date_header_raises(self):
        from kalshi.client import estimate_clock_skew
        with pytest.raises(ValueError):
            estimate_clock_skew(self._session(None), "http://x")

    def test_network_error_propagates(self):
        from kalshi.client import estimate_clock_skew

        class Boom:
            def get(self, *a, **k):
                raise requests.exceptions.ConnectionError("down")
        with pytest.raises(requests.exceptions.ConnectionError):
            estimate_clock_skew(Boom(), "http://x")

    def test_client_method_hits_public_status_endpoint_unsigned(self, client):
        seen = []
        client.session = self._session(self.DATE, seen)
        assert isinstance(client.clock_skew_seconds(), float)
        url, timeout = seen[0]
        assert url == f"{client.base_url}/exchange/status" and timeout == client.timeout


class TestClockSkewSlowRoundTrip:
    """A laptop that sleeps mid-request makes the round-trip midpoint wildly wrong
    (observed: a 61s round trip reported +30.1s skew). Such readings must not be trusted."""

    SERVER = 1_767_268_800.0
    DATE = "Thu, 01 Jan 2026 12:00:00 GMT"

    @staticmethod
    def _session_and_clock(round_trips, start):
        """Each get() advances a fake clock by the next round-trip duration."""
        state = {"t": start, "calls": 0}
        durations = list(round_trips)

        class Resp:
            headers = {"Date": TestClockSkewSlowRoundTrip.DATE}

        class Session:
            def get(self, url, timeout=None):
                state["calls"] += 1
                state["t"] += durations.pop(0)
                return Resp()

        return Session(), (lambda: state["t"]), state

    def test_slow_round_trip_is_rejected_not_reported(self):
        from kalshi.client import estimate_clock_skew
        # In sync at the start, but every request takes 61s (machine asleep / stalled).
        session, clock, state = self._session_and_clock([61, 61, 61], start=self.SERVER + 0.5)
        with pytest.raises(ValueError, match="unreliable"):
            estimate_clock_skew(session, "http://x", now=clock)
        assert state["calls"] == 3                      # tried the full sample budget first

    def test_a_fast_sample_after_a_slow_one_is_used(self):
        from kalshi.client import estimate_clock_skew
        # First sample straddles a sleep (61s), second is a normal 0.2s round trip.
        session, clock, state = self._session_and_clock([61, 0.2], start=self.SERVER + 0.5)
        skew = estimate_clock_skew(session, "http://x", now=clock)
        assert state["calls"] == 2
        # clock now = SERVER + 0.5 + 61 + 0.2; the fast sample's midpoint is 0.1s before that.
        assert skew == pytest.approx(0.5 + 61 + 0.2 - 0.1 - 0.5)

    def test_normal_round_trip_takes_one_sample(self):
        from kalshi.client import estimate_clock_skew
        session, clock, state = self._session_and_clock([0.3], start=self.SERVER + 0.5)
        estimate_clock_skew(session, "http://x", now=clock)
        assert state["calls"] == 1

    def test_main_continues_when_the_measurement_is_unreliable(self, caplog):
        """Live mode must not refuse to start because of a bogus reading: unmeasurable -> warn."""
        import logging
        from nba_trading import main

        class C:
            def clock_skew_seconds(self):
                raise ValueError("clock-skew measurement unreliable: best round trip took 61.0s")

        with caplog.at_level(logging.WARNING):
            assert main.check_clock_skew(C(), dry_run=False) is None
        assert "continuing" in caplog.text.lower()

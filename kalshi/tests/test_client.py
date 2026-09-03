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
    def test_success_parses_order_from_response(self, client, monkeypatch):
        fake_order = {"order_id": "abc123", "status": "resting"}
        captured = {}

        def fake_post(url, json=None, headers=None):
            captured["url"] = url
            captured["json"] = json
            captured["headers"] = headers
            return FakeResponse(200, json_data={"order": fake_order})

        client.session.post = fake_post

        result = client.place_limit_order(
            ticker="KX-TEST", count=5, price=50, action="buy", side="yes"
        )

        assert result == fake_order
        # No double slash / missing slash in the URL used for the request.
        assert captured["url"] == "https://api.elections.kalshi.com/trade-api/v2/portfolio/orders"
        assert captured["json"]["ticker"] == "KX-TEST"
        assert captured["json"]["yes_price"] == 50
        assert "no_price" not in captured["json"]

    def test_no_side_uses_no_price(self, client):
        captured = {}

        def fake_post(url, json=None, headers=None):
            captured["json"] = json
            return FakeResponse(200, json_data={"order": {}})

        client.session.post = fake_post
        client.place_limit_order(ticker="KX-TEST", count=1, price=35, side="no")
        assert captured["json"]["no_price"] == 35
        assert "yes_price" not in captured["json"]

    def test_client_order_id_defaults_to_fresh_uuid(self, client):
        seen_ids = []

        def fake_post(url, json=None, headers=None):
            seen_ids.append(json["client_order_id"])
            return FakeResponse(200, json_data={"order": {}})

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

        def fake_post(url, json=None, headers=None):
            captured["id"] = json["client_order_id"]
            return FakeResponse(200, json_data={"order": {}})

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
        def fake_post(url, json=None, headers=None):
            return FakeResponse(400, text=error_text)

        client.session.post = fake_post
        result = client.place_limit_order(ticker="KX-TEST", count=1, price=50)
        assert result == "INSUFFICIENT_FUNDS"

    def test_generic_http_error_returns_none(self, client, caplog):
        def fake_post(url, json=None, headers=None):
            return FakeResponse(422, text="Unprocessable: bad ticker format")

        client.session.post = fake_post
        with caplog.at_level(logging.ERROR):
            result = client.place_limit_order(ticker="KX-TEST", count=1, price=50)
        assert result is None

    def test_connection_error_returns_none(self, client):
        def fake_post(url, json=None, headers=None):
            raise requests.exceptions.ConnectionError("network down")

        client.session.post = fake_post
        result = client.place_limit_order(ticker="KX-TEST", count=1, price=50)
        assert result is None

    def test_no_key_material_leaks_on_error(self, client, rsa_keypair, caplog):
        """Ensure neither the API key id nor the PEM-encoded private key
        ever appear in logged output or in the return value on error."""
        _, pem = rsa_keypair
        pem_text = pem.decode("utf-8")

        def fake_post(url, json=None, headers=None):
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

        def fake_get(url, headers=None):
            assert url == "https://api.elections.kalshi.com/trade-api/v2/portfolio/orders/xyz"
            return FakeResponse(200, json_data={"order": fake_order})

        client.session.get = fake_get
        assert client.get_order_status("xyz") == fake_order

    def test_http_error_returns_none(self, client):
        def fake_get(url, headers=None):
            return FakeResponse(404, text="not found")

        client.session.get = fake_get
        assert client.get_order_status("missing-id") is None

    def test_unexpected_exception_is_caught(self, client):
        """Regression test for the former bare `except:` — verifies that a
        non-HTTPError exception (e.g. malformed JSON) is still handled
        gracefully rather than propagating or being silently swallowed by
        something broader than Exception."""

        def fake_get(url, headers=None):
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
        def fake_delete(url, headers=None):
            assert url == "https://api.elections.kalshi.com/trade-api/v2/portfolio/orders/abc"
            return FakeResponse(200)

        client.session.delete = fake_delete
        assert client.cancel_order("abc") is True

    def test_404_returns_false_without_raising(self, client):
        def fake_delete(url, headers=None):
            return FakeResponse(404)

        client.session.delete = fake_delete
        assert client.cancel_order("already-gone") is False

    def test_400_http_error_returns_false(self, client):
        def fake_delete(url, headers=None):
            return FakeResponse(400, text="Cannot cancel filled order")

        client.session.delete = fake_delete
        assert client.cancel_order("filled-order") is False

    def test_generic_exception_returns_false(self, client):
        def fake_delete(url, headers=None):
            raise requests.exceptions.ConnectionError("network down")

        client.session.delete = fake_delete
        assert client.cancel_order("some-id") is False

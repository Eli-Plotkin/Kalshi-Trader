"""Tests for agent_trader.executor — order placement + sanity gates."""

from __future__ import annotations

import pytest

from agent_trader.executor import (
    ExecutionResult,
    _client_order_id,
    _sanity_check,
    execute_decision,
)
from agent_trader.schemas import Decision, ExpectedOutcome


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------


def _decision(action="buy_yes", size_usd=1.0):
    return Decision(
        action=action,  # type: ignore[arg-type]
        size_usd=size_usd,
        reasoning="test",
        framework_criteria_hit={"all_good": True},
        expected_outcome=ExpectedOutcome(direction="up", confidence=0.7),
    )


_DEFAULT_SUCCESS = {"order_id": "ord-1", "count_filled": 1}
_UNSET = object()


class FakeClient:
    """Records calls and returns a configurable response.

    Pass `response=` explicitly to control the response, including `None`
    (used to simulate an API error). Omitting it uses the default success
    payload.
    """

    def __init__(self, response=_UNSET):
        self._response = _DEFAULT_SUCCESS if response is _UNSET else response
        self.calls = []

    def place_limit_order(self, **kwargs):
        self.calls.append(kwargs)
        return self._response


# ----------------------------------------------------------------------------
# _client_order_id
# ----------------------------------------------------------------------------


class TestClientOrderId:
    def test_format(self):
        assert _client_order_id(123, "KX-A", 0) == "123-KX-A-0"

    def test_unique_across_seqs(self):
        a = _client_order_id(1, "T", 0)
        b = _client_order_id(1, "T", 1)
        assert a != b


# ----------------------------------------------------------------------------
# _sanity_check
# ----------------------------------------------------------------------------


class TestSanityCheck:
    def test_valid_buy_passes(self):
        decision = _decision(action="buy_yes", size_usd=1.0)
        assert _sanity_check(decision, "KX-A", cash_cents=10000) is None

    def test_invalid_ticker_string(self):
        decision = _decision()
        assert _sanity_check(decision, "", cash_cents=10000) == "invalid_ticker"

    def test_invalid_ticker_type(self):
        decision = _decision()
        assert _sanity_check(decision, None, cash_cents=10000) == "invalid_ticker"  # type: ignore[arg-type]

    def test_hold_skip_close_position_skip_size_check(self):
        # These actions bypass size/cash checks.
        for action in ("hold", "skip", "close_position"):
            decision = _decision(action=action, size_usd=0)
            assert _sanity_check(decision, "KX-A", cash_cents=0) is None

    def test_non_positive_size_rejected_for_buy(self):
        decision = _decision(action="buy_yes", size_usd=0)
        assert _sanity_check(decision, "KX-A", cash_cents=10000) == "non_positive_size"

    def test_insufficient_cash_rejected(self):
        decision = _decision(action="buy_yes", size_usd=100.0)
        # Decision wants $100, cash is $5.
        assert _sanity_check(decision, "KX-A", cash_cents=500) == "insufficient_cash"


# ----------------------------------------------------------------------------
# execute_decision — happy paths
# ----------------------------------------------------------------------------


class TestExecuteDecisionHappyPath:
    def test_hold_returns_no_op_no_api_call(self):
        client = FakeClient()
        decision = _decision(action="hold", size_usd=0)
        result = execute_decision(
            client, decision, ticker="KX-A", cycle_id=1, seq=0,
            yes_ask_cents=50, no_ask_cents=50, cash_cents=10000,
        )
        assert result.ok is True
        assert result.reason == "no_op"
        assert client.calls == []

    def test_skip_returns_no_op_no_api_call(self):
        client = FakeClient()
        decision = _decision(action="skip", size_usd=0)
        result = execute_decision(
            client, decision, ticker="KX-A", cycle_id=1, seq=0,
            yes_ask_cents=50, no_ask_cents=50, cash_cents=10000,
        )
        assert result.ok is True
        assert result.reason == "no_op"

    def test_buy_yes_places_order_with_expected_count(self):
        client = FakeClient()
        # size_usd=1.00 at yes_ask 50¢ → 100 cents / 50 = 2 shares.
        decision = _decision(action="buy_yes", size_usd=1.0)
        result = execute_decision(
            client, decision, ticker="KX-A", cycle_id=1, seq=0,
            yes_ask_cents=50, no_ask_cents=50, cash_cents=10000,
        )
        assert result.ok is True
        assert result.reason == "placed"
        assert len(client.calls) == 1
        call = client.calls[0]
        assert call["ticker"] == "KX-A"
        assert call["side"] == "yes"
        assert call["action"] == "buy"
        assert call["price"] == 50
        assert call["count"] == 2
        assert call["client_order_id"] == "1-KX-A-0"

    def test_buy_no_uses_no_ask(self):
        client = FakeClient()
        decision = _decision(action="buy_no", size_usd=1.0)
        result = execute_decision(
            client, decision, ticker="KX-A", cycle_id=1, seq=0,
            yes_ask_cents=80, no_ask_cents=20, cash_cents=10000,
        )
        assert result.ok is True
        assert client.calls[0]["side"] == "no"
        assert client.calls[0]["price"] == 20
        # $1.00 / 20¢ = 5 shares
        assert client.calls[0]["count"] == 5

    def test_min_count_is_one_share(self):
        # If size_usd / price rounds to 0, we should still place 1 contract.
        client = FakeClient()
        decision = _decision(action="buy_yes", size_usd=0.01)  # 1¢
        result = execute_decision(
            client, decision, ticker="KX-A", cycle_id=1, seq=0,
            yes_ask_cents=50, no_ask_cents=50, cash_cents=10000,
        )
        assert result.ok is True
        assert client.calls[0]["count"] == 1


class TestExecuteDecisionRejections:
    def test_invalid_ticker(self):
        client = FakeClient()
        decision = _decision(action="buy_yes", size_usd=1.0)
        result = execute_decision(
            client, decision, ticker="", cycle_id=1, seq=0,
            yes_ask_cents=50, no_ask_cents=50, cash_cents=10000,
        )
        assert result.ok is False
        assert result.reason == "invalid_ticker"
        assert client.calls == []

    def test_insufficient_cash(self):
        client = FakeClient()
        decision = _decision(action="buy_yes", size_usd=100.0)
        result = execute_decision(
            client, decision, ticker="KX-A", cycle_id=1, seq=0,
            yes_ask_cents=50, no_ask_cents=50, cash_cents=500,
        )
        assert result.ok is False
        assert result.reason == "insufficient_cash"

    def test_zero_yes_ask_blocks_buy(self):
        client = FakeClient()
        decision = _decision(action="buy_yes", size_usd=1.0)
        result = execute_decision(
            client, decision, ticker="KX-A", cycle_id=1, seq=0,
            yes_ask_cents=0, no_ask_cents=50, cash_cents=10000,
        )
        assert result.ok is False
        assert result.reason == "no_ask_price"

    def test_insufficient_funds_response_handled(self):
        client = FakeClient(response="INSUFFICIENT_FUNDS")
        decision = _decision(action="buy_yes", size_usd=1.0)
        result = execute_decision(
            client, decision, ticker="KX-A", cycle_id=1, seq=0,
            yes_ask_cents=50, no_ask_cents=50, cash_cents=10000,
        )
        assert result.ok is False
        assert result.reason == "insufficient_funds"

    def test_api_error_returns_failure(self):
        client = FakeClient(response=None)  # simulate API failure
        decision = _decision(action="buy_yes", size_usd=1.0)
        result = execute_decision(
            client, decision, ticker="KX-A", cycle_id=1, seq=0,
            yes_ask_cents=50, no_ask_cents=50, cash_cents=10000,
        )
        assert result.ok is False
        assert result.reason == "api_error"


# ----------------------------------------------------------------------------
# close_position handling
# ----------------------------------------------------------------------------


class TestExecuteClosePosition:
    def test_no_position_to_close_returns_no_op(self):
        client = FakeClient()
        decision = _decision(action="close_position", size_usd=0)
        result = execute_decision(
            client, decision, ticker="KX-A", cycle_id=1, seq=0,
            yes_ask_cents=50, no_ask_cents=50, cash_cents=10000,
            current_position_count=0,
        )
        assert result.ok is True
        assert result.reason == "no_position_to_close"
        assert client.calls == []

    def test_close_yes_position_sells_yes(self):
        client = FakeClient()
        decision = _decision(action="close_position", size_usd=0)
        result = execute_decision(
            client, decision, ticker="KX-A", cycle_id=1, seq=0,
            yes_ask_cents=50, no_ask_cents=50, cash_cents=10000,
            current_position_count=10,  # positive → holds YES
        )
        assert result.ok is True
        assert client.calls[0]["side"] == "yes"
        assert client.calls[0]["action"] == "sell"
        assert client.calls[0]["count"] == 10

    def test_close_no_position_sells_no(self):
        client = FakeClient()
        decision = _decision(action="close_position", size_usd=0)
        result = execute_decision(
            client, decision, ticker="KX-A", cycle_id=1, seq=0,
            yes_ask_cents=50, no_ask_cents=50, cash_cents=10000,
            current_position_count=-7,  # negative → holds NO
        )
        assert result.ok is True
        assert client.calls[0]["side"] == "no"
        assert client.calls[0]["action"] == "sell"
        assert client.calls[0]["count"] == 7

    def test_close_position_price_below_ask(self):
        # To sell quickly, we cross under the current ask by 1c (min 1c).
        client = FakeClient()
        decision = _decision(action="close_position", size_usd=0)
        result = execute_decision(
            client, decision, ticker="KX-A", cycle_id=1, seq=0,
            yes_ask_cents=50, no_ask_cents=50, cash_cents=10000,
            current_position_count=10,
        )
        assert client.calls[0]["price"] == 49  # 50 - 1


# ----------------------------------------------------------------------------
# Idempotency
# ----------------------------------------------------------------------------


class TestIdempotency:
    def test_same_cycle_and_seq_yields_same_client_order_id(self):
        client = FakeClient()
        decision = _decision(action="buy_yes", size_usd=1.0)
        r1 = execute_decision(
            client, decision, ticker="KX-A", cycle_id=42, seq=0,
            yes_ask_cents=50, no_ask_cents=50, cash_cents=10000,
        )
        r2 = execute_decision(
            client, decision, ticker="KX-A", cycle_id=42, seq=0,
            yes_ask_cents=50, no_ask_cents=50, cash_cents=10000,
        )
        # The executor itself doesn't dedupe — Kalshi's server-side
        # client_order_id should reject duplicates. But both calls should
        # produce the SAME client_order_id, which is the idempotency key.
        assert r1.client_order_id == r2.client_order_id == "42-KX-A-0"

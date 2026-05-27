"""Tests for agent_trader.coarse_filter — payload shaping + fail-open logic.

The LLM call itself isn't exercised here (it'd need full Anthropic mocks);
we lock down the parts that decide what gets sent and what happens when
the LLM call goes sideways.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from agent_trader.coarse_filter import (
    CoarseFilterResult,
    _filter_one,
    _market_payload,
    run_coarse_filter,
)
from agent_trader.market_discovery import EligibleMarket
from agent_trader.runtime import (
    BudgetCounter,
    BudgetExhausted,
    MalformedLLMResponse,
)


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------


def _market(ticker="KX-A", close_hours=24):
    close_time = datetime.now(timezone.utc) + timedelta(hours=close_hours)
    return EligibleMarket(
        ticker=ticker,
        title="Test Market",
        yes_bid_cents=45,
        yes_ask_cents=55,
        last_price_cents=50,
        volume_24h=1500,
        open_interest=800,
        close_time=close_time,
        raw_market_response={},
    )


# ----------------------------------------------------------------------------
# _market_payload — the JSON shape sent to the LLM
# ----------------------------------------------------------------------------


class TestMarketPayload:
    def test_payload_starts_with_input_header(self):
        now = datetime.now(timezone.utc)
        out = _market_payload(_market(), now)
        assert out.startswith("INPUT:\n")

    def test_payload_includes_core_fields(self):
        now = datetime.now(timezone.utc)
        out = _market_payload(_market(), now)
        json_part = out[len("INPUT:\n"):]
        data = json.loads(json_part)
        assert data["market"]["ticker"] == "KX-A"
        assert data["market"]["yes_bid_cents"] == 45
        assert data["market"]["yes_ask_cents"] == 55
        assert data["market"]["last_price_cents"] == 50
        assert data["market"]["volume_24h"] == 1500
        assert data["market"]["open_interest"] == 800

    def test_hours_to_close_computed(self):
        now = datetime.now(timezone.utc)
        market = _market(close_hours=12)
        out = _market_payload(market, now)
        data = json.loads(out[len("INPUT:\n"):])
        # Approximate — generation took microseconds.
        assert 11.5 <= data["market"]["hours_to_close"] <= 12.5

    def test_no_close_time_yields_none(self):
        now = datetime.now(timezone.utc)
        m = _market()
        # Replace close_time with None.
        m_no_close = EligibleMarket(
            ticker=m.ticker, title=m.title,
            yes_bid_cents=m.yes_bid_cents, yes_ask_cents=m.yes_ask_cents,
            last_price_cents=m.last_price_cents,
            volume_24h=m.volume_24h, open_interest=m.open_interest,
            close_time=None,
            raw_market_response={},
        )
        out = _market_payload(m_no_close, now)
        data = json.loads(out[len("INPUT:\n"):])
        assert data["market"]["hours_to_close"] is None


# ----------------------------------------------------------------------------
# _filter_one — fail-open vs budget-exhausted vs success
# ----------------------------------------------------------------------------


class TestFilterOne:
    def _budget(self, cap=10.0, spent=0.0):
        return BudgetCounter(market_cap_usd=cap, cycle_cap_usd=cap, cycle_spent_usd=spent)

    def test_success_path_returns_keep_decision(self):
        with patch("agent_trader.coarse_filter.runtime.call_llm") as mock_llm, \
             patch("agent_trader.coarse_filter.runtime.parse_llm_json") as mock_parse:
            mock_llm.return_value = (
                '{"keep": true, "reason": "liquid"}',
                {"cost_usd": 0.01},
            )
            mock_parse.return_value = {"keep": True, "reason": "liquid"}
            result = _filter_one(
                anthropic_client=object(),
                system="s",
                market=_market(),
                user="u",
                cycle_budget=self._budget(),
            )
            assert isinstance(result, CoarseFilterResult)
            assert result.keep is True
            assert result.reason == "liquid"
            assert result.error is None

    def test_drops_market_when_llm_says_no(self):
        with patch("agent_trader.coarse_filter.runtime.call_llm") as mock_llm, \
             patch("agent_trader.coarse_filter.runtime.parse_llm_json") as mock_parse:
            mock_llm.return_value = (
                '{"keep": false, "reason": "props market"}',
                {"cost_usd": 0.005},
            )
            mock_parse.return_value = {"keep": False, "reason": "props market"}
            result = _filter_one(
                anthropic_client=object(),
                system="s", market=_market(), user="u",
                cycle_budget=self._budget(),
            )
            assert result.keep is False

    def test_malformed_llm_response_fails_open(self):
        # If the LLM call returns garbage, the market must be KEPT (fail-open)
        # so a bug here doesn't silently starve the pipeline.
        with patch("agent_trader.coarse_filter.runtime.call_llm") as mock_llm:
            mock_llm.side_effect = MalformedLLMResponse("bad json")
            result = _filter_one(
                anthropic_client=object(),
                system="s", market=_market(), user="u",
                cycle_budget=self._budget(),
            )
            assert result.keep is True
            assert "filter_error_kept" in result.reason
            assert result.error == "bad json"

    def test_budget_exhausted_drops_market(self):
        # If the budget is gone, we DROP rather than fail-open — keeping
        # is fail-safe only when there are downstream resources to triage.
        with patch("agent_trader.coarse_filter.runtime.call_llm") as mock_llm:
            mock_llm.side_effect = BudgetExhausted("cycle_budget")
            result = _filter_one(
                anthropic_client=object(),
                system="s", market=_market(), user="u",
                cycle_budget=self._budget(),
            )
            assert result.keep is False
            assert "budget_exhausted" in result.reason


# ----------------------------------------------------------------------------
# run_coarse_filter — orchestration
# ----------------------------------------------------------------------------


class TestRunCoarseFilter:
    def _budget(self):
        return BudgetCounter(market_cap_usd=10.0, cycle_cap_usd=10.0)

    def test_empty_input_returns_empty_immediately(self):
        kept, results = run_coarse_filter(
            anthropic_client=object(),
            system_prompt="sys",
            cacheable=False,
            assumptions="",
            eligible=[],
            cycle_budget=self._budget(),
        )
        assert kept == []
        assert results == []

    def test_kept_markets_match_keep_decisions(self):
        markets = [_market(ticker="A"), _market(ticker="B"), _market(ticker="C")]
        with patch("agent_trader.coarse_filter._filter_one") as mock_filter:
            mock_filter.side_effect = [
                CoarseFilterResult(ticker="A", keep=True, reason="ok", usage=None, error=None),
                CoarseFilterResult(ticker="B", keep=False, reason="props", usage=None, error=None),
                CoarseFilterResult(ticker="C", keep=True, reason="ok", usage=None, error=None),
            ]
            kept, results = run_coarse_filter(
                anthropic_client=object(),
                system_prompt="sys",
                cacheable=False,
                assumptions="",
                eligible=markets,
                cycle_budget=self._budget(),
            )
            assert {m.ticker for m in kept} == {"A", "C"}
            assert len(results) == 3

    def test_aggregates_cost_into_budget(self):
        markets = [_market(ticker="A"), _market(ticker="B")]
        budget = self._budget()
        with patch("agent_trader.coarse_filter._filter_one") as mock_filter:
            mock_filter.side_effect = [
                CoarseFilterResult(ticker="A", keep=True, reason="ok",
                                   usage={"cost_usd": 0.01}, error=None),
                CoarseFilterResult(ticker="B", keep=True, reason="ok",
                                   usage={"cost_usd": 0.02}, error=None),
            ]
            run_coarse_filter(
                anthropic_client=object(),
                system_prompt="sys",
                cacheable=False,
                assumptions="",
                eligible=markets,
                cycle_budget=budget,
            )
            # Cost is aggregated post-pool to avoid races on cycle_spent_usd.
            assert budget.cycle_spent_usd == pytest.approx(0.03)

    def test_missing_usage_does_not_crash(self):
        # If a result has no usage dict (e.g. fail-open path), accumulator
        # should treat it as zero cost.
        markets = [_market(ticker="A")]
        budget = self._budget()
        with patch("agent_trader.coarse_filter._filter_one") as mock_filter:
            mock_filter.return_value = CoarseFilterResult(
                ticker="A", keep=True, reason="ok", usage=None, error=None
            )
            run_coarse_filter(
                anthropic_client=object(),
                system_prompt="sys",
                cacheable=False,
                assumptions="",
                eligible=markets,
                cycle_budget=budget,
            )
            assert budget.cycle_spent_usd == 0.0


# Allow pytest.approx without top-level import noise.
import pytest  # noqa: E402

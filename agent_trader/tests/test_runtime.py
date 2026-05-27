"""Tests for agent_trader.runtime — budget, killswitch, LLM cost, JSON parser,
chain log helpers."""

from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timezone

import pytest

from agent_trader.runtime import (
    BudgetCounter,
    BudgetExhausted,
    Killswitch,
    MalformedLLMResponse,
    PortfolioState,
    build_system_block,
    close_cycle,
    estimate_cost_usd,
    estimate_tool_cost_usd,
    init_db,
    log_step,
    now_iso,
    open_cycle,
    parse_llm_json,
    reconcile,
    skip_cycle,
    CACHE_READ_MULTIPLIER,
    CACHE_WRITE_MULTIPLIER,
    MODEL_RATES_PER_MTOK,
    TOOL_COST_USD_PER_CALL,
)


# ----------------------------------------------------------------------------
# now_iso
# ----------------------------------------------------------------------------


class TestNowIso:
    def test_is_iso_format_with_utc(self):
        ts = now_iso()
        # Should be parseable as ISO and tz-aware.
        parsed = datetime.fromisoformat(ts)
        assert parsed.tzinfo is not None
        assert parsed.tzinfo.utcoffset(parsed) == timezone.utc.utcoffset(parsed)


# ----------------------------------------------------------------------------
# init_db + chain-log helpers
# ----------------------------------------------------------------------------


class TestInitDb:
    def test_creates_all_tables(self, tmp_path):
        db = tmp_path / "t.sqlite"
        conn = init_db(db)
        try:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
            names = {r[0] for r in rows}
            assert {"cycles", "chain_log", "orders", "killswitch_events"} <= names
        finally:
            conn.close()

    def test_wal_journal_enabled(self, tmp_path):
        db = tmp_path / "t.sqlite"
        conn = init_db(db)
        try:
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
            assert mode.lower() == "wal"
        finally:
            conn.close()

    def test_idempotent(self, tmp_path):
        db = tmp_path / "t.sqlite"
        init_db(db).close()
        # Second call must not raise even though schema already exists.
        conn = init_db(db)
        conn.close()


class TestCycleAndChainLog:
    def _conn(self, tmp_path):
        return init_db(tmp_path / "t.sqlite")

    def test_open_cycle_creates_running_row(self, tmp_path):
        conn = self._conn(tmp_path)
        try:
            cid = open_cycle(conn, {"role_a": "promptA"})
            row = conn.execute(
                "SELECT status, active_prompts_json FROM cycles WHERE cycle_id=?",
                (cid,),
            ).fetchone()
            assert row[0] == "running"
            assert json.loads(row[1]) == {"role_a": "promptA"}
        finally:
            conn.close()

    def test_close_cycle_marks_status_and_notes(self, tmp_path):
        conn = self._conn(tmp_path)
        try:
            cid = open_cycle(conn, {})
            close_cycle(conn, cid, "ok", notes="happy path")
            row = conn.execute(
                "SELECT status, notes, ended_at FROM cycles WHERE cycle_id=?",
                (cid,),
            ).fetchone()
            assert row[0] == "ok"
            assert row[1] == "happy path"
            assert row[2] is not None
        finally:
            conn.close()

    def test_skip_cycle_records_skipped(self, tmp_path):
        conn = self._conn(tmp_path)
        try:
            skip_cycle(conn, "halt_file_present")
            row = conn.execute(
                "SELECT status, skipped, notes FROM cycles ORDER BY cycle_id DESC LIMIT 1"
            ).fetchone()
            assert row[0] == "skipped"
            assert row[1] == 1
            assert row[2] == "halt_file_present"
        finally:
            conn.close()

    def test_log_step_round_trip(self, tmp_path):
        conn = self._conn(tmp_path)
        try:
            cid = open_cycle(conn, {})
            log_step(conn, cid, "KX-A", "step1", {"k": "v", "n": 1})
            row = conn.execute(
                "SELECT payload_json FROM chain_log WHERE cycle_id=? AND ticker=? AND step=?",
                (cid, "KX-A", "step1"),
            ).fetchone()
            assert json.loads(row[0]) == {"k": "v", "n": 1}
        finally:
            conn.close()

    def test_log_step_replaces_on_duplicate(self, tmp_path):
        conn = self._conn(tmp_path)
        try:
            cid = open_cycle(conn, {})
            log_step(conn, cid, "KX-A", "step1", {"v": 1})
            log_step(conn, cid, "KX-A", "step1", {"v": 2})
            row = conn.execute(
                "SELECT payload_json FROM chain_log WHERE cycle_id=? AND ticker=? AND step=?",
                (cid, "KX-A", "step1"),
            ).fetchone()
            assert json.loads(row[0]) == {"v": 2}
        finally:
            conn.close()

    def test_log_step_handles_non_json_native_via_default_str(self, tmp_path):
        conn = self._conn(tmp_path)
        try:
            cid = open_cycle(conn, {})
            # datetime isn't natively JSON-serializable; helper uses default=str.
            log_step(conn, cid, "KX-A", "step1", {"ts": datetime(2026, 5, 27, tzinfo=timezone.utc)})
            row = conn.execute(
                "SELECT payload_json FROM chain_log WHERE cycle_id=? AND ticker=? AND step=?",
                (cid, "KX-A", "step1"),
            ).fetchone()
            assert "2026-05-27" in row[0]
        finally:
            conn.close()


# ----------------------------------------------------------------------------
# BudgetCounter
# ----------------------------------------------------------------------------


class TestBudgetCounter:
    def test_fresh_budget_does_not_exceed(self):
        b = BudgetCounter(market_cap_usd=1.0, cycle_cap_usd=5.0)
        assert b.will_exceed(0.0) is None
        assert b.will_exceed(0.5) is None

    def test_market_cap_exceeded(self):
        b = BudgetCounter(market_cap_usd=1.0, cycle_cap_usd=10.0)
        reason = b.will_exceed(1.01)
        assert reason is not None
        assert "market_budget" in reason

    def test_cycle_cap_exceeded(self):
        b = BudgetCounter(market_cap_usd=10.0, cycle_cap_usd=1.0)
        reason = b.will_exceed(1.01)
        assert reason is not None
        assert "cycle_budget" in reason

    def test_add_accumulates_both_market_and_cycle(self):
        b = BudgetCounter(market_cap_usd=10.0, cycle_cap_usd=10.0)
        b.add(0.5)
        assert b.market_spent_usd == pytest.approx(0.5)
        assert b.cycle_spent_usd == pytest.approx(0.5)

    def test_market_cap_exceeded_after_add(self):
        b = BudgetCounter(market_cap_usd=1.0, cycle_cap_usd=10.0)
        b.add(0.8)
        reason = b.will_exceed(0.5)
        assert reason is not None
        assert "market_budget" in reason

    def test_market_cap_can_be_zero_no_calls(self):
        # Edge: zero-cap budget rejects any projected spend.
        b = BudgetCounter(market_cap_usd=0.0, cycle_cap_usd=1.0)
        reason = b.will_exceed(0.001)
        assert reason is not None


# ----------------------------------------------------------------------------
# Killswitch
# ----------------------------------------------------------------------------


class TestKillswitch:
    def test_fresh_killswitch_no_trip(self):
        ks = Killswitch()
        assert ks.check(cash_cents=10000) is None
        assert ks.tripped is False

    def test_cash_below_floor_triggers(self):
        ks = Killswitch(cash_floor_cents=50)
        reason = ks.check(cash_cents=40)
        assert reason is not None
        assert "cash_below_floor" in reason

    def test_consecutive_api_error_cycles_trip(self):
        ks = Killswitch(max_consecutive_api_errors=3)
        ks.note_cycle_api_status(had_errors=True)
        ks.note_cycle_api_status(had_errors=True)
        assert ks.check(cash_cents=10000) is None
        ks.note_cycle_api_status(had_errors=True)
        reason = ks.check(cash_cents=10000)
        assert reason is not None
        assert "consecutive_api_error_cycles" in reason

    def test_clean_cycle_resets_consecutive_count(self):
        ks = Killswitch(max_consecutive_api_errors=3)
        ks.note_cycle_api_status(had_errors=True)
        ks.note_cycle_api_status(had_errors=True)
        ks.note_cycle_api_status(had_errors=False)  # reset
        assert ks.consecutive_api_error_cycles == 0

    def test_recent_errors_within_30m(self):
        ks = Killswitch(max_errors_within_30m=3)
        for _ in range(3):
            ks.note_api_error()
        reason = ks.check(cash_cents=10000)
        assert reason is not None
        assert "errors_within_30m" in reason

    def test_old_errors_dropped(self):
        ks = Killswitch(max_errors_within_30m=3)
        # Manually inject a timestamp older than 30 minutes ago.
        ks.recent_error_timestamps = [time.time() - 31 * 60] * 5
        ks.note_api_error()  # this prunes the old ones
        assert len(ks.recent_error_timestamps) == 1

    def test_malformed_responses_trip(self):
        ks = Killswitch(max_consecutive_malformed=2)
        ks.note_agent_response(malformed=True)
        ks.note_agent_response(malformed=True)
        reason = ks.check(cash_cents=10000)
        assert reason is not None
        assert "consecutive_malformed" in reason

    def test_good_response_resets_malformed_count(self):
        ks = Killswitch(max_consecutive_malformed=3)
        ks.note_agent_response(malformed=True)
        ks.note_agent_response(malformed=True)
        ks.note_agent_response(malformed=False)
        assert ks.consecutive_malformed_responses == 0

    def test_trip_writes_db_event(self, tmp_path):
        conn = init_db(tmp_path / "t.sqlite")
        try:
            ks = Killswitch()
            ks.trip(conn, "test_reason")
            assert ks.tripped is True
            assert ks.trip_reason == "test_reason"
            row = conn.execute(
                "SELECT kind, detail FROM killswitch_events ORDER BY ts DESC LIMIT 1"
            ).fetchone()
            assert row == ("tripped", "test_reason")
        finally:
            conn.close()


# ----------------------------------------------------------------------------
# estimate_cost_usd
# ----------------------------------------------------------------------------


class TestEstimateCostUsd:
    def test_unknown_model_returns_zero(self):
        assert estimate_cost_usd("not-a-model", 1000, 500) == 0.0

    def test_haiku_input_only(self):
        # 1M input tokens at $1/MTok = $1.00
        cost = estimate_cost_usd("claude-haiku-4-5-20251001", 1_000_000, 0)
        assert cost == pytest.approx(1.00)

    def test_haiku_output_only(self):
        # 1M output tokens at $5/MTok = $5.00
        cost = estimate_cost_usd("claude-haiku-4-5-20251001", 0, 1_000_000)
        assert cost == pytest.approx(5.00)

    def test_sonnet_combined(self):
        # 100k input @ $3/MTok = $0.30, 50k output @ $15/MTok = $0.75 → $1.05
        cost = estimate_cost_usd("claude-sonnet-4-6", 100_000, 50_000)
        assert cost == pytest.approx(0.30 + 0.75)

    def test_cache_write_multiplier(self):
        # 1M cache-creation tokens on haiku → 1M * 1.00 * 1.25 = $1.25
        cost = estimate_cost_usd(
            "claude-haiku-4-5-20251001",
            input_tokens=0,
            output_tokens=0,
            cache_creation_input_tokens=1_000_000,
        )
        assert cost == pytest.approx(1.00 * CACHE_WRITE_MULTIPLIER)

    def test_cache_read_multiplier(self):
        # 1M cache-read tokens on haiku → 1M * 1.00 * 0.10 = $0.10
        cost = estimate_cost_usd(
            "claude-haiku-4-5-20251001",
            input_tokens=0,
            output_tokens=0,
            cache_read_input_tokens=1_000_000,
        )
        assert cost == pytest.approx(1.00 * CACHE_READ_MULTIPLIER)

    def test_zero_tokens_zero_cost(self):
        assert estimate_cost_usd("claude-haiku-4-5-20251001", 0, 0) == 0.0


class TestEstimateToolCostUsd:
    def test_web_search_priced(self):
        # 100 web searches at $0.010 each = $1.00
        assert estimate_tool_cost_usd("web_search", 100) == pytest.approx(1.00)

    def test_unknown_tool_zero(self):
        assert estimate_tool_cost_usd("invented_tool", 1000) == 0.0

    def test_zero_calls_zero_cost(self):
        assert estimate_tool_cost_usd("web_search", 0) == 0.0


# ----------------------------------------------------------------------------
# build_system_block
# ----------------------------------------------------------------------------


class TestBuildSystemBlock:
    def test_non_cacheable_returns_plain_string(self):
        out = build_system_block("body", cacheable=False)
        assert out == "body"

    def test_non_cacheable_with_prefix_joins_with_newlines(self):
        out = build_system_block("body", cacheable=False, prefix="prefix")
        assert out == "prefix\n\nbody"

    def test_cacheable_no_prefix_single_block(self):
        out = build_system_block("body", cacheable=True)
        assert isinstance(out, list)
        assert len(out) == 1
        assert out[0]["text"] == "body"
        assert out[0]["cache_control"] == {"type": "ephemeral"}

    def test_cacheable_with_prefix_two_blocks(self):
        out = build_system_block("body", cacheable=True, prefix="prefix")
        assert isinstance(out, list)
        assert len(out) == 2
        assert out[0]["text"] == "prefix"
        assert out[1]["text"] == "body"
        # Both blocks marked cacheable.
        assert all(b["cache_control"] == {"type": "ephemeral"} for b in out)


# ----------------------------------------------------------------------------
# parse_llm_json — the 4-strategy fallback parser
# ----------------------------------------------------------------------------


class TestParseLlmJson:
    def test_pure_json(self):
        assert parse_llm_json('{"a": 1}') == {"a": 1}

    def test_pure_json_with_whitespace(self):
        assert parse_llm_json('  \n  {"a": 1}  \n ') == {"a": 1}

    def test_json_array(self):
        assert parse_llm_json('[1, 2, 3]') == [1, 2, 3]

    def test_fenced_json_block_full(self):
        text = '```json\n{"a": 1}\n```'
        assert parse_llm_json(text) == {"a": 1}

    def test_fenced_block_without_json_tag(self):
        text = '```\n{"a": 1}\n```'
        assert parse_llm_json(text) == {"a": 1}

    def test_prose_with_fenced_block(self):
        text = 'Here is my answer:\n\n```json\n{"a": 1}\n```\n\nHope that helps.'
        assert parse_llm_json(text) == {"a": 1}

    def test_prose_with_bare_json_object(self):
        text = 'Sure! The answer is {"a": 1} which I derived from the data.'
        assert parse_llm_json(text) == {"a": 1}

    def test_nested_json_balanced_braces(self):
        text = 'Here: {"outer": {"inner": "value"}, "n": 1}'
        assert parse_llm_json(text) == {"outer": {"inner": "value"}, "n": 1}

    def test_string_with_braces_inside(self):
        # Braces inside a string value must not confuse the brace matcher.
        text = '{"msg": "this is {fine}", "n": 1}'
        assert parse_llm_json(text) == {"msg": "this is {fine}", "n": 1}

    def test_escaped_quotes_in_string(self):
        text = '{"msg": "she said \\"hi\\""}'
        assert parse_llm_json(text) == {"msg": 'she said "hi"'}

    def test_raises_on_garbage(self):
        with pytest.raises(MalformedLLMResponse):
            parse_llm_json("totally not json")

    def test_raises_on_unbalanced_braces(self):
        with pytest.raises(MalformedLLMResponse):
            parse_llm_json('{"a": 1, "b": [1, 2, 3')


# ----------------------------------------------------------------------------
# reconcile — pulls portfolio state from a mock Kalshi client
# ----------------------------------------------------------------------------


class FakeKalshiClient:
    def __init__(self, balance=None, positions=None):
        self._balance = balance
        self._positions = positions if positions is not None else []

    def get_balance(self):
        return self._balance

    def list_positions(self):
        return self._positions


class TestReconcile:
    def test_returns_portfolio_state(self):
        client = FakeKalshiClient(
            balance=12345,
            positions=[
                {"ticker": "KX-A", "position": 10},
                {"ticker": "KX-B", "position": -5},
            ],
        )
        state = reconcile(client)
        assert isinstance(state, PortfolioState)
        assert state.cash_cents == 12345
        assert state.positions == {"KX-A": 10, "KX-B": -5}

    def test_raises_when_balance_unavailable(self):
        client = FakeKalshiClient(balance=None)
        with pytest.raises(RuntimeError, match="failed to fetch balance"):
            reconcile(client)

    def test_filters_positions_with_no_ticker(self):
        client = FakeKalshiClient(
            balance=100,
            positions=[
                {"ticker": "KX-A", "position": 10},
                {"position": 5},  # malformed — no ticker
            ],
        )
        state = reconcile(client)
        assert state.positions == {"KX-A": 10}

    def test_empty_positions(self):
        client = FakeKalshiClient(balance=100, positions=[])
        state = reconcile(client)
        assert state.cash_cents == 100
        assert state.positions == {}

    def test_none_position_count_treated_as_zero(self):
        client = FakeKalshiClient(
            balance=100,
            positions=[{"ticker": "KX-A", "position": None}],
        )
        state = reconcile(client)
        assert state.positions == {"KX-A": 0}


# ----------------------------------------------------------------------------
# Model rate / tool cost tables
# ----------------------------------------------------------------------------


class TestRateTables:
    def test_known_models_have_in_and_out_rates(self):
        for model, rates in MODEL_RATES_PER_MTOK.items():
            assert "in" in rates, f"{model} missing 'in' rate"
            assert "out" in rates, f"{model} missing 'out' rate"
            assert rates["in"] > 0
            assert rates["out"] > 0

    def test_output_rates_higher_than_input(self):
        # Anthropic's pricing has always charged more for output than input.
        for model, rates in MODEL_RATES_PER_MTOK.items():
            assert rates["out"] >= rates["in"], (
                f"{model}: output rate {rates['out']} < input rate {rates['in']}"
            )

    def test_web_search_priced(self):
        assert TOOL_COST_USD_PER_CALL.get("web_search") == 0.010

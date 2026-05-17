from __future__ import annotations

import json
import logging
import os
import signal
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


# ────────────────────────────────────────────────────────────────────────────
# Paths & constants
# ────────────────────────────────────────────────────────────────────────────

REPO_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = REPO_ROOT / "data"
DB_PATH = DATA_DIR / "agent_log.sqlite"
HALT_FILE = Path(os.path.expanduser("~/.kalshi-agent.halt"))

DEFAULT_CASH_FLOOR_CENTS = 50  # $0.50 from design doc
DEFAULT_MAX_CONSECUTIVE_API_ERRORS = 3
DEFAULT_MAX_ERRORS_WITHIN_30M = 5
DEFAULT_MAX_CONSECUTIVE_MALFORMED = 3


# ────────────────────────────────────────────────────────────────────────────
# SQLite chain log
# ────────────────────────────────────────────────────────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS cycles (
  cycle_id      INTEGER PRIMARY KEY,
  started_at    TEXT NOT NULL,
  ended_at      TEXT,
  status        TEXT,
  active_prompts_json TEXT,
  skipped       INTEGER DEFAULT 0,
  notes         TEXT
);

CREATE TABLE IF NOT EXISTS chain_log (
  cycle_id      INTEGER NOT NULL,
  ticker        TEXT NOT NULL,
  step          TEXT NOT NULL,
  ts            TEXT NOT NULL,
  payload_json  TEXT NOT NULL,
  PRIMARY KEY (cycle_id, ticker, step)
);

CREATE TABLE IF NOT EXISTS orders (
  client_order_id TEXT PRIMARY KEY,
  cycle_id      INTEGER NOT NULL,
  ticker        TEXT NOT NULL,
  order_id      TEXT,
  action        TEXT,
  side          TEXT,
  count         INTEGER,
  price_cents   INTEGER,
  status        TEXT,
  placed_at     TEXT NOT NULL,
  raw_json      TEXT
);

CREATE TABLE IF NOT EXISTS killswitch_events (
  ts            TEXT NOT NULL,
  kind          TEXT NOT NULL,
  detail        TEXT
);

CREATE INDEX IF NOT EXISTS idx_chain_cycle_ticker ON chain_log (cycle_id, ticker);
CREATE INDEX IF NOT EXISTS idx_orders_cycle ON orders (cycle_id);
"""


def init_db(path: Path = DB_PATH) -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    with conn:
        conn.executescript(SCHEMA)
    return conn


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def log_step(conn: sqlite3.Connection, cycle_id: int, ticker: str, step: str, payload: Any) -> None:
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO chain_log (cycle_id, ticker, step, ts, payload_json) "
            "VALUES (?, ?, ?, ?, ?)",
            (cycle_id, ticker, step, now_iso(), json.dumps(payload, default=str)),
        )


def log_order(conn: sqlite3.Connection, cycle_id: int, ticker: str, result) -> None:
    if not result.client_order_id:
        return
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO orders "
            "(client_order_id, cycle_id, ticker, order_id, action, side, count, price_cents, status, placed_at, raw_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                result.client_order_id,
                cycle_id,
                ticker,
                result.order_id,
                result.action,
                result.raw_order_response.get("side"),
                result.raw_order_response.get("count"),
                result.raw_order_response.get("yes_price") or result.raw_order_response.get("no_price"),
                result.reason,
                now_iso(),
                json.dumps(result.raw_order_response, default=str),
            ),
        )


# ────────────────────────────────────────────────────────────────────────────
# Reconciliation
# ────────────────────────────────────────────────────────────────────────────

@dataclass
class PortfolioState:
    cash_cents: int
    positions: dict[str, int]  # ticker → signed count (yes positive, no negative)
    raw_positions: list[dict] = field(default_factory=list)


def reconcile(client) -> PortfolioState:
    """Pull authoritative cash + positions from Kalshi. Trust Kalshi over local state."""
    cash = client.get_balance()
    if cash is None:
        raise RuntimeError("reconcile: failed to fetch balance")
    raw_positions = client.list_positions()
    positions: dict[str, int] = {}
    for p in raw_positions:
        ticker = p.get("ticker")
        if not ticker:
            continue
        count = int(p.get("position") or 0)
        positions[ticker] = count
    return PortfolioState(cash_cents=int(cash), positions=positions, raw_positions=raw_positions)


# ────────────────────────────────────────────────────────────────────────────
# Killswitch
# ────────────────────────────────────────────────────────────────────────────

@dataclass
class Killswitch:
    """
    Tracks failure counts and decides when to halt.

    Triggers (any):
      - HALT_FILE exists
      - cash_cents below floor
      - >= max_consecutive_api_errors cycles in a row had API errors
      - >= max_errors_within_30m API errors within last 30 min
      - >= max_consecutive_malformed agent responses in a row
    """
    cash_floor_cents: int = DEFAULT_CASH_FLOOR_CENTS
    max_consecutive_api_errors: int = DEFAULT_MAX_CONSECUTIVE_API_ERRORS
    max_errors_within_30m: int = DEFAULT_MAX_ERRORS_WITHIN_30M
    max_consecutive_malformed: int = DEFAULT_MAX_CONSECUTIVE_MALFORMED

    consecutive_api_error_cycles: int = 0
    consecutive_malformed_responses: int = 0
    recent_error_timestamps: list[float] = field(default_factory=list)
    tripped: bool = False
    trip_reason: Optional[str] = None

    def note_api_error(self) -> None:
        self.recent_error_timestamps.append(time.time())
        cutoff = time.time() - 30 * 60
        self.recent_error_timestamps = [t for t in self.recent_error_timestamps if t >= cutoff]

    def note_cycle_api_status(self, had_errors: bool) -> None:
        self.consecutive_api_error_cycles = self.consecutive_api_error_cycles + 1 if had_errors else 0

    def note_agent_response(self, malformed: bool) -> None:
        self.consecutive_malformed_responses = self.consecutive_malformed_responses + 1 if malformed else 0

    def check(self, cash_cents: int) -> Optional[str]:
        if HALT_FILE.exists():
            return f"halt_file_present:{HALT_FILE}"
        if cash_cents < self.cash_floor_cents:
            return f"cash_below_floor:{cash_cents}<{self.cash_floor_cents}"
        if self.consecutive_api_error_cycles >= self.max_consecutive_api_errors:
            return f"consecutive_api_error_cycles:{self.consecutive_api_error_cycles}"
        if len(self.recent_error_timestamps) >= self.max_errors_within_30m:
            return f"errors_within_30m:{len(self.recent_error_timestamps)}"
        if self.consecutive_malformed_responses >= self.max_consecutive_malformed:
            return f"consecutive_malformed:{self.consecutive_malformed_responses}"
        return None

    def trip(self, conn: sqlite3.Connection, reason: str) -> None:
        self.tripped = True
        self.trip_reason = reason
        with conn:
            conn.execute(
                "INSERT INTO killswitch_events (ts, kind, detail) VALUES (?, ?, ?)",
                (now_iso(), "tripped", reason),
            )
        logging.error("KILLSWITCH TRIPPED: %s", reason)


# ────────────────────────────────────────────────────────────────────────────
# Graceful shutdown
# ────────────────────────────────────────────────────────────────────────────

_shutdown_requested = False


def _handle_signal(signum):
    global _shutdown_requested
    logging.warning("signal %s received; will exit cleanly after current cycle", signum)
    _shutdown_requested = True


def install_signal_handlers() -> None:
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)


def shutdown_requested() -> bool:
    return _shutdown_requested


# ────────────────────────────────────────────────────────────────────────────
# Cycle bookkeeping
# ────────────────────────────────────────────────────────────────────────────

def open_cycle(conn: sqlite3.Connection, active_prompts: dict[str, str]) -> int:
    cycle_id = int(time.time() * 1000)
    with conn:
        conn.execute(
            "INSERT INTO cycles (cycle_id, started_at, status, active_prompts_json) VALUES (?, ?, ?, ?)",
            (cycle_id, now_iso(), "running", json.dumps(active_prompts)),
        )
    return cycle_id


def close_cycle(conn: sqlite3.Connection, cycle_id: int, status: str, notes: str = "") -> None:
    with conn:
        conn.execute(
            "UPDATE cycles SET ended_at=?, status=?, notes=? WHERE cycle_id=?",
            (now_iso(), status, notes, cycle_id),
        )


def skip_cycle(conn: sqlite3.Connection, reason: str) -> None:
    cycle_id = int(time.time() * 1000)
    with conn:
        conn.execute(
            "INSERT INTO cycles (cycle_id, started_at, ended_at, status, skipped, notes) "
            "VALUES (?, ?, ?, ?, 1, ?)",
            (cycle_id, now_iso(), now_iso(), "skipped", reason),
        )


# ────────────────────────────────────────────────────────────────────────────
# LLM cost wrapper + budget enforcement
# ────────────────────────────────────────────────────────────────────────────

# $/MTok rates (verify at implementation; treat as config, not law).
# Source: Anthropic public pricing as of design-doc date.
MODEL_RATES_PER_MTOK: dict[str, dict[str, float]] = {
    "claude-opus-4-7":          {"in": 5.00, "out": 25.00},
    "claude-sonnet-4-6":        {"in":  3.00, "out": 15.00},
    "claude-haiku-4-5-20251001":{"in":  1.00, "out":  5.00},
}

# Anthropic server-side tool pricing — billed PER CALL, not per token, so it
# doesn't fit inside MODEL_RATES_PER_MTOK. Charged in addition to token cost.
# web_search: $10 per 1,000 searches → $0.010/call.
#   https://docs.anthropic.com/en/docs/agents-and-tools/tool-use/web-search-tool
# Rate limits are organization-level and shared with other tools; not codified
# here — if a research call starts 429-ing, surface it through the killswitch
# (note_api_error) rather than trying to pre-throttle.
TOOL_COST_USD_PER_CALL: dict[str, float] = {
    "web_search": 0.010,
}


def estimate_tool_cost_usd(tool_name: str, call_count: int) -> float:
    rate = TOOL_COST_USD_PER_CALL.get(tool_name, 0.0)
    return rate * call_count


# Anthropic prompt-cache multipliers, applied against the model's input rate.
# Writes (first call seeding the cache) cost 1.25× the base input rate; reads
# (subsequent hits within the 5-min TTL) cost 0.10×.
#   https://docs.anthropic.com/en/docs/build-with-claude/prompt-caching
CACHE_WRITE_MULTIPLIER = 1.25
CACHE_READ_MULTIPLIER = 0.10


def build_system_block(text: str, cacheable: bool):
    """
    Build the value passed to `messages.create(system=...)`.

    If `cacheable`, returns a list with an ephemeral cache_control marker so
    Anthropic caches the system prefix. Otherwise returns the plain string.

    Callers must NOT mark a system prompt cacheable if its body varies per call
    (e.g. reflection rewrite targets) — varying bodies cause cache thrash.
    """
    if not cacheable:
        return text
    return [{"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}]


class BudgetExhausted(RuntimeError):
    pass


class MalformedLLMResponse(ValueError):
    pass


@dataclass
class BudgetCounter:
    """Per-market accumulator. Orchestrator owns one per market; checks before each call."""
    market_cap_usd: float
    cycle_cap_usd: float
    cycle_spent_usd: float = 0.0
    market_spent_usd: float = 0.0

    def will_exceed(self, projected_call_usd: float = 0.0) -> Optional[str]:
        if self.market_spent_usd + projected_call_usd > self.market_cap_usd:
            return f"market_budget:{self.market_spent_usd:.4f}+{projected_call_usd:.4f}>{self.market_cap_usd:.4f}"
        if self.cycle_spent_usd + projected_call_usd > self.cycle_cap_usd:
            return f"cycle_budget:{self.cycle_spent_usd:.4f}+{projected_call_usd:.4f}>{self.cycle_cap_usd:.4f}"
        return None

    def add(self, usd: float) -> None:
        self.market_spent_usd += usd
        self.cycle_spent_usd += usd


def estimate_cost_usd(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_creation_input_tokens: int = 0,
    cache_read_input_tokens: int = 0,
) -> float:
    rates = MODEL_RATES_PER_MTOK.get(model)
    if not rates:
        return 0.0
    in_rate = rates["in"]
    return (
        (input_tokens / 1_000_000.0) * in_rate
        + (output_tokens / 1_000_000.0) * rates["out"]
        + (cache_creation_input_tokens / 1_000_000.0) * in_rate * CACHE_WRITE_MULTIPLIER
        + (cache_read_input_tokens / 1_000_000.0) * in_rate * CACHE_READ_MULTIPLIER
    )


def call_llm(
    *,
    client,
    model: str,
    system,
    user: str,
    budget: BudgetCounter,
    max_tokens: int = 2048,
) -> tuple[str, dict]:
    """
    Single LLM call with budget enforcement. Returns (text, usage_dict).

    `system` is either a plain string or the list-of-blocks shape produced by
    `build_system_block(..., cacheable=True)`. Anthropic's API accepts either.

    Raises BudgetExhausted before the call if either cap is already crossed.
    """
    over = budget.will_exceed(0.0)
    if over:
        raise BudgetExhausted(over)

    resp = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    in_t = getattr(resp.usage, "input_tokens", 0)
    out_t = getattr(resp.usage, "output_tokens", 0)
    cache_write = getattr(resp.usage, "cache_creation_input_tokens", 0) or 0
    cache_read = getattr(resp.usage, "cache_read_input_tokens", 0) or 0
    cost = estimate_cost_usd(model, in_t, out_t, cache_write, cache_read)
    budget.add(cost)

    # Extract text from the first text block.
    text = ""
    for block in resp.content:
        if getattr(block, "type", None) == "text":
            text = block.text
            break
    return text, {
        "model": model,
        "input_tokens": in_t,
        "output_tokens": out_t,
        "cache_creation_input_tokens": cache_write,
        "cache_read_input_tokens": cache_read,
        "cost_usd": cost,
    }


def parse_llm_json(text: str):
    """Strip optional code fences and parse JSON. Raises MalformedLLMResponse on failure."""
    import re
    cleaned = text.strip()
    fence = re.match(r"^```(?:json)?\s*\n?(.*?)\n?```$", cleaned, re.DOTALL)
    if fence:
        cleaned = fence.group(1).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as e:
        raise MalformedLLMResponse(f"json_decode_error: {e}; head={cleaned[:200]!r}")

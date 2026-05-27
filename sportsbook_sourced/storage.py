from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = REPO_ROOT / "data"
DB_PATH = DATA_DIR / "sportsbook_sourced.sqlite"


SCHEMA = """
CREATE TABLE IF NOT EXISTS sportsbook_events (
  event_id TEXT PRIMARY KEY,
  league TEXT NOT NULL,
  home_team TEXT NOT NULL,
  away_team TEXT NOT NULL,
  commence_time TEXT NOT NULL,
  raw_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sportsbook_odds_snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  event_id TEXT NOT NULL,
  bookmaker TEXT NOT NULL,
  market_type TEXT NOT NULL,
  outcome_name TEXT NOT NULL,
  american_odds INTEGER NOT NULL,
  last_update TEXT NOT NULL,
  collected_at TEXT NOT NULL,
  raw_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS fair_price_snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  event_id TEXT NOT NULL,
  league TEXT NOT NULL,
  market_type TEXT NOT NULL,
  home_team TEXT NOT NULL,
  away_team TEXT NOT NULL,
  home_prob REAL NOT NULL,
  away_prob REAL NOT NULL,
  source_count INTEGER NOT NULL,
  sharp_source_count INTEGER NOT NULL,
  staleness_seconds INTEGER NOT NULL,
  book_disagreement_cents REAL NOT NULL,
  confidence REAL NOT NULL,
  computed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS kalshi_market_snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ticker TEXT NOT NULL,
  title TEXT NOT NULL,
  yes_subtitle TEXT,
  close_time TEXT,
  yes_bid_cents INTEGER NOT NULL,
  yes_ask_cents INTEGER NOT NULL,
  volume REAL NOT NULL,
  open_interest REAL NOT NULL,
  collected_at TEXT NOT NULL,
  raw_market_json TEXT NOT NULL,
  raw_orderbook_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS event_mappings (
  mapping_id TEXT PRIMARY KEY,
  kalshi_ticker TEXT NOT NULL,
  sportsbook_event_id TEXT NOT NULL,
  mapped_yes_outcome TEXT NOT NULL,
  confidence REAL NOT NULL,
  mismatch_flags_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS opportunities (
  opportunity_id TEXT PRIMARY KEY,
  kalshi_ticker TEXT NOT NULL,
  sportsbook_event_id TEXT NOT NULL,
  side TEXT NOT NULL,
  action TEXT NOT NULL,
  fair_prob REAL NOT NULL,
  kalshi_price_cents INTEGER NOT NULL,
  gross_edge_cents REAL NOT NULL,
  fee_cents_per_contract REAL NOT NULL,
  net_edge_cents REAL NOT NULL,
  max_contracts INTEGER NOT NULL,
  reason TEXT NOT NULL,
  computed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS paper_orders (
  paper_order_id TEXT PRIMARY KEY,
  opportunity_id TEXT NOT NULL,
  ticker TEXT NOT NULL,
  side TEXT NOT NULL,
  action TEXT NOT NULL,
  count INTEGER NOT NULL,
  limit_price_cents INTEGER NOT NULL,
  status TEXT NOT NULL,
  fill_model_version TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS trade_evaluations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  opportunity_id TEXT NOT NULL,
  evaluated_at TEXT NOT NULL,
  entry_price_cents INTEGER NOT NULL,
  fair_prob_at_entry REAL NOT NULL,
  fair_prob_at_close REAL,
  clv_cents REAL,
  resolved_side TEXT,
  pnl_cents REAL
);

CREATE INDEX IF NOT EXISTS idx_odds_event ON sportsbook_odds_snapshots(event_id);
CREATE INDEX IF NOT EXISTS idx_kalshi_ticker_time ON kalshi_market_snapshots(ticker, collected_at);
CREATE INDEX IF NOT EXISTS idx_opportunities_ticker_time ON opportunities(kalshi_ticker, computed_at);
"""


def init_db(path: Path | None = None) -> sqlite3.Connection:
    """Open the SQLite DB and ensure schema exists.

    Default `path` is resolved at call time (not import time) so tests can
    monkeypatch the module-level `DB_PATH` constant and the change takes
    effect immediately. Callers passing an explicit `path` still get
    exactly that path.
    """
    resolved = Path(path) if path is not None else DB_PATH
    resolved.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(resolved))
    conn.execute("PRAGMA journal_mode=WAL")
    with conn:
        conn.executescript(SCHEMA)
    return conn


def to_json(value: Any) -> str:
    if is_dataclass(value):
        value = asdict(value)
    return json.dumps(value, default=str)


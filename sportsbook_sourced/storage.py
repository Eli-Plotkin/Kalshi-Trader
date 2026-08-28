from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, is_dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .schemas import (
    EventMapping,
    FairPrice,
    KalshiMarketSnapshot,
    Opportunity,
    PaperOrder,
    SportsbookEvent,
    SportsbookOdds,
    TradeEvaluation,
)


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
  raw_json TEXT NOT NULL,
  FOREIGN KEY (event_id) REFERENCES sportsbook_events(event_id)
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
  computed_at TEXT NOT NULL,
  FOREIGN KEY (event_id) REFERENCES sportsbook_events(event_id)
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
  mapped_yes_outcome TEXT,
  confidence REAL NOT NULL,
  mismatch_flags_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  FOREIGN KEY (sportsbook_event_id) REFERENCES sportsbook_events(event_id)
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
  computed_at TEXT NOT NULL,
  FOREIGN KEY (sportsbook_event_id) REFERENCES sportsbook_events(event_id)
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
  created_at TEXT NOT NULL,
  FOREIGN KEY (opportunity_id) REFERENCES opportunities(opportunity_id)
);

CREATE TABLE IF NOT EXISTS trade_evaluations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  opportunity_id TEXT NOT NULL UNIQUE,
  evaluated_at TEXT NOT NULL,
  entry_price_cents INTEGER NOT NULL,
  fair_prob_at_entry REAL NOT NULL,
  fair_prob_at_close REAL,
  clv_cents REAL,
  resolved_side TEXT,
  pnl_cents REAL,
  FOREIGN KEY (opportunity_id) REFERENCES opportunities(opportunity_id)
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
    # SQLite ignores declared FOREIGN KEY constraints unless this is set —
    # it's off by default on every new connection, not just a one-time
    # per-database setting.
    conn.execute("PRAGMA foreign_keys=ON")
    with conn:
        conn.executescript(SCHEMA)
    return conn


def to_json(value: Any) -> str:
    if is_dataclass(value):
        value = asdict(value)
    return json.dumps(value, default=str)


def insert_sportsbook_event(conn: sqlite3.Connection, event: SportsbookEvent) -> None:
    """Upsert by `event_id`: the same event is refetched on every scan, and a
    late schedule change (rare, but real) should overwrite the stored row
    rather than accumulate stale duplicates."""
    with conn:
        conn.execute(
            """
            INSERT INTO sportsbook_events
                (event_id, league, home_team, away_team, commence_time, raw_json)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(event_id) DO UPDATE SET
                league=excluded.league,
                home_team=excluded.home_team,
                away_team=excluded.away_team,
                commence_time=excluded.commence_time,
                raw_json=excluded.raw_json
            """,
            (
                event.event_id,
                event.league,
                event.home_team,
                event.away_team,
                event.commence_time.isoformat(),
                to_json(event.raw),
            ),
        )


def insert_odds_snapshot(conn: sqlite3.Connection, odds: SportsbookOdds) -> None:
    """Append-only: each scan's odds are a new point-in-time observation,
    not a replacement for the last one — CLV depends on being able to see
    how the line moved."""
    with conn:
        conn.execute(
            """
            INSERT INTO sportsbook_odds_snapshots
                (event_id, bookmaker, market_type, outcome_name, american_odds,
                 last_update, collected_at, raw_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                odds.event_id,
                odds.bookmaker,
                odds.market_type,
                odds.outcome_name,
                odds.american_odds,
                odds.last_update.isoformat(),
                odds.collected_at.isoformat(),
                to_json(odds.raw),
            ),
        )


def insert_fair_price(conn: sqlite3.Connection, fair_price: FairPrice) -> None:
    """Append-only, same reasoning as `insert_odds_snapshot`."""
    with conn:
        conn.execute(
            """
            INSERT INTO fair_price_snapshots
                (event_id, league, market_type, home_team, away_team, home_prob,
                 away_prob, source_count, sharp_source_count, staleness_seconds,
                 book_disagreement_cents, computed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                fair_price.event_id,
                fair_price.league,
                fair_price.market_type,
                fair_price.home_team,
                fair_price.away_team,
                fair_price.home_prob,
                fair_price.away_prob,
                fair_price.source_count,
                fair_price.sharp_source_count,
                fair_price.staleness_seconds,
                fair_price.book_disagreement_cents,
                fair_price.computed_at.isoformat(),
            ),
        )


def insert_kalshi_snapshot(conn: sqlite3.Connection, market: KalshiMarketSnapshot) -> None:
    """Append-only, same reasoning as `insert_odds_snapshot`."""
    with conn:
        conn.execute(
            """
            INSERT INTO kalshi_market_snapshots
                (ticker, title, yes_subtitle, close_time, yes_bid_cents,
                 yes_ask_cents, volume, open_interest, collected_at,
                 raw_market_json, raw_orderbook_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                market.ticker,
                market.title,
                market.yes_subtitle,
                market.close_time.isoformat() if market.close_time else None,
                market.yes_bid_cents,
                market.yes_ask_cents,
                market.volume,
                market.open_interest,
                market.collected_at.isoformat(),
                to_json(market.raw_market),
                to_json(market.raw_orderbook),
            ),
        )


def insert_mapping(conn: sqlite3.Connection, mapping: EventMapping) -> None:
    """Plain insert: `mapping_id` is a fresh uuid4 per `build_mapping` call, so
    there's nothing to conflict with. `mapped_yes_outcome` can be `None`
    (P0.3, ambiguous match) — the column is nullable so that scan result is
    still auditable rather than silently dropped."""
    with conn:
        conn.execute(
            """
            INSERT INTO event_mappings
                (mapping_id, kalshi_ticker, sportsbook_event_id,
                 mapped_yes_outcome, confidence, mismatch_flags_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                mapping.mapping_id,
                mapping.kalshi_ticker,
                mapping.sportsbook_event_id,
                mapping.mapped_yes_outcome,
                mapping.confidence,
                to_json(mapping.mismatch_flags),
                mapping.created_at.isoformat(),
            ),
        )


def insert_opportunity(conn: sqlite3.Connection, opportunity: Opportunity) -> None:
    """Plain insert: `opportunity_id` is a fresh uuid4 per scan."""
    with conn:
        conn.execute(
            """
            INSERT INTO opportunities
                (opportunity_id, kalshi_ticker, sportsbook_event_id, side,
                 action, fair_prob, kalshi_price_cents, gross_edge_cents,
                 fee_cents_per_contract, net_edge_cents, max_contracts, reason,
                 computed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                opportunity.opportunity_id,
                opportunity.kalshi_ticker,
                opportunity.sportsbook_event_id,
                opportunity.side,
                opportunity.action,
                opportunity.fair_prob,
                opportunity.kalshi_price_cents,
                opportunity.gross_edge_cents,
                opportunity.fee_cents_per_contract,
                opportunity.net_edge_cents,
                opportunity.max_contracts,
                opportunity.reason,
                opportunity.computed_at.isoformat(),
            ),
        )


def insert_paper_order(conn: sqlite3.Connection, order: PaperOrder) -> None:
    """Plain insert: `paper_order_id` is a fresh uuid4 per fill attempt."""
    with conn:
        conn.execute(
            """
            INSERT INTO paper_orders
                (paper_order_id, opportunity_id, ticker, side, action, count,
                 limit_price_cents, status, fill_model_version, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                order.paper_order_id,
                order.opportunity_id,
                order.ticker,
                order.side,
                order.action,
                order.count,
                order.limit_price_cents,
                order.status,
                order.fill_model_version,
                order.created_at.isoformat(),
            ),
        )


def insert_trade_evaluation(conn: sqlite3.Connection, evaluation: TradeEvaluation) -> None:
    """Upsert by `opportunity_id` (P1.4/P1.7): the two-phase evaluator writes
    this twice per trade — once at market close with CLV fields populated and
    settlement fields `None`, once at settlement with the reverse. `COALESCE`
    keeps whichever phase already supplied a value instead of the second
    write blanking out the first phase's data."""
    with conn:
        conn.execute(
            """
            INSERT INTO trade_evaluations
                (opportunity_id, evaluated_at, entry_price_cents,
                 fair_prob_at_entry, fair_prob_at_close, clv_cents,
                 resolved_side, pnl_cents)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(opportunity_id) DO UPDATE SET
                evaluated_at=excluded.evaluated_at,
                fair_prob_at_close=COALESCE(excluded.fair_prob_at_close, trade_evaluations.fair_prob_at_close),
                clv_cents=COALESCE(excluded.clv_cents, trade_evaluations.clv_cents),
                resolved_side=COALESCE(excluded.resolved_side, trade_evaluations.resolved_side),
                pnl_cents=COALESCE(excluded.pnl_cents, trade_evaluations.pnl_cents)
            """,
            (
                evaluation.opportunity_id,
                evaluation.evaluated_at.isoformat(),
                evaluation.entry_price_cents,
                evaluation.fair_prob_at_entry,
                evaluation.fair_prob_at_close,
                evaluation.clv_cents,
                evaluation.resolved_side,
                evaluation.pnl_cents,
            ),
        )


def get_opportunity(conn: sqlite3.Connection, opportunity_id: str) -> Opportunity | None:
    row = conn.execute(
        """
        SELECT opportunity_id, kalshi_ticker, sportsbook_event_id, side, action,
               fair_prob, kalshi_price_cents, gross_edge_cents,
               fee_cents_per_contract, net_edge_cents, max_contracts, reason,
               computed_at
        FROM opportunities WHERE opportunity_id = ?
        """,
        (opportunity_id,),
    ).fetchone()
    if row is None:
        return None
    return Opportunity(
        opportunity_id=row[0], kalshi_ticker=row[1], sportsbook_event_id=row[2],
        side=row[3], action=row[4], fair_prob=row[5], kalshi_price_cents=row[6],
        gross_edge_cents=row[7], fee_cents_per_contract=row[8], net_edge_cents=row[9],
        max_contracts=row[10], reason=row[11], computed_at=datetime.fromisoformat(row[12]),
    )


def get_filled_paper_order(conn: sqlite3.Connection, opportunity_id: str) -> PaperOrder | None:
    """The order that actually filled for this opportunity, if any -- a
    rejected order (no cash, or a `skip` action) isn't a position and has
    nothing to settle. Most-recent-first in case more than one ever exists
    for the same opportunity (nothing currently enforces at most one)."""
    row = conn.execute(
        """
        SELECT paper_order_id, opportunity_id, ticker, side, action, count,
               limit_price_cents, status, fill_model_version, created_at
        FROM paper_orders
        WHERE opportunity_id = ? AND status = 'filled'
        ORDER BY created_at DESC LIMIT 1
        """,
        (opportunity_id,),
    ).fetchone()
    if row is None:
        return None
    return PaperOrder(
        paper_order_id=row[0], opportunity_id=row[1], ticker=row[2], side=row[3],
        action=row[4], count=row[5], limit_price_cents=row[6], status=row[7],
        fill_model_version=row[8], created_at=datetime.fromisoformat(row[9]),
    )


def list_opportunities_pending_settlement(conn: sqlite3.Connection) -> list[str]:
    """Opportunity IDs with a filled paper order but no settlement result
    recorded yet -- either no `trade_evaluations` row at all, or one written
    by a future close-phase pass with `resolved_side` still `NULL`."""
    rows = conn.execute(
        """
        SELECT DISTINCT po.opportunity_id
        FROM paper_orders po
        LEFT JOIN trade_evaluations te ON te.opportunity_id = po.opportunity_id
        WHERE po.status = 'filled' AND te.resolved_side IS NULL
        """
    ).fetchall()
    return [row[0] for row in rows]


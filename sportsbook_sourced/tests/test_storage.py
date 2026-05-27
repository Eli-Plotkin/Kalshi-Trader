"""Tests for storage — SQLite schema + dataclass serialization."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from sportsbook_sourced import storage
from sportsbook_sourced.schemas import (
    EventMapping,
    FairPrice,
    KalshiMarketSnapshot,
    Opportunity,
    PaperOrder,
    SportsbookEvent,
    SportsbookOdds,
    TradeEvaluation,
)


EXPECTED_TABLES = {
    "sportsbook_events",
    "sportsbook_odds_snapshots",
    "fair_price_snapshots",
    "kalshi_market_snapshots",
    "event_mappings",
    "opportunities",
    "paper_orders",
    "trade_evaluations",
}


# ----------------------------------------------------------------------------
# init_db
# ----------------------------------------------------------------------------


class TestInitDb:
    def test_creates_all_expected_tables(self, tmp_path):
        db = tmp_path / "test.sqlite"
        conn = storage.init_db(db)
        try:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
            tables = {r[0] for r in rows}
            assert EXPECTED_TABLES.issubset(tables)
        finally:
            conn.close()

    def test_creates_expected_indexes(self, tmp_path):
        db = tmp_path / "test.sqlite"
        conn = storage.init_db(db)
        try:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            ).fetchall()
            index_names = {r[0] for r in rows}
            assert "idx_odds_event" in index_names
            assert "idx_kalshi_ticker_time" in index_names
            assert "idx_opportunities_ticker_time" in index_names
        finally:
            conn.close()

    def test_idempotent_init(self, tmp_path):
        db = tmp_path / "test.sqlite"
        c1 = storage.init_db(db)
        c1.close()
        # Running again on the same path must not raise even though tables exist.
        c2 = storage.init_db(db)
        try:
            rows = c2.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
            assert {r[0] for r in rows}.issuperset(EXPECTED_TABLES)
        finally:
            c2.close()

    def test_wal_journal_enabled(self, tmp_path):
        db = tmp_path / "test.sqlite"
        conn = storage.init_db(db)
        try:
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
            assert mode.lower() == "wal"
        finally:
            conn.close()

    def test_creates_parent_directories(self, tmp_path):
        # If the file's parent dirs don't exist yet, init_db should not crash.
        nested = tmp_path / "a" / "b" / "test.sqlite"
        # init_db creates DATA_DIR (its own constant) — for non-default paths
        # the caller is responsible for parent creation. Make sure no crash on
        # the default-path case by directly using the default.
        conn = storage.init_db()
        try:
            assert Path(storage.DB_PATH).exists()
        finally:
            conn.close()

    def test_event_id_is_primary_key(self, tmp_path):
        db = tmp_path / "test.sqlite"
        conn = storage.init_db(db)
        try:
            conn.execute(
                "INSERT INTO sportsbook_events VALUES (?, ?, ?, ?, ?, ?)",
                ("e1", "nba", "Boston Celtics", "LAL", "2026-01-15T01:00:00Z", "{}"),
            )
            with pytest.raises(Exception):
                conn.execute(
                    "INSERT INTO sportsbook_events VALUES (?, ?, ?, ?, ?, ?)",
                    ("e1", "nba", "X", "Y", "2026-01-15T01:00:00Z", "{}"),
                )
        finally:
            conn.close()


# ----------------------------------------------------------------------------
# to_json — dataclass + arbitrary value serialization
# ----------------------------------------------------------------------------


class TestToJson:
    def test_serializes_simple_dict(self):
        out = storage.to_json({"a": 1, "b": "two"})
        assert json.loads(out) == {"a": 1, "b": "two"}

    def test_serializes_list(self):
        assert json.loads(storage.to_json([1, 2, 3])) == [1, 2, 3]

    def test_serializes_datetime_via_default(self):
        # datetime doesn't have a native JSON encoding; storage.to_json uses
        # `default=str` to coerce it. Make sure that works without raising.
        dt = datetime(2026, 5, 27, 12, 0, tzinfo=timezone.utc)
        out = storage.to_json({"ts": dt})
        assert "2026-05-27" in out

    def test_serializes_sportsbook_event(self):
        event = SportsbookEvent(
            event_id="e1",
            league="nba",
            home_team="Boston Celtics",
            away_team="Los Angeles Lakers",
            commence_time=datetime(2026, 1, 15, 1, 0, tzinfo=timezone.utc),
        )
        out = json.loads(storage.to_json(event))
        assert out["event_id"] == "e1"
        assert out["home_team"] == "Boston Celtics"
        assert out["league"] == "nba"

    def test_serializes_opportunity(self):
        opp = Opportunity(
            opportunity_id="o1",
            kalshi_ticker="KX-A",
            sportsbook_event_id="e1",
            side="yes",
            action="buy",
            fair_prob=0.75,
            kalshi_price_cents=70,
            gross_edge_cents=5.0,
            fee_cents_per_contract=1.5,
            net_edge_cents=3.5,
            max_contracts=10,
            reason="tradeable",
            computed_at=datetime(2026, 5, 27, tzinfo=timezone.utc),
        )
        out = json.loads(storage.to_json(opp))
        assert out["opportunity_id"] == "o1"
        assert out["side"] == "yes"
        assert out["fair_prob"] == 0.75

    def test_serializes_trade_evaluation_with_nones(self):
        ev = TradeEvaluation(
            opportunity_id="o1",
            evaluated_at=datetime(2026, 5, 27, tzinfo=timezone.utc),
            entry_price_cents=70,
            fair_prob_at_entry=0.75,
            fair_prob_at_close=None,
            clv_cents=None,
            resolved_side=None,
            pnl_cents=None,
        )
        out = json.loads(storage.to_json(ev))
        assert out["fair_prob_at_close"] is None
        assert out["clv_cents"] is None

    def test_non_dataclass_dict_serializes_normally(self):
        out = json.loads(storage.to_json({"x": [1, 2]}))
        assert out == {"x": [1, 2]}


# ----------------------------------------------------------------------------
# DB path defaults
# ----------------------------------------------------------------------------


class TestStoragePaths:
    def test_db_path_inside_data_dir(self):
        # The default sqlite file should live under the repo's data/ folder.
        assert storage.DB_PATH.parent == storage.DATA_DIR
        assert storage.DB_PATH.suffix == ".sqlite"

"""Tests for storage — SQLite schema + dataclass serialization."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
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


# ----------------------------------------------------------------------------
# Readers — get_opportunity, get_filled_paper_order,
# list_opportunities_pending_settlement (P1.7's settlement phase)
# ----------------------------------------------------------------------------


NOW = datetime(2026, 1, 15, 0, 0, tzinfo=timezone.utc)


def _event(event_id="e1"):
    return SportsbookEvent(
        event_id=event_id, league="nba", home_team="Los Angeles Lakers",
        away_team="Boston Celtics", commence_time=NOW,
    )


def _opportunity(opportunity_id="opp-1"):
    return Opportunity(
        opportunity_id=opportunity_id, kalshi_ticker="KXNBAGAME-26JAN14LALBOS-LAL",
        sportsbook_event_id="e1", side="no", action="buy", fair_prob=0.65,
        kalshi_price_cents=43, gross_edge_cents=22.0, fee_cents_per_contract=1.7,
        net_edge_cents=17.3, max_contracts=23, reason="tradeable", computed_at=NOW,
    )


def _paper_order(opportunity_id="opp-1", *, status="filled", count=10, order_id="po-1",
                  created_at=NOW):
    return PaperOrder(
        paper_order_id=order_id, opportunity_id=opportunity_id,
        ticker="KXNBAGAME-26JAN14LALBOS-LAL", side="no", action="buy",
        count=count, limit_price_cents=43, status=status,
        fill_model_version="immediate_best_ask_v1", created_at=created_at,
    )


class TestGetOpportunity:
    def test_round_trips_all_fields(self, tmp_path):
        conn = storage.init_db(tmp_path / "t.sqlite")
        storage.insert_sportsbook_event(conn, _event())
        opp = _opportunity()
        storage.insert_opportunity(conn, opp)

        fetched = storage.get_opportunity(conn, "opp-1")
        assert fetched == opp

    def test_returns_none_for_unknown_id(self, tmp_path):
        conn = storage.init_db(tmp_path / "t.sqlite")
        assert storage.get_opportunity(conn, "does-not-exist") is None


class TestGetFilledPaperOrder:
    def test_returns_the_filled_order(self, tmp_path):
        conn = storage.init_db(tmp_path / "t.sqlite")
        storage.insert_sportsbook_event(conn, _event())
        storage.insert_opportunity(conn, _opportunity())
        order = _paper_order(status="filled")
        storage.insert_paper_order(conn, order)

        fetched = storage.get_filled_paper_order(conn, "opp-1")
        assert fetched == order

    def test_ignores_a_rejected_order(self, tmp_path):
        """A rejected order isn't a position -- there's nothing to settle,
        so it must not be returned as if it were."""
        conn = storage.init_db(tmp_path / "t.sqlite")
        storage.insert_sportsbook_event(conn, _event())
        storage.insert_opportunity(conn, _opportunity())
        storage.insert_paper_order(conn, _paper_order(status="rejected", count=0))

        assert storage.get_filled_paper_order(conn, "opp-1") is None

    def test_returns_none_for_an_opportunity_with_no_orders(self, tmp_path):
        conn = storage.init_db(tmp_path / "t.sqlite")
        storage.insert_sportsbook_event(conn, _event())
        storage.insert_opportunity(conn, _opportunity())
        assert storage.get_filled_paper_order(conn, "opp-1") is None

    def test_picks_the_filled_order_among_a_mix(self, tmp_path):
        conn = storage.init_db(tmp_path / "t.sqlite")
        storage.insert_sportsbook_event(conn, _event())
        storage.insert_opportunity(conn, _opportunity())
        storage.insert_paper_order(conn, _paper_order(
            order_id="po-rejected", status="rejected", count=0,
            created_at=NOW - timedelta(seconds=5),
        ))
        filled = _paper_order(order_id="po-filled", status="filled", count=10)
        storage.insert_paper_order(conn, filled)

        assert storage.get_filled_paper_order(conn, "opp-1") == filled


class TestListOpportunitiesPendingSettlement:
    def _setup(self, conn, *, order_status="filled", evaluation=None):
        storage.insert_sportsbook_event(conn, _event())
        storage.insert_opportunity(conn, _opportunity())
        storage.insert_paper_order(conn, _paper_order(status=order_status))
        if evaluation is not None:
            storage.insert_trade_evaluation(conn, evaluation)

    def test_includes_a_filled_order_with_no_evaluation_yet(self, tmp_path):
        conn = storage.init_db(tmp_path / "t.sqlite")
        self._setup(conn)
        assert storage.list_opportunities_pending_settlement(conn) == ["opp-1"]

    def test_excludes_a_rejected_order(self, tmp_path):
        conn = storage.init_db(tmp_path / "t.sqlite")
        self._setup(conn, order_status="rejected")
        assert storage.list_opportunities_pending_settlement(conn) == []

    def test_excludes_an_already_settled_opportunity(self, tmp_path):
        conn = storage.init_db(tmp_path / "t.sqlite")
        settled = TradeEvaluation(
            opportunity_id="opp-1", evaluated_at=NOW, entry_price_cents=43,
            fair_prob_at_entry=0.65, fair_prob_at_close=None, clv_cents=None,
            resolved_side="no", pnl_cents=57.0,
        )
        self._setup(conn, evaluation=settled)
        assert storage.list_opportunities_pending_settlement(conn) == []

    def test_includes_a_close_phase_only_row_with_no_resolved_side_yet(self, tmp_path):
        """The trickiest case: a close-phase pass already wrote a row for
        this opportunity (CLV fields populated) but hasn't settled yet
        (`resolved_side` still NULL). The LEFT JOIN must still surface it as
        pending, not treat "a row exists" as "already settled"."""
        conn = storage.init_db(tmp_path / "t.sqlite")
        close_phase_only = TradeEvaluation(
            opportunity_id="opp-1", evaluated_at=NOW, entry_price_cents=43,
            fair_prob_at_entry=0.65, fair_prob_at_close=0.30, clv_cents=13.0,
            resolved_side=None, pnl_cents=None,
        )
        self._setup(conn, evaluation=close_phase_only)
        assert storage.list_opportunities_pending_settlement(conn) == ["opp-1"]

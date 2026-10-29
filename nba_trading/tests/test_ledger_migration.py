"""Ledger schema migration (PRAGMA user_version): old DB -> new, fresh DB == migrated DB."""

from __future__ import annotations

import sqlite3

import pytest

from nba_trading import ledger as ledger_mod
from nba_trading.ledger import SCHEMA_VERSION, Ledger

# The schema exactly as it was before the G4 change (orders/settlements without the
# lifecycle columns, no kalshi_fills). Everything else is unchanged.
OLD_SCHEMA = """
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    game_id TEXT NOT NULL,
    home TEXT, away TEXT, tip_off_utc TEXT,
    ticker TEXT, ask_cents INTEGER,
    action TEXT NOT NULL CHECK (action IN ('buy', 'skip')),
    reason TEXT NOT NULL,
    shares INTEGER, limit_price INTEGER,
    balance_cents INTEGER, equity_cents INTEGER,
    band_min INTEGER, band_max INTEGER, fraction REAL,
    dry_run INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id INTEGER REFERENCES decisions(id),
    ts TEXT NOT NULL,
    ticker TEXT NOT NULL,
    client_order_id TEXT NOT NULL,
    order_id TEXT,
    count INTEGER NOT NULL,
    limit_price INTEGER NOT NULL,
    outcome TEXT NOT NULL,
    filled INTEGER
);
CREATE INDEX IF NOT EXISTS idx_orders_order_id ON orders(order_id);
CREATE TABLE IF NOT EXISTS settlements (
    order_pk INTEGER PRIMARY KEY REFERENCES orders(id),
    ts TEXT NOT NULL,
    result TEXT NOT NULL,
    filled INTEGER NOT NULL,
    fee_cents INTEGER NOT NULL,
    pnl_cents INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS paper_trades (
    decision_id INTEGER PRIMARY KEY REFERENCES decisions(id),
    ts TEXT NOT NULL,
    ticker TEXT NOT NULL,
    entry_cents INTEGER NOT NULL,
    result TEXT NOT NULL,
    fee_cents INTEGER NOT NULL,
    pnl_cents INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS snapshots (
    ts TEXT NOT NULL,
    balance_cents INTEGER, open_cost_cents INTEGER, equity_cents INTEGER,
    peak_cents INTEGER, drawdown_pct REAL
);
CREATE TABLE IF NOT EXISTS events (
    ts TEXT NOT NULL,
    level TEXT NOT NULL,
    kind TEXT NOT NULL,
    message TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS portfolio_lots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    price INTEGER NOT NULL,
    qty INTEGER NOT NULL,
    time REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS schedule_games (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id TEXT NOT NULL UNIQUE,
    home_team TEXT, away_team TEXT, tip_off_utc TEXT, trigger_time_utc TEXT,
    fired INTEGER NOT NULL DEFAULT 0
);
"""

TABLES = ["decisions", "orders", "settlements", "kalshi_fills", "paper_trades", "snapshots",
          "events", "kv", "portfolio_lots", "schedule_games"]


def make_old_db(path):
    """Create a database as the pre-change bot would have, with representative rows."""
    c = sqlite3.connect(path)
    c.executescript(OLD_SCHEMA)
    c.execute("INSERT INTO decisions (ts, game_id, action, reason, dry_run, ticker, limit_price)"
              " VALUES ('t0','g1','buy','in_band',0,'T1',92)")
    c.execute("INSERT INTO orders (decision_id, ts, ticker, client_order_id, order_id, count,"
              " limit_price, outcome, filled) VALUES (1,'t1','T1','c1','o1',5,92,'placed',5)")
    c.execute("INSERT INTO orders (decision_id, ts, ticker, client_order_id, order_id, count,"
              " limit_price, outcome, filled) VALUES (1,'t2','T2','c2',NULL,3,90,'failed',NULL)")
    c.execute("INSERT INTO orders (decision_id, ts, ticker, client_order_id, order_id, count,"
              " limit_price, outcome, filled) VALUES (1,'t3','T3','c3','o3',2,80,'placed',2)")
    c.execute("INSERT INTO settlements VALUES (3,'t4','yes',2,1,39)")
    c.execute("INSERT INTO kv VALUES ('high_water_peak_cents','12345')")
    c.execute("INSERT INTO portfolio_lots (ticker, price, qty, time) VALUES ('T1',92,5,1.5)")
    c.commit()
    c.close()


def schema_of(led):
    cols = {t: [tuple(r)[1:] for r in led.conn.execute(f"PRAGMA table_info({t})")] for t in TABLES}
    idx = sorted(r[0] for r in led.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_%'"))
    return cols, idx


def test_fresh_db_is_at_current_version(tmp_path):
    led = Ledger(str(tmp_path / "f.sqlite"))
    assert led.conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    cols = {r["name"] for r in led.conn.execute("PRAGMA table_info(orders)")}
    assert {"status", "avg_fill_cents", "updated_ts", "resolved_via"} <= cols
    cols = {r["name"] for r in led.conn.execute("PRAGMA table_info(settlements)")}
    assert {"source", "avg_entry_cents"} <= cols
    assert [r["name"] for r in led.conn.execute("PRAGMA table_info(kalshi_fills)")] == [
        "fill_id", "order_id", "ticker", "count", "price_cents", "side", "action", "fee_cents", "ts"]
    led.close()


def test_old_db_migrates_to_identical_schema_as_fresh(tmp_path):
    make_old_db(str(tmp_path / "old.sqlite"))
    old = Ledger(str(tmp_path / "old.sqlite"))
    fresh = Ledger(str(tmp_path / "fresh.sqlite"))
    assert schema_of(old) == schema_of(fresh)
    assert old.conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    old.close(); fresh.close()


def test_old_data_survives_migration_unchanged(tmp_path):
    make_old_db(str(tmp_path / "old.sqlite"))
    led = Ledger(str(tmp_path / "old.sqlite"))
    rows = {r["id"]: dict(r) for r in led.conn.execute("SELECT * FROM orders")}
    assert rows[1]["outcome"] == "placed" and rows[1]["order_id"] == "o1" and rows[1]["filled"] == 5
    assert rows[1]["limit_price"] == 92 and rows[1]["client_order_id"] == "c1"
    assert rows[2]["outcome"] == "failed" and rows[2]["order_id"] is None
    for r in rows.values():
        assert r["status"] is None and r["avg_fill_cents"] is None
        assert r["updated_ts"] is None and r["resolved_via"] is None
    s = dict(led.conn.execute("SELECT * FROM settlements").fetchone())
    assert (s["order_pk"], s["result"], s["filled"], s["fee_cents"], s["pnl_cents"]) == (3, "yes", 2, 1, 39)
    assert s["source"] is None and s["avg_entry_cents"] is None
    assert led.get_peak_cents() == 12345
    assert led.load_portfolio_lots() == {"T1": [{"price": 92, "qty": 5, "time": 1.5}]}
    # existing read paths still work on migrated legacy rows
    assert [r["id"] for r in led.unsettled_orders()] == [1]
    assert led.has_order("c2")
    led.close()


def test_migrated_old_db_supports_new_methods(tmp_path):
    make_old_db(str(tmp_path / "old.sqlite"))
    led = Ledger(str(tmp_path / "old.sqlite"))
    pk, created = led.begin_order(1, "T9", "c9", 4, 91)
    assert created and pk == 4
    led.mark_placed(pk, "o9", "resting")
    assert led.upsert_fills([{"fill_id": "f1", "order_id": "o9", "ticker": "T9", "count": 4,
                              "price_cents": 91, "fee_cents": 1}]) == 1
    assert led.record_settlement_from_fills(pk, "yes") == 4 * 9 - 1
    led.close()


def test_migration_is_idempotent_and_noop_on_second_run(tmp_path):
    make_old_db(str(tmp_path / "old.sqlite"))
    led = Ledger(str(tmp_path / "old.sqlite"))
    before = schema_of(led)
    led._migrate()                                    # explicit second run
    # even with user_version rewound, replaying the steps changes nothing
    led.conn.execute("PRAGMA user_version = 0")
    led._migrate()
    assert schema_of(led) == before
    assert led.conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    led.close()
    led2 = Ledger(str(tmp_path / "old.sqlite"))       # reopen: still the same
    assert schema_of(led2) == before
    led2.close()


def test_half_applied_migration_is_completed(tmp_path):
    """Columns already present (e.g. a crash after DDL but before the version bump)."""
    p = str(tmp_path / "half.sqlite")
    make_old_db(p)
    c = sqlite3.connect(p)
    c.execute("ALTER TABLE orders ADD COLUMN status TEXT")
    c.execute("ALTER TABLE orders ADD COLUMN avg_fill_cents REAL")
    c.commit(); c.close()
    led = Ledger(p)
    fresh = Ledger(str(tmp_path / "fresh.sqlite"))
    assert schema_of(led) == schema_of(fresh)
    led.close(); fresh.close()


def test_failed_migration_rolls_back_and_keeps_old_version(tmp_path, monkeypatch):
    p = str(tmp_path / "old.sqlite")
    make_old_db(p)

    def boom(conn):
        conn.execute("ALTER TABLE kv ADD COLUMN extra TEXT")
        raise RuntimeError("boom")

    monkeypatch.setattr(ledger_mod, "MIGRATIONS", ledger_mod.MIGRATIONS + [(2, boom)])
    monkeypatch.setattr(ledger_mod, "SCHEMA_VERSION", 2)
    with pytest.raises(RuntimeError):
        Ledger(p)
    c = sqlite3.connect(p)
    assert c.execute("PRAGMA user_version").fetchone()[0] == 0
    assert "status" not in {r[1] for r in c.execute("PRAGMA table_info(orders)")}
    assert "extra" not in {r[1] for r in c.execute("PRAGMA table_info(kv)")}
    c.close()


def test_newer_database_is_left_untouched(tmp_path):
    p = str(tmp_path / "new.sqlite")
    led = Ledger(p)
    led.conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 5}")
    led.close()
    led = Ledger(p)
    assert led.conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION + 5
    led.close()

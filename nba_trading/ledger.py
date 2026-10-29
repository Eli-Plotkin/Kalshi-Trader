"""SQLite trade ledger for the NBA bot: the source of truth for the dashboard.

Every decision the bot makes (buy OR skip, with the reason) is recorded, plus
orders, fills, settlements, equity snapshots, and health events. The bot is
the only writer; the dashboard opens the file read-only.

Tables:
  decisions    one row per game evaluation: what the bot saw and chose
  orders       one row per order attempt (outcome, fill count once known); lifecycle in
               docs/design-g4-reconciliation.md section 3
  settlements  realized PnL once the market resolves (source: computed | kalshi_fills | ...)
  kalshi_fills Kalshi's own fill records (actual price + fee), synced by the bot
  paper_trades one-contract hypothetical trades for in-band DRY-RUN decisions, settled
               against the real market result (measures edge with no money at risk)
  snapshots    equity / drawdown over time
  events       heartbeats and anything an operator should see (errors, alerts)

Durable bot state lives here too, so ONE file (replicated by Litestream) is all
that needs backing up:
  portfolio_lots  open tax lots per ticker (what Portfolio tracks)
  schedule_games  today's games + their 'fired' flags (the schedule date is in kv)
  kv              small key/value state: high-water peak, schedule date, import markers
"""
from __future__ import annotations

import json
import logging
import math
import os
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone

SCHEMA = """
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
    filled INTEGER,
    status TEXT,
    avg_fill_cents REAL,
    updated_ts TEXT,
    resolved_via TEXT
);
CREATE INDEX IF NOT EXISTS idx_orders_order_id ON orders(order_id);
CREATE INDEX IF NOT EXISTS idx_orders_client_order_id ON orders(client_order_id);
CREATE TABLE IF NOT EXISTS settlements (
    order_pk INTEGER PRIMARY KEY REFERENCES orders(id),
    ts TEXT NOT NULL,
    result TEXT NOT NULL,
    filled INTEGER NOT NULL,
    fee_cents INTEGER NOT NULL,
    pnl_cents INTEGER NOT NULL,
    source TEXT,
    avg_entry_cents REAL
);
CREATE TABLE IF NOT EXISTS kalshi_fills (
    fill_id TEXT PRIMARY KEY,
    order_id TEXT,
    ticker TEXT,
    count INTEGER NOT NULL,
    price_cents INTEGER NOT NULL,
    side TEXT,
    action TEXT,
    fee_cents INTEGER,
    ts TEXT
);
CREATE INDEX IF NOT EXISTS idx_kalshi_fills_order_id ON kalshi_fills(order_id);
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

# kv keys
KV_PEAK = "high_water_peak_cents"
KV_SCHEDULE_DATE = "schedule_date"
_KV_IMPORTED = "legacy_import:"      # + "portfolio" | "high_water" | "schedule"


# --- schema migrations (PRAGMA user_version) ---
# SCHEMA above always describes the FINAL shape (new columns appended at the end, in
# the same order the steps below add them), so a fresh DB and a migrated old DB end up
# with an identical table_info. Each step is idempotent (columns are only added when
# missing) and the whole run is one transaction together with the user_version bump.

def _columns(conn, table):
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def _add_columns(conn, table, columns):
    have = _columns(conn, table)
    for name, decl in columns:
        if name not in have:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


def _migrate_1_order_lifecycle(conn):
    """G4: order lifecycle columns, settlement provenance, kalshi_fills table."""
    _add_columns(conn, "orders", [("status", "TEXT"), ("avg_fill_cents", "REAL"),
                                  ("updated_ts", "TEXT"), ("resolved_via", "TEXT")])
    _add_columns(conn, "settlements", [("source", "TEXT"), ("avg_entry_cents", "REAL")])
    conn.execute(
        "CREATE TABLE IF NOT EXISTS kalshi_fills ("
        " fill_id TEXT PRIMARY KEY, order_id TEXT, ticker TEXT, count INTEGER NOT NULL,"
        " price_cents INTEGER NOT NULL, side TEXT, action TEXT, fee_cents INTEGER, ts TEXT)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_kalshi_fills_order_id ON kalshi_fills(order_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_orders_client_order_id ON orders(client_order_id)")


MIGRATIONS = [            # (target user_version, step), ascending
    (1, _migrate_1_order_lifecycle),
]
SCHEMA_VERSION = MIGRATIONS[-1][0]

# Order lifecycle (orders.outcome). See docs/design-g4-reconciliation.md section 3.
OPEN_OUTCOMES = ("placed", "adopted")                 # tracked until settled
UNRESOLVED_OUTCOMES = ("submitting", "unconfirmed")   # need a Kalshi lookup
REJECTION_OUTCOMES = ("insufficient_funds", "blocked_location", "duplicate", "failed")
SETTABLE_OUTCOMES = ("unconfirmed", "not_found") + REJECTION_OUTCOMES   # via set_order_outcome
# set_order_outcome: allowed (current outcome -> new outcome). Same -> same is a no-op.
_OUTCOME_TRANSITIONS = {
    "submitting": set(SETTABLE_OUTCOMES),
    "unconfirmed": {"unconfirmed", "not_found", "failed"},
}
RESOLVED_VIA = ("response", "lookup", "startup", "adopt")


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def kalshi_fee_cents(count, price_cents):
    """Total taker fee in whole cents: ceil(0.07 * count * p * (1-p)), p in dollars."""
    p = price_cents / 100.0
    return math.ceil(100.0 * 0.07 * count * p * (1 - p) - 1e-9)


def settlement_pnl_cents(filled, price_cents, yes_won):
    """Realized PnL (cents) of buying `filled` YES contracts at `price_cents`."""
    fee = kalshi_fee_cents(filled, price_cents)
    gross = filled * (100 - price_cents) if yes_won else -filled * price_cents
    return gross - fee, fee


class Ledger:
    def __init__(self, path):
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.path = path
        # APScheduler runs jobs on worker threads, so the one connection is shared
        # across threads and every access is serialised by this lock.
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        self._migrate()

    def _migrate(self):
        """Bring the schema to SCHEMA_VERSION; idempotent, one transaction."""
        with self._lock:
            current = self.conn.execute("PRAGMA user_version").fetchone()[0]
            if current > SCHEMA_VERSION:
                logging.warning(f"Ledger {self.path} has user_version {current} > {SCHEMA_VERSION}"
                                " (written by a newer bot); leaving schema untouched.")
                return
            if current == SCHEMA_VERSION:
                return
            self.conn.commit()
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                for version, step in MIGRATIONS:
                    if version > current:
                        step(self.conn)
                self.conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                self.conn.commit()
            except BaseException:
                self.conn.rollback()
                raise

    def close(self):
        self.conn.close()

    # --- writes ---

    def record_decision(self, *, game_id, action, reason, dry_run, home=None, away=None,
                        tip_off_utc=None, ticker=None, ask_cents=None, shares=None,
                        limit_price=None, balance_cents=None, equity_cents=None,
                        band_min=None, band_max=None, fraction=None):
        with self._lock:
            cur = self.conn.execute(
                "INSERT INTO decisions (ts, game_id, home, away, tip_off_utc, ticker, ask_cents,"
                " action, reason, shares, limit_price, balance_cents, equity_cents,"
                " band_min, band_max, fraction, dry_run) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (_now(), game_id, home, away, tip_off_utc, ticker, ask_cents, action, reason,
                 shares, limit_price, balance_cents, equity_cents, band_min, band_max, fraction,
                 int(dry_run)),
            )
            self.conn.commit()
            return cur.lastrowid

    def record_order(self, *, decision_id, ticker, client_order_id, order_id, count,
                     limit_price, outcome):
        with self._lock:
            cur = self.conn.execute(
                "INSERT INTO orders (decision_id, ts, ticker, client_order_id, order_id, count,"
                " limit_price, outcome) VALUES (?,?,?,?,?,?,?,?)",
                (decision_id, _now(), ticker, client_order_id, order_id, count, limit_price, outcome),
            )
            self.conn.commit()
            return cur.lastrowid

    def has_order(self, client_order_id):
        """True if ANY order attempt with this client_order_id was already recorded."""
        with self._lock:
            return self.conn.execute(
                "SELECT 1 FROM orders WHERE client_order_id = ? LIMIT 1", (client_order_id,)
            ).fetchone() is not None

    def set_filled(self, order_id, filled):
        with self._lock:
            self.conn.execute("UPDATE orders SET filled=? WHERE order_id=?", (int(filled), order_id))
            self.conn.commit()

    def record_settlement(self, order_pk, result, filled, price_cents):
        with self._lock:
            pnl, fee = settlement_pnl_cents(filled, price_cents, result == "yes") if filled else (0, 0)
            self.conn.execute(
                "INSERT OR REPLACE INTO settlements (order_pk, ts, result, filled, fee_cents, pnl_cents,"
                " source, avg_entry_cents) VALUES (?,?,?,?,?,?,?,?)",
                (order_pk, _now(), result, filled, fee, pnl, "computed", price_cents))
            self.conn.commit()
            return pnl

    # --- order lifecycle (write-ahead intent, resolution, adoption) ---
    # Contract: docs/design-g4-reconciliation.md sections 3 and 5. Illegal state
    # transitions raise ValueError; nothing here ever talks to Kalshi.

    def _order_locked(self, order_pk):
        return self.conn.execute("SELECT * FROM orders WHERE id = ?", (order_pk,)).fetchone()

    def _require_order_locked(self, order_pk):
        row = self._order_locked(order_pk)
        if row is None:
            raise ValueError(f"no such order row: {order_pk}")
        return row

    def _is_settled_locked(self, order_pk):
        return self.conn.execute(
            "SELECT 1 FROM settlements WHERE order_pk = ?", (order_pk,)).fetchone() is not None

    def begin_order(self, decision_id, ticker, client_order_id, count, limit_price):
        """Write-ahead intent: insert outcome='submitting' BEFORE the POST.

        Idempotent per client_order_id: if a row exists it is returned (created=False)
        and nothing is inserted. -> (order_pk, created)
        """
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT id FROM orders WHERE client_order_id = ? ORDER BY id LIMIT 1",
                (client_order_id,)).fetchone()
            if row is not None:
                return row["id"], False
            now = _now()
            cur = self.conn.execute(
                "INSERT INTO orders (decision_id, ts, ticker, client_order_id, order_id, count,"
                " limit_price, outcome, updated_ts) VALUES (?,?,?,?,NULL,?,?,'submitting',?)",
                (decision_id, now, ticker, client_order_id, int(count), int(limit_price), now))
            return cur.lastrowid, True

    def mark_placed(self, order_pk, order_id, status=None):
        """Kalshi confirmed the order: submitting|unconfirmed|placed -> placed."""
        with self._lock, self.conn:
            row = self._require_order_locked(order_pk)
            if row["outcome"] not in ("submitting", "unconfirmed", "placed"):
                raise ValueError(f"illegal transition {row['outcome']} -> placed (order {order_pk})")
            if self._is_settled_locked(order_pk):
                raise ValueError(f"order {order_pk} is already settled")
            self.conn.execute(
                "UPDATE orders SET outcome='placed', order_id=?, status=COALESCE(?, status),"
                " resolved_via=COALESCE(resolved_via, 'response'), updated_ts=? WHERE id=?",
                (order_id, status, _now(), order_pk))

    def set_order_outcome(self, order_pk, outcome):
        """Record a non-placed outcome (rejection / unconfirmed / not_found).

        Allowed: submitting -> any of unconfirmed|not_found|insufficient_funds|
        blocked_location|duplicate|failed; unconfirmed -> unconfirmed|not_found|failed;
        X -> X is a no-op. placed/adopted/settled rows and other terminal rows are never
        changed here (ValueError); use apply_order_state to adopt Kalshi's truth.
        """
        if outcome not in SETTABLE_OUTCOMES:
            raise ValueError(f"set_order_outcome cannot set outcome {outcome!r}")
        with self._lock, self.conn:
            row = self._require_order_locked(order_pk)
            cur = row["outcome"]
            if cur != outcome and outcome not in _OUTCOME_TRANSITIONS.get(cur, ()):
                raise ValueError(f"illegal transition {cur} -> {outcome} (order {order_pk})")
            if self._is_settled_locked(order_pk):
                raise ValueError(f"order {order_pk} is already settled")
            self.conn.execute("UPDATE orders SET outcome=?, updated_ts=? WHERE id=?",
                              (outcome, _now(), order_pk))

    def apply_order_state(self, order_pk, order_id, status, filled, avg_fill_cents, resolved_via):
        """Adopt Kalshi's truth for an order that exists on Kalshi.

        Any unsettled row (submitting, unconfirmed, placed, not_found, a rejection) becomes
        outcome='placed'; an 'adopted' row stays 'adopted' (provenance). Refuses a settled
        order. filled / avg_fill_cents / status of None keep the stored value.
        """
        if resolved_via not in RESOLVED_VIA:
            raise ValueError(f"resolved_via must be one of {RESOLVED_VIA}, got {resolved_via!r}")
        with self._lock, self.conn:
            row = self._require_order_locked(order_pk)
            if self._is_settled_locked(order_pk):
                raise ValueError(f"order {order_pk} is already settled; refusing to change it")
            self._apply_state_locked(row, order_id, status, filled, avg_fill_cents, resolved_via)

    def _apply_state_locked(self, row, order_id, status, filled, avg_fill_cents, resolved_via):
        outcome = "adopted" if row["outcome"] == "adopted" else "placed"
        self.conn.execute(
            "UPDATE orders SET outcome=?, order_id=COALESCE(?, order_id), status=COALESCE(?, status),"
            " filled=COALESCE(?, filled), avg_fill_cents=COALESCE(?, avg_fill_cents),"
            " resolved_via=?, updated_ts=? WHERE id=?",
            (outcome, order_id, status, None if filled is None else int(filled),
             None if avg_fill_cents is None else float(avg_fill_cents),
             resolved_via, _now(), row["id"]))

    def adopt_order(self, ticker, order_id, filled, avg_fill_cents, client_order_id=None,
                    limit_price=None):
        """Create an outcome='adopted' row for a Kalshi order/position the ledger never knew.

        Idempotent on order_id (an existing row with that order_id is returned unchanged).
        If client_order_id matches an existing unsettled row, that row is resolved with
        Kalshi's state (resolved_via='adopt') instead of creating a second one. A missing
        client_order_id becomes 'adopted:<order_id>'; with no order_id either (position-only
        adoption) a random one is generated, so the caller must guard against repeats.
        -> order_pk
        """
        with self._lock, self.conn:
            if order_id:
                row = self.conn.execute(
                    "SELECT id FROM orders WHERE order_id = ? ORDER BY id LIMIT 1", (order_id,)).fetchone()
                if row is not None:
                    return row["id"]
            if client_order_id:
                row = self.conn.execute(
                    "SELECT * FROM orders WHERE client_order_id = ? ORDER BY id LIMIT 1",
                    (client_order_id,)).fetchone()
                if row is not None:
                    if not self._is_settled_locked(row["id"]):
                        self._apply_state_locked(row, order_id, None, filled, avg_fill_cents, "adopt")
                    return row["id"]
            filled = max(int(filled or 0), 0)
            if limit_price is None:
                limit_price = int(round(avg_fill_cents)) if avg_fill_cents is not None else 0
            if not client_order_id:
                client_order_id = f"adopted:{order_id or uuid.uuid4().hex}"
            now = _now()
            cur = self.conn.execute(
                "INSERT INTO orders (decision_id, ts, ticker, client_order_id, order_id, count,"
                " limit_price, outcome, filled, avg_fill_cents, resolved_via, updated_ts)"
                " VALUES (NULL,?,?,?,?,?,?,'adopted',?,?,'adopt',?)",
                (now, ticker, client_order_id, order_id, filled, int(limit_price), filled,
                 None if avg_fill_cents is None else float(avg_fill_cents), now))
            return cur.lastrowid

    def unresolved_orders_by_ticker(self):
        """{ticker: [order rows]} for orders whose outcome is not settled truth: submitting,
        unconfirmed, or not_found (provisional) -- and that never got an order_id.

        A Kalshi position or fill on one of these tickers belongs to THAT order; it must not be
        adopted as a new trade or the same trade is booked twice.
        """
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM orders WHERE outcome IN ('submitting','unconfirmed','not_found')"
                " AND order_id IS NULL ORDER BY id").fetchall()
        out = {}
        for r in rows:
            out.setdefault(r["ticker"], []).append(r)
        return out

    def provisional_not_found_orders(self, max_age_hours=48):
        """not_found rows young enough to be worth asking Kalshi about again.

        `not_found` is a belief ("the lookup found nothing"), not a fact: a POST that timed out
        client-side can still be processed later, and listings can lag. Re-check each cycle.
        """
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=max_age_hours)).isoformat(timespec="seconds")
        with self._lock:
            return self.conn.execute(
                "SELECT * FROM orders WHERE outcome = 'not_found' AND order_id IS NULL AND ts >= ?"
                " ORDER BY id", (cutoff,)).fetchall()

    def attach_order_id(self, order_pk, order_id):
        """Give a position-adopted row (order_id NULL) its real Kalshi order id once fills reveal it.

        Only for unsettled `adopted` rows without an id; anything else returns False.
        """
        with self._lock, self.conn:
            row = self._order_locked(order_pk)
            if (row is None or row["outcome"] != "adopted" or row["order_id"]
                    or self._is_settled_locked(order_pk)):
                return False
            self.conn.execute("UPDATE orders SET order_id = ?, updated_ts = ? WHERE id = ?",
                              (order_id, _now(), order_pk))
            return True

    def orders_needing_resolution(self):
        """Rows whose Kalshi state is unknown: outcome in (submitting, unconfirmed)."""
        with self._lock:
            return self.conn.execute(
                "SELECT * FROM orders WHERE outcome IN ('submitting', 'unconfirmed') ORDER BY id"
            ).fetchall()

    def open_orders(self):
        """Orders tracked until settled: placed/adopted with no settlement row."""
        return self.unsettled_orders()

    def get_order_row(self, order_pk):
        with self._lock:
            return self._order_locked(order_pk)

    def order_by_order_id(self, order_id):
        """The ledger row for a Kalshi order_id, or None."""
        with self._lock:
            return self.conn.execute(
                "SELECT * FROM orders WHERE order_id = ? ORDER BY id LIMIT 1", (order_id,)
            ).fetchone()

    def order_tip_off(self, order_pk):
        """Tip-off time (ISO string) of the game an order belongs to, via its decision; or None.

        Recovery needs this to time the post-tip fill check for orders the process lost track of.
        """
        with self._lock:
            row = self.conn.execute(
                "SELECT d.tip_off_utc FROM orders o LEFT JOIN decisions d ON d.id = o.decision_id"
                " WHERE o.id = ?", (order_pk,)).fetchone()
        return row[0] if row else None

    def order_by_client_id(self, client_order_id):
        with self._lock:
            return self.conn.execute(
                "SELECT * FROM orders WHERE client_order_id = ? ORDER BY id LIMIT 1",
                (client_order_id,)).fetchone()

    # --- Kalshi fills (actual price + fee) and settlement from them ---

    def upsert_fills(self, fills):
        """Store Kalshi fills, idempotent on fill_id. -> number of NEW rows inserted.

        An already-stored fill is not duplicated; only fields it was missing (fee_cents,
        order_id, ts, ...) are filled in. Fills without fill_id/count/price_cents cannot
        be stored safely and are skipped with an ERROR log.
        """
        inserted = 0
        with self._lock, self.conn:
            for f in fills or ():
                fid = f.get("fill_id")
                if not fid or f.get("count") is None or f.get("price_cents") is None:
                    logging.error(f"Skipping unusable Kalshi fill record: {f!r}")
                    continue
                exists = self.conn.execute(
                    "SELECT 1 FROM kalshi_fills WHERE fill_id = ?", (str(fid),)).fetchone()
                vals = (str(fid), f.get("order_id"), f.get("ticker"), int(f["count"]),
                        int(round(f["price_cents"])), f.get("side"), f.get("action"),
                        None if f.get("fee_cents") is None else int(f["fee_cents"]), f.get("ts"))
                if exists:
                    self.conn.execute(
                        "UPDATE kalshi_fills SET order_id=COALESCE(order_id, ?),"
                        " ticker=COALESCE(ticker, ?), side=COALESCE(side, ?),"
                        " action=COALESCE(action, ?), fee_cents=COALESCE(fee_cents, ?),"
                        " ts=COALESCE(ts, ?) WHERE fill_id = ?",
                        (vals[1], vals[2], vals[5], vals[6], vals[7], vals[8], vals[0]))
                else:
                    self.conn.execute(
                        "INSERT INTO kalshi_fills (fill_id, order_id, ticker, count, price_cents,"
                        " side, action, fee_cents, ts) VALUES (?,?,?,?,?,?,?,?,?)", vals)
                    inserted += 1
        return inserted

    def fills_for_order(self, order_id):
        if not order_id:
            return []
        with self._lock:
            return self.conn.execute(
                "SELECT * FROM kalshi_fills WHERE order_id = ? ORDER BY ts, fill_id",
                (order_id,)).fetchall()

    def record_settlement_from_fills(self, order_pk, result):
        """Settle a placed/adopted order from its ACTUAL Kalshi fills. -> pnl cents, or None.

        Per fill (price is the YES-leg price): a long-YES fill (yes buy / no sell) pays
        count*(100-price) if YES won else -count*price; a short-YES fill (yes sell / no buy)
        is the mirror image. Fees: the fill's fee_cents, or -- when Kalshi did not report
        one (NULL) -- the formula fee kalshi_fee_cents(count, price) for that fill. With no
        stored fills it falls back to the computed settlement from the order's limit price
        and filled count (source='computed'). Idempotent (replaces the row, keeps its ts).
        Returns None when the order does not exist or is not placed/adopted.
        """
        result = (result or "").lower()
        if result not in ("yes", "no"):
            raise ValueError(f"result must be 'yes' or 'no', got {result!r}")
        yes_won = result == "yes"
        with self._lock, self.conn:
            row = self._order_locked(order_pk)
            if row is None or row["outcome"] not in OPEN_OUTCOMES:
                return None
            fills = self.fills_for_order(row["order_id"])
            if fills:
                gross = fee = total = 0
                cost = 0
                for f in fills:
                    c, p = f["count"], f["price_cents"]
                    if (f["side"] or "yes").lower() not in ("yes", "no"):
                        # e.g. a V2 'bid'/'ask' leg label: guessing would silently flip the PnL sign
                        raise ValueError(f"fill {f['fill_id']} has unsupported side {f['side']!r}")
                    long_yes = ((f["side"] or "yes").lower() == "yes") == ((f["action"] or "buy").lower() == "buy")
                    leg = c * (100 - p) if yes_won else -c * p
                    gross += leg if long_yes else -leg
                    fee += f["fee_cents"] if f["fee_cents"] is not None else kalshi_fee_cents(c, p)
                    total += c
                    cost += c * p
                pnl, filled, avg_entry, source = gross - fee, total, cost / total if total else None, "kalshi_fills"
            else:
                filled = row["filled"] or 0
                pnl, fee = settlement_pnl_cents(filled, row["limit_price"], yes_won) if filled else (0, 0)
                avg_entry, source = row["limit_price"], "computed"
            old = self.conn.execute("SELECT ts FROM settlements WHERE order_pk = ?", (order_pk,)).fetchone()
            self.conn.execute(
                "INSERT OR REPLACE INTO settlements (order_pk, ts, result, filled, fee_cents, pnl_cents,"
                " source, avg_entry_cents) VALUES (?,?,?,?,?,?,?,?)",
                (order_pk, old["ts"] if old else _now(), result, filled, fee, pnl, source, avg_entry))
            return pnl

    def record_snapshot(self, balance_cents, open_cost_cents, equity_cents, peak_cents, drawdown_pct):
        with self._lock:
            self.conn.execute(
                "INSERT INTO snapshots VALUES (?,?,?,?,?,?)",
                (_now(), balance_cents, open_cost_cents, equity_cents, peak_cents, drawdown_pct))
            self.conn.commit()

    def record_event(self, level, kind, message):
        with self._lock:
            self.conn.execute("INSERT INTO events VALUES (?,?,?,?)", (_now(), level, kind, message))
            self.conn.commit()

    # --- reads used by the bot ---

    def unsettled_orders(self):
        """Orders that were actually placed (or adopted from Kalshi) and have no settlement row."""
        with self._lock:
            return self.conn.execute(
                "SELECT o.* FROM orders o LEFT JOIN settlements s ON s.order_pk = o.id"
                " WHERE o.outcome IN ('placed', 'adopted') AND s.order_pk IS NULL"
                " ORDER BY o.id").fetchall()

    # --- paper trades (dry-run decisions that were in the band) ---

    @staticmethod
    def _paper_eligible(alias):
        return (f"{alias}.dry_run = 1 AND {alias}.ticker IS NOT NULL AND {alias}.ask_cents IS NOT NULL AND "
                f"({alias}.reason IN ('in_band_unfunded', 'in_band_capped_to_zero') OR {alias}.action = 'buy')")

    def unsettled_paper_candidates(self):
        """First eligible dry-run decision per game that has no paper trade yet.

        Restarts can evaluate a game twice; one game counts once.
        """
        with self._lock:
            return self.conn.execute(
                "SELECT d.id, d.ticker, COALESCE(d.limit_price, d.ask_cents) AS entry_cents"
                f" FROM decisions d WHERE {self._paper_eligible('d')}"
                " AND d.id = (SELECT MIN(d2.id) FROM decisions d2"
                f"            WHERE d2.game_id = d.game_id AND {self._paper_eligible('d2')})"
                " AND NOT EXISTS (SELECT 1 FROM paper_trades p WHERE p.decision_id = d.id)"
            ).fetchall()

    def record_paper_trade(self, decision_id, ticker, entry_cents, result):
        with self._lock:
            pnl, fee = settlement_pnl_cents(1, entry_cents, result == "yes")
            self.conn.execute(
                "INSERT OR REPLACE INTO paper_trades VALUES (?,?,?,?,?,?,?)",
                (decision_id, _now(), ticker, entry_cents, result, fee, pnl))
            self.conn.commit()
            return pnl

    # --- durable bot state (portfolio lots, high-water mark, daily schedule) ---
    # Every multi-row write below is one transaction: `with self.conn` commits on
    # success and rolls back on any exception, so a crash or error never leaves
    # half-written state.

    def get_kv(self, key, default=None):
        with self._lock:
            row = self.conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
            return default if row is None else row["value"]

    def set_kv(self, key, value):
        with self._lock, self.conn:
            self.conn.execute("INSERT OR REPLACE INTO kv (key, value) VALUES (?,?)", (key, str(value)))

    def load_portfolio_lots(self):
        """-> {ticker: [{"price", "qty", "time"}, ...]} in insertion order."""
        with self._lock:
            rows = self.conn.execute(
                "SELECT ticker, price, qty, time FROM portfolio_lots ORDER BY id").fetchall()
        positions = {}
        for r in rows:
            positions.setdefault(r["ticker"], []).append(
                {"price": r["price"], "qty": r["qty"], "time": r["time"]})
        return positions

    def _replace_portfolio_lots_locked(self, positions):
        self.conn.execute("DELETE FROM portfolio_lots")
        self.conn.executemany(
            "INSERT INTO portfolio_lots (ticker, price, qty, time) VALUES (?,?,?,?)",
            [(t, lot["price"], lot["qty"], lot.get("time", 0))
             for t, lots in positions.items() for lot in lots])

    def replace_portfolio_lots(self, positions):
        """Atomically replace the whole portfolio (all-or-nothing)."""
        with self._lock, self.conn:
            self._replace_portfolio_lots_locked(positions)

    def get_peak_cents(self):
        v = self.get_kv(KV_PEAK)
        return None if v is None else int(v)

    def set_peak_cents(self, peak_cents):
        self.set_kv(KV_PEAK, int(peak_cents))

    def get_schedule(self):
        """-> {'date': str, 'games': {game_id: entry}} or {} if nothing is saved."""
        with self._lock:
            date = self.get_kv(KV_SCHEDULE_DATE)
            if date is None:
                return {}
            rows = self.conn.execute(
                "SELECT game_id, home_team, away_team, tip_off_utc, trigger_time_utc, fired"
                " FROM schedule_games ORDER BY seq").fetchall()
        games = {r["game_id"]: {"game_id": r["game_id"], "home_team": r["home_team"],
                                "away_team": r["away_team"], "tip_off_utc": r["tip_off_utc"],
                                "trigger_time_utc": r["trigger_time_utc"],
                                "fired": bool(r["fired"])} for r in rows}
        return {"date": date, "games": games}

    def _replace_schedule_locked(self, schedule):
        # A 'fired' flag already set in the DB (e.g. by a job thread mid-rebuild)
        # is never un-set by a save.
        was_fired = {r["game_id"] for r in
                     self.conn.execute("SELECT game_id FROM schedule_games WHERE fired=1")}
        self.conn.execute("DELETE FROM schedule_games")
        for game_id, g in schedule.get("games", {}).items():
            self.conn.execute(
                "INSERT INTO schedule_games (game_id, home_team, away_team, tip_off_utc,"
                " trigger_time_utc, fired) VALUES (?,?,?,?,?,?)",
                (game_id, g.get("home_team"), g.get("away_team"), g.get("tip_off_utc"),
                 g.get("trigger_time_utc"), int(bool(g.get("fired")) or game_id in was_fired)))
        self.conn.execute("INSERT OR REPLACE INTO kv (key, value) VALUES (?,?)",
                          (KV_SCHEDULE_DATE, str(schedule["date"])))

    def replace_schedule(self, schedule):
        """Atomically replace the saved schedule ({'date': ..., 'games': {...}})."""
        with self._lock, self.conn:
            self._replace_schedule_locked(schedule)

    def mark_game_fired(self, game_id):
        """Set one game's fired flag. Returns True if the game exists."""
        with self._lock, self.conn:
            cur = self.conn.execute("UPDATE schedule_games SET fired=1 WHERE game_id=?", (game_id,))
            return cur.rowcount > 0

    # --- one-time import of the legacy JSON state files ---
    # Runs only while the target table is empty AND the import has never been
    # attempted before (a kv marker), so closing every position -- an empty table
    # -- can never resurrect a stale JSON file on a later restart. The JSON files
    # are only read, never modified or deleted.

    def _import_legacy(self, name, path, is_empty, apply):
        marker = _KV_IMPORTED + name
        with self._lock:
            if self.get_kv(marker) is not None:
                return False
            if not is_empty():
                self.set_kv(marker, "skipped:not-empty")
                return False
            if not path or not os.path.exists(path):
                self.set_kv(marker, "skipped:no-file")
                return False
            try:
                with open(path, "r") as f:
                    data = json.load(f)
                with self.conn:                       # data + marker commit together
                    apply(data)
                    self.conn.execute("INSERT OR REPLACE INTO kv (key, value) VALUES (?,?)",
                                      (marker, "imported:" + _now()))
            except Exception as e:
                logging.error(f"Legacy {name} import from {path} failed ({e}); will retry next start.")
                return False
        logging.info(f"Imported legacy {name} state from {path}.")
        return True

    def import_legacy_portfolio(self, path):
        def apply(data):
            if not isinstance(data, dict):
                raise ValueError("portfolio JSON must be an object")
            self._replace_portfolio_lots_locked(data)
        return self._import_legacy(
            "portfolio", path,
            lambda: self.conn.execute("SELECT 1 FROM portfolio_lots LIMIT 1").fetchone() is None,
            apply)

    def import_legacy_high_water(self, path):
        def apply(data):
            peak = data.get("peak_cents")
            if peak is not None:
                self.conn.execute("INSERT OR REPLACE INTO kv (key, value) VALUES (?,?)",
                                  (KV_PEAK, str(int(peak))))
        return self._import_legacy(
            "high_water", path, lambda: self.get_kv(KV_PEAK) is None, apply)

    def import_legacy_schedule(self, path):
        def apply(data):
            if not isinstance(data, dict) or "date" not in data:
                raise ValueError("schedule JSON needs a 'date'")
            self._replace_schedule_locked(data)
        return self._import_legacy(
            "schedule", path, lambda: self.get_kv(KV_SCHEDULE_DATE) is None, apply)


# --- single-instance guard ---

class InstanceLockError(RuntimeError):
    """Another process already holds the lock for this database."""


class InstanceLock:
    """Exclusive advisory lock (flock) on `<db path>.lock`.

    Two bots must never run against the same database. The OS drops the lock if
    the process dies, so a crash never leaves a stale lock behind. Not reentrant:
    a second InstanceLock on the same path fails even within one process.
    """

    def __init__(self, db_path):
        self.path = f"{db_path}.lock"
        self._fd = None

    def acquire(self):
        import fcntl
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            holder = ""
            try:
                holder = os.pread(fd, 64, 0).decode(errors="replace").strip()
            except OSError:
                pass
            os.close(fd)
            raise InstanceLockError(
                f"Another bot instance already holds {self.path}"
                + (f" (pid {holder})" if holder else "") + ".")
        os.ftruncate(fd, 0)
        os.write(fd, str(os.getpid()).encode())
        self._fd = fd
        return self

    def release(self):
        if self._fd is None:
            return
        import fcntl
        try:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        finally:
            os.close(self._fd)
            self._fd = None

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *exc):
        self.release()

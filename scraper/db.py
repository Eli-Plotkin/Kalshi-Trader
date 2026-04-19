import json
import sqlite3
import threading
import time as _time

from . import config


_local = threading.local()
_init_lock = threading.Lock()
_schema_initialized = False


def get_connection():
    if not hasattr(_local, "conn") or _local.conn is None:
        conn = sqlite3.connect(config.DB_PATH, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA auto_vacuum=INCREMENTAL")
        conn.row_factory = sqlite3.Row
        _local.conn = conn
    return _local.conn


def close_connection():
    conn = getattr(_local, "conn", None)
    if conn is not None:
        conn.close()
        _local.conn = None


def init_schema():
    global _schema_initialized
    with _init_lock:
        if _schema_initialized:
            return
        conn = get_connection()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS markets (
                ticker           TEXT PRIMARY KEY,
                event_ticker     TEXT,
                yes_sub_title    TEXT,
                status           TEXT,
                result           TEXT,
                open_time        TEXT,
                close_time       TEXT,
                expiration_time  TEXT,
                updated_at       INTEGER
            );

            CREATE TABLE IF NOT EXISTS events (
                event_ticker         TEXT PRIMARY KEY,
                home_team            TEXT,
                away_team            TEXT,
                scheduled_tipoff_utc TEXT,
                game_label           TEXT
            );

            CREATE TABLE IF NOT EXISTS orderbook_snapshots (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                market_ticker  TEXT    NOT NULL,
                captured_at    INTEGER NOT NULL,
                yes_levels     TEXT    NOT NULL,
                no_levels      TEXT    NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_ob_lookup
                ON orderbook_snapshots (market_ticker, captured_at);

            CREATE TABLE IF NOT EXISTS trades (
                trade_id     TEXT PRIMARY KEY,
                market_ticker TEXT    NOT NULL,
                yes_price_cents INTEGER,
                no_price_cents  INTEGER,
                count        REAL,
                taker_side   TEXT,
                created_time TEXT,
                created_ts   INTEGER
            );
            CREATE INDEX IF NOT EXISTS idx_trades_lookup
                ON trades (market_ticker, created_ts);

            CREATE TABLE IF NOT EXISTS settled_games (
                event_ticker         TEXT PRIMARY KEY,
                home_team            TEXT,
                away_team            TEXT,
                scheduled_tipoff_utc TEXT,
                game_label           TEXT,
                winner_ticker        TEXT,
                winner_team          TEXT,
                loser_ticker         TEXT,
                loser_team           TEXT,
                settled_at           INTEGER
            );

            CREATE TABLE IF NOT EXISTS column_descriptions (
                table_name  TEXT NOT NULL,
                column_name TEXT NOT NULL,
                description TEXT NOT NULL,
                PRIMARY KEY (table_name, column_name)
            );
        """)
        _seed_descriptions(conn)
        conn.commit()
        _schema_initialized = True


# ---- Column descriptions ----

_DESCRIPTIONS = {
    "markets": {
        "ticker": "Kalshi market ticker for one side of a game (e.g. KXNBAGAME-26APR18HOULAL-HOU). Each game has two market tickers, one per team.",
        "event_ticker": "Kalshi event ticker grouping both sides of a game (e.g. KXNBAGAME-26APR18HOULAL). Foreign key to events table.",
        "yes_sub_title": "Team name pulled from Kalshi (e.g. 'Houston'). Buying YES on this market is a bet that this team wins.",
        "status": "Kalshi market lifecycle status. Values seen: 'active' (open for trading), 'finalized' (settled, result determined).",
        "result": "Settlement outcome: 'yes' if this team won, 'no' if they lost. NULL while the market is still active.",
        "open_time": "ISO 8601 timestamp when Kalshi opened this market for trading.",
        "close_time": "ISO 8601 timestamp when Kalshi closed this market (trading stops). Typically near game tip-off.",
        "expiration_time": "ISO 8601 timestamp when the market expires and settles.",
        "updated_at": "Unix timestamp (seconds) of the last time the scraper wrote or updated this row.",
    },
    "events": {
        "event_ticker": "Kalshi event ticker for the game (e.g. KXNBAGAME-26APR18HOULAL). Groups the two team-specific market tickers.",
        "home_team": "NBA three-letter tricode for the home team (e.g. 'HOU'), sourced from the NBA schedule.",
        "away_team": "NBA three-letter tricode for the away team (e.g. 'LAL'), sourced from the NBA schedule.",
        "scheduled_tipoff_utc": "Official scheduled tip-off time from the NBA CDN schedule, in ISO 8601 UTC. Not adjusted for broadcast delays.",
        "game_label": "NBA schedule label (e.g. 'Regular Season', 'Play-In', 'Playoffs'). NULL if the schedule did not provide one.",
    },
    "orderbook_snapshots": {
        "id": "Auto-incrementing row ID. No business meaning; used for ordering within a capture batch.",
        "market_ticker": "Kalshi market ticker this snapshot belongs to. Foreign key to markets.ticker.",
        "captured_at": "Unix timestamp (seconds) when the scraper fetched this orderbook snapshot. All snapshots in the same poll cycle share the same value.",
        "yes_levels": "JSON array of the top price levels on the YES side. Each element is [price_dollars, count_fp] — e.g. ['0.6100', '273.00'] means 273 contracts resting at 61 cents. Sorted best (highest) price first. Depth is configured to 5 levels.",
        "no_levels": "JSON array of the top price levels on the NO side. Each element is [price_dollars, count_fp] — e.g. ['0.3000', '767.00'] means 767 contracts resting at 30 cents. Sorted best (highest) price first. Depth is configured to 5 levels.",
    },
    "trades": {
        "trade_id": "Unique trade identifier assigned by Kalshi (UUID). Used to deduplicate across polls.",
        "market_ticker": "Kalshi market ticker where this trade executed. Foreign key to markets.ticker.",
        "yes_price_cents": "Price of the YES side in integer cents (0-100). Converted from Kalshi's dollar-string format (e.g. '0.6100' -> 61).",
        "no_price_cents": "Price of the NO side in integer cents (0-100). Always equals 100 minus yes_price_cents for binary markets.",
        "count": "Number of contracts in this trade (float, from Kalshi's fixed-point string). e.g. 20.00 means 20 contracts.",
        "taker_side": "'yes' or 'no' — which side the aggressive (taker) order was on. If 'yes', someone lifted the ask to buy YES; if 'no', someone hit the bid to buy NO.",
        "created_time": "ISO 8601 timestamp of when the trade executed, as returned by Kalshi.",
        "created_ts": "Unix timestamp (seconds) of trade execution. Derived from created_time or the Kalshi 'ts' field. Used for range queries and cursor tracking.",
    },
    "settled_games": {
        "event_ticker": "Kalshi event ticker for the settled game. Foreign key to events table.",
        "home_team": "NBA tricode for the home team, copied from the events table at settlement time.",
        "away_team": "NBA tricode for the away team, copied from the events table at settlement time.",
        "scheduled_tipoff_utc": "Scheduled tip-off from the NBA schedule, copied from the events table.",
        "game_label": "NBA schedule label (e.g. 'Regular Season'), copied from the events table.",
        "winner_ticker": "Market ticker for the team that won (result='yes').",
        "winner_team": "Team name (from yes_sub_title) for the winning side.",
        "loser_ticker": "Market ticker for the team that lost (result='no').",
        "loser_team": "Team name (from yes_sub_title) for the losing side.",
        "settled_at": "Unix timestamp (seconds) when the scraper detected and recorded this settlement. Not the actual Kalshi settlement time.",
    },
    "column_descriptions": {
        "table_name": "Name of the table this description belongs to.",
        "column_name": "Name of the column being described.",
        "description": "Human-readable explanation of what this column stores, its units, source, and any assumptions.",
    },
}


def _seed_descriptions(conn):
    for table_name, columns in _DESCRIPTIONS.items():
        for col_name, desc in columns.items():
            conn.execute("""
                INSERT OR REPLACE INTO column_descriptions (table_name, column_name, description)
                VALUES (?, ?, ?)
            """, (table_name, col_name, desc))


# ---- Markets ----

def upsert_markets_batch(markets):
    conn = get_connection()
    conn.executemany("""
        INSERT INTO markets (ticker, event_ticker, yes_sub_title, status, result,
                             open_time, close_time, expiration_time, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(ticker) DO UPDATE SET
            status         = excluded.status,
            result         = excluded.result,
            close_time     = excluded.close_time,
            updated_at     = excluded.updated_at
    """, [
        (
            m.get("ticker"), m.get("event_ticker"), m.get("yes_sub_title"),
            m.get("status"), m.get("result"), m.get("open_time"),
            m.get("close_time"), m.get("expiration_time"), int(_time.time()),
        )
        for m in markets
    ])
    conn.commit()


def get_markets_for_event(event_ticker):
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM markets WHERE event_ticker = ?", (event_ticker,)
    ).fetchall()
    return [dict(r) for r in rows]


def get_unsettled_event_tickers():
    conn = get_connection()
    rows = conn.execute("""
        SELECT DISTINCT m.event_ticker
        FROM markets m
        WHERE m.status IN ('settled', 'finalized')
          AND m.event_ticker NOT IN (SELECT event_ticker FROM settled_games)
    """).fetchall()
    return [r["event_ticker"] for r in rows]


# ---- Events ----

def upsert_event(event_ticker, home_team, away_team, tipoff_utc, game_label):
    conn = get_connection()
    conn.execute("""
        INSERT INTO events (event_ticker, home_team, away_team, scheduled_tipoff_utc, game_label)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(event_ticker) DO NOTHING
    """, (event_ticker, home_team, away_team, tipoff_utc, game_label))
    conn.commit()


def get_event(event_ticker):
    conn = get_connection()
    row = conn.execute(
        "SELECT * FROM events WHERE event_ticker = ?", (event_ticker,)
    ).fetchone()
    return dict(row) if row else None


def get_known_event_tickers():
    conn = get_connection()
    rows = conn.execute("SELECT event_ticker FROM events").fetchall()
    return {r["event_ticker"] for r in rows}


# ---- Orderbook snapshots ----

def insert_orderbook_snapshots_batch(snapshots):
    """Insert a list of (market_ticker, captured_at, yes_levels, no_levels) tuples."""
    conn = get_connection()
    conn.executemany("""
        INSERT INTO orderbook_snapshots (market_ticker, captured_at, yes_levels, no_levels)
        VALUES (?, ?, ?, ?)
    """, [
        (s[0], s[1], json.dumps(s[2]), json.dumps(s[3]))
        for s in snapshots
    ])
    conn.commit()


def get_orderbook_at(market_ticker, at_ts):
    conn = get_connection()
    row = conn.execute("""
        SELECT * FROM orderbook_snapshots
        WHERE market_ticker = ? AND captured_at <= ?
        ORDER BY captured_at DESC LIMIT 1
    """, (market_ticker, at_ts)).fetchone()
    if not row:
        return None
    result = dict(row)
    result["yes_levels"] = json.loads(result["yes_levels"])
    result["no_levels"] = json.loads(result["no_levels"])
    return result


def get_orderbook_snapshots_range(market_ticker, start_ts, end_ts):
    conn = get_connection()
    rows = conn.execute("""
        SELECT * FROM orderbook_snapshots
        WHERE market_ticker = ? AND captured_at BETWEEN ? AND ?
        ORDER BY captured_at
    """, (market_ticker, start_ts, end_ts)).fetchall()
    results = []
    for row in rows:
        r = dict(row)
        r["yes_levels"] = json.loads(r["yes_levels"])
        r["no_levels"] = json.loads(r["no_levels"])
        results.append(r)
    return results


# ---- Trades ----

def insert_trades_batch(raw_trades):
    """Insert a list of trade dicts from the Kalshi API. Deduplicates by trade_id."""
    if not raw_trades:
        return
    conn = get_connection()
    rows = []
    for t in raw_trades:
        yes_cents = _dollars_to_cents(t.get("yes_price_dollars"))
        no_cents = _dollars_to_cents(t.get("no_price_dollars"))
        count = _parse_fp(t.get("count_fp"))
        created_ts = t.get("ts") or iso_to_ts(t.get("created_time"))
        rows.append((
            t.get("trade_id"),
            t.get("ticker") or t.get("market_ticker"),
            yes_cents, no_cents, count,
            t.get("taker_side"),
            t.get("created_time"),
            created_ts,
        ))
    conn.executemany("""
        INSERT OR IGNORE INTO trades
            (trade_id, market_ticker, yes_price_cents, no_price_cents,
             count, taker_side, created_time, created_ts)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, rows)
    conn.commit()


def get_max_trade_ts(market_ticker):
    conn = get_connection()
    row = conn.execute(
        "SELECT MAX(created_ts) AS max_ts FROM trades WHERE market_ticker = ?",
        (market_ticker,),
    ).fetchone()
    return row["max_ts"] if row and row["max_ts"] else 0


def get_trades_range(market_ticker, start_ts, end_ts):
    conn = get_connection()
    rows = conn.execute("""
        SELECT * FROM trades
        WHERE market_ticker = ? AND created_ts BETWEEN ? AND ?
        ORDER BY created_ts
    """, (market_ticker, start_ts, end_ts)).fetchall()
    return [dict(r) for r in rows]


# ---- Settled games ----

def insert_settled_game(game):
    conn = get_connection()
    conn.execute("""
        INSERT OR IGNORE INTO settled_games
            (event_ticker, home_team, away_team, scheduled_tipoff_utc, game_label,
             winner_ticker, winner_team, loser_ticker, loser_team, settled_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        game["event_ticker"],
        game.get("home_team"),
        game.get("away_team"),
        game.get("scheduled_tipoff_utc"),
        game.get("game_label"),
        game.get("winner_ticker"),
        game.get("winner_team"),
        game.get("loser_ticker"),
        game.get("loser_team"),
        game.get("settled_at"),
    ))
    conn.commit()


# ---- Maintenance ----

def run_incremental_vacuum():
    conn = get_connection()
    conn.execute("PRAGMA incremental_vacuum(1000)")


# ---- Helpers ----

def _dollars_to_cents(val):
    if val is None:
        return None
    try:
        return int(round(float(val) * 100))
    except (TypeError, ValueError):
        return None


def _parse_fp(val):
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def iso_to_ts(iso_str):
    if not iso_str:
        return None
    from datetime import datetime
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        return int(dt.timestamp())
    except (ValueError, TypeError):
        return None

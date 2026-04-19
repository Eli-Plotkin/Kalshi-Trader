#!/usr/bin/env python3
"""
Export scraped data into analysis-ready formats.

Usage:
    # Daily flat CSV (one row per game, drop-in replacement for research dataset)
    python -m scraper.export daily --date 2026-04-18

    # Daily flat CSV for all available dates
    python -m scraper.export daily

    # Raw orderbook snapshots (CSV or Parquet)
    python -m scraper.export orderbooks --date 2026-04-18 --format parquet

    # Raw trade tape (CSV or Parquet)
    python -m scraper.export trades --date 2026-04-18 --format parquet
"""

import argparse
import csv
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone, timedelta

# Output directory
_EXPORT_DIR = os.path.join(os.path.dirname(__file__), "exports")

# Pre-tip window: same as build_research_dataset.py
AVERAGE_TIP_OFF_DELAY_MINUTES = 12
PRE_TIP_LOOKBACK_MINUTES = 180
PRE_TIP_CUTOFF_MINUTES = 15

from . import config


def _get_conn():
    conn = sqlite3.connect(config.DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_export_dir():
    os.makedirs(_EXPORT_DIR, exist_ok=True)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_levels(levels_json):
    """Parse JSON level array into list of (price_cents, size) tuples."""
    if isinstance(levels_json, str):
        levels_json = json.loads(levels_json)
    result = []
    for level in levels_json:
        price_dollars = float(level[0])
        size = float(level[1])
        price_cents = int(round(price_dollars * 100))
        result.append((price_cents, size))
    return result


def _best_yes_ask_from_snapshot(snapshot):
    """
    The YES ask is NOT directly in the book. Kalshi's orderbook returns:
      - yes_dollars: resting YES limit orders (these are YES bids)
      - no_dollars:  resting NO limit orders (these are effectively YES asks)

    To get the YES ask price: take the best (highest) NO bid price, then
    YES ask = 100 - NO bid price.

    To get the YES ask size: it's the size at that best NO bid level.
    """
    no_levels = _parse_levels(snapshot["no_levels"])
    if not no_levels:
        return None, None
    # no_levels sorted best (highest) first
    best_no_bid_cents, size = no_levels[0]
    yes_ask_cents = 100 - best_no_bid_cents
    return yes_ask_cents, size


def _best_yes_bid_from_snapshot(snapshot):
    """
    The YES bid is the best (highest) price in yes_dollars.
    """
    yes_levels = _parse_levels(snapshot["yes_levels"])
    if not yes_levels:
        return None, None
    best_yes_bid_cents, size = yes_levels[0]
    return best_yes_bid_cents, size


def _walk_book_slippage(levels, order_size):
    """
    Walk price levels to fill `order_size` contracts.
    Returns average fill price in cents, or None if book can't fill.
    `levels` should be sorted best price first (for buying, that means lowest ask first).
    """
    if not levels:
        return None
    filled = 0.0
    cost = 0.0
    for price_cents, size in levels:
        take = min(size, order_size - filled)
        cost += take * price_cents
        filled += take
        if filled >= order_size:
            break
    if filled == 0:
        return None
    return cost / filled


def _slippage_cost_100lot(snapshot):
    """
    Compute slippage for a 100-lot market buy of YES contracts.
    YES ask levels come from the NO side of the book (100 - no_bid).
    Returns (avg_fill_cents, slippage_cents) or (None, None).
    """
    no_levels = _parse_levels(snapshot["no_levels"])
    if not no_levels:
        return None, None

    # Convert NO bids to YES ask levels, sorted lowest ask first (best for buyer)
    yes_ask_levels = [(100 - price, size) for price, size in no_levels]
    yes_ask_levels.sort(key=lambda x: x[0])

    best_ask = yes_ask_levels[0][0] if yes_ask_levels else None
    avg_fill = _walk_book_slippage(yes_ask_levels, 100)
    if best_ask is None or avg_fill is None:
        return None, None
    return avg_fill, avg_fill - best_ask


# ---------------------------------------------------------------------------
# Daily flat CSV export
# ---------------------------------------------------------------------------

DAILY_FIELDS = [
    "event_ticker",
    "date",
    "tip_time_utc",
    "home_team",
    "away_team",
    "fav_side",
    "fav_entry_ask",
    "fav_entry_bid",
    "fav_entry_ask_size",
    "fav_entry_bid_size",
    "fav_min_ask_pretip",
    "fav_max_ask_pretip",
    "fav_min_bid_pretip",
    "fav_max_bid_pretip",
    "avg_spread_pretip",
    "total_volume_pretip",
    "avg_depth_at_inside_ask",
    "avg_depth_top3_levels",
    "slippage_cost_100lot",
    "favorite_won",
    "fav_hold_to_settle_pnl_cents",
]

DAILY_DESCRIPTIONS = {
    "event_ticker": "Kalshi event ticker for the NBA game (e.g. KXNBAGAME-26APR18HOULAL).",
    "date": "Game date in YYYY-MM-DD, derived from the scheduled tip-off UTC.",
    "tip_time_utc": "Scheduled tip-off time from the NBA schedule in ISO 8601 UTC.",
    "home_team": "NBA three-letter tricode for the home team.",
    "away_team": "NBA three-letter tricode for the away team.",
    "fav_side": "Which Kalshi market ticker is the favorite, determined by the highest YES ask at the first pre-tip snapshot.",
    "fav_entry_ask": "Favorite's YES ask price in cents at the first pre-tip orderbook snapshot. Derived as 100 minus the best NO bid.",
    "fav_entry_bid": "Favorite's YES bid price in cents at the first pre-tip snapshot. The best resting YES bid.",
    "fav_entry_ask_size": "Number of contracts available at the favorite's inside YES ask at entry.",
    "fav_entry_bid_size": "Number of contracts available at the favorite's inside YES bid at entry.",
    "fav_min_ask_pretip": "Lowest YES ask seen for the favorite across all pre-tip snapshots, in cents.",
    "fav_max_ask_pretip": "Highest YES ask seen for the favorite across all pre-tip snapshots, in cents.",
    "fav_min_bid_pretip": "Lowest YES bid seen for the favorite across all pre-tip snapshots, in cents.",
    "fav_max_bid_pretip": "Highest YES bid seen for the favorite across all pre-tip snapshots, in cents.",
    "avg_spread_pretip": "Average bid-ask spread across all pre-tip snapshots for the favorite, in cents. Spread = YES ask - YES bid.",
    "total_volume_pretip": "Total contract volume traded on the favorite's market during the pre-tip window.",
    "avg_depth_at_inside_ask": "Average number of contracts resting at the favorite's inside YES ask across pre-tip snapshots.",
    "avg_depth_top3_levels": "Average total contracts across the top 3 YES ask levels for the favorite, across pre-tip snapshots.",
    "slippage_cost_100lot": "Average additional cost (in cents above inside ask) to fill a 100-contract market buy of YES on the favorite, averaged across pre-tip snapshots. Computed by walking the NO-side book.",
    "favorite_won": "True if the favorite's market settled as 'yes' (that team won the game).",
    "fav_hold_to_settle_pnl_cents": "Per-contract PnL in cents if you bought the favorite at fav_entry_ask and held to settlement. +100-ask if won, -ask if lost.",
}


def _compute_daily_row(event_ticker, conn):
    """Compute one row of the daily flat CSV for a single event."""
    # Get event metadata
    event = conn.execute(
        "SELECT * FROM events WHERE event_ticker = ?", (event_ticker,)
    ).fetchone()
    if not event:
        return None

    # Get the two markets for this event
    markets = conn.execute(
        "SELECT * FROM markets WHERE event_ticker = ?", (event_ticker,)
    ).fetchall()
    if len(markets) != 2:
        return None

    # Compute pre-tip window
    tipoff_str = event["scheduled_tipoff_utc"]
    if not tipoff_str:
        return None
    tipoff = datetime.fromisoformat(tipoff_str.replace("Z", "+00:00"))
    adjusted_tipoff = tipoff + timedelta(minutes=AVERAGE_TIP_OFF_DELAY_MINUTES)
    window_start = adjusted_tipoff - timedelta(minutes=PRE_TIP_LOOKBACK_MINUTES)
    window_end = adjusted_tipoff - timedelta(minutes=PRE_TIP_CUTOFF_MINUTES)
    start_ts = int(window_start.timestamp())
    end_ts = int(window_end.timestamp())

    m_a, m_b = dict(markets[0]), dict(markets[1])

    # Get first pre-tip snapshot for each market to determine favorite
    snap_a_first = conn.execute("""
        SELECT * FROM orderbook_snapshots
        WHERE market_ticker = ? AND captured_at >= ? AND captured_at <= ?
        ORDER BY captured_at ASC LIMIT 1
    """, (m_a["ticker"], start_ts, end_ts)).fetchone()

    snap_b_first = conn.execute("""
        SELECT * FROM orderbook_snapshots
        WHERE market_ticker = ? AND captured_at >= ? AND captured_at <= ?
        ORDER BY captured_at ASC LIMIT 1
    """, (m_b["ticker"], start_ts, end_ts)).fetchone()

    if not snap_a_first or not snap_b_first:
        return None

    ask_a, ask_a_size = _best_yes_ask_from_snapshot(snap_a_first)
    ask_b, ask_b_size = _best_yes_ask_from_snapshot(snap_b_first)

    if ask_a is None or ask_b is None:
        return None

    # Higher YES ask = higher implied probability = favorite
    if ask_a >= ask_b:
        fav_market = m_a
        fav_first_snap = snap_a_first
    else:
        fav_market = m_b
        fav_first_snap = snap_b_first

    fav_ticker = fav_market["ticker"]

    # Entry prices from first snapshot
    fav_entry_ask, fav_entry_ask_size = _best_yes_ask_from_snapshot(fav_first_snap)
    fav_entry_bid, fav_entry_bid_size = _best_yes_bid_from_snapshot(fav_first_snap)

    # Get all pre-tip snapshots for the favorite
    fav_snaps = conn.execute("""
        SELECT * FROM orderbook_snapshots
        WHERE market_ticker = ? AND captured_at BETWEEN ? AND ?
        ORDER BY captured_at
    """, (fav_ticker, start_ts, end_ts)).fetchall()

    # Pre-tip time series stats
    asks = []
    bids = []
    spreads = []
    inside_ask_depths = []
    top3_depths = []
    slippages = []

    for snap in fav_snaps:
        snap_ask, snap_ask_size = _best_yes_ask_from_snapshot(snap)
        snap_bid, snap_bid_size = _best_yes_bid_from_snapshot(snap)

        if snap_ask is not None:
            asks.append(snap_ask)
            if snap_ask_size is not None:
                inside_ask_depths.append(snap_ask_size)

        if snap_bid is not None:
            bids.append(snap_bid)

        if snap_ask is not None and snap_bid is not None:
            spreads.append(snap_ask - snap_bid)

        # Top 3 YES ask levels depth (from NO side)
        no_levels = _parse_levels(snap["no_levels"])
        if no_levels:
            top3_size = sum(size for _, size in no_levels[:3])
            top3_depths.append(top3_size)

        # 100-lot slippage
        _, slip = _slippage_cost_100lot(snap)
        if slip is not None:
            slippages.append(slip)

    # Pre-tip trade volume for the favorite
    volume_row = conn.execute("""
        SELECT COALESCE(SUM(count), 0) AS vol FROM trades
        WHERE market_ticker = ? AND created_ts BETWEEN ? AND ?
    """, (fav_ticker, start_ts, end_ts)).fetchone()
    total_volume = volume_row["vol"] if volume_row else 0

    # Settlement
    fav_won = fav_market.get("result") == "yes"
    if fav_entry_ask is not None:
        pnl = (100 - fav_entry_ask) if fav_won else (-fav_entry_ask)
    else:
        pnl = None

    return {
        "event_ticker": event_ticker,
        "date": tipoff.date().isoformat(),
        "tip_time_utc": tipoff.isoformat(),
        "home_team": event["home_team"],
        "away_team": event["away_team"],
        "fav_side": fav_ticker,
        "fav_entry_ask": fav_entry_ask,
        "fav_entry_bid": fav_entry_bid,
        "fav_entry_ask_size": round(fav_entry_ask_size, 2) if fav_entry_ask_size else None,
        "fav_entry_bid_size": round(fav_entry_bid_size, 2) if fav_entry_bid_size else None,
        "fav_min_ask_pretip": min(asks) if asks else None,
        "fav_max_ask_pretip": max(asks) if asks else None,
        "fav_min_bid_pretip": min(bids) if bids else None,
        "fav_max_bid_pretip": max(bids) if bids else None,
        "avg_spread_pretip": round(sum(spreads) / len(spreads), 2) if spreads else None,
        "total_volume_pretip": round(total_volume, 2),
        "avg_depth_at_inside_ask": round(sum(inside_ask_depths) / len(inside_ask_depths), 2) if inside_ask_depths else None,
        "avg_depth_top3_levels": round(sum(top3_depths) / len(top3_depths), 2) if top3_depths else None,
        "slippage_cost_100lot": round(sum(slippages) / len(slippages), 4) if slippages else None,
        "favorite_won": fav_won,
        "fav_hold_to_settle_pnl_cents": pnl,
    }


def export_daily(target_date=None):
    """Export the daily flat CSV. If target_date is None, export all available dates."""
    _ensure_export_dir()
    conn = _get_conn()

    if target_date:
        # Find events for this date
        events = conn.execute("""
            SELECT e.event_ticker FROM events e
            WHERE e.scheduled_tipoff_utc LIKE ?
            ORDER BY e.scheduled_tipoff_utc
        """, (f"{target_date}%",)).fetchall()
    else:
        events = conn.execute("""
            SELECT event_ticker FROM events ORDER BY scheduled_tipoff_utc
        """).fetchall()

    rows = []
    for event_row in events:
        row = _compute_daily_row(event_row["event_ticker"], conn)
        if row:
            rows.append(row)

    if not rows:
        print(f"No data to export{f' for {target_date}' if target_date else ''}.")
        conn.close()
        return

    date_suffix = target_date if target_date else "all"
    filename = f"daily_signals_{date_suffix}.csv"
    filepath = os.path.join(_EXPORT_DIR, filename)

    with open(filepath, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=DAILY_FIELDS)
        writer.writeheader()
        # Description row
        writer.writerow({k: f"Description: {v}" for k, v in DAILY_DESCRIPTIONS.items()})
        for row in rows:
            writer.writerow({k: "" if row.get(k) is None else row[k] for k in DAILY_FIELDS})

    print(f"Exported {len(rows)} games to {filepath}")
    conn.close()


# ---------------------------------------------------------------------------
# Raw orderbook export
# ---------------------------------------------------------------------------

OB_FIELDS = ["market_ticker", "timestamp", "side", "level", "price_cents", "size"]
OB_DESCRIPTIONS = {
    "market_ticker": "Kalshi market ticker this snapshot belongs to.",
    "timestamp": "Unix timestamp (seconds) when this orderbook snapshot was captured.",
    "side": "'yes' or 'no' — which side of the book this level is on. YES levels are resting buy orders for YES; NO levels are resting buy orders for NO (equivalently, YES sell orders).",
    "level": "Depth level (1 = best/inside, up to 5). Level 1 on the YES side is the highest YES bid; level 1 on the NO side is the highest NO bid (= lowest YES ask).",
    "price_cents": "Price at this level in integer cents (0-100).",
    "size": "Number of contracts resting at this price level.",
}


def export_orderbooks(target_date=None, fmt="csv"):
    _ensure_export_dir()
    conn = _get_conn()

    if target_date:
        day_start = datetime.strptime(target_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        day_end = day_start + timedelta(days=1)
        start_ts = int(day_start.timestamp())
        end_ts = int(day_end.timestamp())
        snaps = conn.execute("""
            SELECT * FROM orderbook_snapshots
            WHERE captured_at BETWEEN ? AND ?
            ORDER BY captured_at, market_ticker
        """, (start_ts, end_ts)).fetchall()
    else:
        snaps = conn.execute("""
            SELECT * FROM orderbook_snapshots ORDER BY captured_at, market_ticker
        """).fetchall()

    rows = []
    for snap in snaps:
        ticker = snap["market_ticker"]
        ts = snap["captured_at"]
        for side_name, levels_json in [("yes", snap["yes_levels"]), ("no", snap["no_levels"])]:
            levels = _parse_levels(levels_json)
            for i, (price, size) in enumerate(levels, start=1):
                rows.append({
                    "market_ticker": ticker,
                    "timestamp": ts,
                    "side": side_name,
                    "level": i,
                    "price_cents": price,
                    "size": round(size, 2),
                })

    date_suffix = target_date if target_date else "all"

    if fmt == "parquet":
        _write_parquet(rows, OB_FIELDS, f"orderbook_snapshots_{date_suffix}.parquet")
    else:
        _write_csv(rows, OB_FIELDS, OB_DESCRIPTIONS, f"orderbook_snapshots_{date_suffix}.csv")

    print(f"Exported {len(rows)} orderbook rows ({fmt})")
    conn.close()


# ---------------------------------------------------------------------------
# Raw trades export
# ---------------------------------------------------------------------------

TRADE_FIELDS = ["market_ticker", "timestamp", "price_cents", "size", "taker_side", "trade_id"]
TRADE_DESCRIPTIONS = {
    "market_ticker": "Kalshi market ticker where this trade executed.",
    "timestamp": "Unix timestamp (seconds) of trade execution.",
    "price_cents": "YES price of this trade in integer cents (0-100).",
    "size": "Number of contracts traded.",
    "taker_side": "'yes' or 'no' — which side the aggressive (taker) order was on.",
    "trade_id": "Unique trade identifier assigned by Kalshi (UUID).",
}


def export_trades(target_date=None, fmt="csv"):
    _ensure_export_dir()
    conn = _get_conn()

    if target_date:
        day_start = datetime.strptime(target_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        day_end = day_start + timedelta(days=1)
        start_ts = int(day_start.timestamp())
        end_ts = int(day_end.timestamp())
        trade_rows = conn.execute("""
            SELECT * FROM trades
            WHERE created_ts BETWEEN ? AND ?
            ORDER BY created_ts
        """, (start_ts, end_ts)).fetchall()
    else:
        trade_rows = conn.execute("""
            SELECT * FROM trades ORDER BY created_ts
        """).fetchall()

    rows = []
    for t in trade_rows:
        rows.append({
            "market_ticker": t["market_ticker"],
            "timestamp": t["created_ts"],
            "price_cents": t["yes_price_cents"],
            "size": round(t["count"], 2) if t["count"] else None,
            "taker_side": t["taker_side"],
            "trade_id": t["trade_id"],
        })

    date_suffix = target_date if target_date else "all"

    if fmt == "parquet":
        _write_parquet(rows, TRADE_FIELDS, f"trades_{date_suffix}.parquet")
    else:
        _write_csv(rows, TRADE_FIELDS, TRADE_DESCRIPTIONS, f"trades_{date_suffix}.csv")

    print(f"Exported {len(rows)} trade rows ({fmt})")
    conn.close()


# ---------------------------------------------------------------------------
# Output writers
# ---------------------------------------------------------------------------

def _write_csv(rows, fields, descriptions, filename):
    filepath = os.path.join(_EXPORT_DIR, filename)
    with open(filepath, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerow({k: f"Description: {descriptions[k]}" for k in fields})
        for row in rows:
            writer.writerow({k: "" if row.get(k) is None else row[k] for k in fields})
    print(f"  -> {filepath}")


def _write_parquet(rows, fields, filename):
    try:
        import pandas as pd
    except ImportError:
        print("pandas is required for parquet export. Install with: pip install pandas pyarrow")
        sys.exit(1)

    filepath = os.path.join(_EXPORT_DIR, filename)
    df = pd.DataFrame(rows, columns=fields)
    df.to_parquet(filepath, index=False)
    print(f"  -> {filepath}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Export scraped KXNBAGAME data")
    sub = parser.add_subparsers(dest="command", required=True)

    daily_p = sub.add_parser("daily", help="Export daily flat CSV (one row per game)")
    daily_p.add_argument("--date", help="YYYY-MM-DD (omit for all dates)")

    ob_p = sub.add_parser("orderbooks", help="Export raw orderbook snapshots")
    ob_p.add_argument("--date", help="YYYY-MM-DD (omit for all)")
    ob_p.add_argument("--format", choices=["csv", "parquet"], default="csv")

    tr_p = sub.add_parser("trades", help="Export raw trade tape")
    tr_p.add_argument("--date", help="YYYY-MM-DD (omit for all)")
    tr_p.add_argument("--format", choices=["csv", "parquet"], default="csv")

    args = parser.parse_args()

    if args.command == "daily":
        export_daily(args.date)
    elif args.command == "orderbooks":
        export_orderbooks(args.date, args.format)
    elif args.command == "trades":
        export_trades(args.date, args.format)


if __name__ == "__main__":
    main()

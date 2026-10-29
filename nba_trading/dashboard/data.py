"""Read-only data layer for the dashboard: ledger -> DataFrames and metrics.

Pure functions, no Streamlit, so everything here is unit-tested. The ledger is
opened read-only; the dashboard can never write to it or place an order.
"""
from __future__ import annotations

import math
import sqlite3
from datetime import datetime, timezone

import pandas as pd

TABLES = ("decisions", "orders", "settlements", "snapshots", "events", "paper_trades")


def connect_ro(path):
    """Open the ledger read-only (SQLite URI mode=ro)."""
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


PAPER_COLUMNS = ["decision_id", "ts", "ticker", "entry_cents", "result", "fee_cents", "pnl_cents"]


def _read_table(conn, table):
    """Read a ledger table; a ledger written by an older bot may lack paper_trades."""
    try:
        return pd.read_sql_query(f"SELECT * FROM {table}", conn)
    except Exception:
        if table == "paper_trades":
            return pd.DataFrame(columns=PAPER_COLUMNS)
        raise


def load_frames(conn):
    frames = {t: _read_table(conn, t) for t in TABLES}
    for t in ("decisions", "orders", "settlements", "snapshots", "events", "paper_trades"):
        if "ts" in frames[t]:
            frames[t]["ts"] = pd.to_datetime(frames[t]["ts"], utc=True, errors="coerce")
    frames["paper"] = paper_as_trades(frames["paper_trades"])
    frames["trades"] = build_trades(frames["orders"], frames["settlements"], frames["decisions"])
    return frames


def paper_as_trades(paper):
    """Shape paper trades like settled real trades so the same edge math applies."""
    cols = ["ts", "ticker", "limit_price", "result", "pnl_cents", "fee_cents", "status"]
    if paper.empty:
        return pd.DataFrame(columns=cols)
    p = paper.rename(columns={"entry_cents": "limit_price"}).copy()
    p["status"] = "settled"
    return p[cols]


def build_trades(orders, settlements, decisions):
    """One row per order: what was placed, what filled, how it settled.

    `entry_cents` is the price actually paid (Kalshi's average fill, once settled from fills),
    falling back to the limit price. `placed` and `adopted` orders are both live trades.
    """
    cols = ["order_pk", "ts", "ticker", "game_id", "count", "limit_price", "entry_cents", "outcome",
            "filled", "result", "pnl_cents", "fee_cents", "source", "status", "cost_cents"]
    if orders.empty:
        return pd.DataFrame(columns=cols)
    for c in ("avg_entry_cents", "source", "filled"):
        if c not in settlements:
            settlements = settlements.assign(**{c: None})
    st = settlements[["order_pk", "result", "fee_cents", "pnl_cents", "avg_entry_cents", "source", "filled"]]
    st = st.rename(columns={"filled": "settled_filled"})
    t = orders.rename(columns={"id": "order_pk"}).merge(st, on="order_pk", how="left")
    t = t.merge(decisions[["id", "game_id"]].rename(columns={"id": "decision_id"}),
                on="decision_id", how="left")
    t["entry_cents"] = t["avg_entry_cents"].where(t["avg_entry_cents"].notna(), t["limit_price"])
    t["filled"] = t["settled_filled"].where(t["settled_filled"].notna(), t["filled"])

    def status(r):
        if r["outcome"] not in ("placed", "adopted"):
            return r["outcome"]
        if pd.notna(r["pnl_cents"]):
            return "settled" if (r["filled"] or 0) > 0 else "unfilled"
        return "open"

    t["status"] = t.apply(status, axis=1)
    t["cost_cents"] = t["filled"].fillna(0) * t["entry_cents"]
    return t[cols]


def _price_col(df):
    """The entry price column: actual fill price when the frame has it, else the limit price."""
    return "entry_cents" if "entry_cents" in df else "limit_price"


def trade_metrics(trades):
    s = trades[trades["status"] == "settled"]
    n = len(s)
    wins = int((s["result"] == "yes").sum())
    cost = float(s["cost_cents"].sum())
    pnl = float(s["pnl_cents"].sum())
    return {
        "settled": n, "wins": wins, "losses": n - wins,
        "win_rate": wins / n if n else None,
        "net_pnl_cents": pnl,
        "roi": pnl / cost if cost else None,
        "avg_price": float(s[_price_col(s)].mean()) if n else None,
        "open": int((trades["status"] == "open").sum()),
    }


def breakeven_win_rate(avg_price_cents):
    """Win rate at which a buy at avg_price breaks even after the taker fee."""
    p = avg_price_cents / 100.0
    return p + 0.07 * p * (1 - p)


def _wilson(wins, n, z=1.96):
    if n == 0:
        return (None, None)
    phat = wins / n
    denom = 1 + z**2 / n
    centre = (phat + z**2 / (2 * n)) / denom
    half = z * math.sqrt(phat * (1 - phat) / n + z**2 / (4 * n**2)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


def _prob_at_least(probs, k):
    """P(number of wins >= k) for independent Bernoulli(probs): exact DP."""
    dist = [1.0] + [0.0] * len(probs)
    for p in probs:
        for j in range(len(dist) - 1, 0, -1):
            dist[j] = dist[j] * (1 - p) + dist[j - 1] * p
        dist[0] *= 1 - p
    return float(sum(dist[k:]))


def implied_edge(trades):
    """Live results vs what the pre-game prices implied.

    A contract bought at price p implies a p% chance of winning. If pricing were
    unbiased, the live win rate would match the average entry price and, after
    fees, PnL would be about -fees. The edge is the gap.

    Returns None if nothing has settled. `p_value` is the exact one-sided chance of
    winning at least this many games if every game won with exactly its implied
    probability (small => results beat the pricing by more than luck).
    """
    s = trades[trades["status"] == "settled"]
    if s.empty:
        return None
    probs = (s[_price_col(s)] / 100.0).tolist()
    n, wins = len(s), int((s["result"] == "yes").sum())
    lo, hi = _wilson(wins, n)
    implied_rate = sum(probs) / n
    return {
        "n": n, "wins": wins, "losses": n - wins,
        "win_rate": wins / n, "wilson_lo": lo, "wilson_hi": hi,
        "implied_rate": implied_rate,
        "edge_pts": 100.0 * (wins / n - implied_rate),
        "expected_wins": sum(probs),
        "p_value": _prob_at_least(probs, wins),
        "actual_pnl_cents": float(s["pnl_cents"].sum()),
        "fees_cents": float(s["fee_cents"].sum()),
    }


def pnl_vs_pricing(trades):
    """Per settled trade: cumulative actual PnL vs the PnL implied pricing predicts.

    Under unbiased pricing a buyer expects to lose only the fee, so the baseline is
    the negative cumulative fees.
    """
    s = trades[trades["status"] == "settled"].sort_values("ts")
    if s.empty:
        return pd.DataFrame(columns=["ts", "actual", "implied_baseline"])
    return pd.DataFrame({
        "ts": s["ts"].values,
        "actual": s["pnl_cents"].cumsum().values / 100.0,
        "implied_baseline": (-s["fee_cents"]).cumsum().values / 100.0,
    })


def set_kill_switch(path, on):
    """Create (pause new orders) or remove (resume) the kill-switch file."""
    import os
    if on:
        with open(path, "w") as f:
            f.write("paused from dashboard\n")
    elif os.path.exists(path):
        os.remove(path)


def heartbeat_age_minutes(events, now=None):
    hb = events[events["kind"] == "heartbeat"]
    if hb.empty:
        return None
    now = now or datetime.now(timezone.utc)
    return (now - hb["ts"].max()).total_seconds() / 60.0


def skip_reason_counts(decisions):
    d = decisions[decisions["action"] == "skip"].copy()
    if d.empty:
        return pd.DataFrame(columns=["reason", "count"])
    d["reason"] = d["reason"].str.split(":").str[0]
    return d.groupby("reason").size().reset_index(name="count").sort_values("count", ascending=False)

"""Read-only portfolio dashboard for the NBA bot.

Run:  streamlit run nba_trading/dashboard/app.py
Shows decisions, trades, equity, live edge vs pre-game pricing, and health from the
bot's SQLite ledger. The ledger is opened read-only. The only control is the kill-switch
toggle, which creates/removes the STOP_TRADING file the bot checks before every order;
the dashboard cannot place orders or change settings.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

import pandas as pd
import streamlit as st

from nba_trading import config
from nba_trading.dashboard import data

st.set_page_config(page_title="NBA Bot", layout="wide")
LEDGER_PATH = os.environ.get("LEDGER_FILE", config.LEDGER_FILE)

st.title("NBA favorite bot")

if not os.path.exists(LEDGER_PATH):
    st.warning(f"No ledger yet at `{LEDGER_PATH}`. Start the bot (dry run is fine) to create it.")
    st.stop()

REFRESH_SECONDS = 30


def _on_kill_toggle():
    """Runs only when the user clicks the toggle, never on a periodic refresh."""
    data.set_kill_switch(config.KILL_SWITCH_FILE, st.session_state["kill_toggle"])


@st.fragment(run_every=REFRESH_SECONDS)
def render():
    """Re-reads the ledger every REFRESH_SECONDS; only this block reruns, not the whole page."""
    conn = data.connect_ro(LEDGER_PATH)
    try:
        f = data.load_frames(conn)
    finally:
        conn.close()
    trades, decisions, snaps, events = f["trades"], f["decisions"], f["snapshots"], f["events"]
    paper = f["paper"]
    m = data.trade_metrics(trades)

    # ---- status strip ----
    age = data.heartbeat_age_minutes(events)
    kill = os.path.exists(config.KILL_SWITCH_FILE)
    last_dec = decisions.sort_values("ts").iloc[-1] if not decisions.empty else None
    c1, c2, c3, c4, c5 = st.columns(5)
    if age is None:
        c1.metric("Bot", "no heartbeat")
    else:
        c1.metric("Bot", "running" if age < 45 else "STALE", f"{age:.0f} min ago", delta_color="off")
    c2.metric("Kill switch", "ON (paused)" if kill else "off")
    # The file is the source of truth: reflect it in the toggle on every render (so a
    # `touch STOP_TRADING` from a terminal shows up), and write it ONLY on a click.
    st.session_state["kill_toggle"] = kill
    c2.toggle("Pause trading (kill switch)", key="kill_toggle", on_change=_on_kill_toggle,
              help="On: the bot places no new orders. Off: trading resumes at the next game. "
                   "Orders already placed are not cancelled.")
    if not snaps.empty:
        s = snaps.sort_values("ts").iloc[-1]
        c3.metric("Equity", f"${s['equity_cents'] / 100:,.2f}")
        c4.metric("Cash / open", f"${s['balance_cents'] / 100:,.2f} / ${s['open_cost_cents'] / 100:,.2f}")
        c5.metric("Drawdown", f"{s['drawdown_pct']:.1f}%", f"limit {config.MAX_DRAWDOWN_PCT:.0f}%", delta_color="off")
    if last_dec is not None:
        mode = "DRY RUN" if last_dec["dry_run"] else "LIVE"
        st.caption(f"Mode (last decision): **{mode}** | band {int(last_dec['band_min'])}-{int(last_dec['band_max'])}c | "
                   f"{last_dec['fraction']:.0%} of equity per trade | exposure cap {config.MAX_EXPOSURE_PCT:.0f}%")
    if age is not None and age >= 45:
        st.error("No heartbeat in 45+ minutes -- the bot may have stopped.")
    crit = events[events["level"] == "CRITICAL"]
    if not crit.empty:
        st.error(f"{len(crit)} CRITICAL event(s) -- latest: {crit.sort_values('ts').iloc[-1]['kind']} "
                 f"at {crit['ts'].max():%Y-%m-%d %H:%M} UTC")

    tab_over, tab_edge, tab_dec, tab_trades, tab_health = st.tabs(
        ["Overview", "Live edge vs pricing", "Decisions", "Trades", "Health"])

    with tab_over:
        a, b, c = st.columns(3)
        a.metric("Settled trades", m["settled"])
        b.metric("Win rate", f"{m['win_rate']:.1%}" if m["win_rate"] is not None else "-")
        c.metric("Net PnL", f"${m['net_pnl_cents'] / 100:,.2f}")
        if not snaps.empty:
            st.subheader("Equity")
            st.line_chart(snaps.sort_values("ts").set_index("ts")["equity_cents"] / 100.0)

    with tab_edge:
        st.write("Pre-game prices imply a win probability: buying at 93c means the market says ~93%. "
                 "Is the live win rate beating what the price implied?")
        sources = {"Paper (dry-run decisions, 1 contract each)": paper, "Live (real orders)": trades}
        default = 1 if (trades["status"] == "settled").any() else 0
        choice = st.radio("Source", list(sources), index=default, horizontal=True)
        src = sources[choice]
        e = data.implied_edge(src)
        if e is None:
            st.info("Nothing settled yet. Paper trades settle after each game ends "
                    "(the bot checks every 30 minutes).")
        else:
            x, y, z, w = st.columns(4)
            x.metric("Record", f"{e['wins']}W - {e['losses']}L")
            y.metric("Win rate", f"{e['win_rate']:.1%}",
                     f"95% CI {e['wilson_lo']:.1%}-{e['wilson_hi']:.1%}", delta_color="off")
            z.metric("Implied by price", f"{e['implied_rate']:.1%}")
            w.metric("Edge", f"{e['edge_pts']:+.1f} pts")
            st.write(f"Expected wins at the implied rate: **{e['expected_wins']:.1f}**, actual: **{e['wins']}**. "
                     f"If every game won exactly at its implied probability, the chance of winning this many "
                     f"or more is **{e['p_value']:.3f}**"
                     + (" -- small, so the results beat the pricing by more than luck."
                        if e["p_value"] < 0.05 else
                        " -- not small, so the results are still consistent with the pricing being right."))
            st.caption(f"Net PnL ${e['actual_pnl_cents'] / 100:,.2f} after ${e['fees_cents'] / 100:,.2f} fees. "
                       "With fair pricing you'd expect to lose about the fees. "
                       "With few trades the interval is wide; a handful of wins proves little."
                       + (" Paper trades assume a fill at the limit price, which a real resting order may not get."
                          if src is paper else ""))
            cmp_ = data.pnl_vs_pricing(src)
            if not cmp_.empty:
                st.subheader("Cumulative PnL ($): actual vs what fair pricing predicts")
                st.line_chart(cmp_.set_index("ts")[["actual", "implied_baseline"]])

    with tab_dec:
        st.subheader("Why the bot skipped")
        reasons = data.skip_reason_counts(decisions)
        if reasons.empty:
            st.info("No skips recorded.")
        else:
            st.bar_chart(reasons.set_index("reason")["count"])
        show = st.radio("Show", ["all", "buy", "skip"], horizontal=True)
        dd = decisions if show == "all" else decisions[decisions["action"] == show]
        st.dataframe(dd.sort_values("ts", ascending=False), use_container_width=True, hide_index=True)

    with tab_trades:
        if not paper.empty:
            st.subheader("Paper trades (dry run)")
            st.dataframe(paper.sort_values("ts", ascending=False), use_container_width=True, hide_index=True)
        st.subheader("Orders")
        if trades.empty:
            st.info("No orders yet.")
        else:
            t = trades.sort_values("ts", ascending=False).copy()
            t["pnl_$"] = t["pnl_cents"] / 100.0
            st.dataframe(t.drop(columns=["pnl_cents", "fee_cents", "cost_cents"]),
                         use_container_width=True, hide_index=True)

    with tab_health:
        st.subheader("Recent events")
        if events.empty:
            st.info("No events.")
        else:
            ev = events[events["kind"] != "heartbeat"].sort_values("ts", ascending=False).head(200)
            st.dataframe(ev, use_container_width=True, hide_index=True)
        st.caption(f"Ledger: `{LEDGER_PATH}`")


render()
st.caption(f"Auto-refreshes every {REFRESH_SECONDS}s.")

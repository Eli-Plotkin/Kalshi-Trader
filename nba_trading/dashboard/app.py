"""Read-only portfolio dashboard for the NBA bot.

Run:  streamlit run nba_trading/dashboard/app.py
Shows decisions, trades, equity, live edge vs pre-game pricing, and health from the
bot's SQLite ledger. The ledger is opened read-only. The only control is the kill-switch
toggle, which creates/removes the STOP_TRADING file the bot checks before every order;
the dashboard cannot place orders or change settings.

Two modes (DASHBOARD_MODE):
  local   (default) read the ledger file directly; no password unless DASHBOARD_PASSWORD is set.
  remote  deployed on a separate host with no Kalshi credentials: a background thread syncs the
          ledger snapshot from the bucket, everything sits behind DASHBOARD_PASSWORD, a freshness
          banner shows how old the data is, and the only control is "Request pause" (resume is
          done on the backend server). This file must never import kalshi.* or nba_trading.main.
"""
from __future__ import annotations

import os
import sqlite3
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

import pandas as pd
import streamlit as st

from nba_trading import config
from nba_trading.dashboard import auth, data, freshness, remote

st.set_page_config(page_title="NBA Bot", layout="wide")

try:
    REMOTE = remote.get_mode() == "remote"
except ValueError as _bad_mode:
    st.error(str(_bad_mode))
    st.stop()

# Password gate: in remote mode nothing below this line runs until the viewer is signed in.
auth.gate(remote=REMOTE)

LEDGER_PATH = remote.ledger_path() if REMOTE else os.environ.get("LEDGER_FILE", config.LEDGER_FILE)

REFRESH_SECONDS = 10
BANNER_REFRESH_SECONDS = 10


@st.fragment(run_every=BANNER_REFRESH_SECONDS)
def freshness_banner():
    """Remote mode, very top of the page: how old the data is, and whether the bot is alive."""
    # Fragments outlive a logout and can be re-run on their own: check authentication HERE, before
    # anything is read or the sync thread is started (st.stop() renders nothing).
    auth.require_authenticated(REMOTE)
    start_error = remote.ensure_sync_started()
    a = freshness.assess(remote.load_sync_state())
    {"fresh": st.success, "stale": st.warning, "broken": st.error,
     "waiting": st.info}[a["level"]](a["message"])
    hb = a["heartbeat"]
    if hb["level"] == freshness.HB_SILENT:
        st.warning(hb["message"])
    else:
        st.caption(hb["message"])
    if a["error"]:
        st.warning(a["error"])
    if start_error:
        st.error(start_error)


if REMOTE:
    freshness_banner()

st.title("NBA favorite bot")
auth.logout_button()

if not REMOTE and not os.path.exists(LEDGER_PATH):
    st.warning(f"No ledger yet at `{LEDGER_PATH}`. Start the bot (dry run is fine) to create it.")
    st.stop()


def _signed_in() -> bool:
    """For widget callbacks (st.stop() is for scripts/fragments): never act for a logged-out session."""
    return not auth.is_gated(REMOTE) or auth.is_authenticated()


def _on_kill_toggle():
    """Runs only when the user clicks the toggle, never on a periodic refresh."""
    if not _signed_in():
        return
    data.set_kill_switch(config.KILL_SWITCH_FILE, st.session_state["kill_toggle"])


def _on_pause_request():
    """Remote mode: runs only on a click of 'Request pause'."""
    if not _signed_in():
        return
    st.session_state["pause_result"] = remote.request_pause(st.session_state.get("pause_reason", ""))


def _remote_pause_panel(status):
    """Remote mode's only control. There is deliberately no resume button.

    Everything that came from pause.json / ack.json (who, when, why) is untrusted text: it is shown
    with st.text only, never through markdown.
    """
    with st.container(border=True):
        st.markdown("**Remote pause**")
        if status["state"] == "unknown":
            for line in status["lines"]:
                st.text(line)
        elif status["state"] == "requested":
            st.warning("A pause is requested.")
            for line in status["lines"]:
                st.text(line)
            if status["warn_no_ack"]:
                st.warning(f"The bot has not acknowledged the pause after {remote.ACK_WARN_SECONDS} s. "
                           "It may be down, or unable to read the control bucket: do not assume it has "
                           "stopped trading.")
        elif status["state"] == "latched_only":
            st.warning("No pause is requested in the bucket, but the bot reports a latched pause.")
            for line in status["lines"]:
                st.text(line)
        else:
            st.caption("No pause is requested.")
            for line in status["lines"]:
                st.text(line)
        st.text_input("Reason (optional)", key="pause_reason", max_chars=remote.MAX_REASON_CHARS)
        st.button("Request pause", key="pause_request", on_click=_on_pause_request,
                  help="Asks the bot to place no new orders. Takes effect within ~30 s plus the next "
                       "game evaluation; orders already placed are not cancelled.")
        result = st.session_state.get("pause_result")
        if result:
            (st.success if result[0] else st.error)(result[1])
        st.info(remote.RESUME_HELP)


@st.fragment(run_every=REFRESH_SECONDS)
def render():
    """Re-reads the ledger every REFRESH_SECONDS; only this block reruns, not the whole page."""
    auth.require_authenticated(REMOTE)       # a fragment rerun never passes through the gate
    if REMOTE and not os.path.exists(LEDGER_PATH):
        st.info("Waiting for the first data from the backend. This page checks again every "
                f"{REFRESH_SECONDS} s; the banner above shows the sync status.")
        return
    try:
        conn = data.connect_ro(LEDGER_PATH)
        try:
            f = data.load_frames(conn)
        finally:
            conn.close()
    except (sqlite3.Error, pd.errors.DatabaseError) as exc:
        # pandas wraps sqlite errors raised while reading a table in DatabaseError
        if not REMOTE:
            raise
        st.error(f"The synced ledger could not be read yet ({type(exc).__name__}). Retrying shortly.")
        return
    trades, decisions, snaps, events = f["trades"], f["decisions"], f["snapshots"], f["events"]
    paper = f["paper"]
    m = data.trade_metrics(trades)

    # ---- status strip ----
    age = data.heartbeat_age_minutes(events)
    kill = (not REMOTE) and os.path.exists(config.KILL_SWITCH_FILE)
    pause = pause_error = pstat = None
    if REMOTE:
        pause, pause_error = remote.current_pause()
        ack, ack_error = remote.current_ack() if not pause_error else (None, None)
        pstat = remote.pause_status(pause, pause_error, ack, ack_error)
        bot = freshness.assess(remote.load_sync_state())["heartbeat"]
    last_dec = decisions.sort_values("ts").iloc[-1] if not decisions.empty else None
    c1, c2, c3, c4, c5 = st.columns(5)
    if REMOTE:
        # The ledger's own timestamps are on the backend clock; the banner's skew-proof status is used.
        c1.metric("Bot", {freshness.HB_OK: "alive", freshness.HB_SILENT: "SILENT"}.get(bot["level"], "unknown"),
                  bot["message"].replace("Bot ", "", 1) if bot["level"] != freshness.HB_UNKNOWN else None,
                  delta_color="off")
    elif age is None:
        c1.metric("Bot", "no heartbeat")
    else:
        c1.metric("Bot", "running" if age < 45 else "STALE", f"{age:.0f} min ago", delta_color="off")
    if REMOTE:
        c2.metric("Remote pause", {"unknown": "unknown", "requested": "REQUESTED",
                                   "latched_only": "LATCHED"}.get(pstat["state"], "not requested"))
    else:
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
        # Limits come from this process's config, which in remote mode is not the backend's: omit them.
        c5.metric("Drawdown", f"{s['drawdown_pct']:.1f}%",
                  None if REMOTE else f"limit {config.MAX_DRAWDOWN_PCT:.0f}%", delta_color="off")
    if REMOTE:
        _remote_pause_panel(pstat)
    if last_dec is not None:
        mode = "DRY RUN" if last_dec["dry_run"] else "LIVE"
        st.caption(f"Mode (last decision): **{mode}** | band {int(last_dec['band_min'])}-{int(last_dec['band_max'])}c | "
                   f"{last_dec['fraction']:.0%} of equity per trade"
                   + ("" if REMOTE else f" | exposure cap {config.MAX_EXPOSURE_PCT:.0f}%"))
    if (not REMOTE) and age is not None and age >= 45:      # remote: the banner owns bot liveness
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
        st.dataframe(dd.sort_values("ts", ascending=False), width="stretch", hide_index=True)

    with tab_trades:
        if not paper.empty:
            st.subheader("Paper trades (dry run)")
            st.dataframe(paper.sort_values("ts", ascending=False), width="stretch", hide_index=True)
        st.subheader("Orders")
        if trades.empty:
            st.info("No orders yet.")
        else:
            t = trades.sort_values("ts", ascending=False).copy()
            t["pnl_$"] = t["pnl_cents"] / 100.0
            st.dataframe(t.drop(columns=["pnl_cents", "fee_cents", "cost_cents"]),
                         width="stretch", hide_index=True)

    with tab_health:
        st.subheader("Recent events")
        if events.empty:
            st.info("No events.")
        else:
            ev = events[events["kind"] != "heartbeat"].sort_values("ts", ascending=False).head(200)
            st.dataframe(ev, width="stretch", hide_index=True)
        if not REMOTE:
            st.caption(f"Ledger: `{LEDGER_PATH}`")


render()
st.caption(f"Auto-refreshes every {REFRESH_SECONDS}s.")

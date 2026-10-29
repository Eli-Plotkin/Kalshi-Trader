"""NBA price-segment bot — fires 15 minutes before tipoff and buys favorites
whose pre-game YES ask falls inside a configurable price band, sized as a
fraction of EQUITY (cash plus capital at risk), capped by cash and an exposure cap.

All strategy parameters (band edges, sizing, drawdown limits) come from
`strategy_config.py`, which reads them from `.env`. The shipped defaults are the
91-99c band at 10% of equity: a hypothesis under test, not a proven edge.

Dry-run is the default: the full pipeline runs against live Kalshi data but no
order is placed and no position is held. A dry run is NOT stateless: it records
decisions, equity snapshots, events, paper trades, the drawdown peak and the schedule
in the SQLite ledger (it never marks a game fired, so it cannot suppress a live run).
Pass `--live` to trade real money. See docs/system-architecture.md for the full picture.
"""
import argparse
import os
import threading
import time
import logging
import sys
from datetime import datetime, timezone, timedelta
from logging.handlers import RotatingFileHandler
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_MISSED
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

from kalshi.client import KalshiClient
from nba_trading import config, strategy, nba_scheduler
from nba_trading import alerts
from nba_trading.ledger import InstanceLock, InstanceLockError, Ledger
from nba_trading.portfolio import HighWaterMark, Portfolio

# Set by run_bot(); None means "don't record" (unit tests, ad-hoc use).
LEDGER = None
# The running scheduler, set by _run_loop(). Jobs that need to queue follow-up jobs (the health
# job resolving a lost order) use this; None means no scheduler is available.
SCHEDULER = None

EASTERN = ZoneInfo("America/New_York")
SCHEDULE_RETRY_MINUTES = 10

def _configure_logging():
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(message)s',
        handlers=[
            RotatingFileHandler("trade_bot.log", maxBytes=5*1024*1024, backupCount=3),
            logging.StreamHandler(sys.stdout)
        ]
    )


# --- Ledger helpers (no-ops when LEDGER is None) ---

def _event(level, kind, message):
    """Record an operator-visible event in the ledger and push an alert (both best-effort)."""
    if LEDGER is not None:
        try:
            LEDGER.record_event(level, kind, message)
        except Exception:
            logging.exception("ledger event write failed")
    if kind != "heartbeat":                      # heartbeats go to the dead-man's switch instead
        alerts.notify(level, kind, message)


def _decide(game, action, reason, dry_run, **ctx):
    """Record one decision row; returns its id (or None). Never raises."""
    if LEDGER is None:
        return None
    try:
        return LEDGER.record_decision(
            game_id=game['game_id'], home=game['home_team'], away=game['away_team'],
            tip_off_utc=game['tip_off_utc'], action=action, reason=reason, dry_run=dry_run,
            band_min=config.FAVORITE_PRICE_MIN, band_max=config.FAVORITE_PRICE_MAX,
            fraction=config.BANKROLL_FRACTION, **ctx)
    except Exception:
        logging.exception("ledger decision write failed")
        return None


def _snapshot(balance_cents, open_cost, equity_cents, high_water):
    if LEDGER is None:
        return
    try:
        LEDGER.record_snapshot(balance_cents, open_cost, equity_cents, high_water.peak(),
                               high_water.drawdown_pct(equity_cents))
    except Exception:
        logging.exception("ledger snapshot write failed")


# --- Schedule persistence ---

# The schedule lives in the SQLite database (tables schedule_games + kv), the same
# file as the ledger. run_bot() sets LEDGER; code that calls these without one
# (there is no JSON fallback any more) gets a clear error rather than silent loss.

def _state_db():
    if LEDGER is None:
        raise RuntimeError("state database not initialised (main.LEDGER is None)")
    return LEDGER


def load_schedule():
    """-> {'date': 'YYYY-MM-DD', 'games': {game_id: entry}} or {} if none saved."""
    return _state_db().get_schedule()


def save_schedule(schedule):
    """Atomically replace the saved schedule."""
    _state_db().replace_schedule(schedule)


def mark_game_fired(game_id):
    _state_db().mark_game_fired(game_id)


# --- Fill confirmation job ---

NBA_SERIES_PREFIX = "KXNBAGAME-"
FILL_SYNC_LOOKBACK_DAYS = 7
PROVISIONAL_NOT_FOUND_HOURS = 48
HEALTH_LOCK_TIMEOUT_SECONDS = 30
INFLIGHT_GRACE_SECONDS = 90          # a 'submitting' row younger than this may still have its POST in flight
SETTLE_STUCK_HOURS = 24              # an order still unsettleable after this long raises an alert
_SETTLE_STUCK_REPORTED = set()       # order ids already reported as stuck (one alert per order per run)
_DUP_REPORTED = set()                # tickers already reported as double-booked
_WARNED_FOREIGN = set()          # tickers already reported as non-NBA positions (avoid event spam)


def confirm_fill_job(client, portfolio, ticker, order_id, shares_attempted):
    """Post-tip check: record what Kalshi says about the order, then let Kalshi's positions
    correct our lots (an unfilled order expired at tip-off; a partial fill leaves fewer contracts).

    The authority for 'what do we hold' is Kalshi's position list, not the fill count in the
    order payload: a missing field parses as 0 and must never be able to delete a real position.
    """
    try:
        order = client.get_order(order_id)
    except Exception as e:
        logging.error(f"FILL CHECK FAILED for {ticker} order {order_id}: {e}. "
                      f"The health job will reconcile positions.")
        _event("ERROR", "fill_check_failed", f"{ticker} {order_id}: {e}")
        return
    if order is None:
        logging.error(f"FILL CHECK: Kalshi has no order {order_id} for {ticker}.")
        _event("ERROR", "fill_check_no_order", f"{ticker} {order_id}")
        return

    filled = order.get("filled")
    if LEDGER is not None:
        try:
            row = LEDGER.order_by_order_id(order_id)
            if row is not None:
                LEDGER.apply_order_state(row["id"], order_id, order.get("status"), filled,
                                         order.get("avg_fill_cents"), "lookup")
        except ValueError as e:                     # e.g. already settled: nothing to update
            logging.info(f"Fill check: ledger left unchanged ({e})")
        except Exception:
            logging.exception("ledger fill update failed")

    if not _TRADE_LOCK.acquire(timeout=config.TRADE_LOCK_TIMEOUT_SECONDS):
        logging.error(f"Fill check for {ticker}: trade lock busy; the health job will reconcile.")
        return
    try:
        # Tip-off has passed, so THIS order can no longer be resting: give only its ticker zero grace.
        # Other games' orders may still be resting and keep their normal grace.
        _sync_positions(client, portfolio, grace_overrides={ticker: 0}, scheduler=SCHEDULER)
    except Exception as e:
        logging.error(f"Fill check for {ticker}: position sync failed ({e}); health job will retry.")
        _event("ERROR", "reconcile_failed", f"fill check {ticker}: {e}")
        return
    finally:
        _TRADE_LOCK.release()

    if not filled:
        logging.warning(f"ORDER EXPIRED UNFILLED: {ticker} (status {order.get('status')})")
    elif filled < shares_attempted:
        logging.info(f"PARTIAL FILL: {ticker} | {filled}/{shares_attempted} shares")
    else:
        logging.info(f"FILL CONFIRMED: {ticker} | {filled}/{shares_attempted} shares filled")


def _schedule_fill_check(scheduler, client, portfolio, ticker, order_id, shares, tip_off_dt):
    """Queue the post-tip fill check (tip-off + 2 min, or shortly from now if that has passed)."""
    if scheduler is None:
        logging.warning(f"No scheduler available; fill check for {ticker} order {order_id} not queued "
                        f"(the health job still reconciles and settles it).")
        return None
    now = datetime.now(timezone.utc)
    run_at = max((tip_off_dt + timedelta(minutes=2)) if tip_off_dt else now + timedelta(minutes=2),
                 now + timedelta(seconds=10))
    scheduler.add_job(
        confirm_fill_job,
        trigger=DateTrigger(run_date=run_at),
        args=[client, portfolio, ticker, order_id, shares],
        id=f"confirm-{order_id}", replace_existing=True,
        misfire_grace_time=config.CONFIRM_FILL_MISFIRE_GRACE_SECONDS, coalesce=True,
    )
    logging.info(f"Fill check scheduled for {run_at.isoformat()} UTC")
    return run_at


# --- Pre-game job ---

# Reasons where the bot could not READ something it needs, so it never reached a decision.
# These are retried (bounded). Everything else - out of band, kill switch, breaker, an order
# attempt of any kind - is a decision or an outcome and is never retried.
# One evaluation at a time. Everything from reading cash/positions through placing the order
# and recording the position is a check-then-act sequence on shared money: without this, two
# games tipping together each see the same cash and the same open exposure, and together can
# exceed the exposure cap (or spend cash twice).
_TRADE_LOCK = threading.Lock()

TRANSIENT_SKIP_REASONS = frozenset({
    "balance_fetch_failed", "reconcile_failed", "markets_not_found",
    "no_orderbook_data", "no_ask_price",
})


def _schedule_retry(scheduler, client, portfolio, high_water, game, dry_run, attempt):
    """Queue the next evaluation if the retry budget and the clock allow. True if queued."""
    if attempt >= config.PRE_GAME_MAX_RETRIES:
        return False
    tip_off = datetime.fromisoformat(game['tip_off_utc'].replace('Z', '+00:00'))
    run_at = datetime.now(timezone.utc) + timedelta(seconds=config.PRE_GAME_RETRY_DELAY_SECONDS)
    if run_at > tip_off - timedelta(seconds=config.PRE_GAME_RETRY_STOP_BEFORE_TIP_SECONDS):
        return False
    scheduler.add_job(
        pre_game_job, trigger=DateTrigger(run_date=run_at),
        args=[client, portfolio, high_water, scheduler, game, dry_run, attempt + 1],
        id=f"{game['game_id']}-retry{attempt + 1}", replace_existing=True,
        misfire_grace_time=config.PRE_GAME_MISFIRE_GRACE_SECONDS, coalesce=True)
    logging.warning(f"Transient failure on {game['away_team']} @ {game['home_team']}; "
                    f"retry {attempt + 1}/{config.PRE_GAME_MAX_RETRIES} at {run_at:%H:%M:%S} UTC.")
    return True


def _safe_mark_fired(game_id):
    """Marking can fail (disk, lock); that must never escape after an order has gone out."""
    try:
        mark_game_fired(game_id)
    except Exception:
        logging.critical(f"Could not mark {game_id} fired; a restart may re-evaluate it "
                         f"(the fixed client_order_id and position check prevent a double buy).",
                         exc_info=True)


def pre_game_job(client, portfolio, high_water, scheduler, game, dry_run=True, attempt=0):
    """Evaluate one game and (if live) place the order.

    Outcomes:
    - transient read failure (no decision made): retried up to PRE_GAME_MAX_RETRIES times,
      then skipped. The game is NOT marked fired while a retry is pending.
    - anything else (skip for a rule, or any order attempt): final. The game is marked fired.
    Orders are never retried. If the process dies mid-order, the restart re-fires the job
    and the deterministic client_order_id lets Kalshi reject a duplicate rather than
    double-buying. Dry runs never mark games fired.
    """
    retry_queued = False
    try:
        if _TRADE_LOCK.acquire(timeout=config.TRADE_LOCK_TIMEOUT_SECONDS):
            try:
                status = _pre_game_body(client, portfolio, high_water, scheduler, game, dry_run, attempt)
            finally:
                _TRADE_LOCK.release()
        else:
            # Another game held the lock the whole time: we never looked, so nothing was decided.
            logging.error(f"Could not get the trade lock for {game['game_id']} in "
                          f"{config.TRADE_LOCK_TIMEOUT_SECONDS}s.")
            _decide(game, "skip", "trade_lock_timeout" + (f":retry{attempt}" if attempt else ""), dry_run)
            status = "transient"
        if status == "transient":
            retry_queued = _schedule_retry(scheduler, client, portfolio, high_water,
                                           game, dry_run, attempt)
            if not retry_queued:
                _decide(game, "skip", "retries_exhausted", dry_run)
                _event("ERROR", "retries_exhausted",
                       f"{game['game_id']}: skipped after {attempt + 1} attempt(s)")
    finally:
        if not dry_run and not retry_queued:
            _safe_mark_fired(game['game_id'])


def _client_order_id(game_id, ticker):
    return f"nba-{game_id}-{ticker}"


def _kalshi_positions(client):
    """Positions Kalshi says we hold: {ticker: {"position": int, "exposure_cents": int | None}}.

    YES-long only (the bot never buys NO). Raises if the fetch fails, so callers can tell
    'no positions' from 'could not ask'.
    """
    out = {}
    for p in client.list_positions_detailed():
        qty = p.get("position") or 0
        if p.get("ticker") and qty > 0:
            out[p["ticker"]] = {"position": int(qty), "exposure_cents": p.get("exposure_cents")}
    return out


def _kalshi_holdings(client):
    """Map ticker -> YES contracts held, per Kalshi. Raises if the fetch fails."""
    return {t: d["position"] for t, d in _kalshi_positions(client).items()}


def _adopt_unknown_positions(client, portfolio, positions, scheduler=None):
    """Positions Kalshi holds that we have no lot for.

    - A ticker with an unresolved order (submitting/unconfirmed/not_found) is (almost certainly)
      THAT order filling. Ask Kalshi to resolve it; never create a second ledger row for the trade.
    - Otherwise an NBA position becomes ours (lot + ledger row) so it counts toward exposure and
      settles; anything that is not an NBA game market is only reported.
    Returns the tickers adopted as NEW trades.
    """
    adopted = []
    try:
        unresolved = LEDGER.unresolved_orders_by_ticker() if LEDGER is not None else {}
    except Exception:
        # A broken ledger must never block trading or accounting; without it we simply cannot
        # attribute positions to lost orders this cycle.
        logging.warning("Ledger unavailable for the unresolved-order check; skipping attribution.", exc_info=True)
        unresolved = {}
    for ticker, pos in positions.items():
        if portfolio.has_position(ticker):
            continue
        qty, exposure = pos["position"], pos.get("exposure_cents")
        price = round(exposure / qty) if exposure is not None and qty else 100   # 100 = worst case

        if unresolved.get(ticker):
            for row in unresolved[ticker]:
                _resolve_order(client, portfolio, scheduler, row["id"], "lookup", announce_not_found=False)
            if not portfolio.has_position(ticker):
                # Lookup could not place it. The exposure is real, so carry it; the ledger row stays
                # unresolved and the fills (when they sync) will be attributed to it.
                portfolio.add_position(ticker, price, qty)
                _event("WARNING", "position_without_order",
                       f"{ticker}: Kalshi holds {qty} contracts for an order the lookup cannot find yet; "
                       f"carrying the exposure, will attribute it from fills")
            continue

        if not ticker.startswith(NBA_SERIES_PREFIX):
            if ticker not in _WARNED_FOREIGN:
                _WARNED_FOREIGN.add(ticker)
                _event("WARNING", "foreign_position",
                       f"{ticker}: {qty} held on Kalshi, not an NBA game market; "
                       f"counted in equity but not managed")
            continue

        portfolio.add_position(ticker, price, qty)
        if LEDGER is not None:
            try:
                tracked = any(r["ticker"] == ticker for r in LEDGER.open_orders())
                if not tracked:
                    LEDGER.adopt_order(ticker, None, qty, price, limit_price=price)
            except Exception:
                logging.exception("ledger adoption failed")
        _event("ERROR", "position_adopted",
               f"{ticker}: Kalshi holds {qty} contracts the bot had no record of; adopted at {price}c")
        adopted.append(ticker)
    return adopted


def _sync_positions(client, portfolio, grace_seconds=1800, adopt=True, grace_overrides=None,
                    scheduler=None):
    """One Kalshi fetch: correct local lots toward Kalshi, and adopt positions we didn't know.

    `grace_overrides` ({ticker: seconds}) lets a post-tip fill check drop ONLY its own ticker's
    resting lot, not other games'. Returns (positions, dropped, adopted). Raises if the fetch
    fails; callers decide whether that blocks a trade (it does) or just skips an accounting
    cycle. Call under _TRADE_LOCK.
    """
    positions = _kalshi_positions(client)
    dropped = portfolio.reconcile({t: d["position"] for t, d in positions.items()},
                                  grace_seconds=grace_seconds, grace_overrides=grace_overrides)
    adopted = _adopt_unknown_positions(client, portfolio, positions, scheduler) if adopt else []
    if dropped:
        logging.info(f"Reconciled: dropped settled/unfilled positions {dropped}")
    return positions, dropped, adopted


def _parse_utc(value):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _resolve_order(client, portfolio, scheduler, order_pk, via="lookup", announce_not_found=True):
    """Answer 'did this order land?' by ASKING KALSHI. Never sends an order.

    Returns "placed" (Kalshi has it; ledger, portfolio and fill check now track it),
    "not_found" (Kalshi shows no such order *so far*) or "unresolved" (the lookup itself failed;
    the row stays as it was for the next health cycle/startup).

    `not_found` is provisional: a POST that timed out on our side can still be processed a moment
    later and listings can lag, so the health job keeps re-checking young not_found rows and a
    later hit upgrades the row to `placed`. It is never an invitation to re-send.
    """
    if LEDGER is None:
        return "unresolved"
    row = LEDGER.get_order_row(order_pk)
    if row is None:
        return "unresolved"
    ticker, coid = row["ticker"], row["client_order_id"]
    try:
        found = client.get_order_by_client_id(coid, ticker=ticker)
    except Exception as e:
        logging.error(f"Order lookup failed for {coid}: {e}")
        _event("ERROR", "order_lookup_failed",
               f"{coid}: {e}. Order stays unconfirmed; will retry. Check Kalshi manually if this persists.")
        return "unresolved"
    if found is None:
        if row["outcome"] != "not_found":
            try:
                LEDGER.set_order_outcome(order_pk, "not_found")
            except ValueError as e:
                logging.warning(f"Could not mark {coid} not_found: {e}")
        if announce_not_found:
            _event("ERROR", "order_not_found",
                   f"{ticker}: Kalshi shows no order {coid} (yet). NOT re-sending; will keep re-checking "
                   f"for {PROVISIONAL_NOT_FOUND_HOURS}h in case it was processed late.")
        return "not_found"

    try:
        LEDGER.apply_order_state(order_pk, found["order_id"], found.get("status"), found.get("filled"),
                                 found.get("avg_fill_cents"), via)
    except ValueError as e:
        logging.warning(f"Could not apply Kalshi state to {coid}: {e}")
    if not portfolio.has_position(ticker):
        qty = found.get("filled") or found.get("remaining") or row["count"]
        portfolio.add_position(ticker, row["limit_price"], qty)
    tip = _parse_utc(LEDGER.order_tip_off(order_pk))
    _schedule_fill_check(scheduler, client, portfolio, ticker, found["order_id"], row["count"], tip)
    logging.critical(f"ORDER RECOVERED: {coid} exists on Kalshi (status {found.get('status')}, "
                     f"{found.get('filled')} filled). Now tracking it.")
    _event("WARNING", "order_recovered",
           f"{ticker}: order {found['order_id']} was ambiguous; Kalshi confirms it "
           f"({found.get('status')}, {found.get('filled')} filled)")
    return "placed"


def _age_seconds(row):
    ts = _parse_utc(row["updated_ts"] or row["ts"])
    return (datetime.now(timezone.utc) - ts).total_seconds() if ts else float("inf")


def _resolve_pending(client, portfolio, scheduler):
    """Resolve orders whose outcome we do not know yet, plus young `not_found` ones (a late hit
    upgrades them). Run under _TRADE_LOCK so it cannot race an order being placed right now; a
    'submitting' row that is still within the POST's own window is left alone."""
    if LEDGER is None:
        return
    for row in LEDGER.orders_needing_resolution():
        if row["outcome"] == "submitting" and _age_seconds(row) < INFLIGHT_GRACE_SECONDS:
            continue
        _resolve_order(client, portfolio, scheduler, row["id"], "lookup")
    for row in LEDGER.provisional_not_found_orders(PROVISIONAL_NOT_FOUND_HOURS):
        _resolve_order(client, portfolio, scheduler, row["id"], "lookup", announce_not_found=False)


def recover_orders(client, portfolio, scheduler):
    """Startup recovery: resolve every order whose outcome we never learned, and re-create the
    post-tip fill check for every open order (the scheduler's job store lives in memory).

    One bad row must not stop recovery of the rest. Returns (resolved, fill_checks_scheduled).
    """
    if LEDGER is None:
        return 0, 0
    resolved = 0
    for row in LEDGER.orders_needing_resolution():
        try:
            if _resolve_order(client, portfolio, scheduler, row["id"], "startup") != "unresolved":
                resolved += 1
        except Exception:
            logging.exception(f"startup recovery failed for order {row['id']}")
    scheduled = 0
    for row in LEDGER.open_orders():
        try:
            oid = row["order_id"]
            if not oid or str(oid).startswith("adopted:"):
                continue
            tip = _parse_utc(LEDGER.order_tip_off(row["id"]))
            _schedule_fill_check(scheduler, client, portfolio, row["ticker"], oid, row["count"], tip)
            scheduled += 1
        except Exception:
            logging.exception(f"could not re-queue the fill check for order {row['id']}")
    if resolved or scheduled:
        logging.info(f"Startup recovery: resolved {resolved} ambiguous order(s), "
                     f"re-queued {scheduled} fill check(s).")
    return resolved, scheduled


def _reconcile_ambiguous_order(client, portfolio, ticker, shares, limit_price):
    """LEGACY fallback, used only when the ledger itself is unavailable (no order row to resolve).

    Check Kalshi's actual positions; if we hold the ticker, record it locally. Otherwise leave a
    CRITICAL log for a human. Never retries (no double buying).
    """
    try:
        held = _kalshi_positions(client).get(ticker)
    except Exception as e:
        logging.critical(f"AMBIGUOUS ORDER for {ticker}: could not read positions ({e}). VERIFY MANUALLY.")
        return
    if held:
        qty = held["position"]
        logging.critical(f"AMBIGUOUS ORDER for {ticker} actually landed ({qty} held). "
                         f"Recording position locally.")
        portfolio.add_position(ticker, limit_price, qty)
    else:
        logging.critical(
            f"AMBIGUOUS ORDER for {ticker}: placement failed and no position "
            f"found on Kalshi. It may still be resting or settle late -- "
            f"VERIFY MANUALLY. Not retrying."
        )


def _pre_game_body(client, portfolio, high_water, scheduler, game, dry_run, attempt=0):
    home_tri = game['home_team']
    away_tri = game['away_team']
    tip_off_utc = game['tip_off_utc']
    ctx = {}

    def skip(reason):
        """Record the skip. Returns "transient" if we could not read something (retryable)."""
        _decide(game, "skip", reason + (f":retry{attempt}" if attempt else ""), dry_run, **ctx)
        return "transient" if reason in TRANSIENT_SKIP_REASONS else "skipped"

    logging.info(f"Pre-game job firing: {away_tri} @ {home_tri}"
                 f"{' [DRY RUN]' if dry_run else ''}")

    # --- Kill switch: operator-requested stop, checked before anything else ---
    if os.path.exists(config.KILL_SWITCH_FILE):
        logging.warning(f"KILL SWITCH ({config.KILL_SWITCH_FILE}) present -- no new orders.")
        return skip("kill_switch")

    balance_cents = client.get_balance()
    if balance_cents is None:
        logging.error("Balance fetch failed; cannot size order. Skipping.")
        _event("ERROR", "balance_fetch_failed", f"{game['game_id']}")
        return skip("balance_fetch_failed")
    ctx["balance_cents"] = balance_cents

    # Sync local lots with what Kalshi actually holds (settled positions drop out, positions we
    # had no record of are adopted). If we can't verify exposure we don't trade live.
    positions, unknown_cost = {}, []
    if not dry_run:
        try:
            positions, _dropped, _adopted = _sync_positions(client, portfolio, scheduler=scheduler)
        except Exception as e:
            logging.error(f"Position reconcile failed ({e}); cannot verify exposure. Skipping.")
            _event("ERROR", "reconcile_failed", str(e))
            return skip("reconcile_failed")

    # --- Circuit breaker on EQUITY (cash + capital at risk), so cash locked in open positions
    # is not mistaken for a loss. Live: exposure comes from Kalshi's own position data, with local
    # lots only for orders Kalshi doesn't show yet (resting). Dry run holds nothing. ---
    if dry_run:
        open_cost = portfolio.open_cost_cents()
    else:
        open_cost, unknown_cost = portfolio.exposure_cents(positions)
        if unknown_cost:
            _event("WARNING", "equity_degraded",
                   f"Kalshi gave no cost for {unknown_cost}; not recording a new drawdown peak")
    equity_cents = balance_cents + open_cost
    if not unknown_cost:
        high_water.update(equity_cents, False)
    ctx["equity_cents"] = equity_cents
    _snapshot(balance_cents, open_cost, equity_cents, high_water)
    dd_pct = high_water.drawdown_pct(equity_cents)
    if high_water.is_circuit_broken(equity_cents, config.MAX_DRAWDOWN_PCT):
        logging.warning(
            f"CIRCUIT BREAKER: drawdown {dd_pct:.1f}% exceeds "
            f"{config.MAX_DRAWDOWN_PCT}% — halting new bets."
        )
        _event("CRITICAL", "circuit_breaker", f"drawdown {dd_pct:.1f}% >= {config.MAX_DRAWDOWN_PCT}%")
        return skip(f"circuit_breaker:{dd_pct:.1f}%")
    if dd_pct > 0:
        logging.info(f"Current drawdown {dd_pct:.1f}% (under {config.MAX_DRAWDOWN_PCT}% limit).")

    # Find both team tickers in open Kalshi markets
    markets = client.fetch_nba_markets()
    ticker_map = {
        m['ticker'].split('-')[-1]: m['ticker']
        for m in markets
        if m['ticker'].split('-')[-1] in [home_tri, away_tri]
    }

    if home_tri not in ticker_map or away_tri not in ticker_map:
        logging.warning(f"Could not find Kalshi markets for {away_tri} @ {home_tri}. Skipping.")
        return skip("markets_not_found")

    home_ticker = ticker_map[home_tri]
    away_ticker = ticker_map[away_tri]

    home_book = client.get_orderbook(home_ticker)
    away_book = client.get_orderbook(away_ticker)

    favorite_tri = strategy.identify_favorite(home_tri, away_tri, home_book, away_book)
    if not favorite_tri:
        return skip("no_orderbook_data")

    favorite_ticker = ticker_map[favorite_tri]
    ctx["ticker"] = favorite_ticker
    favorite_book = home_book if favorite_tri == home_tri else away_book

    ask_price = strategy.get_implied_ask(favorite_book)
    if ask_price is None:
        logging.warning(f"No ask price available for {favorite_ticker}. Skipping.")
        return skip("no_ask_price")
    ctx["ask_cents"] = ask_price

    if not strategy.should_buy(ask_price, config.FAVORITE_PRICE_MIN, config.FAVORITE_PRICE_MAX):
        logging.info(
            f"{favorite_ticker} ask {ask_price}¢ outside band "
            f"[{config.FAVORITE_PRICE_MIN}, {config.FAVORITE_PRICE_MAX}]. Skipping."
        )
        return skip(f"outside_band:{ask_price}")

    if portfolio.has_position(favorite_ticker):
        logging.info(f"Already have position in {favorite_ticker}. Skipping.")
        return skip("already_have_position")

    limit_price = strategy.compute_limit_price(
        ask_cents=ask_price,
        buffer_cents=config.LIMIT_PRICE_BUFFER_CENTS,
        cap_cents=config.FAVORITE_PRICE_MAX,
    )

    # Defence in depth: never send an order priced outside the configured band, whatever
    # the buffer/cap arithmetic above produced.
    if limit_price is None or not (config.FAVORITE_PRICE_MIN <= limit_price <= config.FAVORITE_PRICE_MAX):
        logging.error(f"Limit price {limit_price} outside band "
                      f"[{config.FAVORITE_PRICE_MIN}, {config.FAVORITE_PRICE_MAX}]; refusing to order.")
        return skip("limit_outside_band")

    # --- Dynamic sizing: fraction of equity, capped by cash and total exposure ---
    shares = strategy.compute_shares_to_buy(
        balance_cents=equity_cents,
        ask_cents=ask_price,
        fraction=config.BANKROLL_FRACTION,
    )
    sized = shares
    ctx["limit_price"] = limit_price          # recorded even on skips: a paper trade uses it
    shares = strategy.cap_shares(
        shares, limit_price, balance_cents, equity_cents, open_cost, config.MAX_EXPOSURE_PCT,
    )
    if shares < sized:
        logging.info(f"Capped {sized} -> {shares} shares (cash ${balance_cents/100:,.2f}, "
                     f"open cost ${open_cost/100:,.2f}, exposure cap {config.MAX_EXPOSURE_PCT}%).")
    if shares <= 0:
        logging.warning(
            f"Sized to {shares} shares for {favorite_ticker} "
            f"(equity ${equity_cents/100:,.2f}, ask {ask_price}¢, "
            f"fraction {config.BANKROLL_FRACTION:.0%}). Skipping."
        )
        # In the band but nothing to buy: unfunded account vs. capped by cash/exposure.
        return skip("in_band_unfunded" if sized <= 0 else "in_band_capped_to_zero")

    # expiration_ts in unix seconds — order auto-cancels at tip-off if unfilled
    clean_time = tip_off_utc.replace('Z', '+00:00')
    tip_off_dt = datetime.fromisoformat(clean_time)
    expiration_ts = int(tip_off_dt.timestamp())

    logging.info(
        f"BUY SIGNAL: {favorite_ticker} | Ask {ask_price}¢ | "
        f"Sizing {config.BANKROLL_FRACTION:.0%} of ${equity_cents/100:,.2f} equity = "
        f"{shares} shares @ limit {limit_price}¢"
    )

    ctx.update(shares=shares, limit_price=limit_price)
    decision_id = _decide(game, "buy", "in_band", dry_run, **ctx)

    if dry_run:
        logging.info(
            f"[DRY RUN] Would place order: {favorite_ticker} | {shares} shares "
            f"@ {limit_price}¢. No order sent."
        )
        return

    client_order_id = _client_order_id(game['game_id'], favorite_ticker)

    # Write-ahead: the intent is on disk BEFORE the order leaves the building. If we crash or time
    # out after the POST, the 'submitting' row is the evidence that an order may exist.
    order_pk, created = None, True
    if LEDGER is not None:
        try:
            order_pk, created = LEDGER.begin_order(decision_id, favorite_ticker, client_order_id,
                                                   shares, limit_price)
        except Exception:
            # A broken ledger must not block trading; the fixed client_order_id (Kalshi rejects a
            # duplicate) and the position check still stand between us and a double buy.
            logging.warning("Ledger write-ahead unavailable; relying on client_order_id.", exc_info=True)
    if not created:
        row = LEDGER.get_order_row(order_pk)
        if row is not None and row["outcome"] in ("submitting", "unconfirmed"):
            # An earlier evaluation sent (or may have sent) this order and never learned the
            # outcome. Ask Kalshi. NEVER send it again.
            logging.critical(f"Order {client_order_id} was in flight when we lost track of it; "
                             f"resolving with Kalshi instead of re-sending.")
            _event("CRITICAL", "order_in_flight", client_order_id)
            _resolve_order(client, portfolio, scheduler, order_pk, "startup")
            return "ordered"
        logging.critical(f"Order for {client_order_id} already recorded; not placing another.")
        _event("CRITICAL", "order_already_recorded", client_order_id)
        return "skipped"

    def _outcome(outcome):
        if order_pk is not None:
            try:
                LEDGER.set_order_outcome(order_pk, outcome)
            except Exception:
                logging.exception("ledger order outcome write failed")

    order = client.place_limit_order(
        ticker=favorite_ticker,
        count=shares,
        price=limit_price,
        action="buy",
        expiration_ts=expiration_ts,
        client_order_id=client_order_id,
    )

    if order == "INSUFFICIENT_FUNDS":
        logging.warning("INSUFFICIENT FUNDS. Cannot place order.")
        _outcome("insufficient_funds")
        _event("WARNING", "insufficient_funds", favorite_ticker)
        return "ordered"

    if order == "LOCATION_ATTESTATION":
        _outcome("blocked_location")
        _event("CRITICAL", "location_attestation",
               "Kalshi rejected the order: re-verify location in the Kalshi app/website")
        return "ordered"

    if isinstance(order, dict) and order.get('order_id'):
        if order_pk is not None:
            try:
                LEDGER.mark_placed(order_pk, order['order_id'], status=order.get('status'))
            except ValueError:
                # The row already moved on (e.g. a lookup raced us and wrote not_found): Kalshi's
                # reply is the truth, so apply it.
                try:
                    LEDGER.apply_order_state(order_pk, order['order_id'], order.get('status'), None, None, "response")
                except Exception:
                    logging.exception("ledger could not record the placed order")
            except Exception:
                logging.exception("ledger mark_placed failed")
        portfolio.add_position(favorite_ticker, limit_price, shares)
        logging.info(
            f"Order placed: {favorite_ticker} | {shares} shares @ {limit_price}¢ | "
            f"Expires at tip-off"
        )
        _schedule_fill_check(scheduler, client, portfolio, favorite_ticker, order['order_id'],
                             shares, tip_off_dt)
        return "ordered"

    # Anything else (timeout, connection error, malformed reply, HTTP 409 duplicate): the order MAY
    # exist. Do not guess and do not re-send: ask Kalshi.
    duplicate = order == "DUPLICATE_ORDER"
    logging.critical(f"Order outcome UNKNOWN for {favorite_ticker} ({'duplicate' if duplicate else 'no usable reply'}); "
                     f"asking Kalshi whether it landed.")
    _outcome("unconfirmed")
    _event("CRITICAL", "duplicate_order" if duplicate else "order_failed",
           f"{favorite_ticker}: outcome unknown, resolving with Kalshi (see trade_bot.log for the server response)")
    if order_pk is not None:
        if not duplicate and config.ORDER_LOOKUP_DELAY_SECONDS > 0:
            # A POST that timed out on our side may still be processing on Kalshi's: let it settle
            # before the first lookup (a miss now is provisional anyway; see _resolve_order).
            time.sleep(config.ORDER_LOOKUP_DELAY_SECONDS)
        _resolve_order(client, portfolio, scheduler, order_pk, "lookup")
    else:
        _reconcile_ambiguous_order(client, portfolio, favorite_ticker, shares, limit_price)
    return "ordered"


# --- Daily schedule setup ---

def _register_job(scheduler, client, portfolio, high_water, game_entry, trigger_dt, dry_run):
    scheduler.add_job(
        pre_game_job,
        trigger=DateTrigger(run_date=trigger_dt),
        args=[client, portfolio, high_water, scheduler, game_entry, dry_run],
        id=game_entry['game_id'],
        replace_existing=True,
        misfire_grace_time=config.PRE_GAME_MISFIRE_GRACE_SECONDS,
        coalesce=True,
    )
    logging.info(
        f"Scheduled: {game_entry['away_team']} @ {game_entry['home_team']} "
        f"triggers at {trigger_dt.isoformat()} UTC"
    )


def setup_daily_schedule(client, portfolio, high_water, scheduler, dry_run=True):
    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    now = datetime.now(timezone.utc)

    existing = load_schedule()

    # On restart with an existing schedule for today, recover unfired jobs
    if existing.get('date') == today:
        logging.info("Recovering schedule from disk...")
        recovered = 0
        for game_id, game in existing.get('games', {}).items():
            if game.get('fired'):
                continue

            trigger_dt = datetime.fromisoformat(game['trigger_time_utc'])
            tip_off_dt = datetime.fromisoformat(game['tip_off_utc'].replace('Z', '+00:00'))

            if trigger_dt > now:
                _register_job(scheduler, client, portfolio, high_water, game, trigger_dt, dry_run)
                recovered += 1
            elif tip_off_dt > now:
                # Missed the T-15 window but game hasn't started — fire immediately
                logging.info(f"Missed T-15 for {game['away_team']} @ {game['home_team']}. Firing now.")
                pre_game_job(client, portfolio, high_water, scheduler, game, dry_run)
                recovered += 1

        logging.info(f"Recovered {recovered} jobs from disk.")
        return

    # Fresh day — fetch schedule and build new job list
    games_today = nba_scheduler.get_schedule(client)
    if games_today is None:
        # A failed fetch is NOT "no games": never persist an empty day for it.
        logging.critical("Schedule fetch FAILED -- no games will be scheduled until it succeeds. "
                         f"Retrying in {SCHEDULE_RETRY_MINUTES} min.")
        _event("CRITICAL", "schedule_fetch_failed", "NBA schedule unavailable; retrying")
        scheduler.add_job(
            setup_daily_schedule, trigger=DateTrigger(run_date=now + timedelta(minutes=SCHEDULE_RETRY_MINUTES)),
            args=[client, portfolio, high_water, scheduler, dry_run],
            id='schedule-retry', replace_existing=True,
            misfire_grace_time=config.SCHEDULE_RETRY_MISFIRE_GRACE_SECONDS, coalesce=True)
        return
    if not games_today:
        logging.info("No NBA games today.")
        save_schedule({'date': today, 'games': {}})
        return

    open_markets = client.fetch_nba_markets()
    open_tricodes = {m['ticker'].split('-')[-1] for m in open_markets}

    schedule = {'date': today, 'games': {}}
    scheduled = 0

    for game in games_today:
        home_tri = game['homeTeam']['teamTricode']
        away_tri = game['awayTeam']['teamTricode']
        tip_off_utc = game.get('gameTimeUTC')

        if not tip_off_utc:
            continue

        # Skip games whose Kalshi markets aren't open yet
        if home_tri not in open_tricodes or away_tri not in open_tricodes:
            logging.info(f"Skipping {away_tri} @ {home_tri}: Kalshi markets not open.")
            continue

        clean_time = tip_off_utc.replace('Z', '+00:00')
        tip_off_dt = datetime.fromisoformat(clean_time)
        trigger_dt = tip_off_dt - timedelta(minutes=15)

        # Keyed by the game's US/Eastern date, not the UTC run date, so the same game
        # keeps the same id (and client_order_id) across the midnight-UTC rollover.
        game_id = f"{tip_off_dt.astimezone(EASTERN).date().isoformat()}-{home_tri}-{away_tri}"
        # The schedule is rebuilt each day and the lookahead window overlaps, so carry
        # over games already handled -- never evaluate (or order) the same game twice.
        already_fired = existing.get('games', {}).get(game_id, {}).get('fired', False)
        game_entry = {
            'game_id': game_id,
            'home_team': home_tri,
            'away_team': away_tri,
            'tip_off_utc': tip_off_utc,
            'trigger_time_utc': trigger_dt.isoformat(),
            'fired': already_fired
        }
        schedule['games'][game_id] = game_entry
        if already_fired:
            logging.info(f"Skipping {away_tri} @ {home_tri}: already handled earlier.")
            continue

        if trigger_dt <= now:
            if tip_off_dt > now:
                logging.info(f"Already past T-15 for {away_tri} @ {home_tri}. Firing immediately.")
                save_schedule(schedule)
                pre_game_job(client, portfolio, high_water, scheduler, game_entry, dry_run)
                if not dry_run:
                    schedule['games'][game_id]['fired'] = True
                scheduled += 1
        else:
            _register_job(scheduler, client, portfolio, high_water, game_entry, trigger_dt, dry_run)
            scheduled += 1

    save_schedule(schedule)
    logging.info(f"Daily setup complete. {scheduled} game(s) scheduled.")


# --- Settlement + health ---

def settle_paper_trades(client):
    """Settle in-band DRY-RUN decisions as one-contract paper trades.

    This measures the live edge (actual results vs the win rate the pre-game price
    implied) without any money at risk. Assumes a fill at the limit price, which a
    real resting order might not get.
    """
    if LEDGER is None:
        return 0
    done = 0
    for row in LEDGER.unsettled_paper_candidates():
        try:
            market = client.get_market(row["ticker"])
            result = (market or {}).get("result")
            if result not in ("yes", "no"):
                continue
            pnl = LEDGER.record_paper_trade(row["id"], row["ticker"], row["entry_cents"], result)
            logging.info(f"PAPER SETTLED {row['ticker']}: {result.upper()} won @ {row['entry_cents']}c, pnl {pnl}c")
            done += 1
        except Exception:
            logging.exception(f"paper settlement failed for decision {row['id']}")
    return done


def sync_fills(client, adopt=True):
    """Mirror Kalshi's fills into the ledger (idempotent) and attribute fills of orders the ledger
    has no order_id for.

    A fill whose order_id the ledger doesn't know is attributed, in this order, to: (1) an unresolved
    row for the same ticker (the order we lost track of), (2) a position-adopted row that has no
    order_id yet, and only then (3) a NEW adopted row (`adopt=False` skips this: a dry run must not
    book trades into the ledger). Every route ends with ONE ledger row per trade.
    """
    if LEDGER is None:
        return 0
    since = datetime.now(timezone.utc) - timedelta(days=FILL_SYNC_LOOKBACK_DAYS)
    fills = client.list_fills(min_ts=since)
    inserted = LEDGER.upsert_fills(fills)

    unresolved = LEDGER.unresolved_orders_by_ticker()
    by_order = {}
    for f in fills:
        oid, ticker = f.get("order_id"), f.get("ticker") or ""
        if oid and ticker.startswith(NBA_SERIES_PREFIX):
            by_order.setdefault(oid, []).append(f)
    for oid, fs in by_order.items():
        if LEDGER.order_by_order_id(oid) is not None:
            continue
        ticker = fs[0]["ticker"]
        total = sum(f["count"] for f in fs)
        if total <= 0:
            continue
        avg = sum(f["count"] * f["price_cents"] for f in fs) / total

        pending = unresolved.get(ticker)
        if pending:                                         # (1) the order we lost track of
            LEDGER.apply_order_state(pending[0]["id"], oid, None, total, avg, "lookup")
            unresolved[ticker] = pending[1:]
            _event("WARNING", "order_attributed_from_fills",
                   f"{ticker}: fills under order {oid} belong to an order that was unconfirmed")
            continue
        bare = [r for r in LEDGER.open_orders() if r["ticker"] == ticker and not r["order_id"]]
        if bare:                                            # (2) position-adopted, id now known
            if LEDGER.attach_order_id(bare[0]["id"], oid):
                continue
        if not adopt:
            continue
        LEDGER.adopt_order(ticker, oid, total, avg, limit_price=round(avg))   # (3) genuinely new
        _event("ERROR", "order_adopted_from_fills",
               f"{ticker}: Kalshi filled {total} contracts under order {oid} that the ledger had no "
               f"record of; adopted so it settles")
    return inserted


def _note_unsettleable(row, why):
    """Waiting is fine for a while. An order that stays unsettleable must become visible."""
    key = row["id"]
    ts = _parse_utc(row["ts"])
    old_enough = ts is not None and (datetime.now(timezone.utc) - ts) > timedelta(hours=SETTLE_STUCK_HOURS)
    if old_enough and key not in _SETTLE_STUCK_REPORTED:
        _SETTLE_STUCK_REPORTED.add(key)
        _event("ERROR", "settlement_stuck",
               f"{row['ticker']} order {row['order_id'] or row['client_order_id']}: cannot settle ({why}) "
               f"{SETTLE_STUCK_HOURS}h+ after it was placed; check it on Kalshi")


def settle_open_orders(client, adopt=True):
    """Write realized PnL, from Kalshi's actual fills, for orders whose market has resolved.

    Never books from a possibly partial picture: if the fill sync failed this cycle, or the stored
    fills add up to fewer contracts than the order says it filled, it waits. Never settles an order
    as 'unfilled' unless Kalshi says it ended unfilled. Never settles when the ledger shows two open
    rows for one ticker (a double-booked trade needs a human, not a second settlement).
    """
    if LEDGER is None:
        return 0
    sync_ok = True
    try:
        sync_fills(client, adopt=adopt)
    except Exception as e:
        sync_ok = False
        logging.error(f"Fill sync failed ({e}); not settling this cycle.")
        _event("WARNING", "fill_sync_failed", str(e))
    open_rows = LEDGER.open_orders()
    per_ticker = {}
    for r in open_rows:
        per_ticker[r["ticker"]] = per_ticker.get(r["ticker"], 0) + 1
    done = 0
    for row in open_rows:
        try:
            if per_ticker[row["ticker"]] > 1:
                if row["ticker"] not in _DUP_REPORTED:
                    _DUP_REPORTED.add(row["ticker"])
                    _event("ERROR", "duplicate_ledger_rows",
                           f"{row['ticker']} has {per_ticker[row['ticker']]} open ledger rows for one trade; "
                           f"not settling any of them until this is fixed by hand")
                continue
            market = client.get_market(row["ticker"])
            result = (market or {}).get("result")
            if result not in ("yes", "no"):
                continue
            if not sync_ok:
                continue                      # never settle from a fill set we could not refresh
            oid = row["order_id"] if row["order_id"] and not str(row["order_id"]).startswith("adopted:") else None
            od = client.get_order(oid) if oid else None
            fills = LEDGER.fills_for_order(oid) if oid else []
            if fills:
                stored = sum(f["count"] for f in fills)
                if od is not None and (od.get("filled") or 0) > stored:
                    why = f"fills stored for {stored} contracts but Kalshi says {od.get('filled')} filled"
                    logging.warning(f"{row['ticker']}: {why}; waiting for the rest.")
                    _note_unsettleable(row, why)
                    continue
            else:
                # No stored fills. Learn the fill count from the order itself, but never settle as
                # 'unfilled' unless Kalshi says it ended unfilled: a missing field parses as 0 and
                # would erase a real trade from the PnL.
                if od is not None:
                    LEDGER.apply_order_state(row["id"], oid, od.get("status"), od.get("filled"),
                                             od.get("avg_fill_cents"), "lookup")
                    row = LEDGER.get_order_row(row["id"])
                ended_unfilled = (row["status"] or "").lower() in ("canceled", "cancelled", "expired")
                if (row["filled"] or 0) == 0 and not ended_unfilled:
                    why = "no fills stored and the order is not confirmed unfilled"
                    logging.warning(f"{row['ticker']} resolved but order {oid}: {why}; waiting.")
                    _note_unsettleable(row, why)
                    continue
            try:
                pnl = LEDGER.record_settlement_from_fills(row["id"], result)
            except ValueError as e:           # e.g. a fill side we cannot interpret
                logging.error(f"Cannot settle {row['ticker']}: {e}")
                _event("ERROR", "settlement_unsupported_fill", f"{row['ticker']}: {e}")
                continue
            logging.info(f"SETTLED {row['ticker']}: {result.upper()} won, pnl {pnl}c "
                         f"({'fills' if fills else 'computed fallback'})")
            done += 1
        except Exception:
            logging.exception(f"settlement check failed for order {row['id']}")
    return done


def _register_health_job(scheduler, client, portfolio, high_water, dry_run=False):
    scheduler.add_job(health_job, 'interval', minutes=30,
                      args=[client, portfolio, high_water, dry_run],
                      id='health', replace_existing=True,
                      misfire_grace_time=config.HEALTH_MISFIRE_GRACE_SECONDS, coalesce=True)


def health_job(client, portfolio, high_water, dry_run=False):
    """Periodic: heartbeat, equity snapshot from Kalshi's own numbers, settle finished games."""
    try:
        balance = client.get_balance()
        healthy = balance is not None
        if balance is None:
            _event("ERROR", "balance_fetch_failed", "health job")
        else:
            # Everything that reads or changes positions and orders happens under the trade lock, so
            # it cannot interleave with an order being placed: (1) resolve orders whose outcome is
            # unknown, FIRST, so positions and fills then attach to them instead of being adopted as
            # new trades; (2) one Kalshi positions fetch to clear settled lots (G1), adopt positions
            # we never knew about (G4), and price equity from Kalshi's own numbers.
            synced, positions = False, {}
            if _TRADE_LOCK.acquire(timeout=HEALTH_LOCK_TIMEOUT_SECONDS):
                try:
                    if LEDGER is not None and not dry_run:
                        _resolve_pending(client, portfolio, SCHEDULER)
                    positions, _d, _a = _sync_positions(client, portfolio, adopt=not dry_run,
                                                        scheduler=SCHEDULER)
                    synced = True
                except Exception as e:
                    healthy = False
                    logging.error(f"Health-job reconcile failed ({e}); not recording equity/peak this cycle.")
                    _event("ERROR", "reconcile_failed", f"health job: {e}")
                finally:
                    _TRADE_LOCK.release()
            else:
                logging.warning("Health job: trade lock busy; skipping reconcile and the equity snapshot this cycle.")
            if synced:
                open_cost, unknown = portfolio.exposure_cents(positions)
                equity = balance + open_cost
                if unknown:
                    _event("WARNING", "equity_degraded",
                           f"Kalshi gave no cost for {unknown}; not recording a new drawdown peak")
                else:
                    high_water.update(equity, False)
                _snapshot(balance, open_cost, equity, high_water)
        settle_open_orders(client, adopt=not dry_run)
        settle_paper_trades(client)
        _event("INFO", "heartbeat", "ok" if healthy else "degraded")
        alerts.ping_healthcheck("ok" if healthy else "fail")
    except Exception:
        logging.exception("health job failed")
        _event("ERROR", "health_job_failed", "see trade_bot.log")
        alerts.ping_healthcheck("fail")


# --- Entry point ---

def _confirm_live(env_name, base_url, input_fn=input):
    """Require the operator to type LIVE before real-money trading starts."""
    print("\n" + "=" * 60)
    print(f"  REAL MONEY: Kalshi env = {env_name.upper()}  ({base_url})")
    print(f"  band {config.FAVORITE_PRICE_MIN}-{config.FAVORITE_PRICE_MAX}c | "
          f"{config.BANKROLL_FRACTION:.0%} of equity per trade | "
          f"exposure cap {config.MAX_EXPOSURE_PCT:.0f}% | "
          f"breaker {config.MAX_DRAWDOWN_PCT:.0f}% drawdown")
    print(f"  Stop new orders any time:  touch {config.KILL_SWITCH_FILE}")
    print("=" * 60)
    # Headless deployments (containers) have no terminal: BOT_LIVE_CONFIRM=LIVE in the
    # environment is the explicit, deliberate equivalent of typing LIVE.
    if os.environ.get("BOT_LIVE_CONFIRM") == "LIVE":
        print("  Confirmed via BOT_LIVE_CONFIRM=LIVE")
        return True
    try:
        return input_fn("Type LIVE to place real orders: ").strip() == "LIVE"
    except EOFError:
        return False


def check_clock_skew(client, dry_run):
    """Compare the local clock to Kalshi's (request signatures are timestamped).

    Warns above CLOCK_SKEW_WARN_SECONDS; in LIVE mode exits above
    CLOCK_SKEW_MAX_LIVE_SECONDS. A failed check only logs a warning. Returns the
    measured skew in seconds, or None if it could not be measured.
    """
    try:
        skew = client.clock_skew_seconds()
    except Exception as e:
        logging.warning(f"Clock-skew check failed ({e}); continuing without it.")
        return None
    if abs(skew) > config.CLOCK_SKEW_MAX_LIVE_SECONDS and not dry_run:
        msg = (f"Local clock is off by {skew:+.1f}s vs Kalshi (limit "
               f"{config.CLOCK_SKEW_MAX_LIVE_SECONDS:.0f}s in LIVE mode); signed requests "
               f"would be rejected. Fix NTP and restart. Refusing to start.")
        logging.critical(msg)
        _event("CRITICAL", "clock_skew", msg)
        sys.exit(msg)
    if abs(skew) > config.CLOCK_SKEW_WARN_SECONDS:
        logging.warning(f"Local clock is off by {skew:+.1f}s vs Kalshi "
                        f"(warn above {config.CLOCK_SKEW_WARN_SECONDS:.0f}s).")
        _event("WARNING", "clock_skew", f"{skew:+.1f}s")
    else:
        logging.info(f"Clock skew vs Kalshi: {skew:+.2f}s")
    return skew


def run_bot(dry_run=True):
    from kalshi.config import API_KEY_ID, BASE_URL, KALSHI_ENV, PRIVATE_KEY_PATH

    _configure_logging()

    # One bot per database: a second process would double-trade and race on state.
    lock = InstanceLock(config.LEDGER_FILE)
    try:
        lock.acquire()
    except InstanceLockError as e:
        logging.critical(str(e))
        sys.exit(f"Refusing to start: {e} Stop the other instance first.")
    try:
        _run_locked(dry_run, API_KEY_ID, BASE_URL, KALSHI_ENV, PRIVATE_KEY_PATH)
    finally:
        lock.release()


def _run_locked(dry_run, API_KEY_ID, BASE_URL, KALSHI_ENV, PRIVATE_KEY_PATH):
    logging.info(f"Mode: {'DRY RUN (no orders)' if dry_run else 'LIVE — REAL MONEY'} | env={KALSHI_ENV} ({BASE_URL})")
    client = KalshiClient(
        base_url=BASE_URL,
        key_id=API_KEY_ID,
        key_file_path=PRIVATE_KEY_PATH,
    )
    global LEDGER
    LEDGER = Ledger(config.LEDGER_FILE)
    try:
        check_clock_skew(client, dry_run)
        _event("INFO", "startup", f"mode={'dry' if dry_run else 'LIVE'} env={KALSHI_ENV} "
               f"band={config.FAVORITE_PRICE_MIN}-{config.FAVORITE_PRICE_MAX} frac={config.BANKROLL_FRACTION}")
        # Legacy JSON files are imported once into the (empty) SQLite tables, never modified.
        LEDGER.import_legacy_schedule(config.SCHEDULE_FILE)
        portfolio = Portfolio(filename=config.PORTFOLIO_FILE, ledger=LEDGER)
        high_water = HighWaterMark(filename=config.HIGH_WATER_FILE, ledger=LEDGER)
        _run_loop(client, portfolio, high_water, dry_run)
    finally:
        LEDGER.close()
        LEDGER = None


def _on_scheduler_event(event):
    """A job that missed its window (machine asleep, overloaded) or crashed must be visible.

    APScheduler drops a missed job silently otherwise: a missed pre-game job is a game we never
    looked at, and nothing in the ledger would say so.
    """
    if event.code == EVENT_JOB_MISSED:
        _event("ERROR", "job_missed", f"job {event.job_id} missed its run window (machine asleep or overloaded?)")
    else:
        _event("ERROR", "job_error", f"job {event.job_id} raised: {getattr(event, 'exception', None)}")


def _run_loop(client, portfolio, high_water, dry_run):
    global SCHEDULER
    # Initialize high-water mark on first run.
    initial_balance = client.get_balance()
    if initial_balance is not None:
        high_water.update(initial_balance, portfolio.has_any_positions())
        logging.info(
            f"Startup: balance ${initial_balance/100:,.2f}, "
            f"peak ${(high_water.peak() or 0)/100:,.2f}, "
            f"drawdown {high_water.drawdown_pct(initial_balance):.1f}%"
        )

    scheduler = BackgroundScheduler(timezone='UTC')
    scheduler.add_listener(_on_scheduler_event, EVENT_JOB_MISSED | EVENT_JOB_ERROR)
    scheduler.start()
    SCHEDULER = scheduler

    logging.info(
        f"NBA Price-Segment Bot Initialized | band {config.FAVORITE_PRICE_MIN}-"
        f"{config.FAVORITE_PRICE_MAX}¢ | sizing {config.BANKROLL_FRACTION:.0%} "
        f"of equity | max drawdown {config.MAX_DRAWDOWN_PCT}%"
    )

    # Before anything new is scheduled: learn the outcome of any order we lost track of, and
    # re-create the post-tip fill checks (the scheduler's job store is in memory).
    try:
        recover_orders(client, portfolio, scheduler)
    except Exception:
        logging.exception("startup order recovery failed")
        _event("ERROR", "startup_recovery_failed", "see trade_bot.log")

    health_job(client, portfolio, high_water, dry_run)
    _register_health_job(scheduler, client, portfolio, high_water, dry_run)

    setup_daily_schedule(client, portfolio, high_water, scheduler, dry_run)

    # Daily re-plan at 00:05 UTC as a wall-clock cron job, NOT a sleep loop: time.sleep stops
    # counting while a laptop sleeps, which delayed the rollover by ~16h. A cron job also catches
    # up (within the grace window) when the machine wakes.
    scheduler.add_job(
        setup_daily_schedule, CronTrigger(hour=0, minute=5, timezone='UTC'),
        args=[client, portfolio, high_water, scheduler, dry_run],
        id='rollover', replace_existing=True, misfire_grace_time=3600, coalesce=True,
    )

    crashed = False
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        logging.warning("Bot stopped manually.")
    except Exception as e:
        logging.error(f"CRITICAL CRASH: {e}", exc_info=True)
        _event("CRITICAL", "bot_crash", f"{type(e).__name__}: {e}")
        crashed = True
    finally:
        scheduler.shutdown()
        SCHEDULER = None
        alerts.flush()
    if crashed:
        sys.exit(1)                      # non-zero so the supervisor / healthcheck sees it


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="NBA price-segment bot")
    parser.add_argument("--live", action="store_true",
                        help="Place real orders. Default is dry-run.")
    args = parser.parse_args()
    if args.live:
        from kalshi.config import BASE_URL, KALSHI_ENV
        if not _confirm_live(KALSHI_ENV, BASE_URL):
            sys.exit("Live trading not confirmed -- exiting without placing orders.")
    run_bot(dry_run=not args.live)

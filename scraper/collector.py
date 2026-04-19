import logging
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from . import config
from . import db
from .db import iso_to_ts
from .client import ScraperClient

logger = logging.getLogger(__name__)

# One-time sys.path setup for importing from data_retrieval
_PROJECT_ROOT = os.path.join(os.path.dirname(__file__), "..")
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)


class Collector:
    def __init__(self):
        self.client = ScraperClient()
        self.active_tickers = []
        self.trade_cursors = {}  # market_ticker -> last seen unix ts
        self._schedule_cache = None
        self._pool = ThreadPoolExecutor(max_workers=config.POLL_WORKERS)

    def shutdown_pool(self):
        self._pool.shutdown(wait=False)

    # ---- NBA schedule (cached) ----

    def _get_schedule(self):
        if self._schedule_cache is None:
            try:
                from data_retrieval.tip_off_time import get_season_schedule_map
                self._schedule_cache = get_season_schedule_map()
            except Exception as exc:
                logger.warning("Failed to fetch NBA schedule: %s", exc)
        return self._schedule_cache

    # ---- Market discovery ----

    def refresh_active_markets(self):
        try:
            markets = self.client.fetch_markets(status="open")
        except Exception as exc:
            logger.error("Failed to fetch open markets: %s", exc)
            return

        db.upsert_markets_batch(markets)

        known_events = db.get_known_event_tickers()
        for market in markets:
            event_ticker = market.get("event_ticker")
            if event_ticker and event_ticker not in known_events:
                self._index_event(event_ticker)
                known_events.add(event_ticker)

        self.active_tickers = [m["ticker"] for m in markets if m.get("ticker")]
        logger.info("Active markets: %d", len(self.active_tickers))

    def _index_event(self, event_ticker):
        try:
            from data_retrieval.tip_off_time import parse_event_ticker
            from datetime import datetime

            target_date, matchup = parse_event_ticker(event_ticker)
            if not target_date or not matchup:
                return

            schedule = self._get_schedule()
            if not schedule:
                return

            for date_entry in schedule.get("gameDates", []):
                game_date_str = date_entry.get("gameDate", "")
                try:
                    nba_date = datetime.strptime(game_date_str[:10], "%m/%d/%Y").date()
                except ValueError:
                    continue
                if nba_date != target_date:
                    continue
                for game in date_entry.get("games", []):
                    h = game.get("homeTeam", {}).get("teamTricode")
                    a = game.get("awayTeam", {}).get("teamTricode")
                    if not h or not a:
                        continue
                    if tuple(sorted((h, a))) == matchup:
                        db.upsert_event(
                            event_ticker, h, a,
                            game.get("gameDateTimeUTC"),
                            game.get("gameLabel"),
                        )
                        return
        except Exception as exc:
            logger.warning("Could not index event %s: %s", event_ticker, exc)

    # ---- Orderbook polling ----

    def poll_all_orderbooks(self):
        if not self.active_tickers:
            return

        captured_at = int(time.time())
        snapshots = []
        empty_count = 0

        def fetch_one(ticker):
            try:
                return ticker, self.client.fetch_orderbook(ticker)
            except Exception as exc:
                logger.warning("Orderbook fetch failed for %s: %s", ticker, exc)
                return ticker, None

        futures = {self._pool.submit(fetch_one, t): t for t in self.active_tickers}
        for future in as_completed(futures):
            ticker, book = future.result()
            if book is None:
                continue
            if not book["yes"] and not book["no"]:
                empty_count += 1
                continue
            snapshots.append((ticker, captured_at, book["yes"], book["no"]))

        if snapshots:
            db.insert_orderbook_snapshots_batch(snapshots)

        if empty_count:
            logger.warning(
                "Orderbook poll: %d/%d markets returned empty books",
                empty_count, len(self.active_tickers),
            )
        logger.debug("Orderbook snapshots: %d/%d", len(snapshots), len(self.active_tickers))

    # ---- Trade polling ----

    def poll_new_trades(self):
        if not self.active_tickers:
            return

        # Build cursor map on main thread before dispatching
        cursor_snapshot = {}
        for ticker in self.active_tickers:
            cursor_snapshot[ticker] = (
                self.trade_cursors.get(ticker) or db.get_max_trade_ts(ticker)
            )

        def fetch_one(ticker):
            min_ts = cursor_snapshot[ticker]
            # No +1: INSERT OR IGNORE deduplicates by trade_id.
            # Adding +1 would skip trades sharing the same second as the cursor.
            try:
                return ticker, self.client.fetch_trades(ticker, min_ts=min_ts or None)
            except Exception as exc:
                logger.warning("Trade fetch failed for %s: %s", ticker, exc)
                return ticker, []

        futures = {self._pool.submit(fetch_one, t): t for t in self.active_tickers}
        all_trades = []
        new_cursors = {}

        for future in as_completed(futures):
            ticker, trades = future.result()
            if not trades:
                continue
            all_trades.extend(trades)
            max_ts = max(
                (t.get("ts") or iso_to_ts(t.get("created_time")) or 0)
                for t in trades
            )
            if max_ts:
                new_cursors[ticker] = max_ts

        if all_trades:
            db.insert_trades_batch(all_trades)
            logger.info("New trades inserted: %d", len(all_trades))

        # Update cursors on main thread
        self.trade_cursors.update(new_cursors)

    # ---- Settlement detection ----

    def check_settlements(self):
        try:
            settled = self.client.fetch_markets(status="settled")
        except Exception as exc:
            logger.error("Failed to fetch settled markets: %s", exc)
            return

        if settled:
            db.upsert_markets_batch(settled)

        pending_events = db.get_unsettled_event_tickers()
        for event_ticker in pending_events:
            self._record_settled_game(event_ticker)

    def _record_settled_game(self, event_ticker):
        markets = db.get_markets_for_event(event_ticker)
        if len(markets) != 2:
            logger.warning(
                "Event %s has %d markets, expected 2 — skipping settlement",
                event_ticker, len(markets),
            )
            return

        m0, m1 = markets[0], markets[1]

        # Label by actual result: winner is the market with result='yes'
        if m0.get("result") == "yes":
            winner, loser = m0, m1
        elif m1.get("result") == "yes":
            winner, loser = m1, m0
        else:
            logger.warning("Event %s: neither market has result='yes'", event_ticker)
            winner, loser = m0, m1

        event = db.get_event(event_ticker) or {}

        db.insert_settled_game({
            "event_ticker": event_ticker,
            "home_team": event.get("home_team"),
            "away_team": event.get("away_team"),
            "scheduled_tipoff_utc": event.get("scheduled_tipoff_utc"),
            "game_label": event.get("game_label"),
            "winner_ticker": winner.get("ticker"),
            "winner_team": winner.get("yes_sub_title"),
            "loser_ticker": loser.get("ticker"),
            "loser_team": loser.get("yes_sub_title"),
            "settled_at": int(time.time()),
        })
        logger.info("Settled game recorded: %s", event_ticker)

    # ---- Position limit check (one-time) ----

    def check_position_limit(self):
        if not self.active_tickers:
            logger.warning("No active markets to check position limits")
            return

        ticker = self.active_tickers[0]
        try:
            details = self.client.fetch_market_details(ticker)
            market = details.get("market", details)
            print(f"\n=== POSITION LIMIT CHECK ===")
            print(f"Market: {ticker}")
            for key in sorted(market.keys()):
                if "limit" in key.lower() or "position" in key.lower() or "max" in key.lower():
                    print(f"  {key}: {market[key]}")
            print(f"============================\n")
        except Exception as exc:
            logger.error("Position limit check failed: %s", exc)

    # ---- Maintenance ----

    def run_maintenance(self):
        try:
            db.run_incremental_vacuum()
        except Exception as exc:
            logger.warning("Incremental vacuum failed: %s", exc)

    # ---- Main loop ----

    def run(self, shutdown_event):
        db.init_schema()
        logger.info("Database initialized at %s", config.DB_PATH)

        self.refresh_active_markets()
        self.check_position_limit()

        tick = 0
        while not shutdown_event.is_set():
            loop_start = time.time()

            if tick > 0 and tick % config.MARKET_REFRESH_TICKS == 0:
                self.refresh_active_markets()

            self.poll_all_orderbooks()
            self.poll_new_trades()

            if tick % config.SETTLEMENT_CHECK_TICKS == 0:
                self.check_settlements()

            # Run incremental vacuum once per hour
            if tick > 0 and tick % 720 == 0:
                self.run_maintenance()

            tick += 1

            elapsed = time.time() - loop_start
            sleep_time = max(0, config.TICK_INTERVAL - elapsed)
            if sleep_time > 0:
                shutdown_event.wait(sleep_time)

        self.shutdown_pool()
        db.close_connection()
        logger.info("Collector stopped")

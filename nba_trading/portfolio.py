"""Portfolio + high-water-mark tracking for circuit-breaker risk control.

Two pieces of persisted state, both stored in the bot's SQLite database (the
same file as the trade ledger, tables `portfolio_lots` and `kv`):
  - Portfolio: open positions keyed by Kalshi ticker (tax-lot style)
  - HighWaterMark: the all-time peak balance, used to compute drawdown

`filename` on each class is the LEGACY JSON file: if the SQLite state is empty
and the file exists it is imported once (and never modified or deleted).

The high-water mark only updates when the bot has no open positions, so that
the dip in cash from placing an order doesn't look like a drawdown.
"""
import functools
import logging
import threading
import time

from nba_trading.ledger import Ledger


def _locked(method):
    """Run a method under the instance's RLock. Games with the same tip-off are evaluated on
    parallel scheduler threads, and the health job runs on another; without this, one thread
    can iterate `_positions` while another mutates it, or read a half-updated peak."""
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return wrapper


def _as_ledger(ledger):
    """A Ledger, a path to one, or None (a throwaway in-memory database)."""
    if isinstance(ledger, Ledger):
        return ledger
    return Ledger(":memory:" if ledger is None else str(ledger))


class Portfolio:
    def __init__(self, filename=None, ledger=None):
        self.filename = filename          # legacy JSON, import-only
        self._lock = threading.RLock()
        self._db = _as_ledger(ledger)
        # Structure: { "TICKER": [ {"price": 20, "qty": 10, "time": time}, ... ] }
        self._positions = {}
        self._load_from_disk()

    # --- Position tracking ---

    @_locked
    def add_position(self, ticker, buy_price, shares):
        """Append a new tax lot to the ticker's history."""
        if ticker not in self._positions:
            self._positions[ticker] = []
        new_lot = {"price": buy_price, "qty": shares, "time": time.time()}
        self._positions[ticker].append(new_lot)
        self._save_to_disk()
        logging.info(f"Portfolio: Added Lot for {ticker}: {shares} shares @ {buy_price}¢")

    @_locked
    def has_position(self, ticker):
        return ticker in self._positions

    @_locked
    def has_any_positions(self):
        return bool(self._positions)

    @_locked
    def remove_position(self, ticker):
        if ticker in self._positions:
            del self._positions[ticker]
            self._save_to_disk()
            logging.info(f"Portfolio: Removed {ticker}")

    @_locked
    def update_position_qty(self, ticker, qty):
        if ticker in self._positions and self._positions[ticker]:
            self._positions[ticker][-1]['qty'] = qty
            self._save_to_disk()
            logging.info(f"Portfolio: Updated {ticker} qty to {qty}")

    @_locked
    def open_cost_cents(self):
        """Total cost basis of all open lots, in cents (qty * entry price)."""
        return sum(
            lot["price"] * lot["qty"]
            for lots in self._positions.values() for lot in lots
        )

    @_locked
    def exposure_cents(self, kalshi_positions):
        """Capital at risk, preferring Kalshi's own numbers over our local lots.

        `kalshi_positions`: {ticker: {"position": int, "exposure_cents": int | None}} for
        positions Kalshi says we hold. For each, Kalshi's cost basis (`exposure_cents`) is used;
        if Kalshi didn't give one, the local lot cost stands in. Local lots for tickers Kalshi
        does NOT yet show (a resting order that has not filled) still count: that money is
        committed. A position with no Kalshi exposure and no local lot has an UNKNOWN cost.

        Returns (total_cents, unknown_tickers). Callers must treat equity as degraded when
        `unknown_tickers` is non-empty (e.g. not record a new drawdown peak).
        """
        total, unknown = 0, []
        for ticker, pos in kalshi_positions.items():
            exposure = pos.get("exposure_cents")
            if exposure is not None:
                total += abs(int(exposure))
                continue
            lots = self._positions.get(ticker)
            if lots:
                total += sum(lot["price"] * lot["qty"] for lot in lots)
            else:
                unknown.append(ticker)
        for ticker, lots in self._positions.items():
            if ticker not in kalshi_positions:
                total += sum(lot["price"] * lot["qty"] for lot in lots)
        return total, unknown

    def tickers(self):
        """Tickers with local lots (a snapshot)."""
        with self._lock:
            return set(self._positions)

    @_locked
    def reconcile(self, kalshi_positions, grace_seconds=1800, now=None, grace_overrides=None):
        """Sync local lots with Kalshi's actual positions.

        `kalshi_positions` maps ticker -> contracts held (YES). A local ticker
        Kalshi no longer holds is dropped (it settled, or never filled), unless
        its newest lot is within `grace_seconds` -- a resting order has no
        position yet. A held quantity differing from the lots overwrites the
        last lot's qty. `grace_overrides` ({ticker: seconds}) replaces the grace for specific
        tickers -- a post-tip fill check uses {its_ticker: 0} so it never drops OTHER games'
        still-resting orders. Returns the list of dropped tickers.
        """
        grace_overrides = grace_overrides or {}
        now = time.time() if now is None else now
        dropped = []
        for ticker in list(self._positions):
            lots = self._positions[ticker]
            held = kalshi_positions.get(ticker, 0)
            newest = max((lot.get("time", 0) for lot in lots), default=0)
            if held <= 0:
                if now - newest >= grace_overrides.get(ticker, grace_seconds):
                    del self._positions[ticker]
                    dropped.append(ticker)
            else:
                local = sum(lot["qty"] for lot in lots)
                if held != local and lots:
                    lots[-1]["qty"] = max(lots[-1]["qty"] + held - local, 0)
        if dropped or kalshi_positions:
            self._save_to_disk()
        return dropped

    # --- Persistence (SQLite; every save is one atomic transaction) ---

    def _save_to_disk(self):
        try:
            self._db.replace_portfolio_lots(self._positions)
        except Exception as e:
            logging.error(f"Error saving portfolio: {e}")

    def _load_from_disk(self):
        try:
            if self.filename:
                self._db.import_legacy_portfolio(self.filename)
            self._positions = self._db.load_portfolio_lots()
        except Exception as e:
            logging.error(f"Error loading portfolio: {e}")
            self._positions = {}


class HighWaterMark:
    """Tracks the all-time peak cash balance for drawdown circuit breaking.

    Only updates the peak when the bot has no open positions, so that the
    cash dip from a pending order doesn't artificially raise then crash the
    drawdown calculation.
    """

    def __init__(self, filename=None, ledger=None):
        self.filename = filename          # legacy JSON, import-only
        self._lock = threading.RLock()
        self._db = _as_ledger(ledger)
        self._peak_cents = None
        self._load_from_disk()

    @_locked
    def update(self, current_cash_cents, has_open_positions):
        """Bump the peak if (a) we have no open positions AND (b) current cash
        exceeds the prior peak. Returns the (possibly unchanged) peak."""
        if current_cash_cents is None or current_cash_cents < 0:
            return self._peak_cents
        if has_open_positions:
            return self._peak_cents
        if self._peak_cents is None or current_cash_cents > self._peak_cents:
            self._peak_cents = int(current_cash_cents)
            self._save_to_disk()
            logging.info(f"HighWaterMark: new peak ${self._peak_cents/100:,.2f}")
        return self._peak_cents

    @_locked
    def peak(self):
        return self._peak_cents

    @_locked
    def drawdown_pct(self, current_cash_cents):
        """Return current drawdown as a percentage of peak (0-100).
        Returns 0.0 if no peak recorded yet."""
        if self._peak_cents is None or self._peak_cents <= 0:
            return 0.0
        if current_cash_cents is None or current_cash_cents >= self._peak_cents:
            return 0.0
        return (self._peak_cents - current_cash_cents) / self._peak_cents * 100.0

    @_locked
    def is_circuit_broken(self, current_cash_cents, max_drawdown_pct):
        """True if the drawdown exceeds the threshold — trading should halt."""
        return self.drawdown_pct(current_cash_cents) >= max_drawdown_pct

    # --- Persistence ---

    def _save_to_disk(self):
        try:
            self._db.set_peak_cents(self._peak_cents)
        except Exception as e:
            logging.error(f"Error saving high-water mark: {e}")

    def _load_from_disk(self):
        try:
            if self.filename:
                self._db.import_legacy_high_water(self.filename)
            self._peak_cents = self._db.get_peak_cents()
        except Exception as e:
            logging.error(f"Error loading high-water mark: {e}")

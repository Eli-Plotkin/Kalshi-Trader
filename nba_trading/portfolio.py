"""Portfolio + high-water-mark tracking for circuit-breaker risk control.

Two persisted state files:
  - PORTFOLIO_FILE: open positions keyed by Kalshi ticker (tax-lot style)
  - HIGH_WATER_FILE: the all-time peak cash balance, used to compute drawdown

The high-water mark only updates when the bot has no open positions, so that
the dip in cash from placing an order doesn't look like a drawdown.
"""
import json
import os
import logging
import time


class Portfolio:
    def __init__(self, filename="portfolio_data.json"):
        self.filename = filename
        # Structure: { "TICKER": [ {"price": 20, "qty": 10, "time": time}, ... ] }
        self._positions = {}
        self._load_from_disk()

    # --- Position tracking ---

    def add_position(self, ticker, buy_price, shares):
        """Append a new tax lot to the ticker's history."""
        if ticker not in self._positions:
            self._positions[ticker] = []
        new_lot = {"price": buy_price, "qty": shares, "time": time.time()}
        self._positions[ticker].append(new_lot)
        self._save_to_disk()
        logging.info(f"Portfolio: Added Lot for {ticker}: {shares} shares @ {buy_price}¢")

    def has_position(self, ticker):
        return ticker in self._positions

    def has_any_positions(self):
        return bool(self._positions)

    def remove_position(self, ticker):
        if ticker in self._positions:
            del self._positions[ticker]
            self._save_to_disk()
            logging.info(f"Portfolio: Removed {ticker}")

    def update_position_qty(self, ticker, qty):
        if ticker in self._positions and self._positions[ticker]:
            self._positions[ticker][-1]['qty'] = qty
            self._save_to_disk()
            logging.info(f"Portfolio: Updated {ticker} qty to {qty}")

    # --- Persistence ---

    def _save_to_disk(self):
        try:
            with open(self.filename, 'w') as f:
                json.dump(self._positions, f, indent=4)
        except Exception as e:
            logging.error(f"Error saving portfolio: {e}")

    def _load_from_disk(self):
        if os.path.exists(self.filename):
            try:
                with open(self.filename, 'r') as f:
                    self._positions = json.load(f)
            except Exception as e:
                logging.error(f"Error loading portfolio: {e}")
                self._positions = {}


class HighWaterMark:
    """Tracks the all-time peak cash balance for drawdown circuit breaking.

    Only updates the peak when the bot has no open positions, so that the
    cash dip from a pending order doesn't artificially raise then crash the
    drawdown calculation.
    """

    def __init__(self, filename="high_water_mark.json"):
        self.filename = filename
        self._peak_cents = None
        self._load_from_disk()

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

    def peak(self):
        return self._peak_cents

    def drawdown_pct(self, current_cash_cents):
        """Return current drawdown as a percentage of peak (0-100).
        Returns 0.0 if no peak recorded yet."""
        if self._peak_cents is None or self._peak_cents <= 0:
            return 0.0
        if current_cash_cents is None or current_cash_cents >= self._peak_cents:
            return 0.0
        return (self._peak_cents - current_cash_cents) / self._peak_cents * 100.0

    def is_circuit_broken(self, current_cash_cents, max_drawdown_pct):
        """True if the drawdown exceeds the threshold — trading should halt."""
        return self.drawdown_pct(current_cash_cents) >= max_drawdown_pct

    # --- Persistence ---

    def _save_to_disk(self):
        try:
            with open(self.filename, 'w') as f:
                json.dump({"peak_cents": self._peak_cents}, f, indent=4)
        except Exception as e:
            logging.error(f"Error saving high-water mark: {e}")

    def _load_from_disk(self):
        if os.path.exists(self.filename):
            try:
                with open(self.filename, 'r') as f:
                    data = json.load(f)
                    self._peak_cents = data.get("peak_cents")
            except Exception as e:
                logging.error(f"Error loading high-water mark: {e}")

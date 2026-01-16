import json
import os
import logging
import time

class Portfolio:
    def __init__(self, filename="portfolio_data.json"):
        self.filename = filename
        # Structure: { "TICKER": [ {"price": 20, "qty": 10, "time": time}, {"price": 22, "qty": 50, "time": time} ] }
        self._positions = {}
        self._load_from_disk()

    def add_position(self, ticker, buy_price, shares):
        """Adds a new tax lot to the ticker's history"""
        if ticker not in self._positions:
            self._positions[ticker] = []
        
        # Append new lot
        new_lot = {"price": buy_price, "qty": shares, "time": time.time()}
        self._positions[ticker].append(new_lot)
        
        self._save_to_disk()
        logging.info(f"Portfolio: Added Lot for {ticker}: {shares} shares @ {buy_price}¢")

    def has_position(self, ticker):
        return ticker in self._positions

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
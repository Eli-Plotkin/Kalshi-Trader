#!/usr/bin/env python3
"""
KXNBAGAME Data Collector

Continuously captures orderbook snapshots and trade tape for all active
Kalshi NBA Game Winner markets into a local SQLite database.

Usage:
    python -m scraper.run
"""

import logging
import os
import signal
import sys
import threading

from .collector import Collector

_LOG_PATH = os.path.join(os.path.dirname(__file__), "scraper.log")


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(_LOG_PATH, mode="a"),
        ],
    )
    logger = logging.getLogger("scraper")

    shutdown = threading.Event()

    def handle_signal(signum, frame):
        logger.info("Shutdown signal received (sig=%d), stopping...", signum)
        shutdown.set()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    logger.info("Starting KXNBAGAME data collector")
    collector = Collector()

    try:
        collector.run(shutdown)
    except Exception as exc:
        logger.exception("Collector crashed: %s", exc)
        sys.exit(1)

    logger.info("Collector stopped cleanly")


if __name__ == "__main__":
    main()

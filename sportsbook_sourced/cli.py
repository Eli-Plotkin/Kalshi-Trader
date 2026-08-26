from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import sample_data, storage
from .config import DEFAULT_SCANNER_CONFIG, DEFAULT_SOURCE_WEIGHTS
from .paper import PaperPortfolio
from .pipeline import run_scan

# Fixed per invocation -- portfolio state (cash/positions) is not persisted
# across CLI runs yet. Every scan starts from the same paper bankroll, which
# is fine for measuring edges but not yet a running account balance; closing
# that loop is future work alongside P1.7's settlement pass.
STARTING_PAPER_CASH_CENTS = 100_000  # $1,000

DEFAULT_KALSHI_BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"


def _dry_run_sources(league: str):
    if league != "nba":
        raise SystemExit(
            f"dry-run sample data is NBA only (got --league {league}); "
            f"pass --live to fetch real data for other leagues"
        )
    return [sample_data.sample_market()], [sample_data.sample_event()], sample_data.sample_odds()


def _live_sources(league: str):
    """Real odds + Kalshi market data. Fills are still simulated by
    `paper.py` -- no real order is ever placed by this package (see
    CLAUDE.md's P1.6 scope decision).

    Credentials are read directly from `os.environ`, not by importing
    `kalshi.config` -- that module raises at import time if credentials are
    missing, but also happily succeeds using whatever real `.env` file
    another package in this repo relies on. Checking here keeps a missing
    credential a clean, local `SystemExit` instead of a side effect of
    unrelated shared state.
    """
    odds_api_key = os.environ.get("ODDS_API_KEY")
    if not odds_api_key:
        raise SystemExit("--live requires the ODDS_API_KEY environment variable")

    api_key_id = os.environ.get("API_KEY_ID")
    private_key_path = os.environ.get("PRIVATE_KEY_PATH")
    if not api_key_id or not private_key_path:
        raise SystemExit(
            "--live requires Kalshi API credentials: set API_KEY_ID and "
            "PRIVATE_KEY_PATH environment variables"
        )

    from kalshi.client import KalshiClient

    from .kalshi_feed import list_sports_markets
    from .odds import TheOddsApiProvider

    base_url = os.environ.get("KALSHI_BASE_URL", DEFAULT_KALSHI_BASE_URL)
    odds_provider = TheOddsApiProvider(odds_api_key)
    events = odds_provider.list_events(league)
    odds = odds_provider.list_moneyline_odds(league)

    kalshi_client = KalshiClient(base_url, api_key_id, private_key_path)
    markets = list_sports_markets(kalshi_client=kalshi_client, league=league)
    return markets, events, odds


def scan(args: argparse.Namespace) -> int:
    dry_run = not args.live

    # Resolve data sources (and thus validate --live credentials, or the
    # dry-run league restriction) before touching any database connection --
    # a validation failure must not have the side effect of creating or
    # opening the real production DB file.
    if dry_run:
        markets, events, odds = _dry_run_sources(args.league)
        now = sample_data.SAMPLE_NOW
    else:
        markets, events, odds = _live_sources(args.league)
        now = None

    if args.db_path is not None:
        conn = storage.init_db(args.db_path)
        db_label = str(args.db_path)
    elif dry_run:
        conn = storage.init_db(":memory:")
        db_label = "in-memory (dry run; pass --db-path to persist)"
    else:
        conn = storage.init_db()
        db_label = str(storage.DB_PATH)

    portfolio = PaperPortfolio(cash_cents=STARTING_PAPER_CASH_CENTS, positions={})
    opportunities = run_scan(
        markets=markets, events=events, odds=odds, portfolio=portfolio,
        conn=conn, weights=DEFAULT_SOURCE_WEIGHTS, config=DEFAULT_SCANNER_CONFIG,
        now=now,
    )
    conn.close()

    payload = {
        "mode": "dry_run" if dry_run else "live",
        "league": args.league,
        "markets_examined": len(markets),
        "opportunities_found": len(opportunities),
        "tradeable": sum(1 for o in opportunities if o.action == "buy"),
        "db_path": db_label,
        "opportunities": [
            {
                "ticker": o.kalshi_ticker,
                "action": o.action,
                "side": o.side,
                "net_edge_cents": round(o.net_edge_cents, 2),
                "reason": o.reason,
            }
            for o in opportunities
        ],
    }
    print(json.dumps(payload, indent=2, default=str))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sportsbook_sourced")
    sub = parser.add_subparsers(dest="cmd", required=True)

    scan_parser = sub.add_parser("scan", help="Run the sportsbook-sourced edge scanner.")
    scan_parser.add_argument("--league", choices=("nba", "nfl"), default="nba")
    scan_parser.add_argument(
        "--live",
        action="store_true",
        default=False,
        help=(
            "Fetch real odds and Kalshi market data instead of the bundled "
            "dry-run sample. Fills are still simulated by paper.py -- this "
            "never places a real order (no broker/execution client exists "
            "in this package yet). Requires ODDS_API_KEY, API_KEY_ID, and "
            "PRIVATE_KEY_PATH to be set."
        ),
    )
    scan_parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help=(
            "Deprecated: dry-run is already the default and cannot be "
            "turned off with this flag. Kept only so existing invocations "
            "of this flag keep working; use --live to opt out of dry-run."
        ),
    )
    scan_parser.add_argument(
        "--db-path",
        type=Path,
        default=None,
        help=(
            "Where to persist scan results. Defaults to the real database "
            "for --live, and an isolated in-memory database for dry-run "
            "(so canned sample data never mixes with real trade history). "
            "Pass explicitly to override either default."
        ),
    )
    scan_parser.set_defaults(func=scan)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

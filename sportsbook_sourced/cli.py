from __future__ import annotations

import argparse
import json
import os
import sys

from . import storage
from .config import DEFAULT_SCANNER_CONFIG


def scan(args: argparse.Namespace) -> int:
    conn = storage.init_db()
    conn.close()
    payload = {
        "status": "scaffold_ready",
        "dry_run": args.dry_run,
        "league": args.league,
        "db_path": str(storage.DB_PATH),
        "scanner_config": DEFAULT_SCANNER_CONFIG,
        "odds_api_configured": bool(os.environ.get("ODDS_API_KEY")),
        "note": "External API wiring is intentionally not enabled in the scaffold command.",
    }
    print(json.dumps(payload, indent=2, default=str))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sportsbook_sourced")
    sub = parser.add_subparsers(dest="cmd", required=True)

    scan_parser = sub.add_parser("scan", help="Run the sportsbook-sourced edge scanner scaffold.")
    scan_parser.add_argument("--league", choices=("nba", "nfl"), default="nba")
    scan_parser.add_argument("--dry-run", action="store_true", default=True)
    scan_parser.set_defaults(func=scan)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())


"""
Day 2 dry-run entry point.

Runs ONE cycle against live Kalshi market data, calls real LLM stages
(triage / plan / framework / decider), but:

  - Uses a STUBBED research findings object (no web search yet — Day 3).
  - Does NOT execute orders. Step 7 logs `{"dry_run": true, "skipped": true}`.

Read the chain_log table afterward to see what each stage produced.

Usage:
  python -m agent_trader.dry_run
  python -m agent_trader.dry_run --top-n 3 --market-budget 0.30
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys

from kalshi.client import KalshiClient
from kalshi.config import API_KEY_ID, BASE_URL, PRIVATE_KEY_PATH

from . import orchestrator, runtime


log = logging.getLogger("agent_trader.dry_run")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--top-n", type=int, default=orchestrator.DEFAULT_TOP_N)
    parser.add_argument("--market-budget", type=float, default=orchestrator.DEFAULT_MARKET_BUDGET_USD)
    parser.add_argument("--cycle-budget", type=float, default=orchestrator.DEFAULT_CYCLE_BUDGET_USD)
    parser.add_argument("--min-volume", type=int, default=100)
    parser.add_argument("--min-hours", type=int, default=48)
    parser.add_argument("--live", action="store_true",
                        help="Actually execute orders. Default is dry-run.")
    parser.add_argument("--no-research", action="store_true",
                        help="Skip the web-search research stage; stub findings instead. "
                             "Useful for shaking out prompt shape cheaply.")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    anthropic_api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not anthropic_api_key:
        log.error("ANTHROPIC_API_KEY not set")
        return 2

    from anthropic import Anthropic
    anthropic_client = Anthropic(api_key=anthropic_api_key)
    kalshi_client = KalshiClient(BASE_URL, API_KEY_ID, PRIVATE_KEY_PATH)

    killswitch = runtime.Killswitch()
    runtime.install_signal_handlers()

    result = orchestrator.run_cycle(
        anthropic_client=anthropic_client,
        kalshi_client=kalshi_client,
        top_n=args.top_n,
        market_budget_usd=args.market_budget,
        cycle_budget_usd=args.cycle_budget,
        min_daily_volume=args.min_volume,
        min_hours_to_close=args.min_hours,
        dry_run=not args.live,
        skip_research=args.no_research,
        killswitch=killswitch,
    )
    print(json.dumps(result, indent=2, default=str))
    return 0 if result.get("status") == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())

"""
Reflection CLI.

Usage:
  python -m agent_trader.reflect --since 24h
  python -m agent_trader.reflect --since 7d --budget 3.00
  python -m agent_trader.reflect --since 2026-05-12T00:00:00+00:00

Reads the chain_log over the window, asks the reflection LLM for proposed
v(N+1) prompts, and writes them to data/proposed_prompts/. Activate by:

  cp data/proposed_prompts/research_plan_v2.md agent_trader/prompts/
  # then edit agent_trader/prompts/active.yaml -> research_plan: research_plan_v2.md
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys

from kalshi.client import KalshiClient
from kalshi.config import API_KEY_ID, BASE_URL, PRIVATE_KEY_PATH

from . import reflection, runtime


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--since", default="24h",
                        help="Window start: '24h', '7d', '30m', or ISO 8601.")
    parser.add_argument("--budget", type=float, default=1.50,
                        help="Max USD spend on the reflection LLM call.")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("ANTHROPIC_API_KEY not set", file=sys.stderr)
        return 2

    from anthropic import Anthropic
    anthropic_client = Anthropic(api_key=api_key)
    kalshi_client = KalshiClient(BASE_URL, API_KEY_ID, PRIVATE_KEY_PATH)

    since = reflection.parse_since_arg(args.since)
    killswitch = runtime.Killswitch()

    result = reflection.run_reflection(
        anthropic_client=anthropic_client,
        kalshi_client=kalshi_client,
        since=since,
        cycle_budget_usd=args.budget,
        killswitch=killswitch,
    )
    print(json.dumps({
        "cycles_in_window": result.cycles_in_window,
        "graded_decisions": result.graded_decisions,
        "proposals_written": result.proposals_written,
        "cost_usd": round(result.cost_usd, 4),
    }, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

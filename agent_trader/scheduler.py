"""
APScheduler entry point for the autonomous trading agent.

Runs two jobs:
  - cycle: every CYCLE_INTERVAL_MIN minutes, invokes orchestrator.run_cycle.
           max_instances=1 + coalesce=True so overlapping fires are dropped
           (the runtime.skip_cycle row records the skip).
  - reflection: once per day at REFLECTION_HOUR_UTC:MINUTE, invokes
           reflection.run_reflection over the last 24h.

Shared state:
  - one Anthropic client
  - one KalshiClient
  - one runtime.Killswitch (persists failure counters across cycles)
  - signal handlers installed once (SIGINT/SIGTERM -> shutdown_requested())

Start:
  python -m agent_trader.scheduler &
  # or, for foreground/dev:
  python -m agent_trader.scheduler --foreground

Stop:
  touch ~/.kalshi-agent.halt   # graceful: next cycle will hit killswitch
  kill <pid>                   # SIGTERM, current cycle finishes then exits

Live trading is OFF by default. Pass --live to enable order execution.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from kalshi.client import KalshiClient
from kalshi.config import API_KEY_ID, BASE_URL, PRIVATE_KEY_PATH

from . import orchestrator, reflection, runtime


log = logging.getLogger("agent_trader.scheduler")


DEFAULT_CYCLE_INTERVAL_MIN = 30
DEFAULT_REFLECTION_HOUR_UTC = 23
DEFAULT_REFLECTION_MINUTE_UTC = 55
DEFAULT_REFLECTION_BUDGET_USD = 1.50


def _make_cycle_job(*, anthropic_client, kalshi_client, killswitch, args):
    def _run():
        if runtime.shutdown_requested():
            log.info("shutdown_requested set; skipping cycle")
            return
        if killswitch.tripped:
            log.warning("killswitch already tripped (%s); skipping cycle", killswitch.trip_reason)
            return
        try:
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
            log.info("cycle complete: %s", result.get("status"))
        except Exception:
            log.exception("run_cycle raised")
    return _run


def _make_reflection_job(*, anthropic_client, kalshi_client, killswitch, args):
    def _run():
        if runtime.shutdown_requested():
            log.info("shutdown_requested set; skipping reflection")
            return
        if killswitch.tripped:
            log.warning("killswitch already tripped (%s); skipping reflection", killswitch.trip_reason)
            return
        try:
            since = datetime.now(timezone.utc) - timedelta(hours=24)
            result = reflection.run_reflection(
                anthropic_client=anthropic_client,
                kalshi_client=kalshi_client,
                since=since,
                cycle_budget_usd=args.reflection_budget,
                killswitch=killswitch,
            )
            log.info(
                "reflection complete: cycles=%d graded=%d proposals=%d cost=$%.4f",
                result.cycles_in_window,
                result.graded_decisions,
                result.proposals_written,
                result.cost_usd,
            )
        except Exception:
            log.exception("run_reflection raised")
    return _run


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cycle-interval-min", type=int, default=DEFAULT_CYCLE_INTERVAL_MIN)
    parser.add_argument("--reflection-hour-utc", type=int, default=DEFAULT_REFLECTION_HOUR_UTC)
    parser.add_argument("--reflection-minute-utc", type=int, default=DEFAULT_REFLECTION_MINUTE_UTC)
    parser.add_argument("--reflection-budget", type=float, default=DEFAULT_REFLECTION_BUDGET_USD)
    parser.add_argument("--top-n", type=int, default=orchestrator.DEFAULT_TOP_N)
    parser.add_argument("--market-budget", type=float, default=orchestrator.DEFAULT_MARKET_BUDGET_USD)
    parser.add_argument("--cycle-budget", type=float, default=orchestrator.DEFAULT_CYCLE_BUDGET_USD)
    parser.add_argument("--min-volume", type=int, default=100)
    parser.add_argument("--min-hours", type=int, default=48)
    parser.add_argument("--live", action="store_true",
                        help="Actually place orders. Default is dry-run.")
    parser.add_argument("--no-research", action="store_true",
                        help="Skip web search; use stub findings.")
    parser.add_argument("--run-once", action="store_true",
                        help="Run a single cycle and exit (no scheduler).")
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

    cycle_job = _make_cycle_job(
        anthropic_client=anthropic_client,
        kalshi_client=kalshi_client,
        killswitch=killswitch,
        args=args,
    )

    if args.run_once:
        cycle_job()
        return 0 if not killswitch.tripped else 1

    scheduler = BlockingScheduler(timezone="UTC")
    scheduler.add_job(
        cycle_job,
        trigger=IntervalTrigger(minutes=args.cycle_interval_min),
        id="cycle",
        max_instances=1,
        coalesce=True,
        next_run_time=datetime.now(timezone.utc),  # fire once immediately on startup
    )
    scheduler.add_job(
        _make_reflection_job(
            anthropic_client=anthropic_client,
            kalshi_client=kalshi_client,
            killswitch=killswitch,
            args=args,
        ),
        trigger=CronTrigger(hour=args.reflection_hour_utc, minute=args.reflection_minute_utc),
        id="reflection",
        max_instances=1,
        coalesce=True,
    )

    log.info(
        "scheduler starting: live=%s, cycle every %dm, reflection daily at %02d:%02d UTC",
        args.live, args.cycle_interval_min, args.reflection_hour_utc, args.reflection_minute_utc,
    )
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("scheduler stopping")
    return 0


if __name__ == "__main__":
    sys.exit(main())

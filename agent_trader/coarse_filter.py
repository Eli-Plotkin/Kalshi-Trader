"""
Layer 2 of the discovery funnel: per-market yes/no gate.

Architecture context:
  Layer 1 (code): discover_eligible_markets   ~10,000 → ~200-500 markets
  Layer 2 (LLM):  coarse_filter (this module) ~200-500 → ~20-50 markets
  Layer 3 (LLM):  triage (batch ranking)      ~20-50 → top N

Each market gets one cheap haiku call asking "is this worth a Sonnet pass?".
Calls run in parallel and share a cached system prompt, so 500 markets cost
~$0.20 total (haiku at $1/MTok input, system prompt amortized across reads).

The reason strings are what reflection.py reads to evolve this stage's prompt.
"""
from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from pydantic import ValidationError

from . import runtime
from .market_discovery import EligibleMarket
from .schemas import CoarseFilterDecision


log = logging.getLogger("agent_trader.coarse_filter")


MODEL_COARSE_FILTER = "claude-haiku-4-5-20251001"
# 4 keeps us under Anthropic's per-second burst ceiling for haiku while still
# finishing 500-market universes in ~30s. The SDK auto-retries 429s with
# exponential backoff if individual calls slip through.
DEFAULT_PARALLELISM = 4
DEFAULT_MAX_TOKENS = 256


@dataclass
class CoarseFilterResult:
    ticker: str
    keep: bool
    reason: str
    usage: Optional[dict]
    error: Optional[str]


def _market_payload(m: EligibleMarket, now: datetime) -> str:
    hours = (m.close_time - now).total_seconds() / 3600.0 if m.close_time else None
    return "INPUT:\n" + json.dumps(
        {
            "market": {
                "ticker": m.ticker,
                "title": m.title,
                "yes_bid_cents": m.yes_bid_cents,
                "yes_ask_cents": m.yes_ask_cents,
                "last_price_cents": m.last_price_cents,
                "volume_24h": m.volume_24h,
                "open_interest": m.open_interest,
                "hours_to_close": round(hours, 1) if hours is not None else None,
            },
        },
        default=str,
    )


def _filter_one(
    *,
    anthropic_client,
    system,
    market: EligibleMarket,
    user: str,
    cycle_budget: runtime.BudgetCounter,
) -> CoarseFilterResult:
    """Worker for a single market. Budget is shared across threads — the cap is
    a soft ceiling on aggregate spend, not a per-call gate."""
    local_budget = runtime.BudgetCounter(
        market_cap_usd=cycle_budget.cycle_cap_usd,
        cycle_cap_usd=cycle_budget.cycle_cap_usd,
        cycle_spent_usd=cycle_budget.cycle_spent_usd,
    )
    try:
        text, usage = runtime.call_llm(
            client=anthropic_client,
            model=MODEL_COARSE_FILTER,
            system=system,
            user=user,
            budget=local_budget,
            max_tokens=DEFAULT_MAX_TOKENS,
        )
        raw = runtime.parse_llm_json(text)
        decision = CoarseFilterDecision.model_validate(raw)
        return CoarseFilterResult(
            ticker=market.ticker,
            keep=decision.keep,
            reason=decision.reason,
            usage=usage,
            error=None,
        )
    except (runtime.MalformedLLMResponse, ValidationError) as e:
        # Fail-open: if the filter call fails, KEEP the market so a bug here
        # doesn't silently starve the pipeline. Triage gets the final say.
        return CoarseFilterResult(
            ticker=market.ticker,
            keep=True,
            reason=f"[filter_error_kept] {e}",
            usage=None,
            error=str(e),
        )
    except runtime.BudgetExhausted as e:
        return CoarseFilterResult(
            ticker=market.ticker,
            keep=False,
            reason=f"[budget_exhausted] {e}",
            usage=None,
            error=str(e),
        )


def run_coarse_filter(
    *,
    anthropic_client,
    system_prompt: str,
    cacheable: bool,
    assumptions: str,
    eligible: list[EligibleMarket],
    cycle_budget: runtime.BudgetCounter,
    parallelism: int = DEFAULT_PARALLELISM,
) -> tuple[list[EligibleMarket], list[CoarseFilterResult]]:
    """
    Run the layer-2 filter across `eligible` in parallel.

    Returns (kept_markets, all_results). `all_results` includes both kept and
    rejected entries so the orchestrator can log every decision to chain_log
    for reflection.
    """
    if not eligible:
        return [], []

    now = datetime.now(timezone.utc)
    system = runtime.build_system_block(
        system_prompt, cacheable=cacheable, prefix=assumptions
    )

    results: list[CoarseFilterResult] = []
    with ThreadPoolExecutor(max_workers=parallelism) as pool:
        future_to_ticker = {
            pool.submit(
                _filter_one,
                anthropic_client=anthropic_client,
                system=system,
                market=m,
                user=_market_payload(m, now),
                cycle_budget=cycle_budget,
            ): m
            for m in eligible
        }
        for future in as_completed(future_to_ticker):
            results.append(future.result())

    # Aggregate the cost from per-call usage and apply once to the shared
    # budget. Doing this here (rather than from worker threads) avoids races
    # on cycle_budget.cycle_spent_usd.
    total_cost = sum((r.usage or {}).get("cost_usd", 0.0) for r in results)
    cycle_budget.add(total_cost)

    kept_tickers = {r.ticker for r in results if r.keep}
    kept_markets = [m for m in eligible if m.ticker in kept_tickers]

    log.info(
        "coarse_filter: %d/%d kept, $%.4f spent across %d parallel calls",
        len(kept_markets), len(eligible), total_cost, len(results),
    )
    return kept_markets, results

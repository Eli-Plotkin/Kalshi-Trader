"""
Per-cycle orchestrator.

Pipeline per cycle:
  1. Reconcile portfolio.
  2. Discover eligible markets.
  3. Triage (haiku) → top N tickers.
  4. For each surfaced ticker:
       a. ResearchPlan      (sonnet, validated against schemas.ResearchPlan)
       b. DecisionFramework (sonnet, validated against schemas.DecisionFramework)
       c. Findings          (Day 2: STUBBED — real research lands Day 3)
       d. Benchmark         (research-sufficient gate folded into orchestrator)
       e. Decision          (opus, validated against schemas.Decision)
       f. Execute           (skipped when dry_run=True)
  5. Close cycle.

Budget caps + malformed-response counts feed the killswitch (runtime.Killswitch).
"""
from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import yaml
from pydantic import ValidationError

from . import coarse_filter, research_agent, runtime
from .executor import ExecutionResult, execute_decision
from .market_discovery import EligibleMarket, discover_eligible_markets
from .schemas import (
    Decision,
    DecisionFramework,
    Findings,
    ResearchPlan,
    TriageOutput,
)


log = logging.getLogger("agent_trader.orchestrator")


PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"
ACTIVE_YAML = PROMPTS_DIR / "active.yaml"


# ────────────────────────────────────────────────────────────────────────────
# Prompt loader
# ────────────────────────────────────────────────────────────────────────────

# Roles whose system prompts are stable enough to benefit from Anthropic
# ephemeral prompt caching. The 5-minute cache TTL is what matters here, not
# how often reflection rewrites a prompt — caching pays off any time the same
# system prefix is reused within the TTL. research_plan + decision_framework
# are called once per market with an identical system prompt, so the 5 markets
# in a cycle amortize the 1.25× write across 4 reads at 0.10×.
CACHEABLE_PROMPTS: frozenset[str] = frozenset({
    "assumptions",
    "coarse_filter",
    "triage",
    "research_plan",
    "decision_framework",
    "decider",
    "research_subagent",
    "reflection",
})


@dataclass
class PromptSet:
    pointers: dict[str, str]      # role → filename, e.g. {"triage": "triage_v1.md"}
    bodies: dict[str, str]        # role → file body
    cacheable: dict[str, bool]    # role → whether to send with cache_control


def load_prompts() -> PromptSet:
    pointers = yaml.safe_load(ACTIVE_YAML.read_text())
    bodies: dict[str, str] = {}
    cacheable: dict[str, bool] = {}
    for role, fname in pointers.items():
        path = PROMPTS_DIR / fname
        if not path.exists():
            log.warning("active prompt %s -> %s missing on disk; skipping body load", role, fname)
            continue
        bodies[role] = path.read_text()
        cacheable[role] = role in CACHEABLE_PROMPTS
    return PromptSet(pointers=pointers, bodies=bodies, cacheable=cacheable)


# ────────────────────────────────────────────────────────────────────────────
# Model assignments (cheap → expensive across the pipeline)
# ────────────────────────────────────────────────────────────────────────────

MODEL_TRIAGE = "claude-haiku-4-5-20251001"
MODEL_PLAN = "claude-sonnet-4-6"
MODEL_FRAMEWORK = "claude-sonnet-4-6"
MODEL_DECIDER = "claude-opus-4-7"

DEFAULT_TOP_N = 5
DEFAULT_MARKET_BUDGET_USD = 1.00
DEFAULT_CYCLE_BUDGET_USD = 3.00

# Per-market workers run in parallel. Each runs a Sonnet plan, framework, and
# research subagent (8-15 web_search calls) plus an Opus decider. With N
# concurrent markets we burst N × ~10 Anthropic calls in flight — keep N small
# to stay under burst RPM limits. Increase only after measuring 429 frequency.
DEFAULT_MAX_CONCURRENT_MARKETS = 3


# ────────────────────────────────────────────────────────────────────────────
# Validated LLM helpers
# ────────────────────────────────────────────────────────────────────────────

def _call_and_validate(
    *,
    anthropic_client,
    model: str,
    prompts: "PromptSet",
    role: str,
    user: str,
    budget: runtime.BudgetCounter,
    schema_model,
    killswitch: Optional[runtime.Killswitch] = None,
    max_tokens: int = 2048,
    assumptions: Optional[str] = None,
):
    system = runtime.build_system_block(
        prompts.bodies[role],
        cacheable=prompts.cacheable.get(role, False),
        prefix=assumptions,
    )
    text, usage = runtime.call_llm(
        client=anthropic_client,
        model=model,
        system=system,
        user=user,
        budget=budget,
        max_tokens=max_tokens,
    )
    try:
        raw = runtime.parse_llm_json(text)
        obj = schema_model.model_validate(raw)
    except (runtime.MalformedLLMResponse, ValidationError) as e:
        if killswitch:
            killswitch.note_agent_response(malformed=True)
        raise runtime.MalformedLLMResponse(f"{schema_model.__name__}: {e}; head={text[:300]!r}")
    if killswitch:
        killswitch.note_agent_response(malformed=False)
    return obj, usage


# ────────────────────────────────────────────────────────────────────────────
# Triage
# ────────────────────────────────────────────────────────────────────────────

def _market_summary(m: EligibleMarket, now: datetime) -> dict:
    hours = (m.close_time - now).total_seconds() / 3600.0 if m.close_time else None
    return {
        "ticker": m.ticker,
        "title": m.title,
        "yes_bid_cents": m.yes_bid_cents,
        "yes_ask_cents": m.yes_ask_cents,
        "last_price_cents": m.last_price_cents,
        "volume_24h": m.volume_24h,
        "open_interest": m.open_interest,
        "hours_to_close": round(hours, 1) if hours is not None else None,
    }


def run_triage(
    *,
    anthropic_client,
    prompts: PromptSet,
    assumptions: str,
    portfolio: runtime.PortfolioState,
    eligible: list[EligibleMarket],
    top_n: int,
    budget: runtime.BudgetCounter,
    killswitch: Optional[runtime.Killswitch] = None,
) -> TriageOutput:
    import json
    now = datetime.now(timezone.utc)
    payload = {
        "portfolio": {
            "cash_cents": portfolio.cash_cents,
            "positions": portfolio.positions,
        },
        "markets": [_market_summary(m, now) for m in eligible],
        "N": top_n,
    }
    user = "INPUT:\n" + json.dumps(payload, default=str)
    out, _ = _call_and_validate(
        anthropic_client=anthropic_client,
        model=MODEL_TRIAGE,
        prompts=prompts,
        role="triage",
        user=user,
        budget=budget,
        schema_model=TriageOutput,
        killswitch=killswitch,
        max_tokens=1024,
        assumptions=assumptions,
    )
    return out


# ────────────────────────────────────────────────────────────────────────────
# Per-market pipeline
# ────────────────────────────────────────────────────────────────────────────

def _stub_findings(plan: ResearchPlan) -> Findings:
    """
    Fallback used when `--no-research` is passed (sanity-checking prompt shape
    without paying for web search). Each question gets a placeholder at low
    confidence so the decider should skip.
    """
    from .schemas import Finding

    findings = [
        Finding(
            question=q.question,
            answer="[stub] research skipped (--no-research)",
            sources=[],
            confidence=0.3,
        )
        for q in plan.questions
    ]
    return Findings(
        findings=findings,
        unanswered=list(plan.required_datapoints),
        tool_calls_used=0,
    )


def _benchmark(plan: ResearchPlan, findings: Findings) -> tuple[bool, str]:
    """
    Research-sufficient gate.

    Sufficient iff:
      - every required_datapoint is answered (i.e. NOT in findings.unanswered)
      - average finding confidence >= plan.confidence_threshold
    """
    answered = set(f.question for f in findings.findings)
    missing_dp = [dp for dp in plan.required_datapoints if dp in findings.unanswered]
    if missing_dp:
        return False, f"missing_datapoints={len(missing_dp)}/{len(plan.required_datapoints)}"
    if not findings.findings:
        return False, "no_findings"
    avg_conf = sum(f.confidence for f in findings.findings) / len(findings.findings)
    if avg_conf < plan.confidence_threshold:
        return False, f"avg_confidence={avg_conf:.2f}<{plan.confidence_threshold:.2f}"
    _ = answered  # reserved for future per-question coverage check
    return True, f"avg_confidence={avg_conf:.2f}"


@dataclass
class MarketOutcome:
    ticker: str
    completed: bool
    skip_reason: Optional[str]
    decision: Optional[Decision]
    execution: Optional[ExecutionResult]


def process_market(
    *,
    conn: sqlite3.Connection,
    cycle_id: int,
    anthropic_client,
    kalshi_client,
    prompts: PromptSet,
    assumptions: str,
    market: EligibleMarket,
    portfolio: runtime.PortfolioState,
    triage_rationale: str,
    market_budget_usd: float,
    cycle_budget: runtime.BudgetCounter,
    killswitch: Optional[runtime.Killswitch] = None,
    dry_run: bool = True,
    skip_research: bool = False,
    seq: int = 1,
) -> tuple[MarketOutcome, float]:
    """Run the full per-market pipeline. All steps written to chain_log.
    Returns (outcome, market_spent_usd)."""
    import json

    ticker = market.ticker
    now = datetime.now(timezone.utc)

    market_budget = runtime.BudgetCounter(
        market_cap_usd=market_budget_usd,
        cycle_cap_usd=cycle_budget.cycle_cap_usd,
        cycle_spent_usd=cycle_budget.cycle_spent_usd,
    )

    def _sync_cycle_spend():
        cycle_budget.cycle_spent_usd = market_budget.cycle_spent_usd

    market_ctx = _market_summary(market, now)
    market_ctx["current_position"] = portfolio.positions.get(ticker, 0)

    runtime.log_step(conn, cycle_id, ticker, "1_triage_surfaced", {
        "rationale": triage_rationale,
        "market": market_ctx,
    })

    try:
        # Step a: ResearchPlan
        plan_user = "INPUT:\n" + json.dumps({
            "portfolio": {"cash_cents": portfolio.cash_cents, "positions": portfolio.positions},
            "market": market_ctx,
            "triage_rationale": triage_rationale,
        }, default=str)
        plan, plan_usage = _call_and_validate(
            anthropic_client=anthropic_client,
            model=MODEL_PLAN,
            prompts=prompts,
            role="research_plan",
            user=plan_user,
            budget=market_budget,
            schema_model=ResearchPlan,
            killswitch=killswitch,
            assumptions=assumptions,
        )
        _sync_cycle_spend()
        runtime.log_step(conn, cycle_id, ticker, "2_research_plan", {
            "plan": plan.model_dump(),
            "usage": plan_usage,
        })

        # Step b: DecisionFramework
        framework_user = "INPUT:\n" + json.dumps({
            "portfolio": {"cash_cents": portfolio.cash_cents, "positions": portfolio.positions},
            "market": market_ctx,
            "research_plan": plan.model_dump(),
        }, default=str)
        framework, fw_usage = _call_and_validate(
            anthropic_client=anthropic_client,
            model=MODEL_FRAMEWORK,
            prompts=prompts,
            role="decision_framework",
            user=framework_user,
            budget=market_budget,
            schema_model=DecisionFramework,
            killswitch=killswitch,
            assumptions=assumptions,
        )
        _sync_cycle_spend()
        runtime.log_step(conn, cycle_id, ticker, "3_decision_framework", {
            "framework": framework.model_dump(),
            "usage": fw_usage,
        })

        # Step c: Findings
        if skip_research:
            findings = _stub_findings(plan)
            runtime.log_step(conn, cycle_id, ticker, "4_findings", {
                "stub": True,
                "findings": findings.model_dump(),
            })
        else:
            findings, research_usage = research_agent.run_research(
                anthropic_client=anthropic_client,
                system_prompt=prompts.bodies["research_subagent"],
                assumptions=assumptions,
                market_ctx=market_ctx,
                plan=plan,
                budget=market_budget,
                killswitch=killswitch,
                cacheable_system=prompts.cacheable.get("research_subagent", False),
            )
            _sync_cycle_spend()
            runtime.log_step(conn, cycle_id, ticker, "4_findings", {
                "stub": False,
                "findings": findings.model_dump(),
                "usage": research_usage,
            })

        # Step d: Benchmark
        ok, bench_reason = _benchmark(plan, findings)
        runtime.log_step(conn, cycle_id, ticker, "5_benchmark", {"ok": ok, "reason": bench_reason})
        if not ok:
            return (MarketOutcome(ticker, completed=True, skip_reason=f"benchmark:{bench_reason}",
                                  decision=None, execution=None),
                    market_budget.market_spent_usd)

        # Step e: Decision
        decider_user = "INPUT:\n" + json.dumps({
            "market": market_ctx,
            "research_plan": plan.model_dump(),
            "decision_framework": framework.model_dump(),
            "findings": findings.model_dump(),
        }, default=str)
        decision, dec_usage = _call_and_validate(
            anthropic_client=anthropic_client,
            model=MODEL_DECIDER,
            prompts=prompts,
            role="decider",
            user=decider_user,
            budget=market_budget,
            schema_model=Decision,
            killswitch=killswitch,
            assumptions=assumptions,
        )
        _sync_cycle_spend()
        runtime.log_step(conn, cycle_id, ticker, "6_decision", {
            "decision": decision.model_dump(),
            "usage": dec_usage,
        })

        # Step f: Execute
        if dry_run:
            runtime.log_step(conn, cycle_id, ticker, "7_execute", {"dry_run": True, "skipped": True})
            return (MarketOutcome(ticker, completed=True, skip_reason=None,
                                  decision=decision, execution=None),
                    market_budget.market_spent_usd)

        result = execute_decision(
            client=kalshi_client,
            decision=decision,
            ticker=ticker,
            cycle_id=cycle_id,
            seq=seq,
            yes_ask_cents=market.yes_ask_cents,
            no_ask_cents=max(0, 100 - market.yes_bid_cents),
            cash_cents=portfolio.cash_cents,
            current_position_count=portfolio.positions.get(ticker, 0),
        )
        runtime.log_step(conn, cycle_id, ticker, "7_execute", {
            "ok": result.ok,
            "reason": result.reason,
            "client_order_id": result.client_order_id,
            "order_id": result.order_id,
            "filled_count": result.filled_count,
        })
        runtime.log_order(conn, cycle_id, ticker, result)
        return (MarketOutcome(ticker, completed=True, skip_reason=None,
                              decision=decision, execution=result),
                market_budget.market_spent_usd)

    except runtime.BudgetExhausted as e:
        runtime.log_step(conn, cycle_id, ticker, "abort_budget", {"reason": str(e)})
        return (MarketOutcome(ticker, completed=False, skip_reason=f"budget:{e}",
                              decision=None, execution=None),
                market_budget.market_spent_usd)
    except runtime.MalformedLLMResponse as e:
        runtime.log_step(conn, cycle_id, ticker, "abort_malformed", {"reason": str(e)})
        return (MarketOutcome(ticker, completed=False, skip_reason=f"malformed:{e}",
                              decision=None, execution=None),
                market_budget.market_spent_usd)


# ────────────────────────────────────────────────────────────────────────────
# Cycle entry point
# ────────────────────────────────────────────────────────────────────────────

def run_cycle(
    *,
    anthropic_client,
    kalshi_client,
    top_n: int = DEFAULT_TOP_N,
    market_budget_usd: float = DEFAULT_MARKET_BUDGET_USD,
    cycle_budget_usd: float = DEFAULT_CYCLE_BUDGET_USD,
    min_daily_volume: int = 100,
    min_hours_to_close: int = 48,
    dry_run: bool = True,
    skip_research: bool = False,
    killswitch: Optional[runtime.Killswitch] = None,
    series_ticker: Optional[str] = None,
    skip_coarse_filter: bool = False,
    coarse_filter_parallelism: int = coarse_filter.DEFAULT_PARALLELISM,
) -> dict:
    conn = runtime.init_db()
    prompts = load_prompts()
    assumptions = prompts.bodies["assumptions"]

    cycle_id = runtime.open_cycle(conn, active_prompts=prompts.pointers)
    log.info("cycle %d opened (dry_run=%s)", cycle_id, dry_run)

    had_api_errors = False

    # Reconcile
    try:
        portfolio = runtime.reconcile(kalshi_client)
    except Exception as e:
        log.exception("reconcile failed")
        had_api_errors = True
        if killswitch:
            killswitch.note_api_error()
            killswitch.note_cycle_api_status(had_errors=True)
        runtime.log_step(conn, cycle_id, "_account", "reconcile_failed", {"error": str(e)})
        runtime.close_cycle(conn, cycle_id, "aborted", f"reconcile_failed:{e}")
        return {"cycle_id": cycle_id, "status": "aborted", "reason": f"reconcile_failed:{e}"}

    runtime.log_step(conn, cycle_id, "_account", "reconcile", {
        "cash_cents": portfolio.cash_cents,
        "position_count": len(portfolio.positions),
    })

    # Killswitch check
    if killswitch:
        reason = killswitch.check(portfolio.cash_cents)
        if reason:
            killswitch.trip(conn, reason)
            runtime.close_cycle(conn, cycle_id, "killswitch", reason)
            return {"cycle_id": cycle_id, "status": "killswitch", "reason": reason}

    # Discover
    try:
        eligible = discover_eligible_markets(
            kalshi_client,
            min_daily_volume=min_daily_volume,
            min_hours_to_close=min_hours_to_close,
            series_ticker=series_ticker,
        )
    except Exception as e:
        log.exception("discover_eligible_markets failed")
        had_api_errors = True
        if killswitch:
            killswitch.note_api_error()
            killswitch.note_cycle_api_status(had_errors=True)
        runtime.log_step(conn, cycle_id, "_universe", "discover_failed", {"error": str(e)})
        runtime.close_cycle(conn, cycle_id, "aborted", f"discover_failed:{e}")
        return {"cycle_id": cycle_id, "status": "aborted", "reason": f"discover_failed:{e}"}

    runtime.log_step(conn, cycle_id, "_universe", "discover", {"count": len(eligible)})
    if not eligible:
        if killswitch:
            killswitch.note_cycle_api_status(had_errors=had_api_errors)
        runtime.close_cycle(conn, cycle_id, "ok", "no_eligible_markets")
        return {"cycle_id": cycle_id, "status": "ok", "outcomes": []}

    # Budgets
    cycle_budget = runtime.BudgetCounter(
        market_cap_usd=market_budget_usd,
        cycle_cap_usd=cycle_budget_usd,
    )

    # Coarse filter (layer 2): parallel per-market haiku yes/no.
    # Skipped when caller passes skip_coarse_filter=True (small universes or
    # when debugging triage in isolation).
    if skip_coarse_filter or "coarse_filter" not in prompts.bodies:
        runtime.log_step(conn, cycle_id, "_universe", "coarse_filter_skipped",
                         {"reason": "flag" if skip_coarse_filter else "no_prompt"})
        filtered = eligible
    else:
        try:
            filtered, all_cf_results = coarse_filter.run_coarse_filter(
                anthropic_client=anthropic_client,
                system_prompt=prompts.bodies["coarse_filter"],
                cacheable=prompts.cacheable.get("coarse_filter", False),
                assumptions=assumptions,
                eligible=eligible,
                cycle_budget=cycle_budget,
                parallelism=coarse_filter_parallelism,
            )
        except Exception as e:
            log.exception("coarse_filter raised")
            runtime.log_step(conn, cycle_id, "_universe", "coarse_filter_error", {"error": str(e)})
            filtered = eligible  # fail-open
            all_cf_results = []
        runtime.log_step(conn, cycle_id, "_universe", "coarse_filter", {
            "input_count": len(eligible),
            "kept_count": len(filtered),
            "decisions": [
                {"ticker": r.ticker, "keep": r.keep, "reason": r.reason,
                 "error": r.error, "usage": r.usage}
                for r in all_cf_results
            ],
        })
        if not filtered:
            if killswitch:
                killswitch.note_cycle_api_status(had_errors=had_api_errors)
            runtime.close_cycle(conn, cycle_id, "ok", "coarse_filter_kept_none")
            return {"cycle_id": cycle_id, "status": "ok", "outcomes": []}

    eligible = filtered

    # Triage
    try:
        triage = run_triage(
            anthropic_client=anthropic_client,
            prompts=prompts,
            assumptions=assumptions,
            portfolio=portfolio,
            eligible=eligible,
            top_n=top_n,
            budget=cycle_budget,
            killswitch=killswitch,
        )
    except (runtime.BudgetExhausted, runtime.MalformedLLMResponse) as e:
        runtime.log_step(conn, cycle_id, "_triage", "abort", {"reason": str(e)})
        if killswitch:
            killswitch.note_cycle_api_status(had_errors=had_api_errors)
        runtime.close_cycle(conn, cycle_id, "aborted", f"triage:{e}")
        return {"cycle_id": cycle_id, "status": "aborted", "reason": str(e)}

    runtime.log_step(conn, cycle_id, "_triage", "result", triage.model_dump())

    # Index eligible by ticker for lookup
    by_ticker = {m.ticker: m for m in eligible}

    # Build the work list (filtering hallucinated tickers before submitting to
    # the pool so we don't spawn workers for non-existent markets).
    work: list[tuple[int, str, EligibleMarket]] = []
    for seq, scored in enumerate(triage.top_tickers, start=1):
        market = by_ticker.get(scored.ticker)
        if not market:
            log.warning("triage surfaced unknown ticker %s", scored.ticker)
            continue
        work.append((seq, scored.rationale, market))

    # Snapshot cycle spend at entry. Each worker runs against a private
    # BudgetCounter seeded with this snapshot — they can't see each other's
    # in-flight spend, so the cycle cap becomes a soft ceiling. Acceptable for
    # paper trading; revisit if we ever route live orders through this path.
    cycle_spent_at_start = cycle_budget.cycle_spent_usd

    from concurrent.futures import ThreadPoolExecutor, as_completed
    import sqlite3 as _sqlite3

    def _run_one(seq: int, rationale: str, market: EligibleMarket):
        # Each worker thread needs its own sqlite connection (the default
        # sqlite3 module raises if a connection is used from multiple threads).
        worker_conn = runtime.init_db()
        try:
            return process_market(
                conn=worker_conn,
                cycle_id=cycle_id,
                anthropic_client=anthropic_client,
                kalshi_client=kalshi_client,
                prompts=prompts,
                assumptions=assumptions,
                market=market,
                portfolio=portfolio,
                triage_rationale=rationale,
                market_budget_usd=market_budget_usd,
                cycle_budget=runtime.BudgetCounter(
                    market_cap_usd=market_budget_usd,
                    cycle_cap_usd=cycle_budget_usd,
                    cycle_spent_usd=cycle_spent_at_start,
                ),
                killswitch=killswitch,
                dry_run=dry_run,
                skip_research=skip_research,
                seq=seq,
            )
        finally:
            worker_conn.close()

    outcomes: list[MarketOutcome] = []
    total_market_spend = 0.0
    with ThreadPoolExecutor(max_workers=DEFAULT_MAX_CONCURRENT_MARKETS) as pool:
        futures = {pool.submit(_run_one, seq, rat, mkt): (seq, mkt.ticker)
                   for seq, rat, mkt in work}
        for future in as_completed(futures):
            seq, ticker = futures[future]
            try:
                outcome, market_spent = future.result()
                outcomes.append(outcome)
                total_market_spend += market_spent
            except Exception as e:
                log.exception("process_market(%s) raised", ticker)
                had_api_errors = True
                if killswitch:
                    killswitch.note_api_error()
                runtime.log_step(conn, cycle_id, ticker, "abort_exception", {"error": str(e)})
                outcomes.append(MarketOutcome(ticker, completed=False,
                                              skip_reason=f"exception:{e}",
                                              decision=None, execution=None))

    # Fold each market's spend into the shared cycle_budget. Workers ran
    # against private copies seeded with the start-of-parallel snapshot, so
    # cycle_budget hasn't seen their increments yet.
    cycle_budget.cycle_spent_usd = cycle_spent_at_start + total_market_spend
    _ = _sqlite3  # imported above for future per-worker cleanup hooks

    if killswitch:
        killswitch.note_cycle_api_status(had_errors=had_api_errors)
    runtime.close_cycle(conn, cycle_id, "ok", f"processed={len(outcomes)}")
    return {
        "cycle_id": cycle_id,
        "status": "ok",
        "cycle_spent_usd": cycle_budget.cycle_spent_usd,
        "outcomes": [
            {
                "ticker": o.ticker,
                "completed": o.completed,
                "skip_reason": o.skip_reason,
                "action": o.decision.action if o.decision else None,
                "size_usd": o.decision.size_usd if o.decision else None,
                "executed": bool(o.execution and o.execution.ok),
            }
            for o in outcomes
        ],
    }

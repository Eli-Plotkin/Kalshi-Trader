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

from . import research_agent, runtime
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

@dataclass
class PromptSet:
    pointers: dict[str, str]      # role → filename, e.g. {"triage": "triage_v1.md"}
    bodies: dict[str, str]        # role → file body


def load_prompts() -> PromptSet:
    pointers = yaml.safe_load(ACTIVE_YAML.read_text())
    bodies: dict[str, str] = {}
    for role, fname in pointers.items():
        path = PROMPTS_DIR / fname
        if not path.exists():
            log.warning("active prompt %s -> %s missing on disk; skipping body load", role, fname)
            continue
        bodies[role] = path.read_text()
    return PromptSet(pointers=pointers, bodies=bodies)


# ────────────────────────────────────────────────────────────────────────────
# Model assignments (cheap → expensive across the pipeline)
# ────────────────────────────────────────────────────────────────────────────

MODEL_TRIAGE = "claude-haiku-4-5-20251001"
MODEL_PLAN = "claude-sonnet-4-6"
MODEL_FRAMEWORK = "claude-sonnet-4-6"
MODEL_DECIDER = "claude-opus-4-7"

DEFAULT_TOP_N = 5
DEFAULT_MARKET_BUDGET_USD = 0.50
DEFAULT_CYCLE_BUDGET_USD = 3.00


# ────────────────────────────────────────────────────────────────────────────
# Validated LLM helpers
# ────────────────────────────────────────────────────────────────────────────

def _call_and_validate(
    *,
    anthropic_client,
    model: str,
    system: str,
    user: str,
    budget: runtime.BudgetCounter,
    schema_model,
    killswitch: Optional[runtime.Killswitch] = None,
    max_tokens: int = 2048,
):
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
        "assumptions_md": assumptions,
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
        system=prompts.bodies["triage"],
        user=user,
        budget=budget,
        schema_model=TriageOutput,
        killswitch=killswitch,
        max_tokens=1024,
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
) -> MarketOutcome:
    """Run the full per-market pipeline. All steps written to chain_log."""
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
            "assumptions_md": assumptions,
            "portfolio": {"cash_cents": portfolio.cash_cents, "positions": portfolio.positions},
            "market": market_ctx,
            "triage_rationale": triage_rationale,
        }, default=str)
        plan, plan_usage = _call_and_validate(
            anthropic_client=anthropic_client,
            model=MODEL_PLAN,
            system=prompts.bodies["research_plan"],
            user=plan_user,
            budget=market_budget,
            schema_model=ResearchPlan,
            killswitch=killswitch,
        )
        _sync_cycle_spend()
        runtime.log_step(conn, cycle_id, ticker, "2_research_plan", {
            "plan": plan.model_dump(),
            "usage": plan_usage,
        })

        # Step b: DecisionFramework
        framework_user = "INPUT:\n" + json.dumps({
            "assumptions_md": assumptions,
            "portfolio": {"cash_cents": portfolio.cash_cents, "positions": portfolio.positions},
            "market": market_ctx,
            "research_plan": plan.model_dump(),
        }, default=str)
        framework, fw_usage = _call_and_validate(
            anthropic_client=anthropic_client,
            model=MODEL_FRAMEWORK,
            system=prompts.bodies["decision_framework"],
            user=framework_user,
            budget=market_budget,
            schema_model=DecisionFramework,
            killswitch=killswitch,
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
            return MarketOutcome(ticker, completed=True, skip_reason=f"benchmark:{bench_reason}",
                                 decision=None, execution=None)

        # Step e: Decision
        decider_user = "INPUT:\n" + json.dumps({
            "assumptions_md": assumptions,
            "market": market_ctx,
            "research_plan": plan.model_dump(),
            "decision_framework": framework.model_dump(),
            "findings": findings.model_dump(),
        }, default=str)
        decision, dec_usage = _call_and_validate(
            anthropic_client=anthropic_client,
            model=MODEL_DECIDER,
            system=prompts.bodies["decider"],
            user=decider_user,
            budget=market_budget,
            schema_model=Decision,
            killswitch=killswitch,
        )
        _sync_cycle_spend()
        runtime.log_step(conn, cycle_id, ticker, "6_decision", {
            "decision": decision.model_dump(),
            "usage": dec_usage,
        })

        # Step f: Execute
        if dry_run:
            runtime.log_step(conn, cycle_id, ticker, "7_execute", {"dry_run": True, "skipped": True})
            return MarketOutcome(ticker, completed=True, skip_reason=None,
                                 decision=decision, execution=None)

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
        return MarketOutcome(ticker, completed=True, skip_reason=None,
                             decision=decision, execution=result)

    except runtime.BudgetExhausted as e:
        runtime.log_step(conn, cycle_id, ticker, "abort_budget", {"reason": str(e)})
        return MarketOutcome(ticker, completed=False, skip_reason=f"budget:{e}",
                             decision=None, execution=None)
    except runtime.MalformedLLMResponse as e:
        runtime.log_step(conn, cycle_id, ticker, "abort_malformed", {"reason": str(e)})
        return MarketOutcome(ticker, completed=False, skip_reason=f"malformed:{e}",
                             decision=None, execution=None)


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

    outcomes: list[MarketOutcome] = []
    for seq, scored in enumerate(triage.top_tickers, start=1):
        if runtime.shutdown_requested():
            log.warning("shutdown requested mid-cycle; stopping further markets")
            break
        market = by_ticker.get(scored.ticker)
        if not market:
            log.warning("triage surfaced unknown ticker %s", scored.ticker)
            continue
        try:
            outcome = process_market(
                conn=conn,
                cycle_id=cycle_id,
                anthropic_client=anthropic_client,
                kalshi_client=kalshi_client,
                prompts=prompts,
                assumptions=assumptions,
                market=market,
                portfolio=portfolio,
                triage_rationale=scored.rationale,
                market_budget_usd=market_budget_usd,
                cycle_budget=cycle_budget,
                killswitch=killswitch,
                dry_run=dry_run,
                skip_research=skip_research,
                seq=seq,
            )
        except Exception as e:
            log.exception("process_market(%s) raised", scored.ticker)
            had_api_errors = True
            if killswitch:
                killswitch.note_api_error()
            runtime.log_step(conn, cycle_id, scored.ticker, "abort_exception", {"error": str(e)})
            outcomes.append(MarketOutcome(scored.ticker, completed=False,
                                          skip_reason=f"exception:{e}",
                                          decision=None, execution=None))
            continue
        outcomes.append(outcome)
        if killswitch and killswitch.check(portfolio.cash_cents):
            log.warning("killswitch tripped mid-cycle; stopping further markets")
            break

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

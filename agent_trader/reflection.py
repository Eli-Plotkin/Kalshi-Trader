"""
End-of-day reflection.

Reads the chain_log for a time window, grades every Decision against its
declared `expected_outcome` using current market state, and asks an LLM to
propose v(N+1) versions of the `research_plan` and/or `decision_framework`
prompts. Proposals are written to `data/proposed_prompts/` for the user to
review and (optionally) promote into `agent_trader/prompts/`.

Reflection NEVER edits files in `agent_trader/prompts/` directly.
Reflection NEVER proposes changes to `assumptions.md`, `triage`, `decider`,
or `research_subagent` — those are out of scope per `reflection_v1.md`.
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from pydantic import TypeAdapter, ValidationError

from . import orchestrator, runtime
from .schemas import (
    Decision,
    DecisionFramework,
    Findings,
    ReflectionProposal,
    ResearchPlan,
)


log = logging.getLogger("agent_trader.reflection")


MODEL_REFLECTION = "claude-opus-4-7"
PROPOSED_DIR = runtime.DATA_DIR / "proposed_prompts"

PROPOSAL_LIST_ADAPTER = TypeAdapter(list[ReflectionProposal])


# ────────────────────────────────────────────────────────────────────────────
# Pull + parse the chain log
# ────────────────────────────────────────────────────────────────────────────

@dataclass
class GradedDecision:
    cycle_id: int
    ticker: str
    decision_ts: str
    decision: Decision
    plan: ResearchPlan
    framework: DecisionFramework
    findings: Findings
    observed: dict
    grade: dict


def _load_step(conn: sqlite3.Connection, cycle_id: int, ticker: str, step: str) -> Optional[dict]:
    row = conn.execute(
        "SELECT payload_json, ts FROM chain_log WHERE cycle_id=? AND ticker=? AND step=?",
        (cycle_id, ticker, step),
    ).fetchone()
    if not row:
        return None
    try:
        return {"payload": json.loads(row[0]), "ts": row[1]}
    except json.JSONDecodeError:
        log.warning("chain_log row had invalid JSON: cycle=%s ticker=%s step=%s", cycle_id, ticker, step)
        return None


def _cycles_in_window(
    conn: sqlite3.Connection, since_ts_ms: int, until_ts_ms: int
) -> list[int]:
    rows = conn.execute(
        "SELECT cycle_id FROM cycles WHERE cycle_id BETWEEN ? AND ? AND skipped=0 ORDER BY cycle_id",
        (since_ts_ms, until_ts_ms),
    ).fetchall()
    return [r[0] for r in rows]


def _market_tickers_in_cycle(conn: sqlite3.Connection, cycle_id: int) -> list[str]:
    rows = conn.execute(
        "SELECT DISTINCT ticker FROM chain_log WHERE cycle_id=? AND ticker NOT LIKE '\\_%' ESCAPE '\\'",
        (cycle_id,),
    ).fetchall()
    return [r[0] for r in rows]


# ────────────────────────────────────────────────────────────────────────────
# Grading
# ────────────────────────────────────────────────────────────────────────────

def _grade_decision(decision: Decision, observed: dict) -> dict:
    """
    Compare expected_outcome against observed market state.

    `observed` carries:
      - decision_yes_ask_cents — yes_ask at decision time
      - now_yes_bid_cents / now_yes_ask_cents — current market
      - status                — open / closed / settled
      - result                — yes / no / null
    """
    eo = decision.expected_outcome
    decision_price = observed.get("decision_yes_ask_cents")
    now_mid = None
    nyb = observed.get("now_yes_bid_cents")
    nya = observed.get("now_yes_ask_cents")
    if nyb is not None and nya is not None:
        now_mid = (nyb + nya) / 2.0

    direction_correct = None
    if decision_price is not None and now_mid is not None:
        delta = now_mid - decision_price
        if eo.direction == "up":
            direction_correct = delta > 0
        elif eo.direction == "down":
            direction_correct = delta < 0
        elif eo.direction == "flat":
            direction_correct = abs(delta) <= 3  # within 3¢

    eod_hit = None
    if eo.eod_price_target_cents is not None and now_mid is not None:
        if eo.direction == "up":
            eod_hit = now_mid >= eo.eod_price_target_cents
        elif eo.direction == "down":
            eod_hit = now_mid <= eo.eod_price_target_cents
        else:
            eod_hit = abs(now_mid - eo.eod_price_target_cents) <= 3

    resolved = observed.get("status") == "settled"
    resolution_match = None
    if resolved and eo.predicted_resolution in ("yes", "no"):
        resolution_match = observed.get("result") == eo.predicted_resolution

    return {
        "decision_yes_ask_cents": decision_price,
        "now_mid_cents": now_mid,
        "direction_correct": direction_correct,
        "eod_target_hit": eod_hit,
        "resolved": resolved,
        "resolution_match": resolution_match,
    }


def _summarize_findings(findings: Findings) -> dict:
    n = len(findings.findings)
    avg_conf = (sum(f.confidence for f in findings.findings) / n) if n else 0.0
    return {
        "n_findings": n,
        "n_unanswered": len(findings.unanswered),
        "avg_confidence": round(avg_conf, 3),
        "tool_calls_used": findings.tool_calls_used,
    }


def collect_graded_decisions(
    conn: sqlite3.Connection,
    kalshi_client,
    since_ts_ms: int,
    until_ts_ms: int,
) -> list[GradedDecision]:
    """For each cycle in the window, materialize every decision + grade it
    against current market state."""
    out: list[GradedDecision] = []
    for cycle_id in _cycles_in_window(conn, since_ts_ms, until_ts_ms):
        for ticker in _market_tickers_in_cycle(conn, cycle_id):
            dec_row = _load_step(conn, cycle_id, ticker, "6_decision")
            if not dec_row:
                continue
            try:
                decision = Decision.model_validate(dec_row["payload"]["decision"])
            except (KeyError, ValidationError) as e:
                log.warning("skip decision parse cycle=%s ticker=%s err=%s", cycle_id, ticker, e)
                continue

            plan_row = _load_step(conn, cycle_id, ticker, "2_research_plan")
            framework_row = _load_step(conn, cycle_id, ticker, "3_decision_framework")
            findings_row = _load_step(conn, cycle_id, ticker, "4_findings")
            triage_row = _load_step(conn, cycle_id, ticker, "1_triage_surfaced")
            if not (plan_row and framework_row and findings_row and triage_row):
                log.warning("skip ticker=%s cycle=%s missing prereq steps", ticker, cycle_id)
                continue

            try:
                plan = ResearchPlan.model_validate(plan_row["payload"]["plan"])
                framework = DecisionFramework.model_validate(framework_row["payload"]["framework"])
                findings = Findings.model_validate(findings_row["payload"]["findings"])
            except (KeyError, ValidationError) as e:
                log.warning("skip parse cycle=%s ticker=%s err=%s", cycle_id, ticker, e)
                continue

            decision_market = triage_row["payload"]["market"]
            current = kalshi_client.get_market(ticker) or {}

            observed = {
                "decision_yes_ask_cents": decision_market.get("yes_ask_cents"),
                "now_yes_bid_cents": current.get("yes_bid"),
                "now_yes_ask_cents": current.get("yes_ask"),
                "now_last_price_cents": current.get("last_price"),
                "status": current.get("status"),
                "result": current.get("result"),
            }
            grade = _grade_decision(decision, observed)

            out.append(GradedDecision(
                cycle_id=cycle_id,
                ticker=ticker,
                decision_ts=dec_row["ts"],
                decision=decision,
                plan=plan,
                framework=framework,
                findings=findings,
                observed=observed,
                grade=grade,
            ))
    return out


# ────────────────────────────────────────────────────────────────────────────
# LLM proposal call
# ────────────────────────────────────────────────────────────────────────────

def _serialize_for_llm(graded: list[GradedDecision]) -> list[dict]:
    return [
        {
            "ticker": g.ticker,
            "cycle_id": g.cycle_id,
            "decision_ts": g.decision_ts,
            "decision": g.decision.model_dump(),
            "plan": g.plan.model_dump(),
            "framework": g.framework.model_dump(),
            "findings_summary": _summarize_findings(g.findings),
            "observed": g.observed,
            "grade": g.grade,
        }
        for g in graded
    ]


def _bump_version(active_filename: str) -> str:
    """research_plan_v1.md → research_plan_v2.md. Falls back to _v2.md suffix."""
    m = re.match(r"^(.*)_v(\d+)\.md$", active_filename)
    if not m:
        return active_filename.replace(".md", "_v2.md")
    return f"{m.group(1)}_v{int(m.group(2)) + 1}.md"


def request_proposals(
    *,
    anthropic_client,
    prompts: orchestrator.PromptSet,
    period_summary: dict,
    graded: list[GradedDecision],
    budget: runtime.BudgetCounter,
    killswitch: Optional[runtime.Killswitch] = None,
) -> list[ReflectionProposal]:
    if not graded:
        log.info("reflection: no graded decisions in window; nothing to propose")
        return []

    payload = {
        "period_summary": period_summary,
        "active_prompts": prompts.pointers,
        "graded_decisions": _serialize_for_llm(graded),
    }
    user = "INPUT:\n" + json.dumps(payload, default=str)

    text, usage = runtime.call_llm(
        client=anthropic_client,
        model=MODEL_REFLECTION,
        system=runtime.build_system_block(
            prompts.bodies["reflection"],
            cacheable=prompts.cacheable.get("reflection", False),
        ),
        user=user,
        budget=budget,
        max_tokens=8192,
    )
    log.info("reflection LLM usage: %s", usage)

    try:
        raw = runtime.parse_llm_json(text)
        if not isinstance(raw, list):
            raise runtime.MalformedLLMResponse(
                f"reflection: expected list, got {type(raw).__name__}"
            )
        proposals = PROPOSAL_LIST_ADAPTER.validate_python(raw)
    except (runtime.MalformedLLMResponse, ValidationError) as e:
        if killswitch:
            killswitch.note_agent_response(malformed=True)
        raise runtime.MalformedLLMResponse(
            f"ReflectionProposal[]: {e}; head={text[:300]!r}"
        )
    if killswitch:
        killswitch.note_agent_response(malformed=False)
    return proposals


# ────────────────────────────────────────────────────────────────────────────
# Persist proposals
# ────────────────────────────────────────────────────────────────────────────

ALLOWED_TARGETS = {"research_plan", "decision_framework"}


def write_proposals(
    proposals: list[ReflectionProposal],
    active_pointers: dict[str, str],
) -> list[Path]:
    """
    Validate target + filename, then write the body to data/proposed_prompts/.
    Returns the list of written paths.
    """
    PROPOSED_DIR.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for p in proposals:
        if p.target_prompt not in ALLOWED_TARGETS:
            log.warning("reflection proposal rejected: target %s not in %s",
                        p.target_prompt, ALLOWED_TARGETS)
            continue
        active = active_pointers.get(p.target_prompt)
        expected = _bump_version(active) if active else None
        if expected and p.proposed_filename != expected:
            log.warning("proposal filename %s does not match expected bump %s; honoring proposal",
                        p.proposed_filename, expected)
        # Disallow path traversal — filename only.
        safe_name = Path(p.proposed_filename).name
        out = PROPOSED_DIR / safe_name
        out.write_text(p.body)
        meta = out.with_suffix(out.suffix + ".meta.json")
        meta.write_text(json.dumps({
            "target_prompt": p.target_prompt,
            "diff_summary": p.diff_summary,
            "active_pointer": active,
            "expected_filename": expected,
            "written_at": runtime.now_iso(),
        }, indent=2))
        written.append(out)
        log.info("wrote proposal: %s (%s)", out, p.diff_summary[:80])
    return written


# ────────────────────────────────────────────────────────────────────────────
# Entry point
# ────────────────────────────────────────────────────────────────────────────

@dataclass
class ReflectionResult:
    cycles_in_window: int
    graded_decisions: int
    proposals_written: list[str]
    cost_usd: float


def run_reflection(
    *,
    anthropic_client,
    kalshi_client,
    since: datetime,
    until: Optional[datetime] = None,
    cycle_budget_usd: float = 1.50,
    killswitch: Optional[runtime.Killswitch] = None,
) -> ReflectionResult:
    until = until or datetime.now(timezone.utc)
    since_ms = int(since.timestamp() * 1000)
    until_ms = int(until.timestamp() * 1000)

    conn = runtime.init_db()
    prompts = orchestrator.load_prompts()
    if "reflection" not in prompts.bodies:
        raise RuntimeError("reflection prompt missing — set active.yaml -> reflection: <file>")

    cycle_ids = _cycles_in_window(conn, since_ms, until_ms)
    log.info("reflection window: %s → %s (%d cycles)", since, until, len(cycle_ids))

    graded = collect_graded_decisions(conn, kalshi_client, since_ms, until_ms)
    log.info("collected %d graded decisions", len(graded))

    period_summary = {
        "since": since.isoformat(),
        "until": until.isoformat(),
        "cycle_count": len(cycle_ids),
        "decision_count": len(graded),
    }

    budget = runtime.BudgetCounter(
        market_cap_usd=cycle_budget_usd,
        cycle_cap_usd=cycle_budget_usd,
    )

    try:
        proposals = request_proposals(
            anthropic_client=anthropic_client,
            prompts=prompts,
            period_summary=period_summary,
            graded=graded,
            budget=budget,
            killswitch=killswitch,
        )
    except runtime.BudgetExhausted as e:
        log.error("reflection budget exhausted: %s", e)
        proposals = []

    written = write_proposals(proposals, prompts.pointers)

    # Log a synthetic chain_log row so the audit trail captures the reflection run.
    runtime.log_step(conn, int(time.time() * 1000), "_reflection", "run", {
        "since": since.isoformat(),
        "until": until.isoformat(),
        "cycles": len(cycle_ids),
        "graded": len(graded),
        "proposals_written": [str(p) for p in written],
        "cost_usd": budget.cycle_spent_usd,
    })

    return ReflectionResult(
        cycles_in_window=len(cycle_ids),
        graded_decisions=len(graded),
        proposals_written=[str(p) for p in written],
        cost_usd=budget.cycle_spent_usd,
    )


def parse_since_arg(s: str) -> datetime:
    """Accepts '24h', '7d', '90m', or an ISO 8601 timestamp."""
    m = re.match(r"^(\d+)\s*([hdm])$", s.strip().lower())
    if m:
        n, unit = int(m.group(1)), m.group(2)
        delta = {"h": timedelta(hours=n), "d": timedelta(days=n), "m": timedelta(minutes=n)}[unit]
        return datetime.now(timezone.utc) - delta
    return datetime.fromisoformat(s)

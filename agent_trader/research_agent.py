"""
Research subagent driver.

Wraps one Anthropic call with the server-side `web_search` tool. Anthropic
runs the search loop on its side and returns:

  - `server_tool_use` blocks   — one per search Claude performed
  - `web_search_tool_result`   — search hits returned to Claude
  - terminal `text` block(s)   — the final JSON `Findings` payload

We:
  1. Pre-check the budget (token cost only — see TODO #3 for web_search dollars).
  2. Make the call with tool_choice="auto" and `max_uses = plan.tool_call_budget`.
  3. Count `server_tool_use` blocks → `tool_calls_used`.
  4. Add token cost (+ per-search cost estimate) to the BudgetCounter.
  5. Parse + validate the final text against `schemas.Findings`.

Returns `(findings, usage_dict)`. Raises `BudgetExhausted` / `MalformedLLMResponse`
to match the rest of the orchestrator's error contract.
"""
from __future__ import annotations

import json
import logging
from typing import Optional

from pydantic import ValidationError

from . import runtime
from .schemas import Findings, ResearchPlan


log = logging.getLogger("agent_trader.research_agent")


MODEL_RESEARCH = "claude-sonnet-4-6"

# Anthropic public pricing as of design-doc date. Verify before going live.
# TODO #3: confirm exact figure and surface in runtime.MODEL_RATES_PER_MTOK.
WEB_SEARCH_COST_USD_PER_CALL = 0.010  # $10 per 1000 searches

WEB_SEARCH_TOOL_TYPE = "web_search_20250305"
WEB_SEARCH_TOOL_NAME = "web_search"


def _build_user_message(
    *,
    assumptions: str,
    market_ctx: dict,
    plan: ResearchPlan,
) -> str:
    return "INPUT:\n" + json.dumps(
        {
            "assumptions_md": assumptions,
            "market": market_ctx,
            "research_plan": plan.model_dump(),
        },
        default=str,
    )


def _extract_final_text(content_blocks) -> str:
    """Return the last text block. Anthropic emits intermediate text + tool use blocks
    while reasoning; the final answer is the trailing text block after all tool use."""
    text = ""
    for block in content_blocks:
        if getattr(block, "type", None) == "text":
            text = block.text
    return text


def _count_tool_uses(content_blocks) -> int:
    n = 0
    for block in content_blocks:
        if getattr(block, "type", None) == "server_tool_use":
            n += 1
    return n


def run_research(
    *,
    anthropic_client,
    system_prompt: str,
    assumptions: str,
    market_ctx: dict,
    plan: ResearchPlan,
    budget: runtime.BudgetCounter,
    killswitch: Optional[runtime.Killswitch] = None,
    max_tokens: int = 4096,
) -> tuple[Findings, dict]:
    over = budget.will_exceed(0.0)
    if over:
        raise runtime.BudgetExhausted(over)

    tools = [
        {
            "type": WEB_SEARCH_TOOL_TYPE,
            "name": WEB_SEARCH_TOOL_NAME,
            "max_uses": plan.tool_call_budget,
        }
    ]

    user = _build_user_message(
        assumptions=assumptions, market_ctx=market_ctx, plan=plan
    )

    resp = anthropic_client.messages.create(
        model=MODEL_RESEARCH,
        max_tokens=max_tokens,
        system=system_prompt,
        tools=tools,
        messages=[{"role": "user", "content": user}],
    )

    in_t = getattr(resp.usage, "input_tokens", 0)
    out_t = getattr(resp.usage, "output_tokens", 0)
    token_cost = runtime.estimate_cost_usd(MODEL_RESEARCH, in_t, out_t)
    tool_calls_used = _count_tool_uses(resp.content)
    search_cost = tool_calls_used * WEB_SEARCH_COST_USD_PER_CALL
    total_cost = token_cost + search_cost
    budget.add(total_cost)

    final_text = _extract_final_text(resp.content)

    try:
        raw = runtime.parse_llm_json(final_text)
        findings = Findings.model_validate(raw)
    except (runtime.MalformedLLMResponse, ValidationError) as e:
        if killswitch:
            killswitch.note_agent_response(malformed=True)
        raise runtime.MalformedLLMResponse(
            f"Findings: {e}; head={final_text[:300]!r}"
        )
    if killswitch:
        killswitch.note_agent_response(malformed=False)

    # Trust the response's observed tool-use count over the LLM's self-reported
    # number — Anthropic's count is authoritative.
    if findings.tool_calls_used != tool_calls_used:
        log.info(
            "research: reconciling tool_calls_used (llm=%d, observed=%d)",
            findings.tool_calls_used,
            tool_calls_used,
        )
        findings = findings.model_copy(update={"tool_calls_used": tool_calls_used})

    usage = {
        "model": MODEL_RESEARCH,
        "input_tokens": in_t,
        "output_tokens": out_t,
        "token_cost_usd": token_cost,
        "web_search_calls": tool_calls_used,
        "web_search_cost_usd": search_cost,
        "cost_usd": total_cost,
        "stop_reason": getattr(resp, "stop_reason", None),
    }
    return findings, usage

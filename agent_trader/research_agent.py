"""
Research subagent driver.

Wraps one Anthropic call with the server-side `web_search` tool. Anthropic
runs the search loop on its side and returns:

  - `server_tool_use` blocks   — one per search Claude performed
  - `web_search_tool_result`   — search hits returned to Claude
  - terminal `text` block(s)   — the final JSON `Findings` payload

We:
  1. Pre-check the budget.
  2. Make the call with tool_choice="auto" and `max_uses = WEB_SEARCH_MAX_USES`
     (constant ceiling for cache stability; plan.tool_call_budget is the
     prompt-level soft cap the subagent sees in its user payload).
  3. Count `server_tool_use` blocks → `tool_calls_used`.
  4. Add token cost + per-search cost (runtime.TOOL_COST_USD_PER_CALL) to BudgetCounter.
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

WEB_SEARCH_TOOL_TYPE = "web_search_20250305"
WEB_SEARCH_TOOL_NAME = "web_search"

# Anthropic's prompt cache prefix includes BOTH `system` and `tools[]`. If
# `max_uses` varies per call (which it would if we keyed it to
# plan.tool_call_budget), every call would cache-miss. Pinning a constant
# ceiling keeps the tools block stable so the cached prefix is reusable.
# The model still self-limits inside this ceiling via tool_choice="auto".
WEB_SEARCH_MAX_USES = 15


def _build_user_message(
    *,
    market_ctx: dict,
    plan: ResearchPlan,
) -> str:
    return "INPUT:\n" + json.dumps(
        {
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
    max_tokens: int = 16384,
    cacheable_system: bool = True,
) -> tuple[Findings, dict]:
    over = budget.will_exceed(0.0)
    if over:
        raise runtime.BudgetExhausted(over)

    tools = [
        {
            "type": WEB_SEARCH_TOOL_TYPE,
            "name": WEB_SEARCH_TOOL_NAME,
            "max_uses": WEB_SEARCH_MAX_USES,
        }
    ]

    user = _build_user_message(market_ctx=market_ctx, plan=plan)

    system = runtime.build_system_block(
        system_prompt, cacheable=cacheable_system, prefix=assumptions
    )

    resp = anthropic_client.messages.create(
        model=MODEL_RESEARCH,
        max_tokens=max_tokens,
        system=system,
        tools=tools,
        messages=[{"role": "user", "content": user}],
    )

    in_t = getattr(resp.usage, "input_tokens", 0)
    out_t = getattr(resp.usage, "output_tokens", 0)
    cache_write = getattr(resp.usage, "cache_creation_input_tokens", 0) or 0
    cache_read = getattr(resp.usage, "cache_read_input_tokens", 0) or 0
    token_cost = runtime.estimate_cost_usd(
        MODEL_RESEARCH, in_t, out_t, cache_write, cache_read
    )
    tool_calls_used = _count_tool_uses(resp.content)
    search_cost = runtime.estimate_tool_cost_usd(WEB_SEARCH_TOOL_NAME, tool_calls_used)
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
        "cache_creation_input_tokens": cache_write,
        "cache_read_input_tokens": cache_read,
        "token_cost_usd": token_cost,
        "web_search_calls": tool_calls_used,
        "web_search_cost_usd": search_cost,
        "cost_usd": total_cost,
        "stop_reason": getattr(resp, "stop_reason", None),
    }
    return findings, usage

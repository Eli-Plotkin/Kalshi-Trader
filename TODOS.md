# TODOS

Deferred work captured during plan reviews. Each item carries enough context to be picked up cold.

---

## 1. Tighten killswitch trigger latency — DONE (2026-05-14)

Both rules live in [agent_trader/runtime.py](agent_trader/runtime.py):
- `max_consecutive_api_error_cycles ≥ 3` (slow rule).
- `max_errors_within_30m ≥ 5` (fast rule, rolling-window timestamps trimmed in `note_api_error`).

Counters populated by [agent_trader/orchestrator.py](agent_trader/orchestrator.py) `run_cycle`:
`note_api_error()` on every Kalshi failure (reconcile, discover, per-market exception); `note_cycle_api_status(had_errors)` on every cycle exit path.

---

## 2. Per-cycle research cache in `research_agent.py`

**What:** In-memory cache keyed on `(cycle_id, normalized_query_hash)` so duplicate web searches inside a single cycle hit the cache instead of the network.

**Why:** When 5+ correlated markets in one cycle want the same fact (e.g. "today's Spurs injury report"), the research subagent web-searches it that many times. With the triage agent surfacing top-N by likely mispricing, correlated clusters are likely.

**Pros:** Cuts duplicate web_search costs; cheap implementation (dict cleared at cycle boundary).
**Cons:** Cache normalization is fiddly — "Spurs injury report" vs "Spurs injuries today" may not hash the same; LLM-driven query normalization adds its own cost. Mitigate by query normalization with cheap model (haiku).

**Context:** `research_agent.py`. Reflection's cost-per-cycle logs will show whether this is worth building — defer until you see the duplicate-query waste in chain logs.
**Depends on:** `research_agent.py` initial implementation (Day 3).

---

## 3. Web search tool cost & rate-limit audit — DONE (2026-05-14)

- `web_search` priced at $0.010/call ($10 per 1,000 searches), per
  [Anthropic web search tool docs](https://docs.anthropic.com/en/docs/agents-and-tools/tool-use/web-search-tool).
- Constant consolidated into `runtime.TOOL_COST_USD_PER_CALL` alongside `MODEL_RATES_PER_MTOK`,
  with a helper `runtime.estimate_tool_cost_usd(tool_name, call_count)`.
- `research_agent.py` no longer carries its own pricing constant — calls
  `runtime.estimate_tool_cost_usd("web_search", n)` and bills the BudgetCounter.
- Rate limits intentionally NOT codified: they're org-level and shared with other tools.
  If a call 429s, it surfaces through the killswitch via `note_api_error()` rather than
  being pre-throttled here.

---

## 4. Naming-convention cleanup pass — CLOSED

Reviewed 2026-05-14. The 22 items audited individually. Outcomes:

**Fixed in this pass:**
- 4.1 — `get_tip_off_time` collision resolved by deleting the dead `data_retrieval/helpers.py` copy (zero callers).
- 4.3 — `expiration_ts` unit conflict: docstring in [kalshi/client.py](kalshi/client.py) corrected to "unix seconds" to match the Kalshi v2 contract and the only caller in [nba_trading/main.py](nba_trading/main.py).

**Previously fixed (kept here for receipts):**
- 4.2 — `run_bot` → `run_agent_trader` on the agent_trader side.
- 4.4 — `*_cents` suffix added to agent_trader market fields.
- 4.8 — `coid` → `client_order_id` in agent_trader/smoke.py.
- 4.17 — `raw` → `raw_market_response` / `raw_order_response`.
- 4.18 — `mark_cycle_skipped` → `skip_cycle`.

**Closed without fix (cost > value):**
- 4.5, 4.6, 4.7, 4.9, 4.10, 4.13, 4.20, 4.22 — local cosmetic rewrites that would touch many call sites for marginal clarity gain. Each package is internally consistent.
- 4.11 — module rename `get_nba_volatility_data.py` → `build_*`: breaking change against scripts users may run.
- 4.12 — `MAX_WORKERS` is unambiguous in scope (single file, single use).
- 4.14 — `SCHEDULE_FILE` is no longer in `kalshi/config.py`; moved to `nba_trading/config.py` during the segmentation pass and the name is fine in that scope.
- 4.15 — `cycle_id` being opaque is the right design; readers should not depend on its ms-timestamp internals.
- 4.16 — `Action` enum rename would require coordinated prompt updates (decider_v1 references `close_position`); revisit when prompts are next versioned.
- 4.19 — `Killswitch.tripped` (public) vs `_shutdown_requested` (module-private) is correct: one is queried by scheduler logic, the other by signal handlers only.
- 4.21 — `get_safe_max` already has a docstring explaining "best realistic sell price"; the local name is fine inside one file.

---

## 5. Wire up Anthropic prompt caching — selectively — DONE (2026-05-12)

Implementation receipts:
- `runtime.build_system_block(text, cacheable)` returns plain string or the
  `[{type:"text", text, cache_control:{type:"ephemeral"}}]` block form.
- `runtime.estimate_cost_usd` now bills `cache_creation_input_tokens` at 1.25×
  and `cache_read_input_tokens` at 0.10× the model's input rate; constants
  `CACHE_WRITE_MULTIPLIER` / `CACHE_READ_MULTIPLIER`.
- `runtime.call_llm` accepts `system: str | list[dict]`, extracts
  `cache_creation_input_tokens` + `cache_read_input_tokens` from the response,
  and surfaces both in its usage dict.
- `orchestrator.CACHEABLE_PROMPTS = {assumptions, triage, decider, research_subagent, reflection}`.
  `PromptSet.cacheable: dict[str, bool]` populated by `load_prompts`.
  `_call_and_validate` now takes `prompts` + `role` and builds the system
  block internally — single source of truth for the cache flag.
- `research_agent.run_research` wraps its system prompt as cacheable and pins
  `max_uses` at the constant `WEB_SEARCH_MAX_USES = 15` so the tools[] portion
  of the cache prefix stops varying per call (was the silent cache-buster).
  Also bills cache tokens through the cache-aware cost helper.
- `reflection.request_proposals` passes `build_system_block(reflection_body,
  cacheable=prompts.cacheable["reflection"])`.
- `research_plan` + `decision_framework` deliberately left uncached: reflection
  rewrites them, and caching a body that flips daily costs more than it saves.

**Deferred sub-TODOs:**
- Move `assumptions_md` from the user JSON into a second cached system block.
  Currently it lives in every user message, so caching the per-role system
  block still helps; the assumptions migration would multiply savings but
  also blast radius — defer until we have live cycles to measure.
- Reflection-side check: "if cached-prompt cache_read_rate < 50%, propose
  dropping cadence or warn caching isn't paying off." Needs live data first.

### Original analysis (kept for receipts)

**What:** Mark *stable* system prompts as cacheable on every `messages.create` call so Anthropic charges 0.1× input rate on cache hits and 1.25× on cache writes. **Do NOT blanket-cache every prompt** — this project's whole point is that reflection rewrites prompts over time, and caching a prompt the reflection loop is actively iterating on costs more than it saves.

**Why:** System prompts dominate per-call input. With ~27 LLM calls per cycle (1 triage + 5 markets × 5 stages + reflection), the same ~3 KB of system prompt for each stage is re-sent uncached every time. Cached input is 10% of base rate — net ~60–70% cut on input spend for the prompts we cache.

The arithmetic (assuming we cache everything):
- Current per-cycle input spend ≈ $1.20 of ~$1.80 total.
- ~80% of that input is the static system prompt body.
- Caching → input drops to ~$0.30; total per-cycle ≈ $0.90.
- Daily (16-cycle / 8h): $30 → ~$13. Daily (24/7): $86 → ~$36.

The realistic number is somewhat lower because we exclude `research_plan` and `decision_framework` from caching (see below) — but those two are also the smallest prompts, so the savings degradation is modest.

### Cache eligibility per prompt

The deciding question is **how often does the prompt body change?** A cache write costs 1.25× input; a cache hit costs 0.10×. Break-even on a single write is ~2.8 hits within the 5-minute TTL window. Anything that changes faster than that is a net cost.

| Prompt | Cache? | Reason |
|---|---|---|
| `assumptions` | ✅ yes | User-edited, slow human cadence. Reflection is explicitly forbidden from touching it. |
| `triage` | ✅ yes | Out of scope for reflection-v1. Stable. |
| `decider` | ✅ yes | Out of scope for reflection-v1. Stable. |
| `research_subagent` | ✅ yes | Out of scope for reflection-v1. Stable. |
| `reflection` | ✅ yes | Reflection doesn't rewrite itself. Stable. Also only called ~once/day, so cache TTL rarely matters — cache mostly to keep the call-site uniform. |
| `research_plan` | ⚠️ conditional | Reflection's allowed target. Cache during a version's active lifespan; expect one cache-write premium per activation. Net positive *unless* the user is iterating on this prompt manually multiple times per day. |
| `decision_framework` | ⚠️ conditional | Same as `research_plan`. |

**Implementation: gate caching on a per-prompt flag**, not "cache everything." Add a field to the `PromptSet` loader that knows which roles are stable vs evolving, and only attach `cache_control` for the stable ones. When the user (or future auto-promotion) bumps `research_plan_v1` → `research_plan_v2`, no special handling is needed for the cached prompts; the evolving prompt just keeps doing what it does today.

If we ever build auto-promotion of reflection proposals (currently human-gated), revisit: a prompt that changes 10× per day should be moved to the ⚠️ list and probably uncached entirely.

**Pros:** Largest single cost lever on the stable prompts. No correctness risk — caching is transparent to the model. Carving out the evolving prompts keeps the recursive-self-improvement loop cheap to iterate on.

**Cons:**
- Cache writes are 1.25× input rate; first call after activation pays a small premium (recouped on call 2).
- 5-minute TTL means low-cadence cycles (≥5 min idle) lose the cache and pay the write premium each cycle. Measure cadence vs TTL before committing — at the design's 30-min cadence the cache will be cold every cycle, defeating most of the win. **Caching pays off best at sub-5-min cadence inside a single cycle** (the 5 markets × 5 stages burst), not across cycles. That's still the dominant cost, so it still works.

**Implementation sketch:**
1. Extend `orchestrator.load_prompts` to return a `cacheable: dict[str, bool]` alongside `bodies`. Mark `assumptions`, `triage`, `decider`, `research_subagent`, `reflection` as `True`; `research_plan`, `decision_framework` as `False`. Source of truth: a constant set in `orchestrator.py`, NOT in `active.yaml` (the YAML is user-facing and shouldn't carry implementation hints).
2. Add a `runtime.build_system_block(text, cacheable=False)` helper that returns either a plain string (cacheable=False) or the block form:
   ```python
   [{"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}]
   ```
3. Extend `runtime.call_llm` signature to accept `system: str | list[dict]` and pass through unchanged. Update every call site to pass through `build_system_block(body, cacheable=prompts.cacheable[role])`.
   Call sites: [agent_trader/runtime.py](agent_trader/runtime.py#L336) (`call_llm`), [agent_trader/research_agent.py](agent_trader/research_agent.py#L91), [agent_trader/reflection.py](agent_trader/reflection.py#L222).
4. Extend `estimate_cost_usd` to bill cache_read / cache_creation / regular input separately. Anthropic returns `usage.cache_read_input_tokens` and `usage.cache_creation_input_tokens` on every response. Rates:
   - regular input: `rates["in"]`
   - cache_creation: `rates["in"] * 1.25`
   - cache_read: `rates["in"] * 0.10`
5. Log the cache hit/miss token counts in the per-step `usage` blob so reflection (and you) can verify caching is actually firing. Reflection should grow a check: "if cached-prompt cache_read_rate < 50%, propose dropping cadence or warn that caching isn't paying off."
6. Don't cache `assumptions.md` content in the user message — only cache it if it's in the system block. The current pipeline puts it in the user message; we could either move it into the system block (cleaner; cacheable) or wrap it as a second cached block in `system=[...]`. Pick one and document it.

**Watch out for:**
- The `tools=[...]` array on the research_agent call is part of the cache prefix. If `max_uses` is derived from `plan.tool_call_budget` (which it is today), every research call has a different prefix and the cache never hits. Fix `max_uses` at a generous constant (e.g. 15) and let the agent self-limit via the prompt, OR move the budget into the user message.
- Minimum cacheable prefix is ~1024 tokens. The triage prompt is short — check it clears the floor or caching is a no-op there.
- If you bump a stable-list prompt version mid-day (rare), expect one write-premium call per stage. Negligible.
- If you ever flip `research_plan` or `decision_framework` to cacheable (e.g. after the reflection loop stabilizes and you stop iterating), revisit and update the flag.

**Verification:**
Run two consecutive cycles with `--no-research` and confirm cycle 2's chain_log shows non-zero `cache_read_input_tokens` on the stable-prompt stages (triage, decider) and effectively zero on `research_plan` / `decision_framework`. Compare per-step `cost_usd` before/after; expect a 5–10× drop on input cost for cached stages from cycle 2 onward.

**Context:** Highest-leverage cost optimization on the stable prompts, with an explicit carve-out for the prompts the reflection loop is designed to evolve. The carve-out matters because the alternative — caching everything — silently raises the cost of every prompt-version flip and creates a perverse incentive for the human reviewer to delay activating reflection proposals.
**Depends on:** `runtime.call_llm`, `research_agent.run_research`, `reflection.request_proposals`, `orchestrator.load_prompts`.

---

## 6. Meta-reflection: LLM proposes *structural* improvements to the developer

**What:** Add a second reflection mode that doesn't propose prompt edits — it proposes **architecture / training / tooling** changes the human needs to implement. Output is a markdown report addressed to the developer, written to `data/dev_recommendations/{date}.md`, summarizing patterns in the chain log that prompt-tuning alone can't fix. Examples of what the report might say:

  - "Across 47 sports markets this week, `direction_correct` was 31% when the research stage didn't surface injury data, and 78% when it did. The research subagent currently does ad-hoc web search. **Recommendation: build a RAG index over the last 30 days of ESPN injury reports** so the subagent has a primary source instead of search results."
  - "62% of decisions in the 40–60¢ range were wrong; the same model called sub-10¢ and super-90¢ markets correctly 84% of the time. **Recommendation: fine-tune the decider on the historical decisions table, weighted toward the mid-range markets where it underperforms.** Provide a JSONL of (market context, findings, decision, observed outcome) tuples from the chain log."
  - "Tool-call budget is exhausted on 41% of research runs. Either bump `tool_call_budget` default from 8 to 12 in `research_plan_v1.md`, or **build a per-cycle research cache (TODO #2) — same fact is being looked up across correlated markets.**"
  - "The decider invokes `buy_no` on 4% of decisions but `buy_yes` on 73%. Either the prompt is biased toward YES, or NO-side opportunities are systematically under-surfaced upstream. **Recommendation: audit the triage prompt for YES-bias, or instrument the triage agent to log per-side scoring.**"
  - "Average market spread is 4¢ on trades the agent placed, but 7¢ on trades it skipped. The skip threshold may be too tight. **Recommendation: add `max_spread_cents` to the assumptions file and let the user tune it.**"

**Why:** The reflection loop currently has a tight scope by design — it only edits `research_plan` and `decision_framework` prompts. That's safe but capped: the agent can only get smarter within the design's architecture. Real performance gains often require *outside the box*: better data sources, fine-tuned models, RAG, new tools, schema changes to `assumptions.md`, restructured pipeline stages. Those changes need human implementation but the LLM is better positioned than the human to *spot the pattern in 10,000 chain_log rows* that justifies them. This closes the loop: prompts evolve automatically, architecture evolves with human-in-the-loop nudges.

**Pros:** Captures improvements that prompt-tuning structurally cannot reach. Gives the developer a prioritized punch list grounded in real data, not vibes. Surfaces "this isn't a prompt problem" early — saves cycles of pointlessly tuning a prompt when the real fix is a missing tool.

**Cons:**
- Risk of "AI sycophancy": the LLM will happily generate plausible-sounding recommendations even when none are warranted. Need a high evidence bar in the prompt (concrete N, concrete percentages, named patterns) — the same discipline as `reflection_v1.md`. Empty report on a quiet week is the right answer.
- Recommendations that involve fine-tuning or RAG aren't trivially actionable — the developer has to decide whether the expected lift justifies the engineering cost. The report should include a rough effort estimate alongside each recommendation.
- Hallucinated patterns are particularly dangerous here because the developer doesn't review every chain_log row before acting. Mitigation: every recommendation must cite specific cycle_ids / tickers the developer can audit by hand.

**Implementation sketch:**
1. New prompt `agent_trader/prompts/meta_reflection_v1.md`. System prompt instructs the model to:
   - Receive aggregated chain_log statistics + a sample of raw rows (sampling, not the whole log — context budget).
   - Identify ≥3 supporting examples per recommendation, cited by `(cycle_id, ticker)`.
   - Categorize each recommendation: `data` (RAG/index), `model` (fine-tune/swap), `tooling` (new agent tool), `pipeline` (new stage / dropped stage), `human_input` (assumptions.md fields the user should add).
   - Estimate effort: `xs` (≤1 day), `s` (1–3 days), `m` (1 week), `l` (>1 week).
   - Estimate confidence the change pays off: `low | medium | high`.
   - Refuse to recommend if evidence is thin. Empty array on a quiet week.
2. New schema `MetaRecommendation` in `schemas.py`:
   ```python
   class MetaRecommendation(BaseModel):
       category: Literal["data", "model", "tooling", "pipeline", "human_input"]
       title: str
       evidence: str  # the pattern, with stats
       supporting_cases: list[dict]  # [{cycle_id, ticker, note}]
       recommendation: str  # the actual proposed change
       effort: Literal["xs", "s", "m", "l"]
       confidence: Literal["low", "medium", "high"]
   ```
3. New module `agent_trader/meta_reflection.py`. Parallel structure to `reflection.py`:
   - `aggregate_chain_log_stats(conn, since_ms, until_ms)` — compute per-category aggregates the LLM can pattern-match on (decision direction-correct rates by market category, research-failure rates by datapoint type, tool-call exhaustion rates, skip rates, etc.).
   - `sample_chain_log_rows(conn, since_ms, until_ms, n=50)` — random sample of full decisions for the model to look at concretely.
   - `request_recommendations(...)` — LLM call returning `list[MetaRecommendation]`.
   - `write_recommendations(recs, period)` — render a markdown report to `data/dev_recommendations/{period_end_date}.md`. Include the evidence + supporting cases per recommendation. Sort by `confidence` desc, then `effort` asc.
4. New CLI `agent_trader/meta_reflect.py`. Same shape as `reflect.py`. Run cadence: weekly or monthly, not daily — needs enough data to spot patterns.
5. Wire into `active.yaml` as a new role `meta_reflection: meta_reflection_v1.md`, off the same loader.

**Watch out for:**
- This is the highest-context LLM call in the system. Budget carefully — likely opus, ~50–100K input. One run could cost $1–3. Add a `meta_reflection` budget cap separate from cycle / market caps.
- Don't let meta-reflection propose changes to its *own* prompt. Same carve-out as reflection — `meta_reflection_v1.md` is out of scope for itself.
- The "fine-tune" recommendation needs a sanity check before acting on it: fine-tuning a frontier API model is non-trivial (or impossible, depending on the provider). The recommendation should be framed as "build a fine-tuning dataset" — the developer decides whether to actually fine-tune, distill into a smaller model, or just use the dataset as a few-shot prefix.
- The recommendations are advisory — never auto-acted on. The developer reads, judges, picks zero or more, files them as new TODOs / tickets / PRs.

**Verification:**
- Seed the chain_log with synthetic biased data (e.g. force the decider to always pick `buy_yes`) and confirm meta-reflection catches it.
- After 2 weeks of real data, run it once and have a human grade each recommendation: "I would act on this" / "I see the pattern but it's not actionable" / "this is hallucinated." Goal: ≥50% in the first bucket. If lower, tighten the evidence bar in the prompt.

**Context:** This is the second loop on top of the first. The first loop (`reflection.py`) makes the agent better at executing the *current* design; this loop makes the *design itself* better. Both are needed — without #6, the agent is locked inside whatever architecture was correct at v1.
**Depends on:** Stable chain_log schema, meaningful trade volume (≥100 graded decisions to spot patterns), `runtime.call_llm`. Best implemented *after* a few weeks of live trading so there's enough data to be worth analyzing.


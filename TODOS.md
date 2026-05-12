# TODOS

Deferred work captured during plan reviews. Each item carries enough context to be picked up cold.

---

## 1. Tighten killswitch trigger latency

**What:** Add a time-windowed killswitch trigger to `runtime.py` alongside the existing cycle-counted rules.

**Why:** Current spec halts after "3 consecutive cycles with API errors." At the default 30-min cadence, that's 90 minutes of unattended drift on a broken state — too loose for a fund-and-forget posture.

**Pros:** Bursty transient failures halt fast; small code change.
**Cons:** Slightly noisier — short outages will trip more often.

**Proposed rule:** halt on `(3 consecutive cycles with API errors) OR (5 errors within any 30-min window)`, whichever fires first.

**Context:** See `Killswitch` paragraph in design doc. Trigger lives in `runtime.py`.
**Depends on:** `runtime.py` implementation (Day 1 in build order).

---

## 2. Per-cycle research cache in `research_agent.py`

**What:** In-memory cache keyed on `(cycle_id, normalized_query_hash)` so duplicate web searches inside a single cycle hit the cache instead of the network.

**Why:** When 5+ correlated markets in one cycle want the same fact (e.g. "today's Spurs injury report"), the research subagent web-searches it that many times. With the triage agent surfacing top-N by likely mispricing, correlated clusters are likely.

**Pros:** Cuts duplicate web_search costs; cheap implementation (dict cleared at cycle boundary).
**Cons:** Cache normalization is fiddly — "Spurs injury report" vs "Spurs injuries today" may not hash the same; LLM-driven query normalization adds its own cost.

**Context:** `research_agent.py`. Reflection's cost-per-cycle logs will show whether this is worth building — defer until you see the duplicate-query waste in chain logs.
**Depends on:** `research_agent.py` initial implementation (Day 3).

---

## 3. Web search tool cost & rate-limit audit

**What:** Document Anthropic web_search tool per-call cost and rate limits in `agent_trader/llm.py` config alongside the per-MTok rates.

**Why:** The budget-enforcement wrapper (per design doc) converts tokens → dollars but doesn't yet account for web_search call cost. With 20 markets × multiple searches each, this is a real cost component.

**Context:** Budget enforcement paragraph in design doc.
**Depends on:** `llm.py` wrapper implementation.

---

## 4. Naming-convention cleanup pass

**What:** Resolve ambiguous, inconsistent, or collision-prone names across the codebase. Each item below stands on its own — pick them off individually.

**Why:** Code currently mixes verbs, units, and abbreviations for the same concepts. Anyone (including future-you) reading cold has to keep a translation table in their head. The list below is what survived a full read-through where the convention was *not* absolutely clear.

### 4.1 Two functions named `get_tip_off_time` with different signatures
- [data_retrieval/helpers.py:27](data_retrieval/helpers.py#L27) — `(event_ticker, league_schedule) -> (ts, label)`
- [nba_trading/nba_scheduler.py:7](nba_trading/nba_scheduler.py#L7) — `(team_tri) -> utc_string`
- Same name, different inputs, different return types. Rename one (e.g. `get_tip_off_ts_for_event` vs `get_tip_off_utc_for_team`).

### 4.2 Two functions named `run_bot` in different `main.py` files — DONE (agent_trader side; nba_trading stub left alone)
- [agent_trader/main.py:4](agent_trader/main.py#L4) (stub)
- [nba_trading/main.py:265](nba_trading/main.py#L265) (real)
- Confusing when grepping. Rename to `run_agent_trader` / `run_nba_bot`.

### 4.3 Timestamp suffix inconsistency: `_ts` vs `_epoch` vs no suffix
- `market_close_epoch()` returns a Unix int — [data_retrieval/get_nba_volatility_data.py:66](data_retrieval/get_nba_volatility_data.py#L66)
- `tip_off_ts`, `start_ts`, `end_ts`, `close_ts` elsewhere — same concept, different suffix.
- `expiration_ts` is documented as "unix milliseconds" in [kalshi/client.py:158](kalshi/client.py#L158) but "unix seconds" in [nba_trading/main.py:122](nba_trading/main.py#L122). Unit conflict, not just naming — verify which is correct.
- Standardize on `_ts_seconds` / `_ts_ms` (explicit unit) or pick one suffix and document the unit.

### 4.4 Price/cents unit not always in the name — PARTIAL (agent_trader fields done; legacy nba_trading untouched)
- `yes_ask_cents`, `no_ask_cents`, `price_cents` (clear) vs `yes_bid`, `yes_ask`, `last_price`, `ask_price`, `price`, `safe_price` (unit only knowable from context).
- `EligibleMarket.yes_ask` is int cents — [agent_trader/market_discovery.py:14](agent_trader/market_discovery.py#L14) — but reads like a price-in-dollars.
- Suffix every cents-valued field with `_cents`.

### 4.5 Contract-count synonyms: `count`, `qty`, `shares`, `filled_count`, `SHARES_TO_BUY`, `position`
- Kalshi API uses `count`. [nba_trading/portfolio.py](nba_trading/portfolio.py) uses `qty` and `shares`. [agent_trader/](agent_trader/) uses `count` and `filled_count`. Config uses `SHARES_TO_BUY`.
- Pick one (`contracts` is unambiguous and matches the domain).

### 4.6 Underdog abbreviation: `und_*` vs full word
- `und_market`, `und_start`, `und_best`, `und_candles` alongside `fav_market`, `favorite_market`, `favorite_entry`.
- Either abbreviate both (`fav_`/`und_`) or neither.
- File: [data_retrieval/get_nba_volatility_data.py](data_retrieval/get_nba_volatility_data.py)

### 4.7 Tricode vs full team name in fields named `home_team` / `away_team`
- `Portfolio` and saved schedule JSON store tricodes ("LAL") under keys named `home_team` — [nba_trading/main.py:241](nba_trading/main.py#L241).
- Either rename to `home_tricode` / `away_tricode`, or store full names.

### 4.8 `coid` abbreviation — DONE
- [agent_trader/smoke.py:90](agent_trader/smoke.py#L90) uses `coid` for `client_order_id`. Nowhere else in the project. Expand it.

### 4.9 Verb inconsistency for HTTP retrieval: `fetch_*` vs `get_*` vs `list_*`
- `fetch_nba_markets`, `fetch_json`, `fetch_paginated_markets`, `fetch_event_candles`, `fetch_schedule_payload`, `fetch_all_candlesticks`
- `get_balance`, `get_orderbook`, `get_order_status`, `get_candlesticks`, `get_settled_markets`, `get_historical_cutoff`
- `list_markets`, `list_positions`
- Convention proposal: `list_*` for paginated collections, `get_*` for single resource, `fetch_*` internal-only helper. Standardize.

### 4.10 `process_event_pair` vs `process_event_task`
- Same conceptual action across two files; different verb. [data_retrieval/build_research_dataset.py:751](data_retrieval/build_research_dataset.py#L751) vs [data_retrieval/get_nba_volatility_data.py:304](data_retrieval/get_nba_volatility_data.py#L304). Pick one.

### 4.11 Module names: `build_research_dataset.py` vs `get_nba_volatility_data.py`
- Both build a CSV from Kalshi candles. Verb mismatch in filenames. Consider `build_*` for both.

### 4.12 `MAX_WORKERS` unqualified
- [data_retrieval/get_nba_volatility_data.py:16](data_retrieval/get_nba_volatility_data.py#L16) — bare `MAX_WORKERS = 30`. Sibling file uses qualified names (`MAX_EVENT_WORKERS`, `MAX_CANDLE_WORKERS_PER_EVENT`, etc.). Rename to `MAX_CANDLE_FETCH_WORKERS`.

### 4.13 `existing_tickers` vs `existing_dataset_keys`
- Same concept ("event tickers already in the CSV"), two different names across the two dataset builders. Unify.

### 4.14 `SCHEDULE_FILE` is a filename, not a path
- [kalshi/config.py:20](kalshi/config.py#L20). Compare with `HALT_FILE` in [agent_trader/runtime.py:23](agent_trader/runtime.py#L23) which is a `Path`. Either rename to `SCHEDULE_FILENAME` or make it a full path.

### 4.15 `cycle_id` is a millisecond timestamp, not an opaque ID
- [agent_trader/runtime.py:243](agent_trader/runtime.py#L243): `cycle_id = int(time.time() * 1000)`. The name implies an opaque PK; readers may assume monotonic counter. Either rename to `cycle_started_ms` or generate a real surrogate id.

### 4.16 `Action` literal mixes two grammatical forms
- `"buy_yes"`, `"buy_no"`, `"close_position"`, `"hold"`, `"skip"` — [agent_trader/schemas.py:8](agent_trader/schemas.py#L8). First two are `<verb>_<side>`; `close_position` is `<verb>_<object>`; `hold`/`skip` are bare verbs. Consider `"sell_yes"`/`"sell_no"` (explicit) instead of `close_position`, and treat `hold`/`skip` as distinct from action enum (they're no-ops).

### 4.17 `raw` field on dataclasses — DONE
- `EligibleMarket.raw`, `ExecutionResult.raw` — what does this contain? Rename to `raw_market_response` / `raw_order_response`.

### 4.18 Open-cycle / close-cycle / mark-cycle-skipped naming pattern — DONE (renamed to `skip_cycle`)
- `open_cycle`, `close_cycle`, `mark_cycle_skipped` — [agent_trader/runtime.py](agent_trader/runtime.py). Third one breaks the pattern. Either `skip_cycle` or rename the others to `mark_cycle_opened` / `mark_cycle_closed`.

### 4.19 `tripped` vs `_shutdown_requested` private-state convention
- `Killswitch.tripped` is public; module-level `_shutdown_requested` is underscore-prefixed. Two pieces of similar process-level state, two conventions. Pick one visibility rule.

### 4.20 Volume parsing locals: `raw_volume` vs `vol`
- [data_retrieval/build_research_dataset.py:77](data_retrieval/build_research_dataset.py#L77) uses `raw_volume`; [data_retrieval/get_nba_volatility_data.py:50](data_retrieval/get_nba_volatility_data.py#L50) uses `vol`. Same helper, different name.

### 4.21 `get_safe_max` is unclear without docstring
- [data_retrieval/get_nba_volatility_data.py:290](data_retrieval/get_nba_volatility_data.py#L290). "safe max" of what? Rename to `best_realistic_sell_price_cents` or `peak_yes_bid_close_cents`.

### 4.22 `m_a`/`m_b`, `market_a`/`market_b`, `pair`
- Generic "two markets per game" naming throughout dataset builders. Unclear whether `a`/`b` corresponds to home/away, yes/no, or arbitrary order. Rename to `home_market`/`away_market` or document the ordering invariant.

**Context:** This is cosmetic but compounds. Group fixes by file when picking these up so each PR is small and reviewable.

---

## 5. Wire up Anthropic prompt caching — selectively

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


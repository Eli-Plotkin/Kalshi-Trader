# Kalshi-Trader: Self-Improving Agentic Architecture

A cascaded LLM pipeline with grounded, replay-validated prompt evolution.

*Last updated: 2026-05-18*

> **Operating mode: paper trading (planned).** The system will run against simulated capital during the development phase via a paper-trading simulator (§4.5) that wraps the real Kalshi client. This shifts the automation boundary: prompt improvements auto-promote without human review (§9). The human gate is reintroduced as part of the go-live checklist, not before.
>
> **Current state:** the simulator is not yet built. The codebase has only `dry_run` (no orders sent) and `--live` (real money). Building the simulator is the immediate next step after the dry-run audit.

---

## 1. Thesis

An LLM agent that trades prediction markets, grades itself against reality, and proposes its own improvements — where every improvement is **measured on real past decisions before it ships**, not just argued for by another LLM.

Most "self-improving" systems either fine-tune on outcomes (lab-scale, opaque) or ask an LLM to critique itself (cheap but unreliable). Neither fits a small-scale trader that needs auditability, cheap iteration, and honest evaluation. The answer this system pursues is **measured prompt evolution over a replayable history**.

---

## 2. Architecture at a glance

```
┌────────────────────────────────────────────────────────────────┐
│                       LIVE TRADING LOOP                        │
│                       (every 30 min)                           │
│                                                                │
│   Markets ──► Triage ──► Plan ──► Framework ──► Research      │
│   (Kalshi)    (Haiku)   (Sonnet)  (Sonnet)     (Sonnet+web)   │
│                                                  │             │
│                                                  ▼             │
│                                              Decision (Opus)   │
│                                                  │             │
│                                                  ▼             │
│                                              Execute (Kalshi)  │
└──────────────────────────────────────────────────┼─────────────┘
                                                   ▼
                                          ┌──────────────────┐
                                          │   chain_log DB   │  ◄──┐
                                          │  (the dataset)   │     │
                                          └──────────────────┘     │
                                                   │               │
                ┌──────────────────────────────────┤               │
                ▼                                  ▼               │
   ┌──────────────────────┐         ┌──────────────────────┐       │
   │ DAILY GRADING JOB    │         │ DAILY REFLECTION     │       │
   │ (resolve + score)    │────────►│ (propose candidates) │       │
   └──────────────────────┘         └──────────────────────┘       │
                                                   │               │
                                                   ▼               │
                                       ┌──────────────────────┐    │
                                       │ EVALUATION LOOP      │────┘
                                       │ (replay on history)  │
                                       └──────────────────────┘
                                                   │
                                                   ▼
                                       ┌──────────────────────┐
                                       │ Proposed prompts +   │
                                       │ evidence pack        │
                                       └──────────────────────┘
                                                   │
                                                   ▼
                                              Human review
                                                   │
                                                   ▼
                                            active.yaml bump
```

---

## 3. Per-cycle pipeline

Five LLM calls per market, escalating in cost as evidence accumulates:

| Stage | Model | Job | Cacheable |
|---|---|---|---|
| Triage | Haiku | Reject ~90% of markets cheaply | ✅ |
| Research Plan | Sonnet | What questions matter? | ❌ (mutable) |
| Decision Framework | Sonnet | What criteria define a buy? | ❌ (mutable) |
| Research | Sonnet + web | Gather evidence (≤15 searches) | ✅ |
| Decision | Opus | Final call with `expected_outcome` | ✅ |

Each call's full input/output lands in `chain_log` with version IDs. The cascade isolates blame: a loss can be attributed to wrong research, wrong framework, or wrong judgment — each independently improvable.

---

## 4. What to build NOW: data collection

The single most important system component is the dataset. Everything in section 5 and beyond is worthless if the dataset underneath isn't trustworthy.

### 4.1 Replay invariant

Every decision row must contain enough state to **reconstruct the exact LLM call** under a different prompt later. That means storing, frozen at decision time:

1. Market context snapshot (ticker, price, volume, hours-to-close, position) — **not a live lookup**.
2. Research findings text — verbatim, not summarized, not by URL reference.
3. Active prompt version IDs (research_plan, decision_framework, decider) at the time.
4. Assumptions snapshot (bankroll, risk limits) as of then.
5. Model name + parameters (`claude-opus-4-7`, temperature, max_tokens).
6. Full Decision JSON including `expected_outcome` and `framework_criteria_hit`.
7. Ground truth — filled in later by the grading job.

### 4.2 Normalized schema (views over `chain_log` initially, promoted later)

```
decisions
  decision_id (PK)
  market_id, timestamp, model
  market_context_snapshot      -- frozen at decision time
  research_findings_text       -- verbatim
  research_plan_version_id     -- FK
  framework_version_id         -- FK
  assumptions_snapshot
  decision_json                -- includes expected_outcome
  schema_version               -- additive-only changes from here

grades
  decision_id (PK, UNIQUE)
  resolved_at
  direction_correct, eod_target_hit, resolution_match
  pnl_realized

prompt_versions
  version_id (PK), role, full_text
  activated_at, deactivated_at
  source (human | reflection | eval)
```

### 4.3 Build list

- [ ] Audit one recent `chain_log` row against the seven-item replay invariant; fix anything missing.
- [ ] Log rejected/held markets at the same fidelity as traded ones (else selection bias poisons evaluation).
- [ ] Snapshot full prompt text into `prompt_versions` on activation. Never reference by filename alone.
- [ ] Add `schema_version` column; commit to additive-only changes.
- [ ] Split the daily grading job out of reflection. Make it idempotent with `UNIQUE(decision_id)` on grades.
- [ ] Nightly SQLite `.backup` to S3 (or any second location).
- [ ] Dataset health script: count graded decisions by month, by prompt version, by market category. Run weekly.

### 4.4 Token spend impact: **~0**

These are all schema, storage, and bookkeeping changes. No new LLM calls. The only cost is whatever marginal logging size adds to `chain_log` (negligible — kilobytes per decision). Logging rejected markets at full fidelity means we *retain* triage and (where applicable) early-stage outputs that already ran — we are not running new inference on them.

**Estimated daily delta: $0.**

---

## 4.5 Paper-trading simulator (build right after the dry-run audit)

The system as written has only two modes: `dry_run` (no orders sent) and `--live` (real money on the production Kalshi endpoint at [kalshi/config.py:6](kalshi/config.py#L6)). There is no demo/sandbox URL wired up. The "paper mode" framing throughout this doc assumes a third mode that doesn't exist yet — this section is the work to make it real.

### 4.5.1 Why dry_run isn't sufficient

`dry_run` skips order placement but produces no simulated fills, no synthetic cash movement, and no positions to reconcile against. That means:

- Grading can never compute `pnl_realized` — the orders never "filled."
- Position-aware logic (closing positions, sizing relative to current exposure) never exercises in dry_run.
- The auto-promotion + auto-rollback machinery in §9 has nothing to measure live performance against.

Paper trading needs simulated fills that flow through the same code paths as real fills, so the system can't tell the difference.

### 4.5.2 Design

A `PaperKalshiClient` that wraps the real `KalshiClient`:

- **Reads** (markets, prices, volume, hours-to-close) pass through to the real Kalshi API. The market data must be real for decisions to be meaningful.
- **Writes** (order placement, cancellation) are intercepted. The wrapper:
  - Assumes immediate fill at the order's limit price if the market quote crosses it; otherwise records the order as resting.
  - Writes a synthetic fill record to a new `paper_orders` table.
  - Updates a synthetic portfolio state in a new `paper_portfolio` table.
- **Position/cash reads** (`reconcile()` in [runtime.py:138](agent_trader/runtime.py#L138)) read from `paper_portfolio` instead of Kalshi when the wrapper is active.
- **Resolution** (market closing at YES=100¢ or NO=100¢) is observed from real Kalshi and applied to paper positions by a background job — same code path as real settlement, just operating on the paper tables.

A single flag (env var or CLI arg) toggles `PaperKalshiClient` vs `KalshiClient`. Everything downstream — orchestrator, grading, reflection — is unaware.

### 4.5.3 Fill model: start simple, document the simplification

Real fills depend on order book depth, queue position, time priority, and luck. A perfect simulator is its own research project. Start with the cheapest defensible model:

- **Marketable limit orders fill 100% at the limit price** if the current best opposing quote crosses it.
- **Non-marketable orders rest** and are checked each cycle against the current quote; fill if the quote crosses, expire after N cycles.
- **No partial fills, no slippage, no queue modeling.**

Document this model in the simulator's docstring and in the schema (`paper_orders.fill_model_version`) so future evaluations know what assumptions baked into the data. When you go live, the eval-vs-live drift watcher in §9.3 will tell you how wrong this simplification was — and that's the signal to invest in a better model.

### 4.5.4 Build list

- [ ] `PaperKalshiClient` class wrapping `KalshiClient`, same interface.
- [ ] New tables: `paper_orders`, `paper_portfolio`, `paper_fills`.
- [ ] Fill resolver: each cycle, walk resting orders, fill any that the current quote crosses.
- [ ] Settlement job: when a real Kalshi market resolves, mark matching paper positions as resolved at 100¢/0¢, write `pnl_realized` into the `grades` table.
- [ ] CLI flag: `python -m agent_trader.scheduler --paper` or env var `AGENT_TRADER_MODE=paper`.
- [ ] Killswitch reads paper cash when in paper mode (so the `$0.50` floor works against simulated balance).
- [ ] Initial paper bankroll set in `assumptions_v1.md` or a config — start with $1000 or whatever matches your intended go-live scale.
- [ ] Documentation: explicit go-live transition checklist — what changes when flipping from `--paper` to `--live`.

### 4.5.5 Token spend impact: **~0**

No new LLM calls. Pure code + storage layer between the orchestrator and Kalshi.

---

## 5. What to build LATER (Phase 2): the evaluation loop

Prerequisite: ~200 graded decisions in the dataset. Until then, scores are too noisy to trust and the current human-gated reflection is the right call.

### 5.1 Design

Replaces "LLM suggests one prompt → human eyeballs it" with "LLM suggests several prompts → code measures each on real past markets → human reviews only the winners with evidence."

```
INPUTS
  current prompt (v3)
  graded history — training set: rolling, oldest N days
  graded history — held-out set: most recent 30 days (chronological split)

STEP 1   Reflection LLM proposes K candidate prompts (v3.a .. v3.k)

STEP 2   For each candidate × each market in training set:
            replay decision using stored findings + new prompt
            score against stored grade

STEP 3   Rank candidates. Pick top 2.

STEP 4   Replay top 2 against held-out set.

STEP 5   If a candidate beats current on BOTH sets by margin > noise floor:
            write to proposed_prompts/ with evidence pack
         Else:
            ship nothing today
```

### 5.2 Build list

- [ ] Factor the Decision stage into a pure function `(market_ctx, findings, prompt_text) → Decision`.
- [ ] Scoring function: weighted sum of existing grade fields (`direction_correct`, `eod_target_hit`, `pnl_realized`).
- [ ] Chronological train/held-out split utility.
- [ ] Candidate generator (reuse current reflection LLM, but ask for K variants not 1).
- [ ] Replay runner with concurrency + cost guardrail.
- [ ] Evidence pack writer (per-market diff of decisions, score comparison, markets where the candidate changed the call).
- [ ] Noise-floor calibration: replay the *current* prompt against itself with temperature=0 to establish a baseline variance.

### 5.3 Token spend impact

Per evaluation run, assuming:
- K = 8 candidates
- Training set = 100 markets
- Held-out replay = top 2 × 30 markets = 60 markets
- Replay uses the **Decision stage only** (research findings are reused from storage — that's the whole point of the replay invariant)
- Decision call ≈ 8K input tokens, 1K output tokens on Opus

Per replay (Opus @ $15/M input, $75/M output, roughly):
- Input: 8K × $15/M = $0.12
- Output: 1K × $75/M = $0.075
- ≈ **$0.20 per replay**

Per daily evaluation run:
- Training: 8 × 100 = 800 replays × $0.20 = **$160**
- Held-out: 60 replays × $0.20 = **$12**
- Candidate generation: ~5 reflection calls × $0.30 = **$1.50**
- **Total: ~$175/day** if run every day with these parameters.

Practical knobs:
- Run weekly, not daily → **~$25/day amortized**.
- Use Sonnet for replay (not Opus) → ~5× cheaper → **~$5/day amortized weekly**.
- Reduce K to 4, training to 50 markets → cuts cost 4× independently.

**Recommended starting config: weekly run, K=4, Sonnet replays, 50-market training set → ~$10/week (~$1.50/day amortized).** Scale up only if measured prompt improvements justify it.

Prompt caching on the replay path (the candidate prompt is stable across all 50 markets within a run) should cut input cost ~10× on the cached portion. Real-world cost likely lower than the figures above.

---

## 5.5 What to build LATER (Phase 2.5): per-stage model evaluation

The prompt-evaluation loop in §5 holds the *model* fixed and varies the *prompt*. A second harness — sharing the same replay infrastructure — does the inverse: holds the *prompt* fixed and varies the *model*. It answers questions like:

- Does Sonnet-4.6 match Opus-4.7 at the Decision stage on 80% of market types?
- Is Haiku-4.5 sufficient for the Research Plan, or does it materially hurt the downstream Decision?
- When a new model ships, does it Pareto-dominate the current one (better score *and* cheaper)?

The current `Haiku → Sonnet → Opus` escalation is a guess. This harness turns it into a measurement.

### 5.5.1 Per-stage scoring

Each pipeline stage gets its own scorecard with its own metric — they are different judgment tasks and should not be reduced to a single score:

| Stage | Metric | Source |
|---|---|---|
| Triage | Precision/recall vs eventual-traded markets | `decisions` + `grades` filtered by triage outcome |
| Research Plan | Coverage of `required_datapoints` in resulting findings | Findings text vs plan |
| Decision Framework | Win rate when `framework_criteria_hit=true` | `grades` joined to framework version |
| Research | Factuality sample-audit + coverage of plan questions | Manual sample + automated coverage check |
| Decision | `direction_correct`, `eod_target_hit`, realized PnL | `grades` |

### 5.5.2 Design

```
INPUTS
  fixed prompt (current active version)
  model lineup, e.g. [opus-4-7, sonnet-4-6, haiku-4-5]
  graded history — same chronological split as §5

For each stage in pipeline:
  For each model × each market in training set:
    replay this stage using stored upstream inputs + this model
    score against stored grade

Output:
  Per-stage table: model × score × cost × p50 latency
  Pareto frontier (score vs $/decision)
  Routing recommendation: "use model M for stage S unless condition C"
```

### 5.5.3 Build list

- [ ] Extend the replay runner with a `models: list[str]` sweep dimension.
- [ ] Per-stage scoring functions (table in §5.5.1).
- [ ] Cost-adjusted ranking: rank by score-per-dollar, plot Pareto frontier.
- [ ] Persist results to a `model_scorecards` table: `(stage, model, prompt_version_id, run_date, score, cost_per_call, p50_latency, sample_size)`.
- [ ] **Reuse stored research findings.** Do not re-run web search on replay — you're measuring the model's *use* of findings, not its ability to re-fetch them.

### 5.5.4 When to run

Not on a daily cadence. Triggers:
- A new Claude model ships.
- Anthropic deprecates a model used in the cascade.
- Quarterly review — has the lineup drifted from optimal?

### 5.5.5 Token spend impact

Per full bake-off, assuming:
- 4 models × 100 markets × 5 stages = 2000 replays
- Average stage cost weighted across model tiers ≈ $0.10/replay (Opus on some, Haiku on others)
- Total per run: **~$200**

Run quarterly (~4×/year) = **~$800/year**, amortized **~$2/day**.

Cheaper variants:
- Bake off only the Decision stage when evaluating a new model (the others change rarely) → **~$50/run**.
- Skip the Research stage bake-off entirely on token budget grounds; do that one manually with a small sample (live web search is the cost driver there, not the model).

**Recommended amortized: ~$2/day** at quarterly cadence, full pipeline bake-off.

A separate, more expensive eval — "would the new model *search* better?" — requires live tool calls and is deferred indefinitely. The cost ceiling there is unbounded (depends on how much web search the model decides to do); don't build it until there's a specific question it answers.

---

## 6. What to build LATER (Phase 3): retrieval-based memory

The replayable dataset built for Phase 2 is also the foundation for a memory layer. Same data, second use.

### 6.1 Design

At decision time, before the Decision LLM call:
1. Embed the current market context.
2. Retrieve the 5 most similar prior *graded* decisions from the dataset.
3. Inject as few-shot examples into the Decision prompt, annotated with their actual outcomes.

This shifts the system from paradigm 6 (agentic framework) toward paradigm 2 (memory-based learning) in industry terms — a Voyager-style skill library, but over decisions instead of code snippets.

### 6.2 Build list

- [ ] Embedding pipeline: backfill embeddings for existing graded decisions, embed new ones on grade.
- [ ] Vector index (SQLite + sqlite-vec is sufficient at this scale; no Pinecone needed).
- [ ] Retrieval function: similarity search filtered by market category, recency window, and grade availability.
- [ ] Few-shot formatter: render retrieved decisions as compact examples with outcome annotation.
- [ ] A/B comparison: run with/without retrieval over a fixed market set, compare scores. (Use the Phase 2 evaluation loop for this.)

### 6.3 Token spend impact

**Embedding cost** (one-time + ongoing):
- Voyage or OpenAI embeddings ~$0.02/M tokens.
- Per decision: market context + findings ≈ 5K tokens → $0.0001 per embedding.
- Backfill 1000 historical decisions: $0.10 total.
- Ongoing: ~$0.005/day at 50 decisions/day. **Negligible.**

**Decision prompt growth:**
- 5 few-shot examples × ~2K tokens each = +10K input tokens per Decision call.
- At 50 decisions/day on Opus: 50 × 10K × $15/M = **$7.50/day**.
- With prompt caching on the few-shot block (stable within a cycle but changes per market — caching helps less here): assume 30% cache hit rate → **~$5/day**.

**Total Phase 3 ongoing: ~$5/day.**

Cheap enough to run continuously. The honest question is whether retrieval actually helps — answered by running it through the Phase 2 evaluation harness before turning it on in production.

---

## 7. What to build LATER (Phase 4): meta-reflection

Currently sketched in TODOS.md #6. Runs weekly/monthly over the full dataset and proposes **structural** changes — new tools, new pipeline stages, RAG indexes, fine-tuning datasets — rather than just prompt edits. Output: `data/dev_recommendations/{date}.md`, human-gated.

Phase 4 only makes sense after Phases 1–3 are stable and the dataset is large enough to support claims like "across 2000 decisions, injury-keyword markets underperformed by X% — consider a dedicated injury index."

**Token spend impact:** one large LLM call per run reading aggregated stats (not raw decisions). ~$2–5 per run. **Negligible.**

---

## 8. Lifecycle of a single market (paper mode)

```
T+0      Market discovered
T+0      Triage → keep
T+0      Plan / Framework / Research / Decision → BUY YES @ 42¢
         │  → logged to decisions table with full snapshot
         │
T+1d     Market resolves YES @ 100¢
         │  → grading job writes row to grades table
         │
T+2d     Reflection reads this decision (and ~50 others)
         │  → proposes K framework variants
         │  → evaluation loop replays them on history
         │
T+2d     Winning candidate beats current by 4pts (train), 3pts (held-out)
         │  → passes auto-promotion safety checks (§9.2)
         │  → active.yaml auto-bumped; promotion record written
         │
T+2d+    All future decisions use the new prompt
         │  → live performance vs eval projection tracked (§9.3)
         │  → becomes evaluation data for future iterations
```

The human is no longer in the cycle. They review *after the fact* via the promotion track record dashboard, not before each ship.

---

## 9. Automation boundary

### 9.1 Paper-mode default: auto-promotion

| Step | Who (paper) | Who (live) |
|---|---|---|
| Market discovery, triage, decisions, execution | LLM | LLM |
| Grading against outcomes | Code | Code |
| Candidate prompt generation | LLM | LLM |
| Candidate evaluation on history | Code | Code |
| **Activation of new prompt** | **Auto** (subject to §9.2) | **Human (canary → full)** |
| Schema changes, new pipeline stages | Human | Human |

**Why auto in paper:** the cost of a bad ship is zero real dollars; the cost of a slow ship is lost learning. The whole point of the paper period is to accumulate the eval-vs-live track record that tells you whether the eval is trustworthy. A human gate during paper trading throws away exactly the data you need to make the go-live decision.

You *want* some bad prompts to ship in paper. They reveal blind spots in the eval. A regime where every shipped prompt wins is one where the eval has learned to endorse only safe-looking changes — exactly the conservative drift to avoid.

### 9.2 Auto-promotion safety checks

Even with auto-promotion, a small set of checks must pass before a candidate ships. These exist not to protect capital (there is none) but to protect the experiment:

- [ ] **Schema validity.** Decision JSON shape under the new prompt matches the active schema. Run the candidate against 5 sample markets; if any output fails parsing, reject.
- [ ] **Cost ceiling.** Candidate prompts that materially raise inference cost (e.g., `tool_call_budget` above ceiling, output length doubled) are rejected. Real-dollar token spend matters even in paper.
- [ ] **Protected sections untouched.** Risk language, kill-switch instructions, and position-sizing rules are tagged in the prompt source. Edits within those tags require human review even in paper — to keep guardrail muscle memory intact for go-live.
- [ ] **Eval margin > noise floor.** Improvement must exceed the calibrated noise floor (§5) on *both* training and held-out sets. Otherwise it's not a real win, just variance.
- [ ] **Rate limit.** At most one auto-promotion per role per day. Prevents thrashing if reflection latches onto a local optimum and proposes near-identical variants repeatedly.

Failures are logged but don't auto-revert. The next reflection cycle sees them and proposes alternatives.

### 9.3 Auto-rollback watcher (paper)

A background job tracks each auto-promoted prompt against its eval projection:

```
For each prompt activated in the last 30 days:
  observed_score = score(live decisions since activation)
  expected_score = eval projection at promotion time
  drift          = expected_score - observed_score

  If drift > tolerance and sample_size >= 30 decisions:
    write to data/promotion_alerts/{date}.md
```

In paper mode the watcher alerts but does not auto-revert. The reflection cycle will propose a fix on its own — the alert is for the human dashboard. (In live mode this becomes auto-revert; see §9.5.)

### 9.4 Promotion track record

A first-class view in the dataset health dashboard:

- Per prompt activation: eval-projected score, 30-day observed score, delta.
- Aggregate: what fraction of eval-endorsed prompts actually outperformed in live?
- This is the **meta-eval**. It tells you whether the eval works. It is the single most important number for the go-live decision.

### 9.5 Go-live checklist (when paper ends)

Auto-promotion is a paper-mode convenience. Re-introducing the human gate is part of the go-live transition, not a separate project. Required before flipping to real capital:

- [ ] **Meta-eval threshold met.** Eval-endorsed prompts outperform their projection in ≥ X% of cases over ≥ N activations. Pick X and N before looking at the data.
- [ ] **Reintroduce human gate** on prompt activation as the default. Auto-promotion becomes opt-in per role.
- [ ] **Canary deploy.** New prompts ship to a fraction of live markets first; full traffic only after live-vs-eval drift stays within tolerance for N decisions.
- [ ] **Auto-rollback enabled.** The watcher in §9.3 gains the authority to revert `active.yaml` if drift exceeds tolerance with sufficient sample size.
- [ ] **Protected sections audited.** Risk language, kill-switch, position sizing — verify the protected-section tagging actually covers everything it should.
- [ ] **Rate limits tightened.** At most one promotion per role per week in live, not per day.

---

## 10. Position vs industry methodologies

| Methodology | This system |
|---|---|
| ReAct / planner-executor cascade | ✅ Core architecture |
| Embedded self-prediction (`expected_outcome`) | ✅ At every decision |
| Prompt evolution (DSPy/OPRO-style) | ⏳ Phase 2 — validation-gated, hand-rolled |
| Model routing / cost-quality Pareto search | ⏳ Phase 2.5 — per-stage bake-offs |
| Episodic memory / skill library | ⏳ Phase 3 — built on the same dataset |
| Meta-reflection on structure | ⏳ Phase 4 |
| RLHF / DPO / trajectory RL | ❌ Out of scope |
| Test-time compute (o-series RL) | ⚠️ Opus-on-final-call only |
| Multi-agent debate | ❌ Not pursued |

**Position:** frontier-adjacent on prompt evolution, conservative on weights, current gap on memory closes in Phase 3.

---

## 11. Risks & guardrails

| Risk | Mitigation |
|---|---|
| Overfitting prompts to history | Held-out chronological split, no random sampling |
| Schema drift breaks old data | `schema_version` column, additive-only changes |
| Mutable prompt files corrupt attribution | Prompt text snapshotted into DB on activation |
| Grading job races / duplicates | `UNIQUE(decision_id)` |
| Selection bias (only logging trades) | Log rejected markets at full fidelity |
| Lost dataset | Nightly SQLite `.backup` to S3 |
| Bad prompt ships in paper | Acceptable — generates eval-vs-live signal. Auto-rollback watcher (§9.3) alerts. |
| Bad prompt ships in live | Human gate + canary + auto-revert reintroduced at go-live (§9.5) |
| LLM critic endorses bad ideas without evidence | Phase 2 evaluation loop is the answer |
| Evaluation loop cost runs away | Weekly cadence, Sonnet for replay, K and N as knobs |
| Auto-promotion ships a malformed prompt | Schema-validity check in §9.2 — sample-replay before activation |
| Auto-promotion erodes risk guardrails | Protected-section tags; edits within them require human review even in paper |
| Reflection latches onto local optimum | One-promotion-per-role-per-day rate limit (§9.2) |

---

## 12. Token spend summary

| Phase | What | Daily delta (amortized) |
|---|---|---|
| 1 — Data collection | Schema fixes, logging, backups, health checks | **~$0** |
| 2 — Prompt evaluation loop | Weekly run, K=4 candidates, Sonnet replays, 50-market train | **~$1.50** |
| 2.5 — Per-stage model evaluation | Quarterly bake-off across model lineup | **~$2** |
| 3 — Retrieval memory | Embeddings + 5 few-shot per Decision call | **~$5** |
| 4 — Meta-reflection | Monthly structural recommendations | **~$0.10** |
| **Total at full build-out** | | **~$9/day** on top of current trading spend |

For context, full build-out adds roughly the cost of one extra Opus decision per cycle. The dataset infrastructure that enables it costs nothing in tokens — only engineering time.

---

## 13. Sequence

```
NOW (day 1)
  ├─ Run a real dry_run cycle (--no-research)
  └─ Audit a real `decision` row against the seven-item invariant (§4.1)

THEN (week 1) — paper-trading simulator (§4.5)
  ├─ PaperKalshiClient wrapping KalshiClient
  ├─ paper_orders / paper_portfolio tables
  ├─ Naive fill model, documented
  ├─ Settlement job → grades table
  └─ --paper flag wired through scheduler + killswitch

THEN (week 1–2) — DB schema fixes (§4.2)
  ├─ Apply fixes informed by the audit
  ├─ Log rejected markets at full fidelity
  ├─ Snapshot prompt text on activation
  ├─ Idempotent grading job, split from reflection
  └─ Nightly backup

NEXT (weeks 2–4, paced by data volume)
  ├─ Dataset health dashboard
  └─ Wait for ~200 graded decisions

THEN (~month 2) — Phase 2
  ├─ Build prompt evaluation loop (§5)
  ├─ Calibrate noise floor
  ├─ Auto-promotion safety checks (§9.2)
  ├─ Auto-rollback watcher in alert-only mode (§9.3)
  ├─ Promotion track record dashboard (§9.4)
  └─ Ship first auto-promoted prompt

ALSO (~month 2) — Phase 2.5
  ├─ Extend replay runner to sweep models (§5.5)
  ├─ Per-stage scorecards persisted to DB
  └─ First full bake-off → routing recommendations

PAPER PHASE ENDS — go-live checklist (§9.5)
  ├─ Confirm meta-eval threshold met
  ├─ Reintroduce human gate as default
  ├─ Canary deploy + auto-revert authority
  └─ Audit protected sections

LATER (~month 3+) — Phase 3
  ├─ Embedding index over graded decisions
  ├─ Retrieval-augmented decision prompt
  └─ A/B through the Phase 2 harness

EVENTUALLY — Phase 4
  └─ Meta-reflection on structural improvements
```

---

> **Build the dataset right. The intelligence follows.**

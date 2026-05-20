# Kalshi-Trader

> **`agent_trader/` is paused at `v0-frozen`.** Pipeline runs end-to-end and
> first real decision rows are verified in `chain_log`. Halted before meaningful
> forward paper trading concluded. See [Project status](#project-status) for what
> shipped, what's reusable across other projects, and the two structural reasons
> it stopped here. The other packages (`kalshi/`, `nba_trading/`, `data_retrieval/`)
> are unaffected.

Monorepo for [Kalshi](https://kalshi.com) trading experiments. Four
independent packages on top of one shared HTTP client:

| Package           | What it is                                                                 | Entry point                                  |
| ----------------- | -------------------------------------------------------------------------- | -------------------------------------------- |
| `kalshi/`         | Signed-request HTTP client + shared credentials. No business logic.        | _imported by everything else_                |
| `agent_trader/`   | Autonomous LLM-driven research-and-trade agent (this README's focus).      | `python -m agent_trader.scheduler`           |
| `nba_trading/`    | NBA "favorite in price range" bot. Bid only when ask ∈ [MIN, MAX], expires at tip-off. | `python -m nba_trading.main`     |
| `data_retrieval/` | Kalshi historicals — NBA volatility, research datasets. Self-contained.    | `python -m data_retrieval.build_research_dataset` |

Dependency direction is strictly downward:
`agent_trader` → `kalshi`, `nba_trading` → `kalshi`, `data_retrieval` → (nothing).
The two trading packages do not import each other and share no config.

This README's runbook is for `agent_trader`. The other two are documented
in their own files / docstrings.

---

## What `agent_trader` does

Every cycle (default: every 30 minutes), the agent:

1. **Reconciles** cash + positions from Kalshi.
2. **Discovers** open markets meeting volume / time-to-close filters.
3. **Triages** the universe with Haiku → top-N candidate tickers.
4. For each surfaced ticker, runs the per-market pipeline:
   - **ResearchPlan** (Sonnet) — what questions to answer, what datapoints are required.
   - **DecisionFramework** (Sonnet) — what edge / confidence / size rules apply.
   - **Findings** — Sonnet + Anthropic server-side `web_search` tool.
   - **Benchmark** — gate: are required datapoints answered + avg confidence ≥ threshold?
   - **Decision** (Opus) — buy / sell / pass, with `expected_outcome` block for later grading.
   - **Execute** — Kalshi limit order (skipped in dry-run).
5. **Closes** the cycle and writes everything to `data/agent_log.sqlite`.

Once a day (default 23:55 UTC), a **reflection** job re-reads the chain log
for the prior 24h, grades each decision against its `expected_outcome` and
the actual market state, and writes proposed prompt revisions to
`data/proposed_prompts/` — a human reviews and activates them manually.

Killswitch trips on: halt file, cash-floor breach, consecutive cycles with
API errors, error rate spike in the last 30 min, or consecutive malformed
LLM responses.

---

## Setup

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# fill in API_KEY_ID, PRIVATE_KEY_PATH, ANTHROPIC_API_KEY
```

Sanity checks before you ever pass `--live`:

```bash
# 1. Kalshi credentials work, can read balance + markets
python -m agent_trader.smoke

# 2. Full pipeline against real markets, no orders, no web search
python -m agent_trader.dry_run --no-research

# 3. Full pipeline + real web search, still no orders
python -m agent_trader.dry_run
```

Review `data/agent_log.sqlite` after each step:

```bash
sqlite3 data/agent_log.sqlite \
  "SELECT step, ticker, substr(payload_json, 1, 200) FROM chain_log
   ORDER BY cycle_id DESC, ts LIMIT 50"
```

---

## Going live

> Reference for the historical record. The agent is paused at `v0-frozen` and
> never went live with real money. The sections below document the intended
> runbook; see [Project status](#project-status) for why the project stopped
> before this path was exercised.

Read [agent_trader/prompts/assumptions_v1.md](agent_trader/prompts/assumptions_v1.md)
first — its bankroll / risk numbers are baked into every prompt.

Shake-out path (do this before turning on the scheduler):

```bash
# One live cycle, with a tight per-cycle budget.
python -m agent_trader.dry_run --live --cycle-budget 1.00 --top-n 1
```

Watch a single cycle hit Kalshi, place at most one order, settle. Cancel
it manually from the Kalshi UI if you want to bail.

Then start the scheduler:

```bash
# Foreground (recommended first day, so you can read logs).
python -m agent_trader.scheduler --live

# Background.
nohup python -m agent_trader.scheduler --live > scheduler.log 2>&1 &
echo $! > scheduler.pid
```

Scheduler defaults:

| flag                     | default | meaning                                            |
| ------------------------ | ------- | -------------------------------------------------- |
| `--cycle-interval-min`   | 30      | minutes between cycles                             |
| `--reflection-hour-utc`  | 23      | hour-of-day (UTC) for daily reflection job        |
| `--reflection-minute-utc`| 55      | minute-of-hour for daily reflection job           |
| `--reflection-budget`    | 1.50    | USD cap for the reflection LLM call                |
| `--top-n`                | 5       | tickers surfaced by triage per cycle              |
| `--market-budget`        | 0.50    | USD cap per market per cycle                       |
| `--cycle-budget`         | 3.00    | USD cap per cycle, summed across all markets       |
| `--min-volume`           | 100     | minimum 24h volume to be eligible                  |
| `--min-hours`            | 48      | minimum hours-to-close to be eligible             |
| `--live`                 | off     | actually place orders                              |
| `--no-research`          | off     | use stub findings instead of web search           |
| `--run-once`             | off     | run a single cycle and exit                       |

Overlapping cycles are dropped (`max_instances=1`, `coalesce=True`); if a
cycle runs long, the next scheduled fire is skipped, not queued.

---

## Stopping

Two ways, both graceful:

```bash
# 1. Halt file. Next cycle will trip the killswitch and exit cleanly.
touch ~/.kalshi-agent.halt

# 2. Signal. Current cycle finishes its current market, then the loop exits.
kill $(cat scheduler.pid)        # SIGTERM
# or Ctrl-C in the foreground
```

To resume after a halt:

```bash
rm ~/.kalshi-agent.halt
python -m agent_trader.scheduler --live
```

`SIGKILL` is fine as a last resort — SQLite is in WAL mode and every step
commits before the next one starts, so the chain log won't corrupt. But
you may be left with a working open order on Kalshi; check the UI.

---

## Reading the chain log

Everything the agent does — per stage, per market, per cycle — is
appended to `data/agent_log.sqlite`. Useful queries:

```bash
# Recent cycles + status
sqlite3 data/agent_log.sqlite \
  "SELECT cycle_id, status, notes FROM cycles
   ORDER BY started_at DESC LIMIT 10"

# What got executed
sqlite3 data/agent_log.sqlite \
  "SELECT placed_at, ticker, action, side, count, price_cents, status
   FROM orders ORDER BY placed_at DESC LIMIT 20"

# Full chain for a specific market in a specific cycle
sqlite3 data/agent_log.sqlite \
  "SELECT step, payload_json FROM chain_log
   WHERE cycle_id = 1715520000000 AND ticker = 'KXNBAGAME-...'
   ORDER BY ts"

# Killswitch history
sqlite3 data/agent_log.sqlite \
  "SELECT ts, kind, detail FROM killswitch_events ORDER BY ts DESC"
```

---

## Reflection: human-in-the-loop prompt updates

The daily reflection job writes proposals to `data/proposed_prompts/`. It
**never** edits `agent_trader/prompts/`. To activate a proposal:

```bash
# 1. Read the proposal + its sidecar metadata
cat data/proposed_prompts/research_plan_v2.md
cat data/proposed_prompts/research_plan_v2.md.meta.json

# 2. If you like it, drop it into the active prompts dir
cp data/proposed_prompts/research_plan_v2.md agent_trader/prompts/

# 3. Point active.yaml at it
$EDITOR agent_trader/prompts/active.yaml
# change:   research_plan: research_plan_v1.md
# to:       research_plan: research_plan_v2.md
```

Only `research_plan` and `decision_framework` are reflection-eligible.
`assumptions`, `triage`, `decider`, `research_subagent`, and `reflection`
itself are human-only.

You can also run reflection on demand:

```bash
python -m agent_trader.reflect --since 24h
python -m agent_trader.reflect --since 7d --budget 3.00
```

---


## What never shipped

The project froze before reaching paper-trading at scale, so several items
on the original roadmap never landed:

- **Paper-trading simulator (`PaperKalshiClient`).** Wraps the real client
  for reads, writes synthetic fills locally for orders. Without this the
  agent only has `dry_run=True` (no orders at all) or `--live` (real money),
  with no honest forward-evaluation middle ground.
- **Grading pipeline.** Joining `6_decision` rows to actual market resolution
  outcomes so reflection has a `Findings` → `Decision` → `Outcome` chain
  to score against.
- **Meta-reflection.** LLM proposes structural improvements (new tools, RAG,
  schema changes) to the developer, not just prompt edits.
- **Shadow-prompt evaluation.** Run current + proposed prompts on the same
  forward markets in parallel for paired-difference statistics. Needed
  before reflection can promote changes autonomously without overfitting.

[Project status](#project-status) for why these stopped being priorities.

---

## Safety reminders

If this is ever resumed and pointed at real money, the rules below were
the operating discipline:

- The killswitch is conservative on purpose. Don't raise its thresholds
  without reading the trip conditions in
  [agent_trader/runtime.py](agent_trader/runtime.py).
- Always run `dry_run` after editing any prompt, schema, or model assignment.
- `active.yaml` is the only source of truth for which prompt version is live.
  Don't edit prompt files in-place — bump the version.

---

## Project status

`agent_trader/` is paused at `v0-frozen`. The pipeline runs end-to-end —
discovery → coarse filter → triage → per-market plan/framework/research/decider —
with parallel execution, shared prompt caching, and full chain_log auditing.
The first real `6_decision` rows are verified: the decider correctly identified
directional edge on an NBA market and refused to size because the framework's
data-completeness criteria weren't met. The infrastructure works.

It's paused because the trading thesis hits two structural problems that
can't be engineered around at the sample sizes that were practical to gather.
Both are documented below so future-me — or anyone reading this — doesn't
re-derive them the hard way.

### Why it was paused

**1. Self-improvement overfits to variance.** The reflection loop reads
recent decisions, identifies failure patterns, and proposes prompt edits.
The trap is sample size: at 20-50 graded paper trades, the win-rate signal
is dominated by variance. A prompt change that lifts win rate from 45% to
55% over 30 trades has a confidence interval that comfortably includes "no
real change." Reflection will "learn" patterns from noise and write prompt
edits that fit historical luck.

The honest workaround is statistical — hold out recent cycles, require
Wilson confidence intervals to exclude zero, constrain diffs to one role
at a time, optimize on calibration (Brier score) as a leading indicator
that converges faster than win rate. All doable, none cheap in sample
size: hundreds of graded cycles before the signal stabilizes.

**2. The backtest problem is structural.** Replaying past decisions
through a new prompt to measure improvement looks like the natural
evaluation path. It doesn't work for LLM+web_search agents because the
historical truth needed to grade a replay is the same information that
the replay would need to *retrieve* — and that information has changed
between then and now. Specifically:

- **Training data leaks outcomes.** Opus 4.7 knows who won every NBA
  game through January 2026. Ask it to "predict" a 2025 game and it
  appears to crush the market because it's recalling, not predicting.
- **`web_search` returns current results.** Articles written after the
  event get retrieved when replaying a decision from before the event.
  Indexed content, rankings, and link availability all reflect today.
- **Time-restricted prompting doesn't help** — the model already knows
  the answer from training.

The only stage that *can* be replayed cleanly is the decider, against
already-logged findings. That evaluates decider-prompt changes
deterministically. It can't evaluate any change upstream of stored
findings — because evaluating those requires re-running research, which
puts you back in the leakage trap.

So forward paper trading is the only honest validation path, and it
needs 100+ cycles to produce stable signal. At ~$1-3/cycle in LLM cost
that's $100-300 to know whether the thesis has legs — cheap experiment,
weeks of wall-clock, and the most likely answer is "no edge in liquid
markets without information advantage, maybe edge in narrow niches we haven't explored." Decided the
engineering value was already extracted and that further investment
didn't pencil out.

### Engineering patterns worth reusing

Domain-independent, useful in any future LLM-agent project:

- **Shared cached prefix across stages.** Assumptions live in the system
  block, marked cacheable, reused across every role (coarse_filter,
  triage, plan, framework, research, decider). One cached prefix
  amortizes across all stages within the 5-minute TTL.
  See [agent_trader/runtime.py](agent_trader/runtime.py) `build_system_block`.

- **Three-layer discovery funnel.** Code filter → LLM coarse filter
  (parallel, per-market, fail-open) → LLM batch ranking. The middle
  layer scales from ~10k open markets to a tractable ranking input
  without losing semantic filtering.
  [agent_trader/coarse_filter.py](agent_trader/coarse_filter.py).

- **Replay-invariant chain_log.** Every stage writes inputs, outputs,
  prompt version pointers, model parameters, and token usage to SQLite
  as one row per `(cycle_id, ticker, step)`. Designed so the seven items
  needed to replay any decision are reconstructable from the log alone,
  without re-running any LLM call.
  [agent_trader/runtime.py](agent_trader/runtime.py) `log_step`.

- **Per-market parallelism with soft cycle budget.** `ThreadPoolExecutor`
  with `DEFAULT_MAX_CONCURRENT_MARKETS = 3` (under Anthropic's burst
  ceiling). Each worker holds a private SQLite connection and a budget
  counter seeded from the cycle-spend snapshot at parallel-block entry;
  spend aggregates back after all workers finish.
  [agent_trader/orchestrator.py](agent_trader/orchestrator.py) `run_cycle`.

- **Tolerant JSON parser for tool-use outputs.** Tool-use models narrate
  before answering and resist instructions to stop. `runtime.parse_llm_json`
  tries pure JSON, wrapping fence, embedded fence, and balanced-brace
  extraction in that order. Cheaper than fighting model habits via prompt.

- **Schema gates between every LLM stage.** Pydantic models in
  [agent_trader/schemas.py](agent_trader/schemas.py) define the contract
  for every transition. Malformed responses raise `MalformedLLMResponse`,
  feed the killswitch, and surface before they propagate.

### What restart would require

Not a checklist. A precondition list — without these, restarting just
re-runs the same questions:

- **A specific edge thesis tied to a narrow niche.** Not "all of Kalshi."
  Long-tail markets with low liquidity, markets requiring multi-source
  synthesis humans haven't done, scheduled-event reactions where LLM
  synthesis competes with other slow analysis.
- **A live baseline during paper trading.** For sports, Vegas implied
  odds captured at decision time. Without a baseline you can't tell
  "agent has edge" from "agent reproduces consensus at $0.50/market."
- **Shadow-prompt evaluation** before reflection runs autonomously.
  Roughly 5× the statistical power of sequential single-arm comparisons
  at the same wall-clock and ~1.05× the cost.
- **Patience for 100+ graded paper cycles** before drawing any conclusion.

### What carries forward

- The `chain_log` pattern with the seven-item replay invariant. Any LLM
  agent that needs to be debugged, audited, or evolved benefits from
  this much state being durable.
- Cascaded model assignment with shared cached prefix. Right shape for
  any multi-stage agent pipeline.
- Cost-vs-edge × size as an architecture-level constraint, not a detail
  to handle later.
- Forward-measurement-only discipline when training-data leakage makes
  backtesting structurally invalid. Most LLM-agent projects have this
  problem and don't acknowledge it.

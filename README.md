# Kalshi-Trader

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

## Repo layout

```
kalshi/                  # shared signed-request HTTP client
├── client.py            #   KalshiClient — sign + send REST calls
└── config.py            #   API_KEY_ID, PRIVATE_KEY_PATH, BASE_URL only

agent_trader/            # LLM-driven research-and-trade agent
├── prompts/             #   versioned prompt bodies + active.yaml pointer
├── schemas.py           #   pydantic models at every LLM boundary
├── runtime.py           #   SQLite + killswitch + budget + cost wrapper
├── market_discovery.py  #   eligibility filter
├── orchestrator.py      #   per-cycle pipeline
├── research_agent.py    #   web_search subagent
├── executor.py          #   order placement + idempotency
├── reflection.py        #   EOD grader + proposal writer
├── reflect.py           #   CLI for reflection
├── dry_run.py           #   one-shot pipeline CLI
├── scheduler.py         #   APScheduler entry point
└── smoke.py             #   Kalshi-only smoke test

nba_trading/             # price-segment NBA bot (rule-based, not LLM)
├── config.py            #   SHARES_TO_BUY, FAVORITE_PRICE_MIN/MAX, SCHEDULE_FILE
├── main.py              #   schedule fetch + per-game bid + auto-cancel-at-tipoff
├── nba_scheduler.py     #   pulls game schedule + tip-off times
├── strategy.py          #   should_buy(ask, lo, hi)
└── portfolio.py         #   simple in-memory position log

data_retrieval/          # Kalshi historicals (offline, no trading)
├── build_research_dataset.py
├── get_nba_volatility_data.py
└── helpers.py

data/
├── agent_log.sqlite     # agent_trader chain log (WAL mode)
└── proposed_prompts/    # reflection output, awaiting human activation
```

The shared `.env` file holds credentials read by `kalshi/config.py`
(`API_KEY_ID`, `PRIVATE_KEY_PATH`) plus per-package settings
(`ANTHROPIC_API_KEY` for agent_trader; `SHARES_TO_BUY`,
`FAVORITE_PRICE_MIN`, `FAVORITE_PRICE_MAX`, `SCHEDULE_FILE` for nba_trading).
See `.env.example` for the agent_trader subset.

---

## Pending work

See [TODOS.md](TODOS.md). High-impact items still open:

- Prompt caching (allow-listed; carve out reflection's targets).
- Meta-reflection: LLM proposes structural improvements (RAG, fine-tuning,
  new tools) to the developer, not just prompt edits.
- Token-spend monitoring + automated cost alerts.

---

## Safety reminders

- This is your real money. The killswitch is conservative on purpose; don't
  raise its thresholds without reading the trip conditions in
  [agent_trader/runtime.py](agent_trader/runtime.py).
- Always run `dry_run` after editing any prompt, schema, or model assignment.
- `active.yaml` is the only source of truth for which prompt version is live.
  Don't edit prompt files in-place — bump the version.

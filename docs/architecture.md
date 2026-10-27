# Kalshi-Trader Architecture

*Last updated: 2026-09-03*

This repo has three trading architectures across its history, plus a research
package. `sportsbook_sourced/` is the active development track — deterministic,
code-owned pricing with sportsbook odds as the fair-value anchor, currently
paper-only. `nba_trading/` is an earlier, simpler bot that is the only package
in the repo with real order-placement code wired up. `agent_trader/` is the
earliest, frozen experiment where an LLM cascade did the research and trading
judgment. `misprice_discovery/` is research scripts and simulators, not a
trading system — see the root [`README.md`](../README.md) for that one. See
the README also for the full package inventory and current project status;
this document goes one level deeper on how each trading system is put
together.

**If you're looking for the package that can place real orders with real
money, it's [`nba_trading/`](#part-2--nba_trading-earlier-nba-price-segment-bot),
not `sportsbook_sourced/`.**

---

## Part 1 — Current: `sportsbook_sourced/`

### Thesis

Sportsbooks are the fair-value anchor for sports event probabilities. Kalshi
is the venue where those probabilities may be mispriced. The system trades
only when Kalshi disagrees with sportsbook-derived fair value after fees,
spread, liquidity, mapping risk, and stale-data buffers are accounted for.
The LLM is not involved anywhere in this path — every stage below is plain
Python, and the primitives (de-vig math, Kalshi's fee curve, CLV/PnL sign
conventions) are unit-tested against hand-computed values.

Current scope: NBA and NFL moneyline-equivalent winner markets only. Scanner
plus paper trading — the package does not place real Kalshi orders yet, even
in `--live` mode (`--live` only switches to real odds/market data; fills are
still simulated). Props, spreads, totals, series markets, live betting, and
parlays are explicitly excluded.

### Data flow

```text
Sportsbook odds snapshots (odds.py)
  -> no-vig, weighted fair price (pricing.py::build_moneyline_fair_price)
Kalshi market + orderbook snapshots (kalshi_feed.py)
  -> event mapping (mapper.py, pipeline.py::find_event_for_market)
Fair price + Kalshi quote
  -> fee-adjusted, liquidity-adjusted edge scan (scanner.py::scan_opportunity)
Opportunity
  -> Kelly-sized paper fill and portfolio update (paper.py::paper_buy)
Trade result
  -> settlement PnL evaluation (evaluation.py, pipeline.py::run_settlement_check)
```

`pipeline.py::run_scan` is the glue that composes all of this per market,
with per-market error isolation (a bad odds row or a fully-stale book skips
that one market rather than aborting the scan) and the foreign-key insert
order `storage.py` requires (parent event before its odds/mapping/opportunity
rows).

### Stages, in order

1. **Odds sourcing** (`odds.py`). `TheOddsApiProvider` pulls two-sided
   moneyline odds from regions `us,eu` so that sharp books (Pinnacle, Circa)
   are reachable, not just US retail books.
2. **Fair price** (`pricing.py::build_moneyline_fair_price`). Converts
   American odds to implied probability, removes the vig with a two-way
   no-vig normalization, and blends books into a weighted fair home
   probability. Weights come from `config.SourceWeights` (sharp books
   upweighted); a book past `max_odds_staleness_seconds` is excluded via
   zero weight rather than raising, so one stale book among several never
   kills the price — only "every book excluded" raises `ValueError`, which
   `run_scan` catches and skips.
3. **Event mapping** (`mapper.py`, `pipeline.py::find_event_for_market`).
   Solves the N×M match between an unordered list of Kalshi markets and
   sportsbook events using two independent signals: a directional
   settlement-window check against `commence_time` (`check_temporal_consistency`)
   and token-subset/Jaccard team-name similarity with an NBA tricode/mascot
   table (`title_similarity`). A market whose top match doesn't clear
   `MIN_TEAM_SIMILARITY`, or whose top two matches are within
   `AMBIGUITY_GAP` of each other, maps to nothing — no coin-flip defaults.
   `mapped_yes_outcome` is `Optional`; an ambiguous mapping produces a clean
   `action="skip"` opportunity rather than pricing the wrong side.
4. **Edge scan** (`scanner.py::scan_opportunity`). Picks the better side
   (YES vs NO) by gross edge, subtracts Kalshi's fee
   (`pricing.py::kalshi_fee_cents_per_contract`) and buffers — a liquidity
   buffer scaled by top-of-book depth (thin book → bigger buffer, deep book
   → smaller) plus a flat stale-odds buffer — and gates the result behind
   mapping confidence, source count, sharp-source count, book disagreement,
   minimum net edge, and available budget. Anything that fails a gate comes
   back as `action="skip"` with the reason(s) recorded.
5. **Paper execution** (`paper.py::paper_buy`). Quarter-Kelly position
   sizing: `kelly_fraction = net_edge_cents / (100 - price_cents)`, clamped
   to `[0, 1]`, staked against the portfolio's current cash at
   `KELLY_FRACTION_MULTIPLIER = 0.25` (a hedge against fair-price estimation
   error), capped by `max_contracts` (budget and order-book depth). Fills
   are immediate-at-quoted-price with no partial fills or slippage modeling
   (`FILL_MODEL_VERSION = "immediate_best_ask_v1"`). YES and NO holdings are
   tracked as separate counters per ticker (`Position.yes_count`,
   `Position.no_count`), not a signed net.
6. **Evaluation** (`evaluation.py`, `pipeline.py::run_settlement_check`).
   Settlement PnL is computed once Kalshi resolves a market
   (`resolved_yes_from_market`), fee-adjusted at settlement time. This is
   one half of a two-phase evaluator — the closing-line-value (CLV) half,
   anchored to `commence_time` rather than Kalshi's post-game
   `close_time`, is not built yet; see the "known limitations" below.

### Persistence

`storage.py::init_db()` creates a WAL-mode SQLite database (default
`data/sportsbook_sourced.sqlite`) with foreign keys enforced. Tables:
`sportsbook_events`, `sportsbook_odds_snapshots`, `fair_price_snapshots`,
`kalshi_market_snapshots`, `event_mappings`, `opportunities`,
`paper_orders`, `trade_evaluations`. Odds/fair-price snapshots are
append-only (CLV needs to see how the line moved); events upsert by
`event_id`; `trade_evaluations` upserts by `opportunity_id` with `COALESCE`
so the close-phase write and the settlement-phase write don't blank out
each other's columns.

### Entry point

```bash
python -m sportsbook_sourced.cli scan               # dry-run, bundled NBA sample data
python -m sportsbook_sourced.cli scan --live --league nba
python -m sportsbook_sourced.cli scan --live --league nfl
```

### Known limitations (as of 2026-09)

- No real order-placement code exists anywhere in the package — `--live`
  only swaps in real odds/market-data sources, execution is always paper.
- No scheduler/wake-scan-sleep orchestration for unattended runs yet.
- The CLV (closing-line-value) half of the evaluator isn't built —
  settlement PnL is, CLV needs a `commence_time`-anchored timing model that
  doesn't exist.
- Paper portfolio state resets each CLI invocation; it isn't persisted
  across runs.
- Liquidity, mapping-confidence, and edge thresholds are uncalibrated
  placeholders, not yet tuned against observed scans.

For the detailed, dated fix log behind these claims (what's verified vs.
assumed, in-flight work, and the reasoning behind specific threshold
decisions), see [`sportsbook_sourced/CLAUDE.md`](../sportsbook_sourced/CLAUDE.md).

---

## Part 2 — `nba_trading/`: earlier NBA price-segment bot

### Status

Not frozen the way `agent_trader/` is, but not under active development
either — it predates `sportsbook_sourced/` and uses a much simpler thesis
(a fixed price band rather than a sportsbook-derived fair value). **This is
currently the only package in the repo that calls `kalshi.client.place_limit_order`
with real money** — `sportsbook_sourced/` and `agent_trader/` are both
paper/simulated only. Anyone about to run this unattended with real capital
should read `nba_trading/main.py` directly, not just this summary.

### Thesis

Before tip-off, the market-favored team's Kalshi YES price should already
reflect most public information, but late-forming price bands may still
contain mispriced longshots-vs-favorites effects. The bot buys the favorite
(the team with the higher implied ask) only when that ask falls inside a
configurable price band (`STRATEGY_PRICE_MIN`/`STRATEGY_PRICE_MAX` in
`.env`, via `strategy_config.py`) — no sportsbook odds are consulted at all.

### Data flow

```text
NBA schedule for today (nba_scheduler.py::get_todays_schedule_map)
  -> one job scheduled per game at T-15-minutes-to-tipoff (APScheduler)
At T-15:
  Kalshi markets + orderbooks for both teams (client.fetch_nba_markets, client.get_orderbook)
  -> identify favorite by implied ask (strategy.py::identify_favorite)
  -> price-band gate (strategy.py::should_buy)
  -> bankroll-fraction position sizing (strategy.py::compute_shares_to_buy)
  -> limit order placement (kalshi.client.place_limit_order), expiring at tip-off
  -> position recorded (portfolio.py::Portfolio.add_position)
At tip-off + 2 minutes:
  -> fill status checked (main.py::confirm_fill_job) and portfolio reconciled
  -> unfilled orders drop the position; partial fills update quantity
```

Before every order, a drawdown circuit breaker (`portfolio.py::HighWaterMark`)
compares current cash against the all-time peak (tracked only while flat, so
an open position's cash dip doesn't get mistaken for a drawdown) and halts
new bets above `STRATEGY_MAX_DRAWDOWN_PCT`.

### Persistence & crash recovery

Three flat JSON files (paths configurable via env, gitignored by default):
`schedule_data.json` (today's games and whether each has fired),
`portfolio_data.json` (open tax-lot positions per ticker), and
`high_water_mark.json` (the peak balance for drawdown calculation). On
restart, `setup_daily_schedule` reloads today's schedule and re-registers
any game whose T-15 job hasn't fired yet; if the T-15 window was missed
entirely during downtime but tip-off hasn't happened, it fires immediately.

### Entry point

```bash
python -m nba_trading.main
```

### Known gaps (as of 2026-09)

- No dry-run/paper mode — every run places real orders if Kalshi credentials
  are present in the environment. Unlike `agent_trader/` and
  `sportsbook_sourced/`, there is no flag to rehearse a day's trading safely.
- `client_order_id` is not set on order placement, so retries (if ever added)
  would not be idempotent.
- The game is marked "fired" before the order call resolves, so a
  crash/timeout during order placement can leave local portfolio state out
  of sync with the real Kalshi account with no automatic reconciliation.
- No manual halt-file kill switch like `agent_trader/`'s
  `~/.kalshi-agent.halt` — the only way to stop new bets mid-run is the
  drawdown circuit breaker or killing the process.
- No process supervisor/auto-restart is documented; an unhandled crash in
  the main loop logs and exits rather than recovering.

These are being addressed; see the repo's issue tracker / recent commits for
current status rather than treating this list as evergreen.

---

## Part 3 — Historical: `agent_trader/` (frozen)

> **Status: frozen at `v0-frozen`, not the active architecture.** This
> section is kept for reference. The pipeline described below ran
> end-to-end and wrote decision rows to a SQLite chain log, but it was
> paused before meaningful forward paper trading, and before most of the
> self-improvement machinery in this design (Phases 2–4 below) was ever
> built. See the root `README.md` for current status. Do not read this
> section as a description of what runs today.

### Thesis

An LLM agent that trades prediction markets, grades itself against reality,
and proposes its own improvements — where every improvement is measured on
real past decisions before it ships, not just argued for by another LLM.
The design goal was "measured prompt evolution over a replayable history":
an LLM suggests prompt changes, but code replays them against stored past
decisions and scores them before anything ships.

### Per-cycle pipeline

Five LLM calls per market, escalating in cost as evidence accumulates
(`agent_trader/orchestrator.py`):

| Stage | Model | Job |
|---|---|---|
| Triage | Haiku | Reject most markets cheaply |
| Research Plan | Sonnet | What questions matter? |
| Decision Framework | Sonnet | What criteria define a buy? |
| Research | Sonnet + web | Gather evidence |
| Decision | Opus | Final call with a self-predicted `expected_outcome` |

Each call's full input/output was logged to a `chain_log` SQLite table
(`agent_trader/runtime.py`) with prompt version IDs, so a loss could in
principle be attributed to wrong research, wrong framework, or wrong
judgment. `agent_trader/scheduler.py` ran this as an APScheduler job every
30 minutes, plus a daily reflection job, with a killswitch
(`runtime.Killswitch`) tracking consecutive API errors and malformed LLM
responses.

### Design beyond the base cycle (mostly unbuilt)

The fuller design called for: a paper-trading simulator wrapping the real
Kalshi client (`PaperKalshiClient`) so fills could be measured without real
money; a Phase 2 evaluation loop that would replay candidate prompts
against a held-out set of graded historical decisions and auto-promote
winners in paper mode; a Phase 2.5 per-stage model bake-off; Phase 3
retrieval-based memory over past graded decisions; and Phase 4
meta-reflection proposing structural changes. Of these, only the base
5-stage cycle and the chain-log dataset were actually implemented and run.
The paper simulator, the evaluation/auto-promotion loop, and the later
phases were designed in detail but not built before the project was
paused — `sportsbook_sourced/` is the design's rebuttal-by-experience: the
follow-up project deliberately moved probability estimation and sizing out
of the LLM and into code.

### Why it was paused

The retrospective conclusion (echoed in `sportsbook_sourced/CLAUDE.md`) was
roughly: no edge in liquid markets without an information advantage, maybe
an edge in narrow niches. Rather than keep iterating on the LLM-judgment
approach, the project pivoted to `sportsbook_sourced/`'s
deterministic-pricing model. `agent_trader/` remains in the repo as
reference code and is not scheduled for further development.

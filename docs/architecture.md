# Kalshi-Trader Architecture

*Last updated: 2026-10-29*

This repo has three trading architectures across its history, plus a research
package. `sportsbook_sourced/` is the active development track — deterministic,
code-owned pricing with sportsbook odds as the fair-value anchor, currently
paper-only. `nba_trading/` is a simpler bot (a fixed price band) that is the only
package in the repo with real order-placement code wired up, and it is the one
being prepared for live trading; it has run as a dry run only and has never
placed a real order. `agent_trader/` is the
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

## Part 2 — `nba_trading/`: NBA price-band bot

### Status

Built up to the point of being ready to try with real money, but **it has never
placed a real order**: it runs as a dry run (the default), and the one real-order
attempt was rejected by Kalshi's location verification. It is the only package
that calls `kalshi.client.place_limit_order`; `sportsbook_sourced/` and
`agent_trader/` are paper/simulated only. The detailed, code-verified picture
(components, data stores, write policies, failure modes, ranked gaps) is in
[`system-architecture.md`](system-architecture.md); the order-reconciliation design
is in [`design-g4-reconciliation.md`](design-g4-reconciliation.md); running it on a
server is in [`deployment.md`](deployment.md). This section is the short version.

### Thesis

Before tip-off, the market-favored team's Kalshi YES price should already reflect
most public information, but the very heavy favorites may be priced a little too
low. The bot buys the favorite (the team with the higher implied ask) only when
that ask falls inside a configurable band (`STRATEGY_PRICE_MIN`/`STRATEGY_PRICE_MAX`,
shipped as 91-99c) — no sportsbook odds are consulted. This is a hypothesis under
test, not a proven edge: the historical backtest (`misprice_discovery/edge_validation.py`)
is suggestive, and the dashboard compares live results with the win rate the entry
price implied.

### Data flow

```text
Kalshi open NBA markets (client.fetch_nba_markets)
  -> games and tip-off times: expected_expiration_time - 3h, per game
     (nba_scheduler.games_from_kalshi_markets; no outside schedule source)
  -> one APScheduler job per game at T-15 minutes; the day is re-planned by a
     00:05 UTC cron job (a wall-clock job, not a sleep, so a sleeping machine catches up)
At T-15 (main.pre_game_job; games that tip off together are evaluated one at a time):
  kill-switch file -> balance
  -> [live] one Kalshi positions fetch: clear settled lots, and adopt positions the bot did not
     know about (attributing one to its unresolved order, via a Kalshi lookup, when there is one)
  -> equity = cash + capital at risk (Kalshi's own exposure figures) -> drawdown breaker
  -> markets + orderbooks -> favorite by implied ask -> band gate
  -> limit price (never above the band maximum) -> size = fraction of EQUITY,
     capped by cash and by the total-exposure cap
  -> write-ahead order row -> place_limit_order (V2 endpoint, expires at tip-off,
     deterministic client_order_id)
  -> placed: record the lot, queue the fill check
     unknown outcome (timeout, 409, malformed reply): ask Kalshi by client_order_id
At tip-off + 2 minutes: confirm_fill_job records Kalshi's order state and lets Kalshi's
  positions correct the lots (an unfilled order expires; a partial fill shrinks the lot).
Every 30 minutes: health_job resolves unknown orders, snapshots equity, syncs Kalshi fills,
  settles finished games from the actual fills, settles paper trades, and sends a heartbeat.
```

A read that fails (balance, positions, markets, orderbook) is *transient*: the game is
retried up to 3 times about a minute apart, then skipped. An **order is never retried**:
a missed trade is preferred to a double buy.

### Risk controls

- **Drawdown breaker** on *equity* (cash plus open cost), against the all-time peak, so
  cash locked in open positions is not mistaken for a loss
  (`STRATEGY_MAX_DRAWDOWN_PCT`, default 25).
- **Exposure cap** on total open cost as a share of equity (`STRATEGY_MAX_EXPOSURE_PCT`,
  default 30), enforced under a lock so simultaneous games cannot each see the same headroom.
- **Kill switch**: creating the `STOP_TRADING` file (or the dashboard toggle) stops new orders
  at the next game evaluation; resting orders are not cancelled (they expire at tip-off).
- **Live gate**: `--live` requires typing `LIVE` (or `BOT_LIVE_CONFIRM=LIVE` when headless).
- **Single instance**: an OS file lock stops two bots running against one database.
- **Clock check** at startup (request signatures are timestamped); live mode refuses to start
  if the clock is more than 30 s off, and a reading is only trusted when its round trip is short.

### Persistence & crash recovery

All durable state lives in **one SQLite file** (`LEDGER_FILE`, default
`data/nba_ledger.sqlite`), so a single replica (Litestream) backs up everything:
`decisions` (every game evaluated, with the reason for each skip), `orders`
(write-ahead `submitting` -> `placed`/`unconfirmed`/`not_found`/`adopted`/...),
`settlements` (PnL from Kalshi's actual fills), `kalshi_fills`, `snapshots`, `events`,
`paper_trades` (one-contract hypothetical trades for in-band dry-run decisions),
`portfolio_lots`, `schedule_games`, `kv` (drawdown peak, schedule date). The schema is
versioned (`PRAGMA user_version`) and migrates in place. The old JSON files
(`schedule_data.json`, `portfolio_data.json`, `high_water_mark.json`) are **import-only**: read
once into an empty database and never written again.

On restart the bot recovers: it asks Kalshi about every order whose outcome it never learned,
re-creates the post-tip fill check for every open order (the scheduler's job store is in
memory), reloads today's schedule and re-registers any game that has not fired, and fires
immediately for a game whose T-15 passed while tip-off has not.

### Entry point

```bash
python -m nba_trading.main           # dry run (default): real data, no orders
python -m nba_trading.main --live    # places real orders (asks you to type LIVE)
streamlit run nba_trading/dashboard/app.py   # read-only dashboard + kill-switch toggle
python -m kalshi.preflight           # read-only checks of the Kalshi endpoints the bot uses
```

A dry run places no order and holds no position, but it is **not stateless**: it writes
decisions, equity snapshots, events, paper trades, the drawdown peak and the schedule to the
ledger. It never marks a game fired, so it cannot suppress a later live run.

### Order safety

- `client_order_id` is deterministic (`nba-<game_id>-<ticker>`; the game id uses the US/Eastern
  date so it is stable across the midnight-UTC rollover), so a repeat is rejected by Kalshi.
- Orders go through Kalshi's V2 endpoint (`POST /portfolio/events/orders`); the legacy
  `/portfolio/orders` path was deprecated in June 2026. A POST is **never** retried
  automatically.
- The order row is written *before* the POST. If the reply is lost (timeout, 409, crash), the
  bot **asks Kalshi** (`get_order_by_client_id`) whether the order landed instead of guessing,
  and it never re-sends. Found: tracked from then on. Not found: recorded as `not_found`, which
  is provisional (re-checked for 48 h) and is not an invitation to re-send.
- One ledger row per real trade: positions and fills Kalshi reports for an order the bot lost
  track of are attributed to that order's row, not adopted as a second trade.
- A game is marked fired only after its job reaches a final outcome, not on entry and not while
  a transient-failure retry is pending.

### Known gaps (as of 2026-10-29)

The ranked list with evidence is `system-architecture.md` section 8.2. The headline ones:

- Never traded live: the order, status and cancel path, and the field names in real fill, order
  and position responses, are verified only against documentation and fakes.
- Kalshi's location verification must be completed by an eligible account holder before API
  trading can work at all.
- The container, Litestream and CI files are written but have never been built or run.
- Not yet verified: whether Kalshi reserves cash for resting orders, which affects how the
  unfilled part of a partly filled order is counted in equity (review item F8).
- Manual trades made on the same account in NBA game markets are adopted into the ledger and
  would contaminate the live-edge statistics.

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

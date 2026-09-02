# Sportsbook-Sourced Kalshi Edge Scanner

This package is the active sports-market architecture track in the repo. It is
separate from `agent_trader/`: sportsbook consensus provides fair value, and
deterministic code decides whether a Kalshi quote is tradeable.

## Thesis

```text
Sportsbooks are the fair-value anchor for sports event probabilities.
Kalshi is the venue where those probabilities may be mispriced.
The system trades only when Kalshi disagrees with sportsbook-derived fair value
after fees, spread, liquidity, mapping risk, and stale-data buffers.
```

The LLM is not the trader in this design. Probability estimation, trade
eligibility, and sizing are code-owned.

## Current Scope

- Leagues: NBA and NFL.
- Market type: moneyline-equivalent winner markets.
- Mode: scanner plus paper trading. `--live` fetches real odds and Kalshi data,
  but fills are still simulated by `paper.py`; this package does not place real
  Kalshi orders yet.
- Excluded: props, spreads, totals, series markets, live betting, parlays,
  conditionals, and markets with ambiguous resolution rules.
- Primary metric: closing line value, then realized PnL.

## Package Layout

```text
sportsbook_sourced/
  schemas.py       shared dataclasses
  config.py        thresholds, league support, source weights
  pricing.py       odds conversion, de-vig, consensus, Kalshi fee math
  odds.py          sportsbook odds ingestion interfaces
  kalshi_feed.py   Kalshi market/orderbook snapshot helpers
  mapper.py        Kalshi market <-> sportsbook event mapping helpers
  scanner.py       opportunity construction and trade/skip decisions
  paper.py         paper order, fill, sizing, and position primitives
  evaluation.py    CLV and settlement PnL scoring helpers
  storage.py       SQLite schema and persistence helpers
  pipeline.py      odds -> fair price -> mapping -> scan -> paper -> store
  sample_data.py   bundled NBA dry-run fixture
  cli.py           command-line entry point
```

## Data Flow

```text
Sportsbook odds snapshots
  -> no-vig / weighted fair price
Kalshi market + orderbook snapshots
  -> event mapping
Fair price + Kalshi quote
  -> fee-adjusted EV scan
Opportunity
  -> paper fill and position update
Trade result
  -> CLV / settlement PnL evaluation records
```

## Commands

Run the bundled NBA sample scan:

```bash
python -m sportsbook_sourced.cli scan
```

Dry-run is the default. It uses in-memory SQLite unless `--db-path` is passed:

```bash
python -m sportsbook_sourced.cli scan --db-path data/sportsbook_sourced.sqlite
```

Fetch real sportsbook odds and Kalshi market data while still paper-filling:

```bash
export ODDS_API_KEY=...
export API_KEY_ID=...
export PRIVATE_KEY_PATH=...
python -m sportsbook_sourced.cli scan --live --league nba
python -m sportsbook_sourced.cli scan --live --league nfl
```

`--live` requires `ODDS_API_KEY`, `API_KEY_ID`, and `PRIVATE_KEY_PATH`.
`KALSHI_BASE_URL` is optional and defaults to Kalshi's production trade API.

## CLI Output

The scanner prints JSON. This is the real output of `scan` (the bundled
sample's fixed mispricing), not an illustrative placeholder:

```json
{
  "mode": "dry_run",
  "league": "nba",
  "markets_examined": 1,
  "opportunities_found": 1,
  "tradeable": 1,
  "db_path": "in-memory (dry run; pass --db-path to persist)",
  "opportunities": [
    {
      "ticker": "KXNBAGAME-26JAN14LALBOS-LAL",
      "action": "buy",
      "side": "no",
      "net_edge_cents": 17.54,
      "reason": "tradeable"
    }
  ]
}
```

## Persistence

Live scans default to `data/sportsbook_sourced.sqlite`; dry-run scans stay
in-memory unless `--db-path` is supplied. `storage.init_db()` enables WAL mode
and foreign-key enforcement.

The scan pipeline writes sportsbook events, odds snapshots, fair prices, Kalshi
snapshots, event mappings, opportunities, and paper orders. Settlement PnL is
handled by `pipeline.run_settlement_check()` for resolved markets. The
commence-time close-phase CLV fetcher is still future work.

## Tests

```bash
pytest sportsbook_sourced -q
```

## Remaining Work

- Scheduler/wake-scan-sleep orchestration for unattended operation.
- Close-phase CLV evaluation anchored to game commence time.
- Persistent paper portfolio state across CLI invocations.
- Threshold calibration from observed real scans.
- Real execution layer, after paper trading has evidence of durable edge.

# Kalshi-Trader

Monorepo for Kalshi trading experiments. The active architecture track is
`sportsbook_sourced/`: a deterministic scanner that uses sportsbook moneyline
odds as the fair-value anchor for Kalshi sports markets.

## Packages

| Package | What it is | Entry point |
| --- | --- | --- |
| `sportsbook_sourced/` | Sportsbook-consensus edge scanner with paper fills, SQLite persistence, and settlement PnL evaluation (CLV evaluation is still scaffolding). | `python -m sportsbook_sourced.cli scan` |
| `kalshi/` | Signed-request HTTP client and shared credentials. No trading logic. | imported by packages |
| `agent_trader/` | Frozen LLM-driven research/trading experiment. Kept for reference, not the active path. | `python -m agent_trader.scheduler` |
| `nba_trading/` | Earlier NBA price-range bot. | `python -m nba_trading.main` |
| `misprice_discovery/` | Historical research scripts, simulations, and validation helpers. | module-specific scripts |

`sportsbook_sourced/` is intentionally separate from `agent_trader/`. In this
design, the LLM is not the trader: deterministic code owns odds conversion,
de-vigging, fair-price construction, event mapping, edge calculation, fees,
buffers, sizing, paper fills, and evaluation records.

## Sportsbook-Sourced Thesis

```text
Sportsbooks are the fair-value anchor for sports event probabilities.
Kalshi is the venue where those probabilities may be mispriced.
The system trades only when Kalshi disagrees with sportsbook-derived fair value
after fees, spread, liquidity, mapping risk, and stale-data buffers.
```

Current scope:

- Leagues: NBA and NFL.
- Market type: moneyline-equivalent winner markets.
- Mode: scanner plus paper trading. The package does not place real Kalshi
  orders yet, even when `--live` is used.
- Excluded: props, spreads, totals, series markets, live betting, parlays,
  conditionals, and markets with ambiguous resolution rules.
- Primary metric: closing line value, then realized PnL.

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

## Quick Start

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

Run the built-in NBA sample scan:

```bash
python -m sportsbook_sourced.cli scan
```

Dry run is the default. It uses bundled NBA sample data, writes to an in-memory
database unless `--db-path` is supplied, and prints a JSON summary.

Persist a dry-run scan:

```bash
python -m sportsbook_sourced.cli scan --db-path data/sportsbook_sourced.sqlite
```

Fetch real odds and Kalshi market data while still paper-filling orders:

```bash
export ODDS_API_KEY=...
export API_KEY_ID=...
export PRIVATE_KEY_PATH=...
python -m sportsbook_sourced.cli scan --live --league nba
python -m sportsbook_sourced.cli scan --live --league nfl
```

Optional:

```bash
export KALSHI_BASE_URL=https://api.elections.kalshi.com/trade-api/v2
```

## CLI Behavior

`python -m sportsbook_sourced.cli scan` supports:

| Flag | Default | Meaning |
| --- | --- | --- |
| `--league {nba,nfl}` | `nba` | League to scan. Dry-run sample data is NBA only; `--live` supports NBA and NFL. |
| `--live` | off | Fetch real sportsbook odds and Kalshi market data. Orders are still paper-simulated. |
| `--dry-run` | no-op | Deprecated compatibility flag; dry-run is already the default. |
| `--db-path PATH` | mode-dependent | Persist scan results to a chosen SQLite database. Dry-run defaults to in-memory; live defaults to `data/sportsbook_sourced.sqlite`. |

The JSON output includes mode, league, market count, opportunity count,
tradeable count, database path, and each opportunity's ticker, action, side,
edge, and reason.

## Persistence

`sportsbook_sourced.storage.init_db()` creates the SQLite schema and enables WAL
mode plus foreign-key enforcement. The default database path is:

```text
data/sportsbook_sourced.sqlite
```

Tables:

- `sportsbook_events`
- `sportsbook_odds_snapshots`
- `fair_price_snapshots`
- `kalshi_market_snapshots`
- `event_mappings`
- `opportunities`
- `paper_orders`
- `trade_evaluations`

Odds, fair prices, Kalshi snapshots, mappings, opportunities, and paper orders
are written during `run_scan()`. Settlement PnL is written by
`run_settlement_check()` once a Kalshi market resolves. The close-phase CLV
fetcher and scheduler are still future work.

## Tests

Run the package tests:

```bash
pytest sportsbook_sourced -q
```

Run the full repo tests:

```bash
pytest -q
```

## Project Status

`sportsbook_sourced/` has the working P1 scan path: data source resolution,
fair-price construction, event mapping, opportunity scanning, paper order
creation, and database writes. The live mode fetches real external data but
still uses paper execution.

Known remaining work:

- Add scheduler/wake-scan-sleep orchestration for unattended runs.
- Add the close-phase CLV evaluator anchored to game commence time.
- Persist paper portfolio state across CLI invocations.
- Calibrate liquidity, mapping, and edge thresholds against observed scans.
- Add a real execution layer only after paper CLV/PnL is measured.

The old `agent_trader/` LLM agent is paused at `v0-frozen`: its pipeline ran
end-to-end and wrote decision rows, but it stopped before meaningful forward
paper trading. It remains useful as reference code, but it is not the current
architecture.

# Sportsbook-Sourced Kalshi Edge Scanner

This package is a separate architecture track from `agent_trader/`.

Thesis:

```text
Sportsbooks are the fair-value anchor for sports event probabilities.
Kalshi is the venue where those probabilities may be mispriced.
The system trades only when Kalshi disagrees with sportsbook-derived fair value
after fees, spread, liquidity, mapping risk, and stale-data buffers.
```

The LLM is not the trader in this design. Deterministic code computes fair
prices, expected value, and trade eligibility. An LLM can later help with event
mapping review and mismatch explanations, but probability estimation and
sizing should remain code-owned.

## V1 Scope

- Leagues: NBA/NFL moneyline-equivalent winner markets.
- Mode: scanner + paper trading first.
- Excluded: props, spreads, totals, series markets, live betting, parlays,
  weird conditionals, and markets with ambiguous resolution rules.
- Primary metric: closing line value (CLV), then realized PnL.

## Package Layout

```text
sportsbook_sourced/
  schemas.py       shared dataclasses
  config.py        thresholds and league/source defaults
  pricing.py       odds conversion, de-vig, consensus, Kalshi fee math
  odds.py          sportsbook odds ingestion interfaces
  kalshi_feed.py   Kalshi market/orderbook snapshot helpers
  mapper.py        Kalshi market ↔ sportsbook event mapping helpers
  scanner.py       opportunity construction and trade/skip decisions
  paper.py         paper order/fill/position primitives
  evaluation.py    CLV and realized outcome scoring
  storage.py       SQLite schema and persistence helpers
  cli.py           command-line scaffold
```

## Intended Data Flow

```text
Sportsbook odds snapshots
  -> de-vig / weighted fair price
Kalshi market + orderbook snapshots
  -> event mapping
Fair price + Kalshi quote
  -> fee-adjusted EV scan
Opportunity
  -> paper/live execution
Trade result
  -> CLV + PnL evaluation
```

## First Useful Command

The scanner CLI is intentionally dry by default:

```bash
python -m sportsbook_sourced.cli scan --dry-run
```

The initial scaffold does not call external APIs until provider credentials and
league mappings are wired in.


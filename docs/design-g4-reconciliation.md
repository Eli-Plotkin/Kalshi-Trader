# Design: order reconciliation (fixes G4/G5 and the "unknown position" gap)

Status: IMPLEMENTED 2026-10-29 (contract was followed; deviations noted in the ledger agent's report: `apply_order_state` keeps `adopted` rows as `adopted`; `adopt_order` is idempotent on `order_id`). Companion to `docs/system-architecture.md`
(gaps G1, G4, G5). Everything below is a *contract*: the Kalshi-client agent, the ledger agent and
the integrator all build against these exact names and shapes.

## 1. Problem

An order can reach Kalshi while our own records never learn about it (reply timed out, process died
between the POST and the ledger insert, restart mid-flight). Today that leaves: a ledger row marked
`failed`/`duplicate` with no `order_id`, no tracked position, no fill check, and a trade that is
never settled (so it silently drops out of the live-edge statistics). Positions Kalshi holds that the
bot does not know about are invisible to `Portfolio.reconcile` and make equity look lower than it is.

## 2. The five fixes

1. **Write-ahead intent.** Insert the order row as `submitting` BEFORE the POST.
2. **Resolve ambiguity by asking Kalshi.** After a timeout, a 409/duplicate, or a restart that finds
   unresolved rows, look the order up by `client_order_id` and adopt the true state. **Never re-send
   an order to resolve ambiguity.** If Kalshi has no such order, record `not_found` and do not retry
   (a missed trade is preferred to a double buy).
3. **Adopt unknown positions + equity from Kalshi.** Positions on Kalshi that the portfolio does not
   know are adopted (with an alert). Equity for sizing and the circuit breaker is computed from
   Kalshi's own balance plus Kalshi's own position exposure, not from locally tracked lots.
4. **Settle from Kalshi's data.** Realized PnL comes from Kalshi fills (actual price and fees) plus
   the market result, so a trade cannot vanish from PnL because our order row was wrong.
5. **Rebuild on startup.** On boot, resolve every unresolved order and re-create the post-tip
   fill-check for every open order (the APScheduler job store is in memory).

## 3. Order lifecycle (ledger `orders.outcome`)

| outcome | meaning | next |
|---|---|---|
| `submitting` | row written before the POST; no answer seen yet | -> `placed`, `unconfirmed`, or a rejection |
| `placed` | Kalshi confirmed the order (we hold `order_id`) | tracked until settled |
| `unconfirmed` | POST failed/timed out/409; the order MAY exist | resolved by lookup -> `placed` or `not_found` |
| `not_found` | lookup proved Kalshi has no such order | terminal; never retried; alert |
| `insufficient_funds`, `blocked_location`, `duplicate`(legacy), `failed`(legacy) | existing terminal values | unchanged; legacy rows are left alone |
| `adopted` | a Kalshi position/fill with no ledger order, adopted so it can settle | tracked until settled |

Resolution outcomes `placed`/`adopted` carry `order_id`, `filled`, `avg_fill_cents`, `status`.

## 4. Kalshi client contract (`kalshi/client.py`)

All new methods **raise** on network/HTTP errors (the caller must be able to tell "Kalshi says no"
from "could not ask"). `None` / empty means a *confirmed* not-found. All money is **integer cents**;
Kalshi fixed-point strings (e.g. `"0.9300"`, `"285.00"`) are parsed to ints/rounded. Prices are the
YES-leg price in cents. Parsers must tolerate missing optional fields (return `None`) and unknown
extra fields, and must accept both the legacy integer-cent and the newer `*_dollars` / `*_fp` field
names (Kalshi is mid-migration).

```python
get_order(order_id) -> dict | None
get_order_by_client_id(client_order_id, ticker=None) -> dict | None
# normalized order dict:
# {"order_id": str, "client_order_id": str|None, "ticker": str,
#  "status": str,            # Kalshi's own status text, lower-cased: "resting"|"executed"|"canceled"|...
#  "filled": int,            # contracts filled
#  "remaining": int|None,
#  "limit_cents": int|None,  # YES-leg limit price
#  "avg_fill_cents": float|None,
#  "created_time": str|None, "expiration_time": str|None}

list_fills(ticker=None, order_id=None, min_ts=None) -> list[dict]   # paginated; follows cursor
# {"fill_id": str, "order_id": str|None, "ticker": str, "count": int, "price_cents": int,
#  "side": "yes"|"no", "action": "buy"|"sell", "fee_cents": int|None, "ts": str|None}

list_positions_detailed() -> list[dict]
# {"ticker": str, "position": int,        # signed: + YES, - NO
#  "exposure_cents": int|None,            # Kalshi's cost basis of the open position
#  "realized_pnl_cents": int|None, "fees_paid_cents": int|None}

list_settlements(min_ts=None) -> list[dict]
# {"ticker": str, "result": "yes"|"no"|None, "yes_count": int, "no_count": int,
#  "revenue_cents": int|None, "settled_ts": str|None}
```

`get_order_by_client_id` implements the lookup with whatever the real API supports (a
`client_order_id` filter if one exists, otherwise list orders for `ticker` across statuses and match
locally, following pagination, and also checking archived/historical orders if the API has such an
endpoint). It must never place or cancel anything. `kalshi/preflight.py` gains READ-ONLY checks for
each new endpoint (200 + shape sanity; an empty list is a pass).

## 5. Ledger contract (`nba_trading/ledger.py`)

Schema changes need a **migration** (the live `data/nba_ledger.sqlite` already exists): use
`PRAGMA user_version`; migrations are idempotent `ALTER TABLE ... ADD COLUMN` steps run in `Ledger.__init__`
inside a transaction; a fresh DB and an old DB must both end on the same schema.

- `orders` gains: `status TEXT`, `avg_fill_cents REAL`, `updated_ts TEXT`, `resolved_via TEXT`
  (`response|lookup|startup|adopt`). Existing columns/semantics are unchanged.
- `settlements` gains: `source TEXT` (`computed|kalshi_fills|kalshi_settlement`), `avg_entry_cents REAL`.
- New table `kalshi_fills(fill_id PRIMARY KEY, order_id, ticker, count, price_cents, side, action,
  fee_cents, ts)`.

```python
begin_order(decision_id, ticker, client_order_id, count, limit_price) -> (order_pk, created)
#   Inserts outcome='submitting'. If a row for this client_order_id already exists it is returned
#   with created=False (restart re-evaluation must reuse it, never create a second row).
mark_placed(order_pk, order_id, status=None)
set_order_outcome(order_pk, outcome)                  # insufficient_funds|blocked_location|unconfirmed|not_found|...
apply_order_state(order_pk, order_id, status, filled, avg_fill_cents, resolved_via)
#   adopt Kalshi's truth: sets outcome='placed' (if it exists on Kalshi), order_id, status, filled, avg_fill_cents
adopt_order(ticker, order_id, filled, avg_fill_cents, client_order_id=None, limit_price=None) -> order_pk
#   creates an outcome='adopted' row for a Kalshi order/position the ledger never knew
orders_needing_resolution() -> list[Row]      # outcome in ('submitting','unconfirmed')
open_orders() -> list[Row]                    # outcome in ('placed','adopted') with no settlement row
get_order_row(order_pk) -> Row | None
order_by_client_id(client_order_id) -> Row | None
upsert_fills(list[dict]) -> int               # kalshi_fills; idempotent on fill_id; returns rows inserted
fills_for_order(order_id) -> list[Row]
record_settlement_from_fills(order_pk, result) -> int | None
#   PnL (cents) from ACTUAL fills: sum(count * (100 - price)) if yes won else -sum(count*price),
#   minus sum(fee_cents); falls back to the computed settlement (limit price, formula fee) when no fills
#   are stored, marking source accordingly. Idempotent (INSERT OR REPLACE on order_pk).
```

`unsettled_orders()` must now include `adopted` rows. Nothing else about existing methods changes;
all existing ledger tests must keep passing.

## 6. Integrator behaviour (`nba_trading/main.py`, `portfolio.py`) - implemented in phase 2

- Order path: `begin_order` -> POST -> `mark_placed` / `set_order_outcome`. Timeout, connection error,
  `None`, or `DUPLICATE_ORDER` => outcome `unconfirmed` then `_resolve_order(order_pk)` which calls
  `client.get_order_by_client_id`. Found => `apply_order_state`, record the lot (qty = filled, or the
  full count while still resting), schedule the fill check. Not found => `set_order_outcome('not_found')`
  + ERROR alert; **no re-send**. Lookup itself failing => leave `unconfirmed`, alert, retry at the next
  health cycle / startup.
- Startup recovery (before scheduling): resolve `orders_needing_resolution()`, re-schedule a fill
  check for every `open_orders()` (immediately if tip-off already passed).
- Reconcile: positions come from `list_positions_detailed()`. Unknown positions are adopted
  (`adopt_order` + `Portfolio` lot) with a WARNING/ERROR event. Equity = Kalshi balance + sum of Kalshi
  `exposure_cents` (falls back to local lots only if Kalshi exposure is missing, with a warning).
- Settlement: health job syncs `list_fills` into `kalshi_fills`, then settles every open order whose
  market has resolved via `record_settlement_from_fills`; paper-trade settlement is unchanged.

## 7. Non-goals

No automatic cancel/re-send of ambiguous orders; no change to strategy, sizing, or the band; no new
infrastructure. Unverified against the live API: lookup-by-client-id support, exact field names for
fills/settlements (build tolerant parsers and flag what the docs did not confirm).

## 8. Implementation notes (added after build)

- Extra behaviour beyond the contract: equity prefers Kalshi's exposure but still counts local lots for
  tickers Kalshi does not show yet (resting orders); a position with no cost anywhere marks equity
  "degraded" (no new drawdown peak is recorded). Only `KXNBAGAME-` positions are adopted; others are
  reported once as `foreign_position`.
- Settlement refuses to book an order as 'unfilled' unless Kalshi's status says it ended unfilled
  (`canceled`/`expired`), because a missing fill field parses as 0.
- The daily re-plan is now a wall-clock cron job (00:05 UTC) and missed/errored jobs raise
  `job_missed` / `job_error` events (alerts at ERROR+).
- Still unverified: live field names for fills/orders/positions (the read-only preflight passes on the
  empty account), and cash reservation semantics for resting orders.

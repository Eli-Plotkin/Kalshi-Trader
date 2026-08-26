from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone

from . import mapper, scanner, storage
from .config import DEFAULT_SCANNER_CONFIG, DEFAULT_SOURCE_WEIGHTS, ScannerConfig, SourceWeights
from .evaluation import evaluate_opportunity
from .kalshi_feed import resolved_yes_from_market
from .paper import PaperPortfolio, paper_buy
from .pricing import build_moneyline_fair_price
from .schemas import (
    KalshiMarketSnapshot,
    Opportunity,
    SportsbookEvent,
    SportsbookOdds,
    TradeEvaluation,
)


log = logging.getLogger("sportsbook_sourced.pipeline")


def find_event_for_market(
    market: KalshiMarketSnapshot,
    events: list[SportsbookEvent],
    config: ScannerConfig = DEFAULT_SCANNER_CONFIG,
) -> SportsbookEvent | None:
    """Pick the sportsbook event a Kalshi market describes, out of a whole list.

    Every stage function downstream of this (`build_mapping`, `scan_opportunity`,
    `build_moneyline_fair_price`) takes one *already-paired* market + event; a
    real scan has an unordered list of each and has to solve that N×M pairing
    first. This runs the same two signals `infer_yes_outcome` and
    `check_temporal_consistency` already use for a fixed pair, scored across
    every candidate instead, and applies the same "real match" / "too close to
    call" thresholds (`mapper.MIN_TEAM_SIMILARITY`, `mapper.AMBIGUITY_GAP`) to
    the *ranking* rather than to a single home-vs-away comparison.

    An event whose settlement window doesn't fit this market at all (wrong
    calendar day, or a close time before the game starts) is dropped before
    scoring — `check_temporal_consistency` returns `confidence == 0.0` for
    those, and no team-name similarity should be able to overrule a wrong day.
    Returns `None` if no candidate clears the similarity floor, or if the top
    two candidates are too close to call.
    """
    yes_text = market.yes_subtitle or market.title

    # Only the top two scores matter (best match, and how close the runner-up
    # came) -- track both in one pass instead of sorting every candidate.
    best_score = float("-inf")
    second_score = float("-inf")
    best_event: SportsbookEvent | None = None

    for event in events:
        time_conf, _flags = mapper.check_temporal_consistency(market, event, config)
        if time_conf <= 0.0:
            continue
        team_score = max(
            mapper.title_similarity(yes_text, event.home_team),
            mapper.title_similarity(yes_text, event.away_team),
        )
        if team_score > best_score:
            best_score, second_score = team_score, best_score
            best_event = event
        elif team_score > second_score:
            second_score = team_score

    if best_event is None or best_score < mapper.MIN_TEAM_SIMILARITY:
        return None
    if best_score - second_score < mapper.AMBIGUITY_GAP:
        return None
    return best_event


def run_scan(
    *,
    markets: list[KalshiMarketSnapshot],
    events: list[SportsbookEvent],
    odds: list[SportsbookOdds],
    portfolio: PaperPortfolio,
    conn: sqlite3.Connection,
    weights: SourceWeights = DEFAULT_SOURCE_WEIGHTS,
    config: ScannerConfig = DEFAULT_SCANNER_CONFIG,
    now: datetime | None = None,
) -> list[Opportunity]:
    """Compose odds -> fair price -> mapping -> scan -> paper -> store, per market.

    Each market is isolated in its own try/except: a bad odds row or a
    fully-stale book (`build_moneyline_fair_price` raising `ValueError`, see
    P2.12) skips that one market instead of aborting the whole scan.

    Write order follows the foreign-key discipline `storage.py` now enforces
    (see P1.4): the parent `sportsbook_events` row lands before anything that
    references it, and `insert_opportunity` before `insert_paper_order`.

    **Assumes exactly one Kalshi market per event per scan** (see CLAUDE.md
    P1.5 "one moneyline market per event"). The odds/fair-price stages are
    computed and stored once per *event*, not once per market -- if a second
    market also resolves to an event already scanned this cycle, it's
    skipped with a warning rather than silently re-appending duplicate
    odds/fair-price rows for the same event.
    """
    now = now or datetime.now(timezone.utc)
    opportunities: list[Opportunity] = []
    seen_event_ids: set[str] = set()

    for market in markets:
        try:
            event = find_event_for_market(market, events, config)
            if event is None:
                log.info("no event match for market %s", market.ticker)
                continue

            if event.event_id in seen_event_ids:
                log.warning(
                    "market %s also resolved to event %s, already scanned this "
                    "cycle by another market -- run_scan assumes one moneyline "
                    "market per event (see CLAUDE.md); skipping to avoid "
                    "duplicate odds/fair-price rows",
                    market.ticker, event.event_id,
                )
                continue
            seen_event_ids.add(event.event_id)

            storage.insert_sportsbook_event(conn, event)
            storage.insert_kalshi_snapshot(conn, market)

            event_odds = [row for row in odds if row.event_id == event.event_id]
            for row in event_odds:
                storage.insert_odds_snapshot(conn, row)

            fair_price = build_moneyline_fair_price(
                event=event,
                odds=event_odds,
                weights=weights,
                max_staleness_seconds=config.max_odds_staleness_seconds,
                now=now,
            )
            storage.insert_fair_price(conn, fair_price)

            mapping = mapper.build_mapping(market=market, event=event, config=config, created_at=now)
            storage.insert_mapping(conn, mapping)

            opportunity = scanner.scan_opportunity(
                market=market, fair_price=fair_price, mapping=mapping,
                config=config, computed_at=now,
            )
            storage.insert_opportunity(conn, opportunity)

            order = paper_buy(opportunity, portfolio=portfolio)
            storage.insert_paper_order(conn, order)

            opportunities.append(opportunity)
        except Exception:
            log.exception("scan failed for market %s", market.ticker)
            continue

    return opportunities


def run_settlement_check(
    *,
    conn: sqlite3.Connection,
    kalshi_client,
    evaluated_at: datetime | None = None,
) -> list[TradeEvaluation]:
    """Check Kalshi settlement status for every filled paper order that
    hasn't been scored yet, and write PnL for the ones that have resolved.

    This is the settlement half of P1.7's two-phase evaluator, not the
    whole thing: it only fills in `resolved_side`/`pnl_cents`. The other
    half -- CLV (`fair_prob_at_close`) -- needs a fresh fair-price fetch
    anchored to the event's `commence_time` (game start, the standard
    "closing line" instant), not Kalshi's `close_time` (the settlement
    deadline, hours later, by which point the outcome is already known and
    "fair probability" is degenerate). That timing model doesn't exist yet
    and isn't built here -- see CLAUDE.md P1.7.

    Per-opportunity error isolation, same as `run_scan`: one bad
    `kalshi_client.get_market` call shouldn't stop the rest from being
    checked.
    """
    evaluated_at = evaluated_at or datetime.now(timezone.utc)
    results: list[TradeEvaluation] = []

    for opportunity_id in storage.list_opportunities_pending_settlement(conn):
        try:
            opportunity = storage.get_opportunity(conn, opportunity_id)
            order = storage.get_filled_paper_order(conn, opportunity_id)
            if opportunity is None or order is None:
                continue

            market = kalshi_client.get_market(opportunity.kalshi_ticker)
            if market is None:
                continue

            resolved_yes = resolved_yes_from_market(market)
            if resolved_yes is None:
                log.info("market %s not settled yet", opportunity.kalshi_ticker)
                continue

            result = evaluate_opportunity(
                opportunity=opportunity,
                fair_prob_at_close=None,
                resolved_yes=resolved_yes,
                count=order.count,
                evaluated_at=evaluated_at,
            )
            storage.insert_trade_evaluation(conn, result)
            results.append(result)
        except Exception:
            log.exception("settlement check failed for opportunity %s", opportunity_id)
            continue

    return results

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from .config import ScannerConfig
from .mapper import fair_yes_probability
from .pricing import kalshi_fee_cents_per_contract
from .schemas import EventMapping, FairPrice, KalshiMarketSnapshot, Opportunity


def _max_contracts_for_budget(price_cents: int, max_position_usd: float) -> int:
    if price_cents <= 0:
        return 0
    return int((max_position_usd * 100) // price_cents)


def scan_opportunity(
    *,
    market: KalshiMarketSnapshot,
    fair_price: FairPrice,
    mapping: EventMapping,
    config: ScannerConfig,
    computed_at: datetime | None = None,
) -> Opportunity:
    """Compute the best YES/NO opportunity for one mapped market."""
    computed_at = computed_at or datetime.now(timezone.utc)
    fair_yes = fair_yes_probability(mapping, fair_price)
    fair_no = 1.0 - fair_yes

    yes_price = market.yes_ask_cents
    no_price = max(0, 100 - market.yes_bid_cents)

    yes_gross = fair_yes * 100.0 - yes_price
    no_gross = fair_no * 100.0 - no_price

    if yes_gross >= no_gross:
        side = "yes"
        price = yes_price
        fair_prob = fair_yes
        gross_edge = yes_gross
    else:
        side = "no"
        price = no_price
        fair_prob = fair_no
        gross_edge = no_gross

    fee = kalshi_fee_cents_per_contract(price)
    buffers = (
        config.liquidity_buffer_cents
        + config.stale_odds_buffer_cents
        + config.mapping_risk_buffer_cents
    )
    net_edge = gross_edge - fee - buffers

    reasons: list[str] = []
    if mapping.confidence < config.min_mapping_confidence:
        reasons.append(f"mapping_confidence:{mapping.confidence:.2f}<{config.min_mapping_confidence:.2f}")
    if mapping.mismatch_flags:
        reasons.append("mapping_flags:" + ",".join(mapping.mismatch_flags))
    if fair_price.source_count < config.min_source_count:
        reasons.append(f"source_count:{fair_price.source_count}<{config.min_source_count}")
    if fair_price.sharp_source_count < config.min_sharp_source_count:
        reasons.append(f"sharp_source_count:{fair_price.sharp_source_count}<{config.min_sharp_source_count}")
    if fair_price.staleness_seconds > config.max_odds_staleness_seconds:
        reasons.append(f"odds_stale:{fair_price.staleness_seconds}s")
    if fair_price.book_disagreement_cents > config.max_book_disagreement_cents:
        reasons.append(f"book_disagreement:{fair_price.book_disagreement_cents:.1f}c")
    if net_edge < config.min_net_edge_cents:
        reasons.append(f"net_edge:{net_edge:.2f}c<{config.min_net_edge_cents:.2f}c")

    max_contracts = _max_contracts_for_budget(price, config.max_position_usd)
    if max_contracts <= 0:
        reasons.append("no_contracts_for_budget")

    action = "skip" if reasons else "buy"
    reason = "tradeable" if not reasons else "; ".join(reasons)

    return Opportunity(
        opportunity_id=str(uuid4()),
        kalshi_ticker=market.ticker,
        sportsbook_event_id=fair_price.event_id,
        side=side,
        action=action,
        fair_prob=fair_prob,
        kalshi_price_cents=price,
        gross_edge_cents=gross_edge,
        fee_cents_per_contract=fee,
        net_edge_cents=net_edge,
        max_contracts=max_contracts,
        reason=reason,
        computed_at=computed_at,
    )


from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from statistics import pstdev

from .config import SourceWeights
from .schemas import FairPrice, League, MarketType, SportsbookEvent, SportsbookOdds


def american_to_implied_prob(american_odds: int) -> float:
    """Convert American odds to raw implied probability."""
    if american_odds == 0:
        raise ValueError("american_odds cannot be zero")
    if american_odds > 0:
        return 100.0 / (american_odds + 100.0)
    return abs(american_odds) / (abs(american_odds) + 100.0)


def remove_two_way_vig(prob_a: float, prob_b: float) -> tuple[float, float]:
    """Normalize two raw implied probabilities to no-vig probabilities."""
    total = prob_a + prob_b
    if total <= 0:
        raise ValueError("probability total must be positive")
    return prob_a / total, prob_b / total


def kalshi_fee_cents_per_contract(price_cents: int) -> float:
    """Estimate Kalshi fee in cents for one contract.

    Kalshi's fee schedule is based on expected earnings:
    fee dollars = ceil_to_cent(0.07 * contracts * price * (1 - price)).
    For scanning, return the unrounded per-contract cents estimate; execution
    code can apply exact rounding at order size.
    """
    if price_cents < 0 or price_cents > 100:
        raise ValueError("price_cents must be between 0 and 100")
    p = price_cents / 100.0
    return 100.0 * 0.07 * p * (1.0 - p)


def exact_kalshi_fee_cents(contract_count: int, price_cents: int) -> int:
    """Ceiling-to-cent total fee estimate for a Kalshi order."""
    if contract_count <= 0:
        return 0
    raw_cents = contract_count * kalshi_fee_cents_per_contract(price_cents)
    return int(raw_cents) if raw_cents == int(raw_cents) else int(raw_cents) + 1


def _book_weight(bookmaker: str, *, stale: bool, weights: SourceWeights) -> float:
    if stale:
        return weights.stale_book_weight
    return weights.sharp_books.get(bookmaker.lower(), weights.default_book_weight)


def build_moneyline_fair_price(
    *,
    event: SportsbookEvent,
    odds: list[SportsbookOdds],
    weights: SourceWeights,
    max_staleness_seconds: int,
    now: datetime | None = None,
) -> FairPrice:
    """Build a weighted no-vig fair price from two-sided moneyline odds."""
    now = now or datetime.now(timezone.utc)
    by_book: dict[str, dict[str, SportsbookOdds]] = defaultdict(dict)
    for row in odds:
        if row.event_id != event.event_id or row.market_type != "moneyline":
            continue
        by_book[row.bookmaker][row.outcome_name.lower()] = row

    home_values: list[tuple[float, float, bool, int]] = []
    for bookmaker, outcomes in by_book.items():
        home = outcomes.get(event.home_team.lower())
        away = outcomes.get(event.away_team.lower())
        if not home or not away:
            continue
        raw_home = american_to_implied_prob(home.american_odds)
        raw_away = american_to_implied_prob(away.american_odds)
        fair_home, _ = remove_two_way_vig(raw_home, raw_away)
        staleness = int((now - max(home.last_update, away.last_update)).total_seconds())
        stale = staleness > max_staleness_seconds
        weight = _book_weight(bookmaker, stale=stale, weights=weights)
        if weight <= 0:
            continue
        home_values.append((fair_home, weight, bookmaker.lower() in weights.sharp_books, staleness))

    if not home_values:
        raise ValueError(f"no usable two-sided moneyline odds for event {event.event_id}")

    total_weight = sum(weight for _, weight, _, _ in home_values)
    home_prob = sum(prob * weight for prob, weight, _, _ in home_values) / total_weight
    disagreement = pstdev([prob for prob, _, _, _ in home_values]) * 100.0 if len(home_values) > 1 else 0.0
    staleness_seconds = max(staleness for _, _, _, staleness in home_values)
    sharp_count = sum(1 for _, _, is_sharp, _ in home_values if is_sharp)
    confidence = max(0.0, min(1.0, 1.0 - (disagreement / 20.0)))

    return FairPrice(
        event_id=event.event_id,
        league=event.league,
        market_type="moneyline",
        home_team=event.home_team,
        away_team=event.away_team,
        home_prob=home_prob,
        away_prob=1.0 - home_prob,
        source_count=len(home_values),
        sharp_source_count=sharp_count,
        staleness_seconds=staleness_seconds,
        book_disagreement_cents=disagreement,
        confidence=confidence,
        computed_at=now,
    )


from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from sportsbook_sourced.config import SourceWeights
from sportsbook_sourced.pricing import (
    american_to_implied_prob,
    build_moneyline_fair_price,
    exact_kalshi_fee_cents,
    kalshi_fee_cents_per_contract,
    remove_two_way_vig,
)
from sportsbook_sourced.schemas import SportsbookEvent, SportsbookOdds


# ────────────────────────────────────────────────────────────────────────────
# american_to_implied_prob
# ────────────────────────────────────────────────────────────────────────────


def test_american_to_implied_prob_even_money():
    # +100 = 50% (1:1 payout)
    assert american_to_implied_prob(100) == pytest.approx(0.5)


def test_american_to_implied_prob_favorite():
    # -150: bet $150 to win $100 → 150/250 = 60%
    assert american_to_implied_prob(-150) == pytest.approx(0.6)


def test_american_to_implied_prob_underdog():
    # +200: bet $100 to win $200 → 100/300 = 33.33%
    assert american_to_implied_prob(200) == pytest.approx(1 / 3, rel=1e-6)


def test_american_to_implied_prob_heavy_favorite():
    # -500 = 5/6 ≈ 83.3%
    assert american_to_implied_prob(-500) == pytest.approx(500 / 600, rel=1e-6)


def test_american_to_implied_prob_zero_raises():
    with pytest.raises(ValueError):
        american_to_implied_prob(0)


# ────────────────────────────────────────────────────────────────────────────
# remove_two_way_vig
# ────────────────────────────────────────────────────────────────────────────


def test_remove_two_way_vig_equal_probs():
    fair_a, fair_b = remove_two_way_vig(0.55, 0.55)
    assert fair_a == pytest.approx(0.5)
    assert fair_b == pytest.approx(0.5)


def test_remove_two_way_vig_no_vig_passthrough():
    fair_a, fair_b = remove_two_way_vig(0.7, 0.3)
    assert fair_a == pytest.approx(0.7)
    assert fair_b == pytest.approx(0.3)


def test_remove_two_way_vig_sums_to_one():
    fair_a, fair_b = remove_two_way_vig(0.6, 0.45)
    assert fair_a + fair_b == pytest.approx(1.0)


def test_remove_two_way_vig_zero_total_raises():
    with pytest.raises(ValueError):
        remove_two_way_vig(0.0, 0.0)


# ────────────────────────────────────────────────────────────────────────────
# kalshi_fee_cents_per_contract
# ────────────────────────────────────────────────────────────────────────────


def test_kalshi_fee_zero_at_zero_price():
    # p*(1-p) is zero at boundaries
    assert kalshi_fee_cents_per_contract(0) == 0.0


def test_kalshi_fee_zero_at_hundred_price():
    assert kalshi_fee_cents_per_contract(100) == 0.0


def test_kalshi_fee_max_at_fifty():
    # p*(1-p) peaks at p=0.5 → 0.07 * 0.5 * 0.5 * 100 = 1.75 cents
    assert kalshi_fee_cents_per_contract(50) == pytest.approx(1.75)


def test_kalshi_fee_symmetric():
    assert kalshi_fee_cents_per_contract(30) == pytest.approx(
        kalshi_fee_cents_per_contract(70)
    )


def test_kalshi_fee_out_of_range_raises():
    with pytest.raises(ValueError):
        kalshi_fee_cents_per_contract(-1)
    with pytest.raises(ValueError):
        kalshi_fee_cents_per_contract(101)


# ────────────────────────────────────────────────────────────────────────────
# exact_kalshi_fee_cents
# ────────────────────────────────────────────────────────────────────────────


def test_exact_fee_zero_contracts():
    assert exact_kalshi_fee_cents(0, 50) == 0


def test_exact_fee_ceils_to_cent():
    # 10 contracts at 50c → 10 * 1.75 = 17.5 → ceil to 18
    assert exact_kalshi_fee_cents(10, 50) == 18


def test_exact_fee_already_integer_no_extra_round():
    # 4 contracts at 50c → 4 * 1.75 = 7.0 (integer) → 7
    assert exact_kalshi_fee_cents(4, 50) == 7


def test_exact_fee_boundary_zero_fee():
    # Fee math at p=0 or p=100 is zero regardless of count
    assert exact_kalshi_fee_cents(100, 0) == 0
    assert exact_kalshi_fee_cents(100, 100) == 0


# ────────────────────────────────────────────────────────────────────────────
# build_moneyline_fair_price — fixtures + tests
# ────────────────────────────────────────────────────────────────────────────


def _event(home: str = "lakers", away: str = "celtics") -> SportsbookEvent:
    return SportsbookEvent(
        event_id="e1",
        league="nba",
        home_team=home,
        away_team=away,
        commence_time=datetime(2026, 5, 20, 0, 0, tzinfo=timezone.utc),
    )


def _odds_row(
    *,
    book: str,
    outcome: str,
    american: int,
    last_update: datetime,
    event_id: str = "e1",
) -> SportsbookOdds:
    return SportsbookOdds(
        event_id=event_id,
        bookmaker=book,
        market_type="moneyline",
        outcome_name=outcome,
        american_odds=american,
        last_update=last_update,
        collected_at=last_update,
    )


def test_fair_price_single_book_devigs_correctly():
    now = datetime(2026, 5, 20, 12, 0, tzinfo=timezone.utc)
    event = _event()
    odds = [
        _odds_row(book="pinnacle", outcome="lakers", american=-200, last_update=now),
        _odds_row(book="pinnacle", outcome="celtics", american=+170, last_update=now),
    ]
    fair = build_moneyline_fair_price(
        event=event,
        odds=odds,
        weights=SourceWeights(),
        max_staleness_seconds=300,
        now=now,
    )
    # -200 implied = 0.667, +170 implied = 0.370 → de-vigged ≈ 0.643
    assert fair.home_prob == pytest.approx(2 / 3 / (2 / 3 + 100 / 270), rel=1e-3)
    assert fair.home_prob + fair.away_prob == pytest.approx(1.0)
    assert fair.source_count == 1
    assert fair.sharp_source_count == 1
    assert fair.book_disagreement_cents == 0.0


def test_fair_price_weights_sharp_books_higher():
    now = datetime(2026, 5, 20, 12, 0, tzinfo=timezone.utc)
    event = _event()
    # Pinnacle says 60/40, DraftKings says 50/50.
    # Default weights: pinnacle=3.0, draftkings=1.0 → weighted home = 0.575
    odds = [
        _odds_row(book="pinnacle", outcome="lakers", american=-150, last_update=now),
        _odds_row(book="pinnacle", outcome="celtics", american=+130, last_update=now),
        _odds_row(book="draftkings", outcome="lakers", american=-100, last_update=now),
        _odds_row(book="draftkings", outcome="celtics", american=-100, last_update=now),
    ]
    fair = build_moneyline_fair_price(
        event=event,
        odds=odds,
        weights=SourceWeights(),
        max_staleness_seconds=300,
        now=now,
    )
    # Pinnacle de-vigged home = 150/250 / (150/250 + 100/230) ≈ 0.580
    # DK de-vigged = 0.5 exactly
    # Weighted: (0.580*3 + 0.5*1) / 4 ≈ 0.560
    assert fair.home_prob == pytest.approx(0.560, abs=0.01)
    assert fair.source_count == 2
    assert fair.sharp_source_count == 1
    # Book disagreement is non-zero
    assert fair.book_disagreement_cents > 0


def test_fair_price_stale_book_excluded():
    now = datetime(2026, 5, 20, 12, 0, tzinfo=timezone.utc)
    fresh = now - timedelta(seconds=30)
    stale = now - timedelta(seconds=600)
    event = _event()
    odds = [
        _odds_row(book="pinnacle", outcome="lakers", american=-150, last_update=fresh),
        _odds_row(book="pinnacle", outcome="celtics", american=+130, last_update=fresh),
        # Stale book with different odds — must be dropped via stale_book_weight=0
        _odds_row(book="caesars", outcome="lakers", american=-300, last_update=stale),
        _odds_row(book="caesars", outcome="celtics", american=+250, last_update=stale),
    ]
    fair = build_moneyline_fair_price(
        event=event,
        odds=odds,
        weights=SourceWeights(),
        max_staleness_seconds=120,
        now=now,
    )
    # Only pinnacle should remain
    assert fair.source_count == 1
    assert fair.sharp_source_count == 1


def test_fair_price_missing_two_way_drops_book():
    """A book with only one side of the market is unusable for de-vig."""
    now = datetime(2026, 5, 20, 12, 0, tzinfo=timezone.utc)
    event = _event()
    odds = [
        _odds_row(book="pinnacle", outcome="lakers", american=-150, last_update=now),
        _odds_row(book="pinnacle", outcome="celtics", american=+130, last_update=now),
        # Only home side from this book
        _odds_row(book="fanduel", outcome="lakers", american=-140, last_update=now),
    ]
    fair = build_moneyline_fair_price(
        event=event,
        odds=odds,
        weights=SourceWeights(),
        max_staleness_seconds=300,
        now=now,
    )
    assert fair.source_count == 1


def test_fair_price_no_usable_odds_raises():
    now = datetime(2026, 5, 20, 12, 0, tzinfo=timezone.utc)
    event = _event()
    with pytest.raises(ValueError, match="no usable two-sided moneyline odds"):
        build_moneyline_fair_price(
            event=event,
            odds=[],
            weights=SourceWeights(),
            max_staleness_seconds=300,
            now=now,
        )


def test_fair_price_filters_other_event_ids():
    now = datetime(2026, 5, 20, 12, 0, tzinfo=timezone.utc)
    event = _event()
    odds = [
        _odds_row(book="pinnacle", outcome="lakers", american=-150, last_update=now),
        _odds_row(book="pinnacle", outcome="celtics", american=+130, last_update=now),
        # Different event_id — must be ignored
        _odds_row(
            book="pinnacle", outcome="lakers", american=-500, last_update=now,
            event_id="other_event",
        ),
    ]
    fair = build_moneyline_fair_price(
        event=event,
        odds=odds,
        weights=SourceWeights(),
        max_staleness_seconds=300,
        now=now,
    )
    assert fair.source_count == 1


def test_fair_price_confidence_drops_with_disagreement():
    now = datetime(2026, 5, 20, 12, 0, tzinfo=timezone.utc)
    event = _event()
    # Two books with very different views
    odds = [
        _odds_row(book="pinnacle", outcome="lakers", american=-500, last_update=now),
        _odds_row(book="pinnacle", outcome="celtics", american=+400, last_update=now),
        _odds_row(book="draftkings", outcome="lakers", american=+100, last_update=now),
        _odds_row(book="draftkings", outcome="celtics", american=-120, last_update=now),
    ]
    fair = build_moneyline_fair_price(
        event=event,
        odds=odds,
        weights=SourceWeights(),
        max_staleness_seconds=300,
        now=now,
    )
    # Confidence is 1 - disagreement/20, big disagreement => lower confidence
    assert fair.confidence < 0.5
    assert fair.book_disagreement_cents > 10

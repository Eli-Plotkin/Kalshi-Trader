"""Tests for nba_trading.strategy — the pure trading-decision functions."""

from __future__ import annotations

import pytest

from nba_trading.strategy import (
    compute_limit_price,
    compute_shares_to_buy,
    get_implied_ask,
    identify_favorite,
    should_buy,
)


# ----------------------------------------------------------------------------
# get_implied_ask
# ----------------------------------------------------------------------------


class TestGetImpliedAsk:
    def test_normal_orderbook(self):
        # Best NO bid 0.10 → implied YES ask = 100 - 10 = 90¢.
        book = {"no_dollars": [["0.05", 100], ["0.10", 200]]}
        assert get_implied_ask(book) == 90

    def test_empty_orderbook_returns_none(self):
        assert get_implied_ask({}) is None
        assert get_implied_ask(None) is None

    def test_missing_no_bids_returns_none(self):
        assert get_implied_ask({"no_dollars": []}) is None

    def test_malformed_bid_returns_none(self):
        assert get_implied_ask({"no_dollars": [["bad", 100]]}) is None


# ----------------------------------------------------------------------------
# identify_favorite
# ----------------------------------------------------------------------------


class TestIdentifyFavorite:
    def _book(self, no_bid_dollars):
        return {"no_dollars": [[str(no_bid_dollars), 100]]}

    def test_home_with_higher_ask_wins(self):
        # Home implied ask = 100 - 5 = 95¢; away = 100 - 30 = 70¢.
        result = identify_favorite("HOM", "AWY", self._book(0.05), self._book(0.30))
        assert result == "HOM"

    def test_away_with_higher_ask_wins(self):
        result = identify_favorite("HOM", "AWY", self._book(0.30), self._book(0.05))
        assert result == "AWY"

    def test_ties_break_to_home(self):
        result = identify_favorite("HOM", "AWY", self._book(0.10), self._book(0.10))
        assert result == "HOM"

    def test_both_books_missing_returns_none(self):
        assert identify_favorite("HOM", "AWY", None, None) is None

    def test_only_away_book_picks_away(self):
        # Even if home book is None, if away has data, use away.
        result = identify_favorite("HOM", "AWY", None, self._book(0.10))
        assert result == "AWY"


# ----------------------------------------------------------------------------
# should_buy
# ----------------------------------------------------------------------------


class TestShouldBuy:
    def test_within_band_inclusive(self):
        assert should_buy(95, 90, 99)
        assert should_buy(90, 90, 99)   # lower boundary
        assert should_buy(99, 90, 99)   # upper boundary

    def test_outside_band(self):
        assert not should_buy(89, 90, 99)
        assert not should_buy(100, 90, 99)

    def test_none_ask_returns_false(self):
        assert not should_buy(None, 90, 99)


# ----------------------------------------------------------------------------
# compute_shares_to_buy — the new dynamic sizing
# ----------------------------------------------------------------------------


class TestComputeSharesToBuy:
    def test_normal_case_floors_to_integer(self):
        # $100 balance, 10% fraction, 92¢ ask → $10 at risk → 10¢00 cents → 10/92 = 10.86 → 10 shares.
        # Actually: balance_cents=10000, fraction=0.10 → 1000 cents at risk.
        # 1000 / 92 = 10.86 → floor = 10 shares.
        shares = compute_shares_to_buy(balance_cents=10_000, ask_cents=92, fraction=0.10)
        assert shares == 10

    def test_larger_bankroll_buys_more_shares(self):
        small = compute_shares_to_buy(10_000, 92, 0.10)
        large = compute_shares_to_buy(100_000, 92, 0.10)
        assert large > small
        assert large == 108  # 10000 / 92 = 108.69 → 108

    def test_smaller_fraction_buys_fewer_shares(self):
        ten_pct = compute_shares_to_buy(100_000, 92, 0.10)
        one_pct = compute_shares_to_buy(100_000, 92, 0.01)
        assert one_pct < ten_pct
        assert one_pct == 10  # 1000 / 92 = 10.86 → 10

    def test_zero_balance_returns_zero(self):
        assert compute_shares_to_buy(0, 92, 0.10) == 0
        assert compute_shares_to_buy(None, 92, 0.10) == 0

    def test_negative_balance_returns_zero(self):
        assert compute_shares_to_buy(-100, 92, 0.10) == 0

    def test_zero_ask_returns_zero(self):
        # Defensive against degenerate orderbooks.
        assert compute_shares_to_buy(10_000, 0, 0.10) == 0
        assert compute_shares_to_buy(10_000, None, 0.10) == 0

    def test_zero_fraction_returns_zero(self):
        assert compute_shares_to_buy(10_000, 92, 0) == 0

    def test_insufficient_funds_for_one_share(self):
        # $0.50 balance can't afford a single 92¢ share.
        assert compute_shares_to_buy(50, 92, 1.0) == 0

    def test_fraction_one_uses_all_bankroll(self):
        # 100% bet at 50¢ entry: 10000/50 = 200 shares.
        assert compute_shares_to_buy(10_000, 50, 1.0) == 200


# ----------------------------------------------------------------------------
# compute_limit_price
# ----------------------------------------------------------------------------


class TestComputeLimitPrice:
    def test_buffer_added_under_cap(self):
        # Ask 92¢ + 1¢ buffer = 93¢, well under 99¢ cap.
        assert compute_limit_price(ask_cents=92, buffer_cents=1, cap_cents=99) == 93

    def test_cap_respected(self):
        # Ask 99¢ + 1¢ would be 100, but cap is 99.
        assert compute_limit_price(ask_cents=99, buffer_cents=1, cap_cents=99) == 99

    def test_zero_buffer_uses_ask_directly(self):
        assert compute_limit_price(ask_cents=92, buffer_cents=0, cap_cents=99) == 92

    def test_none_ask_returns_none(self):
        assert compute_limit_price(None, 1, 99) is None

    def test_zero_ask_returns_none(self):
        assert compute_limit_price(0, 1, 99) is None

    def test_high_buffer_clamped_to_cap(self):
        # Ask 95¢ + 10¢ buffer = 105, clamped to 99.
        assert compute_limit_price(95, 10, 99) == 99

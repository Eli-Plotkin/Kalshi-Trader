"""Tests for kalshi_feed — Kalshi market snapshot helpers."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from sportsbook_sourced.kalshi_feed import (
    dollars_str_to_cents,
    list_sports_markets,
    snapshot_from_market,
)
from sportsbook_sourced.schemas import KalshiMarketSnapshot


# ----------------------------------------------------------------------------
# dollars_str_to_cents — defensive price parsing
# ----------------------------------------------------------------------------


class TestDollarsStrToCents:
    def test_normal_decimal_string(self):
        assert dollars_str_to_cents("0.4200") == 42
        assert dollars_str_to_cents("0.5") == 50
        assert dollars_str_to_cents("0.92") == 92
        assert dollars_str_to_cents("1.00") == 100

    def test_zero_string(self):
        assert dollars_str_to_cents("0.00") == 0
        assert dollars_str_to_cents("0") == 0

    def test_none_returns_zero(self):
        assert dollars_str_to_cents(None) == 0

    def test_empty_string_returns_zero(self):
        assert dollars_str_to_cents("") == 0

    def test_malformed_returns_zero(self):
        # Defensive: any garbage string should produce 0, never raise.
        assert dollars_str_to_cents("not a number") == 0
        assert dollars_str_to_cents("$0.42") == 0
        assert dollars_str_to_cents([0.5]) == 0

    def test_rounds_half_up(self):
        # 0.425 → 42.5 cents → 42 (banker's rounding in Python 3 → even).
        # We only need the function to be consistent, not picky about half-cents.
        assert dollars_str_to_cents("0.426") == 43
        assert dollars_str_to_cents("0.424") == 42

    def test_float_input(self):
        # Although intended for strings, floats shouldn't blow up.
        assert dollars_str_to_cents(0.50) == 50


# ----------------------------------------------------------------------------
# snapshot_from_market — pure function, transforms Kalshi market JSON
# ----------------------------------------------------------------------------


def _kalshi_market(**overrides):
    base = {
        "ticker": "KX-NBAGAME-26JAN14LALBOS-BOS",
        "title": "Will Boston beat Lakers?",
        "yes_sub_title": "Boston Celtics",
        "close_time": "2026-01-15T01:00:00Z",
        "yes_bid_dollars": "0.5800",
        "yes_ask_dollars": "0.5900",
        "volume_24h_fp": "12345.6",
        "open_interest_fp": "789.0",
    }
    base.update(overrides)
    return base


class TestSnapshotFromMarket:
    def test_basic_snapshot(self):
        market = _kalshi_market()
        snap = snapshot_from_market(market=market)
        assert isinstance(snap, KalshiMarketSnapshot)
        assert snap.ticker == "KX-NBAGAME-26JAN14LALBOS-BOS"
        assert snap.title == "Will Boston beat Lakers?"
        assert snap.yes_subtitle == "Boston Celtics"
        assert snap.yes_bid_cents == 58
        assert snap.yes_ask_cents == 59
        assert snap.volume == pytest.approx(12345.6)
        assert snap.open_interest == pytest.approx(789.0)

    def test_close_time_parsed(self):
        market = _kalshi_market(close_time="2026-01-15T01:00:00Z")
        snap = snapshot_from_market(market=market)
        assert snap.close_time == datetime(2026, 1, 15, 1, 0, tzinfo=timezone.utc)

    def test_missing_close_time_yields_none(self):
        market = _kalshi_market(close_time=None)
        snap = snapshot_from_market(market=market)
        assert snap.close_time is None

    def test_malformed_close_time_yields_none(self):
        market = _kalshi_market(close_time="not-a-date")
        snap = snapshot_from_market(market=market)
        assert snap.close_time is None

    def test_alt_subtitle_field_used(self):
        # Some payloads use yes_subtitle (no underscore between yes and sub).
        market = _kalshi_market()
        market.pop("yes_sub_title")
        market["yes_subtitle"] = "Alt Form"
        snap = snapshot_from_market(market=market)
        assert snap.yes_subtitle == "Alt Form"

    def test_missing_volume_defaults_to_zero(self):
        market = _kalshi_market()
        market.pop("volume_24h_fp")
        snap = snapshot_from_market(market=market)
        assert snap.volume == 0.0

    def test_alt_volume_field_used(self):
        # Some payloads expose volume_fp instead of volume_24h_fp.
        market = _kalshi_market()
        market.pop("volume_24h_fp")
        market["volume_fp"] = "42.0"
        snap = snapshot_from_market(market=market)
        assert snap.volume == pytest.approx(42.0)

    def test_explicit_collected_at_respected(self):
        when = datetime(2026, 5, 27, 12, 0, tzinfo=timezone.utc)
        snap = snapshot_from_market(market=_kalshi_market(), collected_at=when)
        assert snap.collected_at == when

    def test_default_collected_at_uses_now(self):
        before = datetime.now(timezone.utc)
        snap = snapshot_from_market(market=_kalshi_market())
        after = datetime.now(timezone.utc)
        assert before <= snap.collected_at <= after

    def test_orderbook_preserved_in_raw(self):
        orderbook = {"yes": [[42, 100]], "no": [[58, 100]]}
        snap = snapshot_from_market(market=_kalshi_market(), orderbook=orderbook)
        assert snap.raw_orderbook == orderbook

    def test_missing_orderbook_yields_empty_dict(self):
        snap = snapshot_from_market(market=_kalshi_market())
        assert snap.raw_orderbook == {}

    def test_raw_market_preserved(self):
        market = _kalshi_market()
        snap = snapshot_from_market(market=market)
        assert snap.raw_market is market

    def test_missing_title_defaults_to_empty(self):
        market = _kalshi_market()
        market.pop("title")
        snap = snapshot_from_market(market=market)
        assert snap.title == ""


# ----------------------------------------------------------------------------
# list_sports_markets — paginated fetch with mocked client
# ----------------------------------------------------------------------------


class FakeKalshiClient:
    """Minimal mock that mimics the KalshiClient pagination contract.

    `list_markets` returns (markets, cursor) tuples; `get_orderbook` returns
    a dict. We pre-seed pages and capture call counts for assertion.
    """

    def __init__(self, pages, orderbooks=None):
        self._pages = list(pages)
        self._orderbooks = orderbooks or {}
        self.list_calls = 0
        self.orderbook_calls = []

    def list_markets(self, *, status, limit, cursor, series_ticker):
        self.list_calls += 1
        if not self._pages:
            return [], None
        markets, next_cursor = self._pages.pop(0)
        return markets, next_cursor

    def get_orderbook(self, ticker):
        self.orderbook_calls.append(ticker)
        return self._orderbooks.get(ticker, {"yes": [], "no": []})


class TestListSportsMarkets:
    def test_single_page_returns_all_markets(self):
        markets = [_kalshi_market(ticker="A"), _kalshi_market(ticker="B")]
        client = FakeKalshiClient(pages=[(markets, None)])
        snapshots = list_sports_markets(
            kalshi_client=client, series_ticker="KX-NBAGAME"
        )
        assert len(snapshots) == 2
        assert {s.ticker for s in snapshots} == {"A", "B"}

    def test_paginates_until_cursor_none(self):
        page1 = [_kalshi_market(ticker="A")]
        page2 = [_kalshi_market(ticker="B")]
        page3 = [_kalshi_market(ticker="C")]
        client = FakeKalshiClient(
            pages=[(page1, "cur1"), (page2, "cur2"), (page3, None)]
        )
        snapshots = list_sports_markets(
            kalshi_client=client, series_ticker="KX-NBAGAME"
        )
        assert [s.ticker for s in snapshots] == ["A", "B", "C"]
        assert client.list_calls == 3

    def test_respects_max_pages(self):
        # Pages exist with cursor forever; max_pages should stop us early.
        pages = [([_kalshi_market(ticker=f"T{i}")], f"c{i}") for i in range(10)]
        client = FakeKalshiClient(pages=pages)
        snapshots = list_sports_markets(
            kalshi_client=client, series_ticker="KX-NBAGAME", max_pages=3
        )
        assert client.list_calls == 3
        assert len(snapshots) == 3

    def test_empty_response_terminates(self):
        client = FakeKalshiClient(pages=[([], None)])
        snapshots = list_sports_markets(
            kalshi_client=client, series_ticker="KX-NBAGAME"
        )
        assert snapshots == []

    def test_orderbook_fetched_per_market(self):
        markets = [_kalshi_market(ticker="A"), _kalshi_market(ticker="B")]
        client = FakeKalshiClient(pages=[(markets, None)])
        list_sports_markets(kalshi_client=client, series_ticker="KX-NBAGAME")
        assert client.orderbook_calls == ["A", "B"]

    def test_orderbook_attached_to_snapshot(self):
        ob = {"yes": [[42, 100]]}
        markets = [_kalshi_market(ticker="A")]
        client = FakeKalshiClient(pages=[(markets, None)], orderbooks={"A": ob})
        [snap] = list_sports_markets(
            kalshi_client=client, series_ticker="KX-NBAGAME"
        )
        assert snap.raw_orderbook == ob

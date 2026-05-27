"""Tests for agent_trader.market_discovery — Kalshi market filtering."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from agent_trader.market_discovery import (
    EligibleMarket,
    _parse_close_time,
    discover_eligible_markets,
    dollars_str_to_cents,
)


# ----------------------------------------------------------------------------
# dollars_str_to_cents — defensive Kalshi price parsing
# ----------------------------------------------------------------------------


class TestDollarsStrToCents:
    def test_decimal_string(self):
        assert dollars_str_to_cents("0.4200") == 42

    def test_dollar_string(self):
        assert dollars_str_to_cents("1.00") == 100

    def test_zero(self):
        assert dollars_str_to_cents("0") == 0
        assert dollars_str_to_cents("0.00") == 0

    def test_none_returns_zero(self):
        assert dollars_str_to_cents(None) == 0

    def test_empty_string_returns_zero(self):
        assert dollars_str_to_cents("") == 0

    def test_malformed_string_returns_zero(self):
        assert dollars_str_to_cents("garbage") == 0
        assert dollars_str_to_cents("$0.42") == 0

    def test_float_input_works(self):
        assert dollars_str_to_cents(0.5) == 50

    def test_unsupported_type_returns_zero(self):
        # Lists/dicts shouldn't crash, just produce 0.
        assert dollars_str_to_cents([0.5]) == 0


# ----------------------------------------------------------------------------
# _parse_close_time
# ----------------------------------------------------------------------------


class TestParseCloseTime:
    def test_iso_with_z_suffix(self):
        result = _parse_close_time("2026-05-27T18:00:00Z")
        assert result == datetime(2026, 5, 27, 18, 0, tzinfo=timezone.utc)

    def test_iso_with_offset(self):
        result = _parse_close_time("2026-05-27T18:00:00+00:00")
        assert result == datetime(2026, 5, 27, 18, 0, tzinfo=timezone.utc)

    def test_none_returns_none(self):
        assert _parse_close_time(None) is None

    def test_empty_returns_none(self):
        assert _parse_close_time("") is None

    def test_malformed_returns_none(self):
        # Defensive: bad timestamp shouldn't crash the pipeline.
        assert _parse_close_time("not a date") is None
        assert _parse_close_time("2026-13-99T99:99:99Z") is None


# ----------------------------------------------------------------------------
# discover_eligible_markets — paginated filter against a fake Kalshi client
# ----------------------------------------------------------------------------


def _market(
    *,
    ticker="KX-A",
    title="Test Market",
    volume_24h=1000,
    open_interest=500,
    close_hours_ahead=24,
    yes_bid="0.45",
    yes_ask="0.55",
    last_price="0.50",
):
    close_dt = datetime.now(timezone.utc) + timedelta(hours=close_hours_ahead)
    return {
        "ticker": ticker,
        "title": title,
        "volume_24h_fp": str(volume_24h),
        "open_interest_fp": str(open_interest),
        "close_time": close_dt.isoformat().replace("+00:00", "Z"),
        "yes_bid_dollars": yes_bid,
        "yes_ask_dollars": yes_ask,
        "last_price_dollars": last_price,
    }


class FakeKalshiClient:
    """Returns pre-seeded paginated responses."""

    def __init__(self, pages):
        self._pages = list(pages)
        self.calls = []

    def list_markets(self, **kwargs):
        self.calls.append(kwargs)
        if not self._pages:
            return [], None
        return self._pages.pop(0)


class TestDiscoverEligibleMarkets:
    def test_returns_empty_when_no_markets(self):
        client = FakeKalshiClient(pages=[([], None)])
        result = discover_eligible_markets(client)
        assert result == []

    def test_includes_market_meeting_all_filters(self):
        client = FakeKalshiClient(pages=[([_market()], None)])
        result = discover_eligible_markets(client)
        assert len(result) == 1
        assert isinstance(result[0], EligibleMarket)
        assert result[0].ticker == "KX-A"
        assert result[0].yes_bid_cents == 45
        assert result[0].yes_ask_cents == 55
        assert result[0].last_price_cents == 50

    def test_filters_low_volume(self):
        low = _market(ticker="LOW", volume_24h=50)  # below default 100
        good = _market(ticker="GOOD", volume_24h=500)
        client = FakeKalshiClient(pages=[([low, good], None)])
        result = discover_eligible_markets(client, min_daily_volume=100)
        assert [m.ticker for m in result] == ["GOOD"]

    def test_filters_zero_open_interest(self):
        zero_oi = _market(ticker="ZERO", open_interest=0)
        good = _market(ticker="GOOD", open_interest=100)
        client = FakeKalshiClient(pages=[([zero_oi, good], None)])
        result = discover_eligible_markets(client)
        assert [m.ticker for m in result] == ["GOOD"]

    def test_filters_too_soon_to_close(self):
        soon = _market(ticker="SOON", close_hours_ahead=2)  # below default 8
        good = _market(ticker="GOOD", close_hours_ahead=24)
        client = FakeKalshiClient(pages=[([soon, good], None)])
        result = discover_eligible_markets(client, min_hours_to_close=8)
        assert [m.ticker for m in result] == ["GOOD"]

    def test_filters_already_closed(self):
        closed = _market(ticker="CLOSED", close_hours_ahead=-1)
        client = FakeKalshiClient(pages=[([closed], None)])
        result = discover_eligible_markets(client)
        assert result == []

    def test_skips_markets_with_missing_close_time(self):
        bad = _market(ticker="BAD")
        bad["close_time"] = None
        client = FakeKalshiClient(pages=[([bad], None)])
        result = discover_eligible_markets(client)
        assert result == []

    def test_paginates_until_cursor_none(self):
        page1 = [_market(ticker="A")]
        page2 = [_market(ticker="B")]
        page3 = [_market(ticker="C")]
        client = FakeKalshiClient(
            pages=[(page1, "c1"), (page2, "c2"), (page3, None)]
        )
        result = discover_eligible_markets(client)
        assert [m.ticker for m in result] == ["A", "B", "C"]

    def test_respects_max_pages(self):
        pages = [
            ([_market(ticker=f"T{i}")], f"cur{i}")
            for i in range(10)
        ]
        client = FakeKalshiClient(pages=pages)
        result = discover_eligible_markets(client, max_pages=3)
        assert len(client.calls) == 3
        assert len(result) == 3

    def test_passes_series_ticker_filter(self):
        client = FakeKalshiClient(pages=[([], None)])
        discover_eligible_markets(client, series_ticker="KXNBAGAME")
        assert client.calls[0].get("series_ticker") == "KXNBAGAME"

    def test_omits_series_ticker_when_unset(self):
        client = FakeKalshiClient(pages=[([], None)])
        discover_eligible_markets(client)
        assert "series_ticker" not in client.calls[0]

    def test_falls_back_to_volume_fp_when_24h_missing(self):
        # Older Kalshi API responses sometimes only have volume_fp, not volume_24h_fp.
        market = _market(ticker="OLD")
        del market["volume_24h_fp"]
        market["volume_fp"] = "750"
        client = FakeKalshiClient(pages=[([market], None)])
        result = discover_eligible_markets(client)
        assert len(result) == 1
        assert result[0].volume_24h == 750.0

    def test_raw_market_preserved(self):
        market = _market(ticker="KEEP-RAW")
        client = FakeKalshiClient(pages=[([market], None)])
        [result] = discover_eligible_markets(client)
        assert result.raw_market_response is market

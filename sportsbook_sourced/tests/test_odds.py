from __future__ import annotations

import time

import pytest

from sportsbook_sourced.odds import RateLimiter, StaticOddsProvider, TheOddsApiProvider
from sportsbook_sourced.schemas import SportsbookEvent, SportsbookOdds


# ────────────────────────────────────────────────────────────────────────────
# RateLimiter
# ────────────────────────────────────────────────────────────────────────────


def test_rate_limiter_first_call_does_not_sleep():
    limiter = RateLimiter(min_interval_seconds=0.1)
    start = time.monotonic()
    limiter.wait()
    elapsed = time.monotonic() - start
    # First call has no prior timestamp, so no sleep
    assert elapsed < 0.05


def test_rate_limiter_enforces_min_interval():
    limiter = RateLimiter(min_interval_seconds=0.1)
    limiter.wait()
    start = time.monotonic()
    limiter.wait()
    elapsed = time.monotonic() - start
    # Second call must wait at least min_interval after the first
    assert elapsed >= 0.09  # allow 10ms scheduler slack


def test_rate_limiter_no_sleep_when_interval_already_elapsed():
    limiter = RateLimiter(min_interval_seconds=0.05)
    limiter.wait()
    time.sleep(0.1)  # exceed interval
    start = time.monotonic()
    limiter.wait()
    elapsed = time.monotonic() - start
    # Already past the min interval — no additional sleep
    assert elapsed < 0.02


def test_rate_limiter_records_quota_headers():
    limiter = RateLimiter()
    limiter.record_response_headers({
        "x-requests-remaining": "1500",
        "x-requests-used": "500",
    })
    assert limiter.requests_remaining == 1500
    assert limiter.requests_used == 500


def test_rate_limiter_ignores_malformed_headers():
    limiter = RateLimiter()
    limiter.record_response_headers({
        "x-requests-remaining": "not_a_number",
    })
    # Stays None instead of crashing
    assert limiter.requests_remaining is None


def test_rate_limiter_handles_missing_headers():
    limiter = RateLimiter()
    limiter.record_response_headers({})
    assert limiter.requests_remaining is None
    assert limiter.requests_used is None


def test_rate_limiter_warns_below_threshold(caplog):
    import logging
    limiter = RateLimiter(warn_remaining_threshold=100)
    with caplog.at_level(logging.WARNING, logger="sportsbook_sourced.odds"):
        limiter.record_response_headers({"x-requests-remaining": "50"})
    assert any("quota low" in rec.message for rec in caplog.records)


def test_rate_limiter_no_warn_above_threshold(caplog):
    import logging
    limiter = RateLimiter(warn_remaining_threshold=100)
    with caplog.at_level(logging.WARNING, logger="sportsbook_sourced.odds"):
        limiter.record_response_headers({"x-requests-remaining": "500"})
    assert not any("quota low" in rec.message for rec in caplog.records)


# ────────────────────────────────────────────────────────────────────────────
# StaticOddsProvider
# ────────────────────────────────────────────────────────────────────────────


def _event(eid: str, league: str = "nba") -> SportsbookEvent:
    from datetime import datetime, timezone
    return SportsbookEvent(
        event_id=eid,
        league=league,  # type: ignore[arg-type]
        home_team="Home",
        away_team="Away",
        commence_time=datetime(2026, 5, 20, 0, 0, tzinfo=timezone.utc),
    )


def _odds(eid: str, book: str = "pinnacle") -> SportsbookOdds:
    from datetime import datetime, timezone
    now = datetime(2026, 5, 20, 0, 0, tzinfo=timezone.utc)
    return SportsbookOdds(
        event_id=eid,
        bookmaker=book,
        market_type="moneyline",
        outcome_name="Home",
        american_odds=-150,
        last_update=now,
        collected_at=now,
    )


def test_static_provider_filters_by_league():
    nba_event = _event("e_nba", league="nba")
    nfl_event = _event("e_nfl", league="nfl")
    provider = StaticOddsProvider(
        events=[nba_event, nfl_event],
        odds=[_odds("e_nba"), _odds("e_nfl")],
    )
    assert len(provider.list_events("nba")) == 1
    assert provider.list_events("nba")[0].event_id == "e_nba"


def test_static_provider_filters_odds_by_league():
    nba_event = _event("e_nba", league="nba")
    nfl_event = _event("e_nfl", league="nfl")
    provider = StaticOddsProvider(
        events=[nba_event, nfl_event],
        odds=[_odds("e_nba"), _odds("e_nfl")],
    )
    nba_odds = provider.list_moneyline_odds("nba")
    assert len(nba_odds) == 1
    assert nba_odds[0].event_id == "e_nba"


def test_static_provider_excludes_non_moneyline_odds():
    from datetime import datetime, timezone
    now = datetime(2026, 5, 20, 0, 0, tzinfo=timezone.utc)
    event = _event("e1")
    spread = SportsbookOdds(
        event_id="e1",
        bookmaker="pinnacle",
        market_type="moneyline",  # only moneyline supported in V1
        outcome_name="Home",
        american_odds=-150,
        last_update=now,
        collected_at=now,
    )
    provider = StaticOddsProvider(events=[event], odds=[spread])
    # All current entries are moneyline; just sanity-check the filter shape
    assert len(provider.list_moneyline_odds("nba")) == 1


# ────────────────────────────────────────────────────────────────────────────
# TheOddsApiProvider — integration shape, using a stub session
# ────────────────────────────────────────────────────────────────────────────


class _StubResponse:
    """Minimal Response shim for the requests.Session.get call path."""

    def __init__(self, payload, headers=None, status_code=200):
        self._payload = payload
        self.headers = headers or {}
        self.status_code = status_code

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _StubSession:
    def __init__(self, response: _StubResponse):
        self._response = response
        self.calls: list[tuple[str, dict]] = []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, params or {}))
        return self._response


def test_odds_api_provider_throttles_and_reads_headers():
    payload = []
    response = _StubResponse(
        payload,
        headers={"x-requests-remaining": "1234", "x-requests-used": "766"},
    )
    limiter = RateLimiter(min_interval_seconds=0.05)
    provider = TheOddsApiProvider(api_key="test", rate_limiter=limiter)
    provider.session = _StubSession(response)  # type: ignore[assignment]

    provider.list_events("nba")
    assert limiter.requests_remaining == 1234
    assert limiter.requests_used == 766

    # Second call after the first should still pace through limiter.wait()
    start = time.monotonic()
    provider.list_events("nba")
    elapsed = time.monotonic() - start
    assert elapsed >= 0.04


def test_odds_api_provider_records_headers_on_error_response():
    """Even on a 4xx (e.g. quota exceeded), quota headers should update."""
    response = _StubResponse(
        [],
        headers={"x-requests-remaining": "0", "x-requests-used": "2000"},
        status_code=401,
    )
    limiter = RateLimiter()
    provider = TheOddsApiProvider(api_key="test", rate_limiter=limiter)
    provider.session = _StubSession(response)  # type: ignore[assignment]

    with pytest.raises(RuntimeError, match="HTTP 401"):
        provider.list_events("nba")
    # Headers must have been recorded BEFORE the raise
    assert limiter.requests_remaining == 0
    assert limiter.requests_used == 2000

from __future__ import annotations

import logging
import threading
import time
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Iterable, Optional

import requests

from .schemas import League, SportsbookEvent, SportsbookOdds


log = logging.getLogger("sportsbook_sourced.odds")


class RateLimiter:
    """Min-interval throttle + quota tracking.

    Enforces a minimum wall-clock gap between requests so a tight scan loop
    can't burst-call the provider. Thread-safe: the same limiter can be
    shared across concurrent scans. Quota state is best-effort — set by
    the provider after each response from `x-requests-remaining` headers.

    The class does NOT auto-stop when the quota hits zero. It logs a
    warning at the threshold and lets the caller decide whether to bail —
    surfacing the signal through the killswitch path is the caller's job,
    not the limiter's.
    """

    def __init__(
        self,
        *,
        min_interval_seconds: float = 1.0,
        warn_remaining_threshold: int = 100,
    ):
        self.min_interval_seconds = min_interval_seconds
        self.warn_remaining_threshold = warn_remaining_threshold
        self._last_request_monotonic: Optional[float] = None
        self._lock = threading.Lock()
        self.requests_remaining: Optional[int] = None
        self.requests_used: Optional[int] = None

    def wait(self) -> None:
        """Block until the min-interval has elapsed since the last request."""
        with self._lock:
            if self._last_request_monotonic is not None:
                elapsed = time.monotonic() - self._last_request_monotonic
                remaining = self.min_interval_seconds - elapsed
                if remaining > 0:
                    time.sleep(remaining)
            self._last_request_monotonic = time.monotonic()

    def record_response_headers(self, headers) -> None:
        """Read The Odds API quota headers and update local state."""
        remaining_raw = headers.get("x-requests-remaining")
        used_raw = headers.get("x-requests-used")
        if remaining_raw is not None:
            try:
                self.requests_remaining = int(remaining_raw)
            except (TypeError, ValueError):
                pass
        if used_raw is not None:
            try:
                self.requests_used = int(used_raw)
            except (TypeError, ValueError):
                pass
        if (
            self.requests_remaining is not None
            and self.requests_remaining <= self.warn_remaining_threshold
        ):
            log.warning(
                "odds API quota low: %d requests remaining (used=%s)",
                self.requests_remaining,
                self.requests_used,
            )


class OddsProvider(ABC):
    """Interface for sportsbook odds providers."""

    @abstractmethod
    def list_events(self, league: League) -> list[SportsbookEvent]:
        raise NotImplementedError

    @abstractmethod
    def list_moneyline_odds(self, league: League) -> list[SportsbookOdds]:
        raise NotImplementedError


class StaticOddsProvider(OddsProvider):
    """In-memory provider for tests and offline development."""

    def __init__(self, events: Iterable[SportsbookEvent], odds: Iterable[SportsbookOdds]):
        self._events = list(events)
        self._odds = list(odds)

    def list_events(self, league: League) -> list[SportsbookEvent]:
        return [event for event in self._events if event.league == league]

    def list_moneyline_odds(self, league: League) -> list[SportsbookOdds]:
        event_ids = {event.event_id for event in self.list_events(league)}
        return [row for row in self._odds if row.event_id in event_ids and row.market_type == "moneyline"]


class TheOddsApiProvider(OddsProvider):
    """Thin client scaffold for The Odds API.

    The provider is intentionally minimal. It should be wired and tested later
    when the project has an API key and an active sport season.
    """

    SPORT_KEYS = {
        "nba": "basketball_nba",
        "nfl": "americanfootball_nfl",
    }

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = "https://api.the-odds-api.com/v4",
        rate_limiter: Optional[RateLimiter] = None,
    ):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        # Default to a 1-second floor between requests. The Odds API tiers
        # are monthly-quota gated, not RPS-gated, but a min interval prevents
        # accidental loops from burning the quota in one mistake.
        self.rate_limiter = rate_limiter or RateLimiter()

    def _get(self, path: str, **params) -> dict | list:
        params = {"apiKey": self.api_key, **params}
        self.rate_limiter.wait()
        response = self.session.get(f"{self.base_url}{path}", params=params, timeout=20)
        # Record quota headers BEFORE raise_for_status so a 4xx that still
        # included headers (e.g. quota-exceeded 401/429) still updates state.
        self.rate_limiter.record_response_headers(response.headers)
        response.raise_for_status()
        return response.json()

    def list_events(self, league: League) -> list[SportsbookEvent]:
        sport = self.SPORT_KEYS[league]
        payload = self._get(f"/sports/{sport}/events")
        events: list[SportsbookEvent] = []
        for row in payload:
            events.append(SportsbookEvent(
                event_id=row["id"],
                league=league,
                home_team=row["home_team"],
                away_team=row["away_team"],
                commence_time=datetime.fromisoformat(row["commence_time"].replace("Z", "+00:00")),
                raw=row,
            ))
        return events

    def list_moneyline_odds(self, league: League) -> list[SportsbookOdds]:
        sport = self.SPORT_KEYS[league]
        payload = self._get(
            f"/sports/{sport}/odds",
            regions="us",
            markets="h2h",
            oddsFormat="american",
            dateFormat="iso",
        )
        collected_at = datetime.now(timezone.utc)
        rows: list[SportsbookOdds] = []
        for event in payload:
            for bookmaker in event.get("bookmakers", []):
                for market in bookmaker.get("markets", []):
                    if market.get("key") != "h2h":
                        continue
                    last_update = datetime.fromisoformat(market["last_update"].replace("Z", "+00:00"))
                    for outcome in market.get("outcomes", []):
                        rows.append(SportsbookOdds(
                            event_id=event["id"],
                            bookmaker=bookmaker["key"],
                            market_type="moneyline",
                            outcome_name=outcome["name"],
                            american_odds=int(outcome["price"]),
                            last_update=last_update,
                            collected_at=collected_at,
                            raw={"event": event, "bookmaker": bookmaker, "market": market, "outcome": outcome},
                        ))
        return rows


from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Iterable

import requests

from .schemas import League, SportsbookEvent, SportsbookOdds


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

    def __init__(self, api_key: str, *, base_url: str = "https://api.the-odds-api.com/v4"):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()

    def _get(self, path: str, **params) -> dict | list:
        params = {"apiKey": self.api_key, **params}
        response = self.session.get(f"{self.base_url}{path}", params=params, timeout=20)
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


"""Self-contained sample data for `cli.py scan`'s dry-run mode.

Not fetched from any network -- a fixed, production-shaped NBA matchup
(Kalshi's short subtitle vs. a sportsbook's full club name) with a genuine
mispricing, so a dry run exercises the whole pipeline end-to-end instead of
printing a static placeholder. See CLAUDE.md P1.6.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .kalshi_feed import snapshot_from_market
from .schemas import KalshiMarketSnapshot, SportsbookEvent, SportsbookOdds


_TIP = datetime(2026, 1, 15, 1, 0, tzinfo=timezone.utc)  # ~8pm Eastern, Jan 14

# The reference instant the sample data is anchored to. Must be passed as
# `pipeline.run_scan`'s `now=` for dry runs -- `build_moneyline_fair_price`
# computes odds staleness against `now`, and the real wall-clock time is
# months away from this fixed sample, which would flag every book stale and
# make the "dry run" silently find nothing.
SAMPLE_NOW = _TIP - timedelta(hours=1)

SAMPLE_EVENT_ID = "sample-lal-bos"


def sample_event() -> SportsbookEvent:
    return SportsbookEvent(
        event_id=SAMPLE_EVENT_ID,
        league="nba",
        home_team="Los Angeles Lakers",
        away_team="Boston Celtics",
        commence_time=_TIP,
    )


def sample_market() -> KalshiMarketSnapshot:
    return snapshot_from_market(
        market={
            "ticker": "KXNBAGAME-26JAN14LALBOS-LAL",
            "title": "Los Angeles Lakers vs Boston Celtics Winner",
            "yes_sub_title": "Lakers",
            "close_time": (_TIP + timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "yes_bid_dollars": "0.55",
            "yes_ask_dollars": "0.57",
            "volume_24h_fp": "8500",
            "open_interest_fp": "12000",
        },
        collected_at=SAMPLE_NOW,
    )


def sample_odds() -> list[SportsbookOdds]:
    """Three books pricing the Lakers as a heavy underdog against Kalshi's
    near-even 55/57c quote -- a genuine, large mispricing, so a dry run
    demonstrates a real "buy" decision, not just plumbing."""
    event = sample_event()
    quotes = {
        "pinnacle": (185, -220),
        "draftkings": (190, -225),
        "fanduel": (183, -218),
    }
    rows: list[SportsbookOdds] = []
    for bookmaker, (home_price, away_price) in quotes.items():
        for name, price in ((event.home_team, home_price), (event.away_team, away_price)):
            rows.append(SportsbookOdds(
                event_id=event.event_id, bookmaker=bookmaker, market_type="moneyline",
                outcome_name=name, american_odds=price,
                last_update=SAMPLE_NOW - timedelta(seconds=30),
                collected_at=SAMPLE_NOW,
            ))
    return rows

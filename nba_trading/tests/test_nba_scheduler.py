"""Schedule derived from Kalshi markets: tip-off = expected_expiration_time - 3h."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import pytest

from nba_trading import nba_scheduler as ns

NOW = datetime(2026, 10, 28, 12, 0, tzinfo=timezone.utc)


def _mkt(event, team, tip_hours, status="open", exp=True):
    tip = NOW + timedelta(hours=tip_hours)
    m = {"ticker": f"{event}-{team}", "event_ticker": event, "status": status}
    if exp:
        m["expected_expiration_time"] = (tip + timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return m


def _pair(event, tip_hours, **kw):
    away, home = event[-6:-3], event[-3:]
    return [_mkt(event, away, tip_hours, **kw), _mkt(event, home, tip_hours, **kw)]


def test_tip_is_expected_expiration_minus_3h_and_teams_are_away_then_home():
    g = ns.games_from_kalshi_markets(_pair("KXNBAGAME-26OCT28OKCHOU", 5), now=NOW)
    assert g == [{"homeTeam": {"teamTricode": "HOU"}, "awayTeam": {"teamTricode": "OKC"},
                  "gameTimeUTC": "2026-10-28T17:00:00Z"}]


def test_matches_real_observed_kalshi_values():
    # Real market seen 2026-10-28: KXNBAGAME-26OCT31OKCHOU, expected_expiration 2026-11-01T03:30Z
    mk = [{"ticker": f"KXNBAGAME-26OCT31OKCHOU-{t}", "event_ticker": "KXNBAGAME-26OCT31OKCHOU",
           "status": "open", "expected_expiration_time": "2026-11-01T03:30:00Z"} for t in ("OKC", "HOU")]
    g = ns.games_from_kalshi_markets(mk, now=datetime(2026, 10, 31, 12, 0, tzinfo=timezone.utc))
    assert g[0]["gameTimeUTC"] == "2026-11-01T00:30:00Z"


def test_window_excludes_started_and_far_future_games():
    mk = (_pair("KXNBAGAME-26OCT28AAABBB", -1) + _pair("KXNBAGAME-26OCT30CCCDDD", 40)
          + _pair("KXNBAGAME-26OCT28EEEFFF", 3))
    g = ns.games_from_kalshi_markets(mk, now=NOW)
    assert [x["homeTeam"]["teamTricode"] for x in g] == ["FFF"]


def test_requires_both_team_markets(caplog):
    one_sided = [_pair("KXNBAGAME-26OCT28OKCHOU", 5)[0]]
    with caplog.at_level(logging.WARNING):
        assert ns.games_from_kalshi_markets(one_sided, now=NOW) == []
    assert "both team markets" in caplog.text


def test_missing_or_bad_expiration_is_skipped_not_guessed(caplog):
    with caplog.at_level(logging.WARNING):
        assert ns.games_from_kalshi_markets(_pair("KXNBAGAME-26OCT28OKCHOU", 5, exp=False), now=NOW) == []
        bad = _pair("KXNBAGAME-26OCT28OKCHOU", 5)
        bad[0]["expected_expiration_time"] = "garbage"
        bad[1]["expected_expiration_time"] = "garbage"
        assert ns.games_from_kalshi_markets(bad, now=NOW) == []
    assert "no usable expected_expiration_time" in caplog.text


def test_closed_markets_ignored():
    assert ns.games_from_kalshi_markets(_pair("KXNBAGAME-26OCT28OKCHOU", 5, status="closed"), now=NOW) == []


def test_sorted_by_tipoff():
    mk = _pair("KXNBAGAME-26OCT28AAABBB", 8) + _pair("KXNBAGAME-26OCT28CCCDDD", 2)
    assert [g["homeTeam"]["teamTricode"] for g in ns.games_from_kalshi_markets(mk, now=NOW)] == ["DDD", "BBB"]


class _Client:
    def __init__(self, markets=None, exc=None):
        self.markets, self.exc = markets, exc

    def fetch_nba_markets(self, raise_on_error=False):
        assert raise_on_error is True            # the schedule must be able to see failures
        if self.exc:
            raise self.exc
        return self.markets


def test_get_schedule_returns_none_on_fetch_failure_but_empty_list_when_no_games():
    assert ns.get_schedule(_Client(exc=RuntimeError("boom"))) is None
    assert ns.get_schedule(_Client(markets=[])) == []


# --- Real-data check -------------------------------------------------------
# Kalshi's expected_expiration_time values captured from the live API on 2026-10-28,
# compared with the tip-off times on Basketball-Reference's 2026-27 schedule for the
# same day (listed in ET). Independent sources; every game and time must agree.

CAPTURED_KALSHI_EXPECTED_EXPIRATION = {      # event ticker suffix -> expected_expiration_time
    "PHIIND": "2026-10-29T02:00:00Z", "CHADET": "2026-10-29T02:00:00Z",
    "WASCLE": "2026-10-29T02:00:00Z", "MIAATL": "2026-10-29T02:00:00Z",
    "ORLTOR": "2026-10-29T02:30:00Z", "DENNOP": "2026-10-29T03:00:00Z",
    "GSWMIN": "2026-10-29T03:00:00Z", "HOUMIL": "2026-10-29T03:00:00Z",
    "NYKCHI": "2026-10-29T03:00:00Z", "SASUTA": "2026-10-29T04:00:00Z",
    "OKCDAL": "2026-10-29T04:30:00Z", "LALLAC": "2026-10-29T05:30:00Z",
}
BASKETBALL_REFERENCE_2026_10_28_ET = {       # away+home -> tip-off, America/New_York
    "MIAATL": "19:00", "WASCLE": "19:00", "PHIIND": "19:00", "CHADET": "19:00",
    "ORLTOR": "19:30", "HOUMIL": "20:00", "GSWMIN": "20:00", "DENNOP": "20:00",
    "NYKCHI": "20:00", "SASUTA": "21:00", "OKCDAL": "21:30", "LALLAC": "22:30",
}


def test_kalshi_derived_tipoffs_match_independent_schedule_for_2026_10_28():
    from zoneinfo import ZoneInfo
    markets = []
    for teams, exp in CAPTURED_KALSHI_EXPECTED_EXPIRATION.items():
        event = f"KXNBAGAME-26OCT28{teams}"
        markets += [{"ticker": f"{event}-{t}", "event_ticker": event, "status": "open",
                     "expected_expiration_time": exp} for t in (teams[:3], teams[3:])]
    now = datetime(2026, 10, 28, 14, 0, tzinfo=timezone.utc)
    games = ns.games_from_kalshi_markets(markets, now=now, window_hours=30)

    derived = {}
    for g in games:
        tip = datetime.fromisoformat(g["gameTimeUTC"].replace("Z", "+00:00")).astimezone(ZoneInfo("America/New_York"))
        derived[g["awayTeam"]["teamTricode"] + g["homeTeam"]["teamTricode"]] = tip.strftime("%H:%M")

    assert derived == BASKETBALL_REFERENCE_2026_10_28_ET

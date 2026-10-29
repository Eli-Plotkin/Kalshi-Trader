import logging
from datetime import datetime, timedelta, timezone

import requests

# Kalshi sets a market's expected_expiration_time to the scheduled tip-off + 3h.
# Verified on 30 settled games (regular season + playoffs): offset was exactly 3.00h
# for every one. That lets the bot derive tip-off times from the markets it already
# fetches, with no outside schedule source.
EXPECTED_EXPIRATION_AFTER_TIP = timedelta(hours=3)
SCHEDULE_WINDOW_HOURS = 24

NBA_SCOREBOARD_URL = "https://cdn.nba.com/static/json/liveData/scoreboard/todaysScoreboard_00.json"

def get_tip_off_time(team_tri):
    """
    Finds the UTC start time for a game involving the given team tricode (e.g. 'LAL').
    Checks BOTH Home and Away fields.
    """
    try:
        resp = requests.get(NBA_SCOREBOARD_URL, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        
        games = data.get('scoreboard', {}).get('games', [])
        
        for game in games:
            # Check if our team is EITHER the home or away team
            h_team = game['homeTeam'].get('teamTricode')
            a_team = game['awayTeam'].get('teamTricode')
            
            if team_tri in [h_team, a_team]:
                # ALWAYS use gameTimeUTC. 
                # gameEt is local time but often has a confusing 'Z' suffix.
                return game.get('gameTimeUTC') 

        return None

    except Exception as e:
        logging.error(f"Failed to fetch tip off time for {team_tri}: {e}")
        return None
    

def get_todays_schedule_map():
    try:
        resp = requests.get(NBA_SCOREBOARD_URL, timeout=10)
        resp.raise_for_status()
        data = resp.json()

        return data.get('scoreboard', {}).get('games', [])

    except Exception as e:
        logging.error(f"Failed to fetch schedule for NBA: {type(e).__name__}: {e}")
        return None


def _parse_utc(value):
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return None


def games_from_kalshi_markets(markets, now=None, window_hours=SCHEDULE_WINDOW_HOURS):
    """Build the day's games from open KXNBAGAME markets.

    Event tickers look like KXNBAGAME-26OCT31OKCHOU-OKC: the last six letters of the
    event part are AWAY then HOME tricodes. A game needs BOTH team markets open and a
    usable expected_expiration_time; anything else is skipped, never guessed. Only games
    tipping off within the next `window_hours` are returned, in the scoreboard shape
    main.py consumes.
    """
    now = now or datetime.now(timezone.utc)
    horizon = now + timedelta(hours=window_hours)
    by_event = {}
    for m in markets:
        if m.get("status") not in (None, "open", "active"):
            continue
        by_event.setdefault(m.get("event_ticker") or m["ticker"].rsplit("-", 1)[0], []).append(m)

    games = []
    for event, mkts in by_event.items():
        teams = event.rsplit("-", 1)[-1][-6:]
        away, home = teams[:3], teams[3:]
        sides = {m["ticker"].rsplit("-", 1)[-1] for m in mkts}
        if sides != {away, home}:
            logging.warning(f"Skipping {event}: expected both team markets, found {sorted(sides)}")
            continue
        exp = _parse_utc(mkts[0].get("expected_expiration_time"))
        if exp is None:
            logging.warning(f"Skipping {event}: no usable expected_expiration_time")
            continue
        tip = exp - EXPECTED_EXPIRATION_AFTER_TIP
        if not (now < tip <= horizon):
            continue
        games.append({
            "homeTeam": {"teamTricode": home},
            "awayTeam": {"teamTricode": away},
            "gameTimeUTC": tip.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        })
    return sorted(games, key=lambda g: g["gameTimeUTC"])


def get_schedule(client):
    """Upcoming games derived from Kalshi's open markets.

    Returns a list (possibly empty) or None if the market fetch FAILED -- callers must
    not treat None as "no games".
    """
    try:
        return games_from_kalshi_markets(client.fetch_nba_markets(raise_on_error=True))
    except Exception as e:
        logging.error(f"Failed to build schedule from Kalshi markets: {type(e).__name__}: {e}")
        return None

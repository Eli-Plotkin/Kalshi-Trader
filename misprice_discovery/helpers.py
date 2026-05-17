import requests
from datetime import datetime

# Official NBA CDN Endpoint
NBA_SCOREBOARD_URL = "https://cdn.nba.com/static/json/staticData/scheduleLeagueV2.json"


def parse_event_ticker(event_ticker):
    # Kalshi inputs can be either an event ticker
    # ("KXNBAGAME-26JAN14LALBOS") or a market ticker
    # ("KXNBAGAME-26JAN14LALBOS-LAL"). Drop the market-side suffix first.
    ticker_root = event_ticker.rsplit('-', 1)[0] if event_ticker.count('-') >= 2 else event_ticker
    clean_ticker = ticker_root.replace('-', '')

    if "KXNBAGAME" not in clean_ticker:
        return None, None

    remainder = clean_ticker[len("KXNBAGAME"):]
    teams_str = remainder[-6:]
    raw_date_str = remainder[:-6]
    target_date = datetime.strptime(raw_date_str, "%y%b%d").date()
    matchup = tuple(sorted((teams_str[:3], teams_str[3:])))

    return target_date, matchup

def get_season_schedule_map():
    try:
        resp = requests.get(NBA_SCOREBOARD_URL, timeout=10)
        resp.raise_for_status()
        data = resp.json()

        return data.get("leagueSchedule")
    
    except Exception as e:
        print(f"Failed to fetch schedule for NBA: {e}")
        return None

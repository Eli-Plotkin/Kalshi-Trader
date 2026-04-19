import requests
from datetime import datetime, timedelta

# Official NBA CDN Endpoint
NBA_SCOREBOARD_URL = "https://cdn.nba.com/static/json/staticData/scheduleLeagueV2.json"

AVERAGE_TIP_OFF_DELAY = 12

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

def get_tip_off_time(event_ticker, league_schedule):
    """
    Finds the UTC start time for a game involving the given team tricode (e.g. 'LAL').
    Checks BOTH Home and Away fields.
    """
    try:
        target_date, matchup = parse_event_ticker(event_ticker)
        if not target_date or not matchup:
            print(f"Skipping non-NBA ticker: {event_ticker}")
            return None, None

        game_dates = league_schedule.get('gameDates')

        if not game_dates: 
            print(f"Bad schedule")
            return None, None
        
        for date_entry in game_dates:
            nba_date_str = date_entry.get('gameDate')
                # Parse NBA date to compare with our Target Date
            nba_date = datetime.strptime(nba_date_str[:10], "%m/%d/%Y").date()

            if nba_date != target_date:
                    continue
            
            for game in date_entry.get('games'):

                    h_team = game['homeTeam']['teamTricode']
                    a_team = game['awayTeam']['teamTricode']
                    
                    if tuple(sorted((h_team, a_team))) == matchup:
                        
                        if game.get('gameLabel') == 'Preseason':
                        # print(f"Skipping Preseason: {event_ticker}") 
                            return None, None

                        utc_str = game['gameDateTimeUTC'] # "2026-01-14T23:00:00Z"
                        
                        # 1. Parse Scheduled Time
                        dt = datetime.fromisoformat(utc_str.replace('Z', '+00:00'))
                        
                        # 2. Apply The "Real World" Buffer
                        # Shifts start time to likely Tip-Off
                        adjusted_dt = dt + timedelta(minutes=AVERAGE_TIP_OFF_DELAY)
                        
                        return int(adjusted_dt.timestamp()), game['gameLabel']

        return None, None
    
    except Exception as e:
        print(f"Error parsing schedule for {event_ticker}: {e}")
        return None, None

    

def get_season_schedule_map():
    try:
        resp = requests.get(NBA_SCOREBOARD_URL, timeout=10)
        resp.raise_for_status()
        data = resp.json()

        return data.get("leagueSchedule")
    
    except Exception as e:
        print(f"Failed to fetch schedule for NBA: {e}")
        return None

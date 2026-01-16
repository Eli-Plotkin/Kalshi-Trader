import requests
import logging

# Official NBA CDN Endpoint
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
        logging.error(f"Failed to fetch schedule for NBA")
        return None
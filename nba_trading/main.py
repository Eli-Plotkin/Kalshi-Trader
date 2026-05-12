import time
import json
import logging
import sys
from datetime import datetime, timezone, timedelta
from logging.handlers import RotatingFileHandler

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.date import DateTrigger

from kalshi.client import KalshiClient
from kalshi.config import API_KEY_ID, BASE_URL, PRIVATE_KEY_PATH
from nba_trading import config, strategy, nba_scheduler
from nba_trading.portfolio import Portfolio

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        RotatingFileHandler("trade_bot.log", maxBytes=5*1024*1024, backupCount=3),
        logging.StreamHandler(sys.stdout)
    ]
)


# --- Schedule persistence ---

def load_schedule():
    try:
        with open(config.SCHEDULE_FILE, 'r') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_schedule(schedule):
    with open(config.SCHEDULE_FILE, 'w') as f:
        json.dump(schedule, f, indent=4)


def mark_game_fired(game_id):
    schedule = load_schedule()
    if game_id in schedule.get('games', {}):
        schedule['games'][game_id]['fired'] = True
        save_schedule(schedule)


# --- Fill confirmation job ---

def confirm_fill_job(client, portfolio, ticker, order_id, shares_attempted):
    order = client.get_order_status(order_id)
    if not order:
        logging.error(f"FILL CHECK FAILED: Could not fetch status for {ticker} order {order_id}")
        return

    try:
        filled = round(float(order.get('fill_count_fp', '0')))
    except (ValueError, TypeError):
        filled = 0

    if filled == 0:
        logging.warning(f"ORDER EXPIRED UNFILLED: {ticker} | Removing from portfolio")
        portfolio.remove_position(ticker)
    elif filled < shares_attempted:
        logging.info(f"PARTIAL FILL: {ticker} | {filled}/{shares_attempted} shares | Updating portfolio")
        portfolio.update_position_qty(ticker, filled)
    else:
        logging.info(f"FILL CONFIRMED: {ticker} | {filled}/{shares_attempted} shares filled")


# --- Pre-game job ---

def pre_game_job(client, portfolio, scheduler, game):
    home_tri = game['home_team']
    away_tri = game['away_team']
    tip_off_utc = game['tip_off_utc']

    logging.info(f"Pre-game job firing: {away_tri} @ {home_tri}")
    mark_game_fired(game['game_id'])

    # Find both team tickers in open Kalshi markets
    markets = client.fetch_nba_markets()
    ticker_map = {
        m['ticker'].split('-')[-1]: m['ticker']
        for m in markets
        if m['ticker'].split('-')[-1] in [home_tri, away_tri]
    }

    if home_tri not in ticker_map or away_tri not in ticker_map:
        logging.warning(f"Could not find Kalshi markets for {away_tri} @ {home_tri}. Skipping.")
        return

    home_ticker = ticker_map[home_tri]
    away_ticker = ticker_map[away_tri]

    home_book = client.get_orderbook(home_ticker)
    away_book = client.get_orderbook(away_ticker)

    favorite_tri = strategy.identify_favorite(home_tri, away_tri, home_book, away_book)
    if not favorite_tri:
        return

    favorite_ticker = ticker_map[favorite_tri]
    favorite_book = home_book if favorite_tri == home_tri else away_book

    ask_price = strategy.get_implied_ask(favorite_book)
    if ask_price is None:
        logging.warning(f"No ask price available for {favorite_ticker}. Skipping.")
        return

    if not strategy.should_buy(ask_price, config.FAVORITE_PRICE_MIN, config.FAVORITE_PRICE_MAX):
        logging.info(
            f"{favorite_ticker} ask {ask_price}¢ outside range "
            f"[{config.FAVORITE_PRICE_MIN}, {config.FAVORITE_PRICE_MAX}]. Skipping."
        )
        return

    if portfolio.has_position(favorite_ticker):
        logging.info(f"Already have position in {favorite_ticker}. Skipping.")
        return

    # expiration_ts in unix seconds — order auto-cancels at tip-off if unfilled
    clean_time = tip_off_utc.replace('Z', '+00:00')
    tip_off_dt = datetime.fromisoformat(clean_time)
    expiration_ts = int(tip_off_dt.timestamp())

    logging.info(
        f"BUY SIGNAL: {favorite_ticker} | Ask: {ask_price}¢ | "
        f"Placing {config.SHARES_TO_BUY} shares @ {config.FAVORITE_PRICE_MAX}¢"
    )

    order = client.place_limit_order(
        ticker=favorite_ticker,
        count=config.SHARES_TO_BUY,
        price=config.FAVORITE_PRICE_MAX,
        action="buy",
        expiration_ts=expiration_ts
    )

    if order == "INSUFFICIENT_FUNDS":
        logging.warning("INSUFFICIENT FUNDS. Cannot place order.")
        return

    if not order or 'order_id' not in order:
        logging.error(f"Order placement failed for {favorite_ticker}.")
        return

    portfolio.add_position(favorite_ticker, config.FAVORITE_PRICE_MAX, config.SHARES_TO_BUY)
    logging.info(f"Order placed: {favorite_ticker} | {config.SHARES_TO_BUY} shares @ {config.FAVORITE_PRICE_MAX}¢ | Expires at tip-off")

    # Schedule a fill check 2 minutes after tip-off
    confirm_time = tip_off_dt + timedelta(minutes=2)
    scheduler.add_job(
        confirm_fill_job,
        trigger=DateTrigger(run_date=confirm_time),
        args=[client, portfolio, favorite_ticker, order['order_id'], config.SHARES_TO_BUY],
        id=f"confirm-{order['order_id']}"
    )
    logging.info(f"Fill check scheduled for {confirm_time.isoformat()} UTC")


# --- Daily schedule setup ---

def _register_job(scheduler, client, portfolio, game_entry, trigger_dt):
    scheduler.add_job(
        pre_game_job,
        trigger=DateTrigger(run_date=trigger_dt),
        args=[client, portfolio, scheduler, game_entry],
        id=game_entry['game_id'],
        replace_existing=True
    )
    logging.info(
        f"Scheduled: {game_entry['away_team']} @ {game_entry['home_team']} "
        f"triggers at {trigger_dt.isoformat()} UTC"
    )


def setup_daily_schedule(client, portfolio, scheduler):
    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    now = datetime.now(timezone.utc)

    existing = load_schedule()

    # On restart with an existing schedule for today, recover unfired jobs
    if existing.get('date') == today:
        logging.info("Recovering schedule from disk...")
        recovered = 0
        for game_id, game in existing.get('games', {}).items():
            if game.get('fired'):
                continue

            trigger_dt = datetime.fromisoformat(game['trigger_time_utc'])
            tip_off_dt = datetime.fromisoformat(game['tip_off_utc'].replace('Z', '+00:00'))

            if trigger_dt > now:
                _register_job(scheduler, client, portfolio, game, trigger_dt)
                recovered += 1
            elif tip_off_dt > now:
                # Missed the T-15 window but game hasn't started — fire immediately
                logging.info(f"Missed T-15 for {game['away_team']} @ {game['home_team']}. Firing now.")
                pre_game_job(client, portfolio, scheduler, game)
                recovered += 1

        logging.info(f"Recovered {recovered} jobs from disk.")
        return

    # Fresh day — fetch schedule and build new job list
    games_today = nba_scheduler.get_todays_schedule_map()
    if not games_today:
        logging.info("No NBA games today or failed to fetch schedule.")
        save_schedule({'date': today, 'games': {}})
        return

    open_markets = client.fetch_nba_markets()
    open_tricodes = {m['ticker'].split('-')[-1] for m in open_markets}

    schedule = {'date': today, 'games': {}}
    scheduled = 0

    for game in games_today:
        home_tri = game['homeTeam']['teamTricode']
        away_tri = game['awayTeam']['teamTricode']
        tip_off_utc = game.get('gameTimeUTC')

        if not tip_off_utc:
            continue

        # Skip games whose Kalshi markets aren't open yet
        if home_tri not in open_tricodes or away_tri not in open_tricodes:
            logging.info(f"Skipping {away_tri} @ {home_tri}: Kalshi markets not open.")
            continue

        clean_time = tip_off_utc.replace('Z', '+00:00')
        tip_off_dt = datetime.fromisoformat(clean_time)
        trigger_dt = tip_off_dt - timedelta(minutes=15)

        game_id = f"{today}-{home_tri}-{away_tri}"
        game_entry = {
            'game_id': game_id,
            'home_team': home_tri,
            'away_team': away_tri,
            'tip_off_utc': tip_off_utc,
            'trigger_time_utc': trigger_dt.isoformat(),
            'fired': False
        }
        schedule['games'][game_id] = game_entry

        if trigger_dt <= now:
            if tip_off_dt > now:
                logging.info(f"Already past T-15 for {away_tri} @ {home_tri}. Firing immediately.")
                save_schedule(schedule)
                pre_game_job(client, portfolio, game_entry)
                schedule['games'][game_id]['fired'] = True
                scheduled += 1
        else:
            _register_job(scheduler, client, portfolio, game_entry, trigger_dt)
            scheduled += 1

    save_schedule(schedule)
    logging.info(f"Daily setup complete. {scheduled} game(s) scheduled.")


# --- Entry point ---

def run_bot():
    client = KalshiClient(
        base_url=BASE_URL,
        key_id=API_KEY_ID,
        key_file_path=PRIVATE_KEY_PATH
    )
    portfolio = Portfolio()

    scheduler = BackgroundScheduler(timezone='UTC')
    scheduler.start()

    logging.info("NBA Price-Target Bot Initialized - v2.0")

    setup_daily_schedule(client, portfolio, scheduler)

    try:
        while True:
            now = datetime.now(timezone.utc)
            # Sleep until 5 minutes past midnight UTC, then re-run setup for the new day
            next_rollover = (now + timedelta(days=1)).replace(
                hour=0, minute=5, second=0, microsecond=0
            )
            sleep_secs = (next_rollover - now).total_seconds()
            logging.info(f"Sleeping until midnight rollover ({sleep_secs:.0f}s).")
            time.sleep(sleep_secs)
            setup_daily_schedule(client, portfolio, scheduler)

    except KeyboardInterrupt:
        logging.warning("Bot stopped manually.")
    except Exception as e:
        logging.error(f"CRITICAL CRASH: {e}", exc_info=True)
    finally:
        scheduler.shutdown()


if __name__ == "__main__":
    run_bot()

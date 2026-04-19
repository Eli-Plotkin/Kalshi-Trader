import time
import config
import strategy
import logging
from logging.handlers import RotatingFileHandler
import sys
from client import KalshiClient
from portfolio import Portfolio
import nba_scheduler

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        RotatingFileHandler("trade_bot.log", maxBytes=5*1024*1024, backupCount=3),
        logging.StreamHandler(sys.stdout)
    ]
)


def run_bot():
    client = KalshiClient(
        base_url=config.BASE_URL, 
        key_id=config.API_KEY_ID, 
        key_file_path=config.PRIVATE_KEY_PATH
    )

    portfolio = Portfolio()
    
    logging.info("NBA Bot Initialized - v1.0 Production") 


    while True:
        todays_schedule = nba_scheduler.get_todays_schedule_map()


        if not strategy.is_market_open(config.WEEKLY_SCHEDULE):
            logging.info("Market Closed. Sleeping for one hour ...")
            time.sleep(3600)
            continue

        logging.info("Scanning markets...")
        markets = client.fetch_nba_markets()

        for market in markets:
            ticker = market['ticker']

            if portfolio.has_position(ticker):
                continue

            ticker_split = ticker.strip().split('-')
            team_tri = ticker_split[-1]

            if not strategy.is_pre_game(team_tri, todays_schedule):
                logging.info(f"Not within window to buy to buy {ticker}")
                continue

            book = client.get_orderbook(ticker)

            if not book:
                continue
 
            yes_bids = book.get('yes', [])
            no_bids = book.get('no', [])

            if not yes_bids or not no_bids:
                continue

            try:
                raw_bid = no_bids[-1][0]
                best_no_bid = int(raw_bid) 
            except (ValueError, TypeError):
                continue

            # 5. Calculate Implied Prices
            # Ask Price (Buy Price) = 100 - Best NO Bid
            ask_price = 100 - best_no_bid 
                            

            if strategy.should_enter_trade(ask_price, config.BUY_PRICE_MIN, config.BUY_PRICE_MAX):
                if ask_price <= 0: continue

                execution_price = min(ask_price + 2, config.BUY_PRICE_MAX)

                shares_to_buy = config.INVESTMENT_PER_BET // execution_price
                if shares_to_buy < 1: continue
                logging.info(f"SIGNAL FOUND: Buying {ticker} | Price: {ask_price}, Shares: {shares_to_buy}")
                
                num_bought = strategy.execute_trade_cycle(client=client,
                                                ticker=ticker,
                                                limit_price=execution_price, 
                                                shares_to_buy=shares_to_buy,
                                                profit_target=config.SELL_PROFIT_TARGET)
                
                if num_bought == "INSUFFICIENT_FUNDS":
                    logging.warning("INSUFFICIENT FUNDS. Sleeping 5 mins.")
                    time.sleep(300)
                    continue
                
                if num_bought and num_bought > 0:
                    portfolio.add_position(ticker, execution_price, num_bought)
                    logging.info(f"Position secured: {ticker}")

            else:
                logging.info(f"{ticker} not eligible for trade based on criteria")

            time.sleep(0.1)

        time.sleep(config.SCAN_INTERVAL)

if __name__ == "__main__":
    try:
        run_bot()
    except KeyboardInterrupt:
        logging.warning("Bot stopped manually by user.")
    except Exception as e:
        logging.error(f"CRITICAL CRASH: {e}", exc_info=True)
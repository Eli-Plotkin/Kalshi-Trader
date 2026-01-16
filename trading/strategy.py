from datetime import datetime, timezone
from client import KalshiClient
import time
import logging
from nba_scheduler import get_tip_off_time

def is_market_open(market_schedule):
    """Checks if now is between the configured start and end hours."""
    day_ind = datetime.now().weekday()

    open_hour_utc = market_schedule[day_ind][0]
    close_hour_utc = market_schedule[day_ind][1]

    now_hour = datetime.now(timezone.utc).hour
    if open_hour_utc <= close_hour_utc:
        return open_hour_utc <= now_hour <= close_hour_utc
    else:
        # Wraps around midnight (e.g. start 21:00, end 06:00)
        return open_hour_utc <= now_hour or now_hour <= close_hour_utc    


def is_pre_game(target_team_tri, today_schedule):
    """
    Returns True ONLY if we are within the 'Golden Window':
    - Less than 3 hours before Tip-Off.
    - More than 15 minutes before Tip-Off.
    """
    if not target_team_tri:
        logging.warning("Skipping pre-game check: No team tricode provided.")
        return False
    
    if not today_schedule:
        logging.warning("Skipping pre-game check: No schedule provided.")
        return False
    
    raw_time = None

    for game in today_schedule:
        # Check if our team is EITHER the home or away team
        h_team = game['homeTeam'].get('teamTricode')
        a_team = game['awayTeam'].get('teamTricode')
        
        if target_team_tri in [h_team, a_team]:
            # ALWAYS use gameTimeUTC. 
            # gameEt is local time but often has a confusing 'Z' suffix.

            raw_time = game.get('gameTimeUTC')


    if not raw_time:
         # If we can't find the game in the real schedule, safe to skip
        return False
                    

    # 2. Parse UTC
    clean_time = raw_time.replace('Z', '+00:00')
    tip_off_time = datetime.fromisoformat(clean_time)
    now = datetime.now(timezone.utc)
    
    # 3. Calculate Seconds Until TIP-OFF
    seconds_until_tip = (tip_off_time - now).total_seconds()
    
    # Window:
    # Max: 3 Hours (10,800 sec)
    # Min: 15 Mins (900 sec)
    
    is_too_early = seconds_until_tip > 10800  # More than 3 hours away
    is_too_late  = seconds_until_tip < 900    # Less than 15 mins away (or started)
    
    # If seconds_until_tip is negative, the game has already started -> Too Late.
    if seconds_until_tip < 0:
        return False

    return not is_too_early and not is_too_late
    

def should_enter_trade(ask_price, min_price, max_price):
    """Returns True if price is within the 'Underdog' range."""
    return min_price <= ask_price <= max_price

def should_exit_trade(current_bid, bought_price, profit_target):
    """Returns True if the current bid hits our profit target."""
    return current_bid >= (bought_price + profit_target)


def execute_trade_cycle(client: KalshiClient, ticker, shares_to_buy, limit_price, profit_target):
    logging.info(f"\nENTRY: {ticker} | Ask: {limit_price-1}¢ | Limit Order: {limit_price}¢")

    # 1. Place Limit Buy at the TIGHT price
    buy_resp = client.place_limit_order(
        ticker=ticker,
        price=limit_price, 
        count=shares_to_buy,
        action="buy"
    )

    if buy_resp == "INSUFFICIENT_FUNDS":
        return "INSUFFICIENT_FUNDS"


    if not buy_resp or 'order_id' not in buy_resp: return 0 

    order_id = buy_resp['order_id']
    time.sleep(4) 
    
    # We don't care if this returns True or False.
    # If it returns True: The order was resting and is now dead.
    # If it returns False: The order was likely already filled (or 404).
    # In BOTH cases, the next step is the same: Check what we own.
    client.cancel_order(order_id)


    # 2. Check Fill
    final_order = client.get_order_status(order_id)

    if not final_order:
        logging.info("Error fetching order status. Assuming 0 fill.")
        return 0
    
    shares_filled = final_order.get('fill_count', 0)
    
    if shares_filled == 0:
        # Since we used a tight limit, it's possible the market moved away (to 24c).
        # In this case, we just cancel and loop again. We missed the bus.
        logging.info("Order successfully cancelled. No execution.")
        return 0
    
    logging.info(f"ORDER FILLED! Qty: {shares_filled}. Cost: ~{limit_price}¢")

    # 3. Calculate Sell Target
    # Now this assumption is SAFE. 
    # If we bid 23 and got filled, we paid 23 (or 22). The error margin is tiny.
    
    sell_price = min(limit_price + profit_target, 99)
    if sell_price > 99: sell_price = 99


    # 4. Place Sell
    
    sell_resp = client.place_limit_order(ticker=ticker, 
                       count=shares_filled, 
                       price=sell_price, 
                       action="sell")
    
    if not sell_resp or 'order_id' not in sell_resp:
        logging.error(f"CRITICAL: Sell order failed for {ticker}. Trying once more, see issue!")
        # Optional: Retry once
        time.sleep(1)
        client.place_limit_order(ticker=ticker, count=shares_filled, price=sell_price, action="sell")
    
    return shares_filled
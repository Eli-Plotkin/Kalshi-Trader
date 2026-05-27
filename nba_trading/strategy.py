"""Strategy primitives for the NBA price-segment bot.

Pure functions only — no IO, no state. Trading decisions live here; the
plumbing that calls them lives in main.py.
"""
import logging
import math


def get_implied_ask(orderbook):
    """Convert orderbook NO bids into an implied YES ask price.

    Kalshi exposes YES asks and NO bids separately; the cheapest available
    YES buy price equals (100 - best NO bid). Returns price in cents or None.
    """
    if not orderbook:
        return None
    no_bids = orderbook.get('no_dollars', [])
    if not no_bids:
        return None
    try:
        return 100 - round(float(no_bids[-1][0]) * 100)
    except (ValueError, TypeError, IndexError):
        return None


def identify_favorite(home_tri, away_tri, home_book, away_book):
    """Return the tricode of the team with the higher implied ask price.

    Defaults to home team on a tie or when both orderbooks are unavailable.
    """
    home_ask = get_implied_ask(home_book)
    away_ask = get_implied_ask(away_book)

    if home_ask is None and away_ask is None:
        logging.warning(f"No orderbook data for {away_tri} @ {home_tri}. Cannot identify favorite.")
        return None

    if away_ask is not None and (home_ask is None or away_ask > home_ask):
        return away_tri

    return home_tri


def should_buy(ask_price, price_min, price_max):
    """Inclusive range check: is the favorite priced in our tradeable band?"""
    if ask_price is None:
        return False
    return price_min <= ask_price <= price_max


def compute_shares_to_buy(balance_cents, ask_cents, fraction):
    """Translate a bankroll fraction into an integer share count.

    bankroll_at_risk_cents = balance_cents * fraction
    shares = floor(bankroll_at_risk_cents / ask_cents)

    Returns 0 (skip the trade) when:
      - balance_cents <= 0 (no cash)
      - ask_cents <= 0 (degenerate price, can't divide)
      - fraction <= 0
      - The math rounds down to fewer than 1 share

    The Kalshi quote unit is one whole contract — we never fractional.
    """
    if balance_cents is None or balance_cents <= 0:
        return 0
    if ask_cents is None or ask_cents <= 0:
        return 0
    if fraction is None or fraction <= 0:
        return 0
    dollars_at_risk = balance_cents * fraction
    return max(int(math.floor(dollars_at_risk / ask_cents)), 0)


def compute_limit_price(ask_cents, buffer_cents, cap_cents):
    """Pick the limit price for the order.

    Default rule: pay up to `ask + buffer`, never more than `cap`. This gives
    a small slippage tolerance without letting a sudden ask spike fill the
    order at the top of the band.

    Returns None if `ask_cents` is None or <= 0.
    """
    if ask_cents is None or ask_cents <= 0:
        return None
    return min(int(ask_cents) + int(buffer_cents), int(cap_cents))

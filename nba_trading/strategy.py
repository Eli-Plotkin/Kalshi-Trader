import logging


def get_implied_ask(orderbook):
    """Converts orderbook NO bids into an implied YES ask price (100 - best NO bid)."""
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
    """
    Returns the tricode of the team with the higher implied ask price.
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
    return price_min <= ask_price <= price_max

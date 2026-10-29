"""Strategy parameters — load from environment, never hardcoded.

This module centralizes every knob that defines the trading strategy:
which price band to target, how aggressively to size, what circuit breakers
apply. Both `nba_trading/` and `misprice_discovery/` read from it so the
analysis tools and the live bot stay in sync.

The defaults are the 91-99c band, 10% of equity per bet, being tested live against
the win probability implied by pre-game prices. They are a hypothesis under test, not
a proven edge. Set the env vars below in your `.env` file to change them.

Env vars (all optional, all have defaults):
  STRATEGY_PRICE_MIN            cents — lower bound of the favorite price band
  STRATEGY_PRICE_MAX            cents — upper bound
  STRATEGY_BANKROLL_FRACTION    0..1  — fraction of cash bankroll to risk per bet
  STRATEGY_LIMIT_BUFFER_CENTS   cents — slippage tolerance above current ask
  STRATEGY_MAX_DRAWDOWN_PCT     0..100 — circuit-breaker threshold
  STRATEGY_MAX_EXPOSURE_PCT     0..100 — cap on total open cost as % of equity

Use `misprice_discovery/sweep_bands.py` and friends to figure out which
band makes sense for your dataset — the defaults here are placeholders.
"""

from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv()


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    return int(raw) if raw not in (None, "") else default


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    return float(raw) if raw not in (None, "") else default


# Shipped defaults: the 91-99c favorite band being tested live against the win
# probability implied by pre-game prices (see misprice_discovery/edge_validation.py for
# the historical backtest; it is not a proven edge). Override any of these in .env.
PRICE_MIN = _env_int("STRATEGY_PRICE_MIN", 91)
PRICE_MAX = _env_int("STRATEGY_PRICE_MAX", 99)

# Fraction of equity risked per bet. 10% means one loss costs ~9-10% of equity.
BANKROLL_FRACTION = _env_float("STRATEGY_BANKROLL_FRACTION", 0.10)

# How many cents above current ask we're willing to pay (slippage tolerance).
LIMIT_BUFFER_CENTS = _env_int("STRATEGY_LIMIT_BUFFER_CENTS", 0)

# Halt new bets if cash bankroll falls this far below its all-time peak.
MAX_DRAWDOWN_PCT = _env_float("STRATEGY_MAX_DRAWDOWN_PCT", 25.0)

# Cap on total capital at risk across all open positions, as a % of equity
# (cash + open cost). Stops several same-night games from stacking.
MAX_EXPOSURE_PCT = _env_float("STRATEGY_MAX_EXPOSURE_PCT", 30.0)

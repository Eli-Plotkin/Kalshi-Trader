"""Strategy parameters — load from environment, never hardcoded.

This module centralizes every knob that defines the trading strategy:
which price band to target, how aggressively to size, what circuit breakers
apply. Both `nba_trading/` and `misprice_discovery/` read from it so the
analysis tools and the live bot stay in sync.

The defaults here are intentionally **neutral / placeholder values**, not the values
from any specific researched edge. Set the env vars below in your `.env` file to
configure for your own research.

Env vars (all optional, all have defaults):
  STRATEGY_PRICE_MIN            cents — lower bound of the favorite price band
  STRATEGY_PRICE_MAX            cents — upper bound
  STRATEGY_BANKROLL_FRACTION    0..1  — fraction of EQUITY (cash + open cost) to risk per bet
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


# Neutral, broad defaults — the code shows the architecture, not a specific edge.
PRICE_MIN = _env_int("STRATEGY_PRICE_MIN", 50)
PRICE_MAX = _env_int("STRATEGY_PRICE_MAX", 99)

# Conservative default sizing — well below any plausible Kelly fraction.
BANKROLL_FRACTION = _env_float("STRATEGY_BANKROLL_FRACTION", 0.05)

# How many cents above current ask we're willing to pay (slippage tolerance).
LIMIT_BUFFER_CENTS = _env_int("STRATEGY_LIMIT_BUFFER_CENTS", 1)

# Halt new bets if EQUITY (cash + capital at risk) falls this far below its all-time peak.
MAX_DRAWDOWN_PCT = _env_float("STRATEGY_MAX_DRAWDOWN_PCT", 25.0)

# Cap on total capital at risk across all open positions, as a % of equity
# (cash + open cost). Stops several same-night games from stacking.
MAX_EXPOSURE_PCT = _env_float("STRATEGY_MAX_EXPOSURE_PCT", 30.0)

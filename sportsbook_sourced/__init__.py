"""Sportsbook-sourced Kalshi edge scanner.

This package is intentionally separate from `agent_trader`: the fair price comes
from sportsbook odds, not an LLM research/decision cascade.
"""

__all__ = [
    "config",
    "evaluation",
    "kalshi_feed",
    "mapper",
    "odds",
    "paper",
    "pricing",
    "scanner",
    "schemas",
    "storage",
]


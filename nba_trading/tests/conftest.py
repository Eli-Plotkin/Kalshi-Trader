"""Shared test setup for nba_trading."""

import pytest

from nba_trading import config


@pytest.fixture(autouse=True)
def _no_real_sleeps(monkeypatch):
    """The bot pauses a few seconds before looking up an order whose POST timed out. That is real
    behaviour in production but would add seconds to every timeout test; tests that care about the
    pause set it explicitly (see test_review_fixes.py::test_F4_the_first_lookup_waits...)."""
    monkeypatch.setattr(config, "ORDER_LOOKUP_DELAY_SECONDS", 0, raising=False)

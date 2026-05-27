"""Tests for Portfolio + HighWaterMark — the persisted risk-tracking state."""

from __future__ import annotations

import json
import os

import pytest

from nba_trading.portfolio import HighWaterMark, Portfolio


# ----------------------------------------------------------------------------
# Portfolio
# ----------------------------------------------------------------------------


class TestPortfolio:
    def test_starts_empty(self, tmp_path):
        p = Portfolio(filename=str(tmp_path / "p.json"))
        assert not p.has_position("KX-FAKE")
        assert not p.has_any_positions()

    def test_add_position_persists(self, tmp_path):
        path = tmp_path / "p.json"
        p = Portfolio(filename=str(path))
        p.add_position("KX-NBA-LAL", buy_price=92, shares=10)
        assert p.has_position("KX-NBA-LAL")
        assert p.has_any_positions()
        # File should now exist with the lot saved.
        with open(path) as f:
            data = json.load(f)
        assert "KX-NBA-LAL" in data
        assert data["KX-NBA-LAL"][0]["price"] == 92
        assert data["KX-NBA-LAL"][0]["qty"] == 10

    def test_remove_position(self, tmp_path):
        p = Portfolio(filename=str(tmp_path / "p.json"))
        p.add_position("KX-NBA-LAL", 92, 10)
        p.remove_position("KX-NBA-LAL")
        assert not p.has_position("KX-NBA-LAL")
        assert not p.has_any_positions()

    def test_remove_nonexistent_is_noop(self, tmp_path):
        p = Portfolio(filename=str(tmp_path / "p.json"))
        # Should not raise.
        p.remove_position("KX-NOT-THERE")

    def test_update_position_qty(self, tmp_path):
        p = Portfolio(filename=str(tmp_path / "p.json"))
        p.add_position("KX-NBA-LAL", 92, 10)
        p.update_position_qty("KX-NBA-LAL", 7)  # partial fill
        # Reload to verify persistence.
        p2 = Portfolio(filename=p.filename)
        assert p2._positions["KX-NBA-LAL"][-1]["qty"] == 7

    def test_multiple_lots_for_same_ticker(self, tmp_path):
        p = Portfolio(filename=str(tmp_path / "p.json"))
        p.add_position("KX-NBA-LAL", 92, 10)
        p.add_position("KX-NBA-LAL", 94, 5)
        assert len(p._positions["KX-NBA-LAL"]) == 2

    def test_state_survives_restart(self, tmp_path):
        path = tmp_path / "p.json"
        p1 = Portfolio(filename=str(path))
        p1.add_position("KX-NBA-LAL", 92, 10)
        # Simulate restart by creating a new instance against the same file.
        p2 = Portfolio(filename=str(path))
        assert p2.has_position("KX-NBA-LAL")


# ----------------------------------------------------------------------------
# HighWaterMark
# ----------------------------------------------------------------------------


class TestHighWaterMark:
    def test_starts_with_no_peak(self, tmp_path):
        hw = HighWaterMark(filename=str(tmp_path / "hw.json"))
        assert hw.peak() is None
        assert hw.drawdown_pct(current_cash_cents=5000) == 0.0

    def test_first_update_sets_peak_when_no_positions(self, tmp_path):
        hw = HighWaterMark(filename=str(tmp_path / "hw.json"))
        hw.update(current_cash_cents=10_000, has_open_positions=False)
        assert hw.peak() == 10_000

    def test_open_positions_block_peak_updates(self, tmp_path):
        hw = HighWaterMark(filename=str(tmp_path / "hw.json"))
        hw.update(10_000, has_open_positions=False)  # peak = 10_000
        hw.update(15_000, has_open_positions=True)   # blocked
        assert hw.peak() == 10_000

    def test_peak_only_moves_up_not_down(self, tmp_path):
        hw = HighWaterMark(filename=str(tmp_path / "hw.json"))
        hw.update(10_000, has_open_positions=False)
        hw.update(8_000, has_open_positions=False)
        assert hw.peak() == 10_000

    def test_drawdown_calculation(self, tmp_path):
        hw = HighWaterMark(filename=str(tmp_path / "hw.json"))
        hw.update(10_000, has_open_positions=False)
        # Drop to 8,000 → 20% drawdown.
        assert hw.drawdown_pct(8_000) == pytest.approx(20.0)
        # Drop to 5,000 → 50% drawdown.
        assert hw.drawdown_pct(5_000) == pytest.approx(50.0)
        # Above peak → 0% drawdown.
        assert hw.drawdown_pct(12_000) == 0.0

    def test_circuit_breaker_trips_at_threshold(self, tmp_path):
        hw = HighWaterMark(filename=str(tmp_path / "hw.json"))
        hw.update(10_000, has_open_positions=False)
        # 20% drawdown vs 25% threshold → not broken.
        assert not hw.is_circuit_broken(8_000, max_drawdown_pct=25)
        # 30% drawdown vs 25% threshold → broken.
        assert hw.is_circuit_broken(7_000, max_drawdown_pct=25)

    def test_circuit_breaker_exact_threshold(self, tmp_path):
        hw = HighWaterMark(filename=str(tmp_path / "hw.json"))
        hw.update(10_000, has_open_positions=False)
        # Exactly 25% drawdown should trip (>=).
        assert hw.is_circuit_broken(7_500, max_drawdown_pct=25)

    def test_no_peak_means_no_circuit_break(self, tmp_path):
        # Before any peak recorded, the bot must not refuse to trade.
        hw = HighWaterMark(filename=str(tmp_path / "hw.json"))
        assert not hw.is_circuit_broken(5_000, max_drawdown_pct=10)

    def test_state_persists_across_restart(self, tmp_path):
        path = tmp_path / "hw.json"
        hw1 = HighWaterMark(filename=str(path))
        hw1.update(10_000, has_open_positions=False)
        hw2 = HighWaterMark(filename=str(path))
        assert hw2.peak() == 10_000

    def test_negative_cash_does_not_set_peak(self, tmp_path):
        hw = HighWaterMark(filename=str(tmp_path / "hw.json"))
        hw.update(-100, has_open_positions=False)
        assert hw.peak() is None

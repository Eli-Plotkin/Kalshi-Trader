"""Tests for friction_simulator."""

from __future__ import annotations

import csv

import matplotlib
matplotlib.use("Agg")

import numpy as np
import pytest

from build_multisport_research_dataset import FIELDNAMES
from friction_simulator import (
    apply_friction,
    find_breakeven,
    main,
    simulate_with_friction,
    sweep_friction,
    FrictionPoint,
)


def _row(event, date, sport, price, won):
    pnl = (100 - price) if won else (-price)
    return {
        "Sport": sport, "Event_Ticker": event, "Date": date,
        "Favorite_Avg_Ask_Cents": price,
        "Favorite_Won": "True" if won else "False",
        "Favorite_Hold_To_Settle_PnL_Cents": pnl,
    }


def _write_csv(path, rows):
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerow({c: f"Description: {c}" for c in FIELDNAMES})
        for r in rows:
            full = {c: "" for c in FIELDNAMES}
            full.update(r)
            writer.writerow(full)


# ----------------------------------------------------------------------------
# apply_friction
# ----------------------------------------------------------------------------


class TestApplyFriction:
    def test_zero_friction_matches_clean_pnl(self):
        entries = np.array([92, 88, 95])
        outcomes = np.array([True, False, True])
        eff, pnl = apply_friction(entries, outcomes, slippage_cents=0, fee_cents=0)
        assert np.array_equal(eff, entries)
        assert np.array_equal(pnl, np.array([8, -88, 5]))

    def test_slippage_raises_effective_entry(self):
        entries = np.array([92])
        outcomes = np.array([True])
        eff, pnl = apply_friction(entries, outcomes, slippage_cents=1.5, fee_cents=0)
        assert eff[0] == pytest.approx(93.5)
        # Profit on win drops by slippage.
        assert pnl[0] == pytest.approx(100 - 93.5)

    def test_fee_reduces_both_wins_and_losses(self):
        entries = np.array([92, 92])
        outcomes = np.array([True, False])
        _, pnl = apply_friction(entries, outcomes, slippage_cents=0, fee_cents=0.5)
        assert pnl[0] == pytest.approx(8 - 0.5)
        assert pnl[1] == pytest.approx(-92 - 0.5)

    def test_combined_friction(self):
        entries = np.array([92])
        outcomes = np.array([True])
        _, pnl = apply_friction(entries, outcomes, slippage_cents=1, fee_cents=0.5)
        # effective entry = 93, profit on win = 7, minus 0.5 fee = 6.5.
        assert pnl[0] == pytest.approx(6.5)


# ----------------------------------------------------------------------------
# simulate_with_friction
# ----------------------------------------------------------------------------


class TestSimulateWithFriction:
    def test_zero_friction_matches_clean_simulation(self):
        # With 0 friction, simulate_with_friction should reduce to a fixed-size sim.
        entries = [92, 92, 92]
        outcomes = [True, False, True]
        eq = simulate_with_friction(entries, outcomes, fraction=0.1,
                                    slippage=0, fee=0,
                                    starting_bankroll=1000, warmup=0)
        # Manually: start 1000.
        # bet 1 win at 92: +100*0.1*(8/92) = 0.8696 → 1000 * (1 + 0.1*8/92).
        expected = 1000 * (1 + 0.1 * 8 / 92)
        expected = expected * (1 - 0.1)   # loss at 92
        expected = expected * (1 + 0.1 * 8 / 92)   # win at 92
        assert eq[-1] == pytest.approx(expected, rel=1e-9)

    def test_higher_friction_lower_final(self):
        rng = np.random.default_rng(0)
        entries = rng.integers(90, 100, size=100).tolist()
        # 94% wins → positive EV.
        outcomes = (rng.random(100) < 0.94).astype(bool).tolist()
        clean = simulate_with_friction(entries, outcomes, 0.05, 0, 0, 1000, 0)
        with_friction = simulate_with_friction(entries, outcomes, 0.05, 2, 0, 1000, 0)
        assert with_friction[-1] < clean[-1]

    def test_extreme_friction_kills_edge(self):
        rng = np.random.default_rng(0)
        entries = rng.integers(90, 100, size=100).tolist()
        outcomes = (rng.random(100) < 0.94).astype(bool).tolist()
        # 10¢ friction on 92¢ entry destroys the edge.
        crushed = simulate_with_friction(entries, outcomes, 0.05, 10, 0, 1000, 0)
        assert crushed[-1] < 1000

    def test_warmup_skipped(self):
        eq = simulate_with_friction([92]*10, [True]*10, 0.1, 0, 0, 1000, warmup=5)
        # First 5 bets are warmup → bankroll == 1000 at index 0..5.
        assert (eq[:6] == 1000).all()

    def test_bankroll_never_negative(self):
        eq = simulate_with_friction([92]*5, [False]*5, 1.0, 0, 0, 1000, warmup=0)
        assert (eq >= 0).all()


# ----------------------------------------------------------------------------
# sweep_friction
# ----------------------------------------------------------------------------


class TestSweepFriction:
    def test_returns_n_points_for_n_steps(self):
        rng = np.random.default_rng(0)
        entries = rng.integers(90, 100, size=100).tolist()
        outcomes = (rng.random(100) < 0.94).astype(bool).tolist()
        pts = sweep_friction(entries, outcomes, 0.05, max_friction=3, steps=7,
                             starting_bankroll=1000, warmup=20)
        assert len(pts) == 7
        assert pts[0].total_friction == 0.0
        assert pts[-1].total_friction == pytest.approx(3.0)

    def test_returns_monotonically_decline(self):
        # As friction increases, final bankroll should decrease (or stay same).
        rng = np.random.default_rng(0)
        entries = rng.integers(90, 100, size=100).tolist()
        outcomes = (rng.random(100) < 0.94).astype(bool).tolist()
        pts = sweep_friction(entries, outcomes, 0.05, max_friction=4, steps=9,
                             starting_bankroll=1000, warmup=20)
        finals = [p.final for p in pts]
        for prev, nxt in zip(finals, finals[1:]):
            assert nxt <= prev + 1e-6  # allow tiny float drift


# ----------------------------------------------------------------------------
# find_breakeven
# ----------------------------------------------------------------------------


class TestFindBreakeven:
    def _pt(self, friction, ret_pct):
        return FrictionPoint(
            total_friction=friction, slippage=friction, fee=0,
            final=1000 * (1 + ret_pct / 100),
            return_pct=ret_pct, max_dd_pct=0, sharpe=0,
            profitable=ret_pct > 0,
        )

    def test_interpolates_between_profitable_and_loss(self):
        # At 1¢ friction: +5% return. At 2¢: -5% return. Break-even halfway.
        pts = [self._pt(1, 5), self._pt(2, -5)]
        assert find_breakeven(pts) == pytest.approx(1.5)

    def test_returns_none_when_all_profitable(self):
        pts = [self._pt(0, 50), self._pt(1, 30), self._pt(2, 10)]
        assert find_breakeven(pts) is None

    def test_returns_none_when_all_losses(self):
        pts = [self._pt(0, -5), self._pt(1, -10), self._pt(2, -20)]
        assert find_breakeven(pts) is None

    def test_handles_multiple_levels(self):
        # 0¢: +20%, 1¢: +10%, 2¢: -10%, 3¢: -30%. Break-even between 1 and 2.
        pts = [self._pt(0, 20), self._pt(1, 10), self._pt(2, -10), self._pt(3, -30)]
        be = find_breakeven(pts)
        assert 1.0 < be < 2.0


# ----------------------------------------------------------------------------
# End-to-end
# ----------------------------------------------------------------------------


def test_main_runs_end_to_end(tmp_path):
    csv_path = tmp_path / "ds.csv"
    out_png = tmp_path / "fric.png"
    rng = np.random.default_rng(0)
    rows = []
    for i in range(150):
        price = int(rng.integers(90, 100))
        won = rng.random() < 0.94
        rows.append(_row(f"KX-{i}", f"2026-01-{(i % 28) + 1:02d}", "NBA", price, won))
    _write_csv(csv_path, rows)

    main(argv=[
        "--dataset", str(csv_path),
        "--out", str(out_png),
        "--sport", "NBA",
        "--low", "90", "--high", "99",
        "--max-friction", "3",
        "--steps", "7",
    ])
    assert out_png.exists()
    assert out_png.stat().st_size > 1000

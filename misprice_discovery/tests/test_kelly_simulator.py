"""Tests for kelly_simulator.

Pure-function tests for the math (compounding, drawdown, Sharpe, time
underwater) plus an end-to-end smoke test that runs the full CLI against a
synthetic CSV and verifies a figure is produced.
"""

from __future__ import annotations

import csv
import os

import matplotlib
matplotlib.use("Agg")

import numpy as np
import pandas as pd
import pytest

from build_multisport_research_dataset import FIELDNAMES
from kelly_simulator import (
    derive_kelly_fractions,
    evaluate,
    log_sharpe,
    main,
    make_strategies,
    max_drawdown_pct,
    pct_time_underwater,
    simulate_bankroll,
)


# ----------------------------------------------------------------------------
# Synthetic-CSV helper (mirrors the one in test_analyze_multisport)
# ----------------------------------------------------------------------------


def _write_csv(path, rows):
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerow({c: f"Description: {c}" for c in FIELDNAMES})
        for row in rows:
            full = {c: "" for c in FIELDNAMES}
            full.update(row)
            writer.writerow(full)


def _row(event, date, sport, price, won):
    pnl = (100 - price) if won else (-price)
    return {
        "Sport": sport,
        "Event_Ticker": event,
        "Date": date,
        "Favorite_Avg_Ask_Cents": price,
        "Favorite_Won": "True" if won else "False",
        "Favorite_Hold_To_Settle_PnL_Cents": pnl,
    }


# ----------------------------------------------------------------------------
# simulate_bankroll: the core compounding math
# ----------------------------------------------------------------------------


class TestSimulateBankroll:
    def test_zero_fraction_leaves_bankroll_unchanged(self):
        eq = simulate_bankroll([92, 92, 92], [True, False, True], 0.0, 1000)
        assert np.allclose(eq, [1000, 1000, 1000, 1000])

    def test_single_win_at_92_with_full_kelly(self):
        # f=1 at c=92 → win adds (100-92)/92 = 8/92 of bankroll.
        eq = simulate_bankroll([92], [True], 1.0, 1000)
        assert eq[-1] == pytest.approx(1000 * (1 + 8 / 92))

    def test_single_loss_at_full_fraction_busts(self):
        eq = simulate_bankroll([92], [False], 1.0, 1000)
        assert eq[-1] == 0.0

    def test_loss_at_half_fraction_halves_bankroll(self):
        eq = simulate_bankroll([92], [False], 0.5, 1000)
        assert eq[-1] == pytest.approx(500)

    def test_compounding_two_wins(self):
        # Two wins at f=0.5, c=50 → each bet multiplies by 1 + 0.5*(50/50) = 1.5.
        eq = simulate_bankroll([50, 50], [True, True], 0.5, 100)
        assert eq[-1] == pytest.approx(100 * 1.5 * 1.5)

    def test_equity_length_is_n_plus_1(self):
        eq = simulate_bankroll([92] * 10, [True] * 10, 0.1, 1000)
        assert len(eq) == 11

    def test_bankroll_never_negative(self):
        # Even with f=1 + chained losses, equity should clamp to 0.
        eq = simulate_bankroll([90, 90, 90], [False, False, False], 1.0, 1000)
        assert (eq >= 0).all()

    def test_starts_at_starting_bankroll(self):
        eq = simulate_bankroll([92], [True], 0.5, 12345)
        assert eq[0] == 12345

    def test_deterministic_given_inputs(self):
        # Pure function: same inputs → same outputs.
        a = simulate_bankroll([92, 80, 70], [True, False, True], 0.2, 1000)
        b = simulate_bankroll([92, 80, 70], [True, False, True], 0.2, 1000)
        assert np.array_equal(a, b)


# ----------------------------------------------------------------------------
# max_drawdown_pct
# ----------------------------------------------------------------------------


class TestMaxDrawdown:
    def test_monotonic_up_has_zero_drawdown(self):
        eq = np.array([100, 110, 120, 130])
        assert max_drawdown_pct(eq) == 0.0

    def test_simple_50pct_drawdown(self):
        eq = np.array([100, 200, 100])
        # Peak 200 → trough 100 = 50% drawdown.
        assert max_drawdown_pct(eq) == pytest.approx(50.0)

    def test_largest_of_multiple_drawdowns(self):
        # Two drawdowns: 20% then 60%. Should report 60%.
        eq = np.array([100, 200, 160, 250, 100])
        assert max_drawdown_pct(eq) == pytest.approx(60.0)

    def test_zero_bankroll_treated_as_100pct(self):
        eq = np.array([100, 0])
        assert max_drawdown_pct(eq) == pytest.approx(100.0)

    def test_empty_or_single_returns_zero(self):
        assert max_drawdown_pct(np.array([100])) == 0.0


# ----------------------------------------------------------------------------
# log_sharpe — relative ranking is the contract, not absolute value
# ----------------------------------------------------------------------------


class TestLogSharpe:
    def test_constant_equity_returns_zero_sharpe(self):
        assert log_sharpe(np.array([100, 100, 100, 100])) == 0.0

    def test_pure_growth_has_positive_sharpe(self):
        eq = np.array([100, 110, 121, 133, 146])  # +10% per step
        assert log_sharpe(eq) > 0

    def test_pure_decline_has_negative_sharpe(self):
        eq = np.array([100, 90, 81, 73, 66])  # -10% per step
        assert log_sharpe(eq) < 0

    def test_more_volatile_path_has_lower_sharpe_than_smooth_one(self):
        smooth = np.array([100, 105, 110, 115, 120, 125])
        volatile = np.array([100, 150, 80, 160, 90, 125])  # same end, jagged
        assert log_sharpe(smooth) > log_sharpe(volatile)


# ----------------------------------------------------------------------------
# pct_time_underwater
# ----------------------------------------------------------------------------


class TestPctTimeUnderwater:
    def test_monotonic_up_is_zero_underwater(self):
        eq = np.array([100, 110, 120, 130])
        assert pct_time_underwater(eq) == 0.0

    def test_after_drawdown_underwater(self):
        eq = np.array([100, 200, 150, 180, 200])
        # Indices 0..4. Running peaks: 100,200,200,200,200.
        # Underwater (eq < peak): F, F, T, T, F → 2/5 = 40%
        assert pct_time_underwater(eq) == pytest.approx(40.0)


# ----------------------------------------------------------------------------
# derive_kelly_fractions
# ----------------------------------------------------------------------------


class TestDeriveKellyFractions:
    def test_known_distribution_gives_expected_full_kelly(self):
        # Construct a sample where mean PnL = 5, avg entry = 92 → Kelly = 5/8 = 0.625.
        pnl = pd.Series([8] * 95 + [-92] * 5)  # 95 wins, 5 losses
        entries = pd.Series([92] * 100)
        rng = np.random.default_rng(0)
        info = derive_kelly_fractions(pnl, entries, rng)
        assert info["mean_pnl"] == pytest.approx(3.0)  # 95*8 - 5*92 / 100 = 3
        assert info["max_profit"] == pytest.approx(8.0)
        assert info["full_kelly"] == pytest.approx(3.0 / 8.0, abs=1e-6)

    def test_negative_edge_returns_zero_kelly(self):
        # Losing strategy — Kelly should clamp to 0, not go negative.
        pnl = pd.Series([8] * 50 + [-92] * 50)  # mostly losses by PnL magnitude
        entries = pd.Series([92] * 100)
        rng = np.random.default_rng(0)
        info = derive_kelly_fractions(pnl, entries, rng)
        assert info["mean_pnl"] < 0
        assert info["full_kelly"] == 0.0

    def test_ci_lower_below_or_equal_full(self):
        pnl = pd.Series([8] * 90 + [-92] * 10)
        entries = pd.Series([92] * 100)
        rng = np.random.default_rng(0)
        info = derive_kelly_fractions(pnl, entries, rng)
        assert info["ci_lower_kelly"] <= info["full_kelly"]

    def test_kelly_capped_at_one(self):
        # Pathological sample with huge mean PnL — Kelly mathematically > 1
        # would mean "bet more than 100%" which our impl must clamp.
        pnl = pd.Series([100] * 100)  # always win full pot
        entries = pd.Series([1] * 100)  # 1-cent entries — would give Kelly ≈ 100
        rng = np.random.default_rng(0)
        info = derive_kelly_fractions(pnl, entries, rng)
        assert info["full_kelly"] <= 1.0


# ----------------------------------------------------------------------------
# evaluate (the integration wrapper)
# ----------------------------------------------------------------------------


class TestEvaluate:
    def test_returns_simresult_with_all_fields(self):
        result = evaluate(
            "Test", 0.1,
            entries=[92, 88, 95],
            outcomes=[True, False, True],
            starting_bankroll=1000,
        )
        assert result.name == "Test"
        assert result.fraction == 0.1
        assert len(result.equity) == 4
        assert result.final == result.equity[-1]


# ----------------------------------------------------------------------------
# Sanity: aggressive strategies SHOULD have bigger drawdowns
# ----------------------------------------------------------------------------


class TestSizingComparison:
    def _setup_positive_ev_band(self, rng_seed=0, n=500):
        """Fixed-50¢ entries with 60% win rate → +10¢ true edge per bet,
        full-Kelly fraction = 10/50 = 20%."""
        rng = np.random.default_rng(rng_seed)
        entries = [50] * n
        outcomes = (rng.random(n) < 0.60).astype(bool).tolist()
        return entries, outcomes

    def test_higher_kelly_has_higher_drawdown(self):
        entries, outcomes = self._setup_positive_ev_band(rng_seed=0)
        bigger = evaluate("Bigger", 0.20, entries, outcomes, 1000)
        smaller = evaluate("Smaller", 0.05, entries, outcomes, 1000)
        assert bigger.max_drawdown_pct >= smaller.max_drawdown_pct

    def test_kelly_beats_undersizing_when_edge_is_real(self):
        # At-or-below full Kelly with a real edge: bigger fraction → bigger
        # final bankroll over many trials. Single-seed luck can flip this, so
        # average across seeds for robustness.
        n_seeds = 10
        kelly_wins = 0
        for seed in range(n_seeds):
            entries, outcomes = self._setup_positive_ev_band(rng_seed=seed)
            kelly = evaluate("Kelly", 0.20, entries, outcomes, 1000)
            tiny = evaluate("1%", 0.01, entries, outcomes, 1000)
            if kelly.final > tiny.final:
                kelly_wins += 1
        assert kelly_wins >= 8, (
            f"Kelly should outperform 1% cap in most trials when edge is real; "
            f"got {kelly_wins}/{n_seeds}"
        )

    def test_overbetting_kelly_underperforms_full_kelly(self):
        # The classic Kelly result: betting above the optimal fraction has lower
        # expected log growth than betting at it. Demonstrate by going 3x over.
        n_seeds = 10
        underperforms = 0
        for seed in range(n_seeds):
            entries, outcomes = self._setup_positive_ev_band(rng_seed=seed)
            optimal = evaluate("Kelly", 0.20, entries, outcomes, 1000)
            over = evaluate("3xKelly", 0.60, entries, outcomes, 1000)
            if over.final < optimal.final:
                underperforms += 1
        assert underperforms >= 7, (
            f"3xKelly should underperform Kelly in most trials; "
            f"got {underperforms}/{n_seeds}"
        )


# ----------------------------------------------------------------------------
# End-to-end smoke test
# ----------------------------------------------------------------------------


def test_main_writes_figure_against_synthetic_dataset(tmp_path):
    csv_path = tmp_path / "ds.csv"
    out_png = tmp_path / "fig.png"

    rng = np.random.default_rng(0)
    rows = []
    for i in range(150):
        price = int(rng.integers(90, 100))
        won = rng.random() < 0.92
        rows.append(_row(f"KX-{i}", f"2026-01-{(i % 28) + 1:02d}", "NBA", price, won))
    _write_csv(csv_path, rows)

    main(argv=[
        "--dataset", str(csv_path),
        "--out", str(out_png),
        "--sport", "NBA",
        "--low", "90", "--high", "99",
        "--start", "1000",
    ])
    assert out_png.exists()
    assert out_png.stat().st_size > 1000


def test_main_exits_when_no_games_in_band(tmp_path):
    csv_path = tmp_path / "ds.csv"
    rows = [_row(f"KX-{i}", f"2026-01-{i+1:02d}", "NBA", 70, True) for i in range(10)]
    _write_csv(csv_path, rows)
    with pytest.raises(SystemExit, match="No NBA games"):
        main(argv=[
            "--dataset", str(csv_path),
            "--out", str(tmp_path / "x.png"),
            "--sport", "NBA",
            "--low", "90", "--high", "99",
        ])


def test_make_strategies_preserves_order():
    kelly_info = {"full_kelly": 0.6, "ci_lower_kelly": 0.12}
    strategies = make_strategies(kelly_info)
    names = [s[0] for s in strategies]
    assert names == [
        "Full Kelly", "Half Kelly", "Quarter Kelly",
        "CI-lower Kelly", "5% cap", "1% cap",
    ]
    # Sanity: full is largest, fractional Kelly is full*k.
    fracs = dict(strategies)
    assert fracs["Full Kelly"] == 0.6
    assert fracs["Half Kelly"] == pytest.approx(0.3)
    assert fracs["Quarter Kelly"] == pytest.approx(0.15)

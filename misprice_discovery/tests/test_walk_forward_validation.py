"""Tests for walk_forward_validation.

The critical invariant the tests must lock down: **no look-ahead bias**. At
each bet, the Kelly fraction is computed using only games that happened
strictly before it.
"""

from __future__ import annotations

import csv

import matplotlib
matplotlib.use("Agg")

import numpy as np
import pandas as pd
import pytest

from build_multisport_research_dataset import FIELDNAMES
from walk_forward_validation import (
    kelly_from_history,
    main,
    simulate_fixed_fraction,
    simulate_walk_forward,
    summarize,
)


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------


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
# kelly_from_history
# ----------------------------------------------------------------------------


class TestKellyFromHistory:
    def test_short_history_returns_zero(self):
        # < 5 games of history → no bet.
        assert kelly_from_history([], [], multiplier=0.25, use_ci_lower=False) == 0.0
        assert kelly_from_history([8], [92], multiplier=0.25, use_ci_lower=False) == 0.0

    def test_negative_mean_returns_zero(self):
        # All losses → mean PnL is negative → Kelly clamped to 0.
        pnl = [-92] * 10
        entries = [92] * 10
        f = kelly_from_history(pnl, entries, multiplier=1.0, use_ci_lower=False)
        assert f == 0.0

    def test_positive_mean_returns_positive_fraction(self):
        # 9 wins at 92 (+8), 1 loss (-92). Mean = (72 - 92) / 10 = -2¢. Negative.
        # Use 19 wins, 1 loss: (152 - 92) / 20 = +3 → Kelly = 3/8 = 0.375.
        pnl = [8] * 19 + [-92]
        entries = [92] * 20
        f = kelly_from_history(pnl, entries, multiplier=1.0, use_ci_lower=False)
        assert f == pytest.approx(3 / 8, abs=1e-6)

    def test_quarter_multiplier_scales_fraction(self):
        pnl = [8] * 19 + [-92]
        entries = [92] * 20
        full = kelly_from_history(pnl, entries, multiplier=1.0, use_ci_lower=False)
        quarter = kelly_from_history(pnl, entries, multiplier=0.25, use_ci_lower=False)
        assert quarter == pytest.approx(full * 0.25)

    def test_ci_lower_bound_is_smaller_than_point_estimate(self):
        # Realistic sample: 92% win rate at 92¢.
        rng = np.random.default_rng(0)
        outcomes = rng.random(100) < 0.92
        pnl = [8 if w else -92 for w in outcomes]
        entries = [92] * 100
        point = kelly_from_history(pnl, entries, multiplier=1.0, use_ci_lower=False)
        ci = kelly_from_history(pnl, entries, multiplier=1.0, use_ci_lower=True)
        assert ci <= point

    def test_fraction_capped_at_one(self):
        # Pathological huge edge: should clamp to 1.0 (don't bet >100%).
        pnl = [99] * 50
        entries = [1] * 50
        f = kelly_from_history(pnl, entries, multiplier=1.0, use_ci_lower=False)
        assert f <= 1.0


# ----------------------------------------------------------------------------
# simulate_walk_forward — no look-ahead, correct compounding
# ----------------------------------------------------------------------------


class TestSimulateWalkForward:
    def test_warmup_bets_have_zero_fraction(self):
        entries = [92] * 20
        outcomes = [True] * 20
        eq, fracs, edges = simulate_walk_forward(
            entries, outcomes, multiplier=0.25, use_ci_lower=False,
            starting_bankroll=1000, warmup=5,
        )
        assert (fracs[:5] == 0).all()

    def test_warmup_period_leaves_bankroll_unchanged(self):
        entries = [92] * 10
        outcomes = [True, False, True, False, True, True, True, True, True, True]
        eq, _, _ = simulate_walk_forward(
            entries, outcomes, multiplier=0.25, use_ci_lower=False,
            starting_bankroll=1000, warmup=10,  # warmup covers all bets
        )
        # No bets placed → bankroll never changes.
        assert (eq == 1000).all()

    def test_no_lookahead_bias_first_active_bet_uses_only_prior_data(self):
        """Critical invariant: at the first non-warmup bet (index = warmup),
        the Kelly fraction must equal kelly_from_history(history[:warmup]).

        If the simulator accidentally peeks at the current or future game,
        this test will fail because the recommended fraction would be different.
        """
        warmup = 10
        entries = [92] * 20
        outcomes = [True] * warmup + [False, True, True, True, True,
                                       True, True, True, True, True]

        # Expected fraction at index 'warmup': computed from first warmup PnLs.
        prior_pnl = [(100 - 92) if w else -92 for w in outcomes[:warmup]]
        prior_entries = entries[:warmup]
        expected = kelly_from_history(
            prior_pnl, prior_entries, multiplier=0.5, use_ci_lower=False,
        )

        _, fracs, _ = simulate_walk_forward(
            entries, outcomes, multiplier=0.5, use_ci_lower=False,
            starting_bankroll=1000, warmup=warmup,
        )
        assert fracs[warmup] == pytest.approx(expected, abs=1e-10)

    def test_equity_length_is_n_plus_1(self):
        entries = [92] * 15
        outcomes = [True] * 15
        eq, fracs, _ = simulate_walk_forward(
            entries, outcomes, multiplier=0.25, use_ci_lower=False,
            starting_bankroll=1000, warmup=5,
        )
        assert len(eq) == 16
        assert len(fracs) == 15

    def test_bankroll_never_negative(self):
        # Half-Kelly with all losses after warmup — bankroll should clamp at 0.
        entries = [50] * 30
        outcomes = [True] * 10 + [False] * 20  # warmup builds positive edge then catastrophe
        eq, _, _ = simulate_walk_forward(
            entries, outcomes, multiplier=1.0, use_ci_lower=False,
            starting_bankroll=1000, warmup=10,
        )
        assert (eq >= 0).all()

    def test_deterministic_given_inputs(self):
        entries = [92] * 30
        outcomes = [True, True, True, False, True, True, True, True, True, True] * 3
        a = simulate_walk_forward(
            entries, outcomes, multiplier=0.25, use_ci_lower=False,
            starting_bankroll=1000, warmup=10,
        )
        b = simulate_walk_forward(
            entries, outcomes, multiplier=0.25, use_ci_lower=False,
            starting_bankroll=1000, warmup=10,
        )
        assert np.array_equal(a[0], b[0])
        assert np.array_equal(a[1], b[1])

    def test_negative_edge_history_keeps_fraction_zero(self):
        """If past games show negative EV, the walk-forward sizer must refuse to bet."""
        entries = [92] * 30
        outcomes = [False] * 30  # all losses
        _, fracs, _ = simulate_walk_forward(
            entries, outcomes, multiplier=0.25, use_ci_lower=False,
            starting_bankroll=1000, warmup=5,
        )
        # All fractions (after warmup) must be 0 since edge is always negative.
        assert (fracs == 0).all()


# ----------------------------------------------------------------------------
# simulate_fixed_fraction — the 5% cap baseline
# ----------------------------------------------------------------------------


class TestSimulateFixedFraction:
    def test_warmup_skipped(self):
        eq, fracs, _ = simulate_fixed_fraction(
            entries=[92] * 10, outcomes=[True] * 10,
            fraction=0.05, starting_bankroll=1000, warmup=5,
        )
        assert (fracs[:5] == 0).all()
        assert (fracs[5:] == 0.05).all()

    def test_compounding_matches_manual(self):
        # 1 warmup loss (no bet), then 1 win at 92¢ with f=0.5:
        # bankroll = 1000 * (1 + 0.5 * 8/92).
        eq, _, _ = simulate_fixed_fraction(
            entries=[92, 92], outcomes=[False, True],
            fraction=0.5, starting_bankroll=1000, warmup=1,
        )
        assert eq[2] == pytest.approx(1000 * (1 + 0.5 * 8 / 92))


# ----------------------------------------------------------------------------
# summarize — packaging stats
# ----------------------------------------------------------------------------


class TestSummarize:
    def test_average_active_fraction_excludes_warmup_zeros(self):
        equity = np.array([1000, 1000, 1000, 1050, 1100])
        fractions = np.array([0.0, 0.0, 0.0, 0.1, 0.1])
        edges = np.array([np.nan, np.nan, np.nan, 5.0, 5.0])
        result = summarize("Test", equity, fractions, edges, 1000)
        assert result.avg_active_fraction == pytest.approx(0.1)

    def test_total_return_pct_computed_correctly(self):
        equity = np.array([1000, 1200])
        fractions = np.array([0.1])
        edges = np.array([5.0])
        result = summarize("Test", equity, fractions, edges, 1000)
        assert result.total_return_pct == pytest.approx(20.0)


# ----------------------------------------------------------------------------
# End-to-end pipeline smoke test
# ----------------------------------------------------------------------------


def test_main_writes_figure(tmp_path):
    csv_path = tmp_path / "ds.csv"
    out_png = tmp_path / "wf.png"

    rng = np.random.default_rng(0)
    rows = []
    # 150 NBA games at 90-99¢ with ~94% win rate (positive EV)
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
        "--start", "1000",
        "--warmup", "20",
    ])
    assert out_png.exists()
    assert out_png.stat().st_size > 1000


def test_main_exits_when_too_few_games(tmp_path):
    csv_path = tmp_path / "ds.csv"
    # Only 5 games in band — fewer than default warmup of 20.
    rows = [_row(f"KX-{i}", f"2026-01-{i+1:02d}", "NBA", 92, True) for i in range(5)]
    _write_csv(csv_path, rows)
    with pytest.raises(SystemExit, match="need >"):
        main(argv=[
            "--dataset", str(csv_path),
            "--out", str(tmp_path / "x.png"),
            "--sport", "NBA",
            "--low", "90", "--high", "99",
        ])

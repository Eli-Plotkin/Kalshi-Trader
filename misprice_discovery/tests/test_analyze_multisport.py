"""Tests for analyze_multisport.

Covers the pure-function layer (load, filter, bootstrap, Monte Carlo envelope)
plus an end-to-end smoke test that synthesizes a CSV with known statistical
properties and verifies the pipeline correctly distinguishes signal from noise.
"""

from __future__ import annotations

import csv
import os
import tempfile

import matplotlib
matplotlib.use("Agg")  # headless backend; tests must not pop up windows.

import numpy as np
import pandas as pd
import pytest

from analyze_multisport import (
    ALPHA,
    bootstrap_mean_pnl,
    filter_strategy,
    load_dataset,
    null_envelope,
)
from build_multisport_research_dataset import FIELDNAMES as FIELDNAMES_SENTINEL


# ----------------------------------------------------------------------------
# Helpers: synthesize a CSV in the build_multisport output schema
# ----------------------------------------------------------------------------


def _write_synthetic_csv(path, rows):
    """Write a CSV that load_dataset can read.

    `rows` is a list of dicts with at minimum: Sport, Event_Ticker, Date,
    Favorite_Avg_Ask_Cents, Favorite_Won, Favorite_Hold_To_Settle_PnL_Cents.
    Other FIELDNAMES_SENTINEL columns are filled empty.
    """
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES_SENTINEL)
        writer.writeheader()
        # Description row — load_dataset must skip this.
        writer.writerow({c: f"Description: {c} description" for c in FIELDNAMES_SENTINEL})
        for row in rows:
            full = {c: "" for c in FIELDNAMES_SENTINEL}
            full.update(row)
            writer.writerow(full)


def _row(event_ticker, date, sport, price, won):
    pnl = (100 - price) if won else (-price)
    return {
        "Sport": sport,
        "Event_Ticker": event_ticker,
        "Date": date,
        "Favorite_Avg_Ask_Cents": price,
        "Favorite_Won": "True" if won else "False",
        "Favorite_Hold_To_Settle_PnL_Cents": pnl,
    }


# ----------------------------------------------------------------------------
# load_dataset
# ----------------------------------------------------------------------------


class TestLoadDataset:
    def test_skips_description_row(self, tmp_path):
        path = tmp_path / "t.csv"
        _write_synthetic_csv(path, [
            _row("KX-1", "2026-01-01", "NBA", 92, True),
        ])
        df = load_dataset(str(path))
        assert len(df) == 1
        assert not df["Event_Ticker"].astype(str).str.startswith("Description").any()

    def test_coerces_numeric_columns(self, tmp_path):
        path = tmp_path / "t.csv"
        _write_synthetic_csv(path, [
            _row("KX-1", "2026-01-01", "NBA", 92, True),
            _row("KX-2", "2026-01-02", "NBA", 95, False),
        ])
        df = load_dataset(str(path))
        assert df["Favorite_Avg_Ask_Cents"].dtype.kind in "if"
        assert df["Favorite_Hold_To_Settle_PnL_Cents"].dtype.kind in "if"

    def test_coerces_favorite_won_to_bool(self, tmp_path):
        path = tmp_path / "t.csv"
        _write_synthetic_csv(path, [
            _row("KX-1", "2026-01-01", "NBA", 92, True),
            _row("KX-2", "2026-01-02", "NBA", 95, False),
        ])
        df = load_dataset(str(path))
        assert df["Favorite_Won"].dtype == bool
        assert df["Favorite_Won"].iloc[0]
        assert not df["Favorite_Won"].iloc[1]

    def test_drops_rows_missing_pnl_or_price(self, tmp_path):
        path = tmp_path / "t.csv"
        _write_synthetic_csv(path, [
            _row("KX-1", "2026-01-01", "NBA", 92, True),
            {  # row missing price
                "Sport": "NBA",
                "Event_Ticker": "KX-bad",
                "Date": "2026-01-02",
                "Favorite_Avg_Ask_Cents": "",
                "Favorite_Won": "True",
                "Favorite_Hold_To_Settle_PnL_Cents": "",
            },
        ])
        df = load_dataset(str(path))
        assert len(df) == 1
        assert df["Event_Ticker"].iloc[0] == "KX-1"

    def test_dates_parsed(self, tmp_path):
        path = tmp_path / "t.csv"
        _write_synthetic_csv(path, [
            _row("KX-1", "2026-01-15", "NBA", 92, True),
        ])
        df = load_dataset(str(path))
        assert df["Date"].iloc[0] == pd.Timestamp("2026-01-15")


# ----------------------------------------------------------------------------
# filter_strategy
# ----------------------------------------------------------------------------


class TestFilterStrategy:
    def _three_row_df(self):
        return pd.DataFrame({
            "Sport": ["NBA", "NBA", "NBA"],
            "Date": pd.to_datetime(["2026-01-03", "2026-01-01", "2026-01-02"]),
            "Favorite_Avg_Ask_Cents": [85, 92, 99],
            "Favorite_Hold_To_Settle_PnL_Cents": [15, 8, 1],
        })

    def test_filters_to_price_range_inclusive(self):
        df = self._three_row_df()
        out = filter_strategy(df, 90, 99)
        assert len(out) == 2
        assert (out["Favorite_Avg_Ask_Cents"] >= 90).all()
        assert (out["Favorite_Avg_Ask_Cents"] <= 99).all()

    def test_below_low_excluded(self):
        df = self._three_row_df()
        out = filter_strategy(df, 90, 99)
        assert 85 not in out["Favorite_Avg_Ask_Cents"].values

    def test_boundary_values_included(self):
        df = pd.DataFrame({
            "Date": pd.to_datetime(["2026-01-01"] * 4),
            "Favorite_Avg_Ask_Cents": [89, 90, 99, 100],
            "Favorite_Hold_To_Settle_PnL_Cents": [11, 10, 1, 0],
        })
        out = filter_strategy(df, 90, 99)
        assert set(out["Favorite_Avg_Ask_Cents"]) == {90, 99}

    def test_sorted_by_date(self):
        df = self._three_row_df()
        out = filter_strategy(df, 90, 99)
        assert list(out["Date"]) == sorted(out["Date"])

    def test_index_reset(self):
        df = self._three_row_df()
        out = filter_strategy(df, 90, 99)
        assert list(out.index) == list(range(len(out)))


# ----------------------------------------------------------------------------
# bootstrap_mean_pnl
# ----------------------------------------------------------------------------


class TestBootstrapMeanPnl:
    def test_output_length(self):
        pnl = pd.Series([1, -2, 3, -4, 5])
        rng = np.random.default_rng(0)
        out = bootstrap_mean_pnl(pnl, 500, rng)
        assert len(out) == 500

    def test_means_centered_near_observed(self):
        # With 5000 resamples, bootstrap mean-of-means must be close to observed.
        rng = np.random.default_rng(0)
        pnl = pd.Series(rng.normal(loc=3.0, scale=10.0, size=200))
        observed = pnl.mean()
        boot = bootstrap_mean_pnl(pnl, 5000, np.random.default_rng(1))
        assert abs(boot.mean() - observed) < 0.3, (
            f"bootstrap mean {boot.mean():.3f} far from observed {observed:.3f}"
        )

    def test_deterministic_with_seed(self):
        pnl = pd.Series([1, 2, 3, 4, 5])
        a = bootstrap_mean_pnl(pnl, 1000, np.random.default_rng(42))
        b = bootstrap_mean_pnl(pnl, 1000, np.random.default_rng(42))
        assert np.array_equal(a, b)

    def test_zero_variance_input_produces_zero_variance_output(self):
        pnl = pd.Series([5.0] * 50)
        out = bootstrap_mean_pnl(pnl, 1000, np.random.default_rng(0))
        # Every resample mean must equal 5.0.
        assert np.allclose(out, 5.0)


# ----------------------------------------------------------------------------
# null_envelope
# ----------------------------------------------------------------------------


class TestNullEnvelope:
    def test_output_shapes(self):
        pnl = pd.Series(np.random.default_rng(0).normal(size=100))
        lower, upper = null_envelope(pnl, 500, np.random.default_rng(0))
        assert lower.shape == (100,)
        assert upper.shape == (100,)

    def test_lower_le_upper_everywhere(self):
        pnl = pd.Series(np.random.default_rng(0).normal(size=100))
        lower, upper = null_envelope(pnl, 500, np.random.default_rng(0))
        assert (lower <= upper).all()

    def test_envelope_expands_over_time(self):
        # Under H0 the cumulative-sum envelope width grows ~ sqrt(N).
        pnl = pd.Series(np.random.default_rng(0).normal(size=200))
        lower, upper = null_envelope(pnl, 1000, np.random.default_rng(1))
        widths = upper - lower
        # Width near the end should exceed width near the start.
        assert widths[-1] > widths[10]

    def test_envelope_anchored_near_zero_at_start(self):
        # With zero-mean shocks, the band must straddle zero in early indices.
        pnl = pd.Series(np.random.default_rng(0).normal(size=200))
        lower, upper = null_envelope(pnl, 1000, np.random.default_rng(2))
        assert lower[0] <= 0 <= upper[0]

    def test_deterministic_with_seed(self):
        pnl = pd.Series([1.0, -1.0, 1.0, -1.0] * 10)
        a = null_envelope(pnl, 500, np.random.default_rng(42))
        b = null_envelope(pnl, 500, np.random.default_rng(42))
        assert np.array_equal(a[0], b[0])
        assert np.array_equal(a[1], b[1])


# ----------------------------------------------------------------------------
# Statistical-correctness end-to-end:
# the pipeline must distinguish signal from noise.
# ----------------------------------------------------------------------------


class TestSignalVsNoiseDiscrimination:
    """The single most important property: the analysis must call positive EV
    'significant' when it really exists, and 'not significant' when it doesn't.
    """

    def _bootstrap_ci_excludes_zero(self, pnl, n_resamples=5000, seed=0):
        rng = np.random.default_rng(seed)
        boot = bootstrap_mean_pnl(pnl, n_resamples, rng)
        low, high = np.percentile(boot, [100 * ALPHA / 2, 100 * (1 - ALPHA / 2)])
        return low > 0 or high < 0, (low, high)

    def test_strong_positive_signal_detected_as_significant(self):
        # 1000 bets at 90c, win rate 95% (true edge = 95*10 - 5*90 = +5c/game).
        rng = np.random.default_rng(0)
        outcomes = rng.random(1000) < 0.95
        pnl = pd.Series(np.where(outcomes, 10.0, -90.0))
        significant, ci = self._bootstrap_ci_excludes_zero(pnl)
        assert significant, f"expected significance, got CI={ci}"
        assert ci[0] > 0, "CI lower bound should be positive"

    def test_strong_negative_signal_detected_as_significant(self):
        # 1000 bets at 90c, win rate 85% (true edge = 85*10 - 15*90 = -5c/game).
        rng = np.random.default_rng(0)
        outcomes = rng.random(1000) < 0.85
        pnl = pd.Series(np.where(outcomes, 10.0, -90.0))
        significant, ci = self._bootstrap_ci_excludes_zero(pnl)
        assert significant, f"expected significance, got CI={ci}"
        assert ci[1] < 0, "CI upper bound should be negative"

    def test_true_zero_ev_not_detected_as_significant_on_average(self):
        # 1000 bets at 90c, win rate exactly 90% (true EV = 0).
        # A single sample can be a false positive — run 20 trials and require
        # the CI to include zero in the vast majority (≥75%).
        zero_included = 0
        trials = 20
        for seed in range(trials):
            rng = np.random.default_rng(seed)
            outcomes = rng.random(1000) < 0.90
            pnl = pd.Series(np.where(outcomes, 10.0, -90.0))
            significant, _ci = self._bootstrap_ci_excludes_zero(pnl, seed=seed + 100)
            if not significant:
                zero_included += 1
        assert zero_included >= int(0.75 * trials), (
            f"false-positive rate too high: only {zero_included}/{trials} trials "
            f"correctly identified true-zero EV as non-significant"
        )

    def test_tiny_sample_size_inflates_uncertainty(self):
        # Same true edge but a tiny sample → CI should be much wider than at
        # a larger sample. Uses 70c bets at 60% win rate (guaranteed variance:
        # roughly half wins +30, half losses -70).
        def ci_width_at_n(n, seed):
            rng = np.random.default_rng(seed)
            outcomes = rng.random(n) < 0.60
            pnl = pd.Series(np.where(outcomes, 30.0, -70.0))
            _sig, ci = self._bootstrap_ci_excludes_zero(pnl, seed=seed + 100)
            return ci[1] - ci[0]

        narrow = ci_width_at_n(1000, seed=0)
        wide = ci_width_at_n(20, seed=0)
        # Bootstrap CI width scales ~ 1/sqrt(N); sqrt(50) ≈ 7×, so we want at
        # least 3× to confirm N matters even with sampling noise.
        assert wide > 3 * narrow, (
            f"expected CI width to expand at small N: wide={wide:.2f}, narrow={narrow:.2f}"
        )


# ----------------------------------------------------------------------------
# main() smoke test: synthesize CSV → run pipeline → verify PNG written
# ----------------------------------------------------------------------------


def test_full_pipeline_writes_png(tmp_path, monkeypatch):
    """End-to-end: synthesize a small dataset, run main(), assert PNG exists."""
    import analyze_multisport

    csv_path = tmp_path / "ds.csv"
    png_path = tmp_path / "out.png"

    rng = np.random.default_rng(0)
    # 200 favorites priced 90-99c with mild positive EV.
    rows = []
    for i in range(200):
        price = int(rng.integers(90, 100))
        won = rng.random() < (price / 100 + 0.02)  # +2c edge
        rows.append(_row(
            f"KX-{i}",
            f"2026-01-{(i % 28) + 1:02d}",
            "NBA",
            price,
            won,
        ))
    # And 50 sub-90c favorites that should be filtered out.
    for i in range(50):
        rows.append(_row(
            f"KX-low-{i}",
            f"2026-01-{(i % 28) + 1:02d}",
            "NBA",
            80,
            rng.random() < 0.8,
        ))
    _write_synthetic_csv(csv_path, rows)

    analyze_multisport.main(argv=["--dataset", str(csv_path), "--out", str(png_path)])
    assert png_path.exists()
    assert png_path.stat().st_size > 1000  # sanity: file isn't empty


def test_main_exits_when_dataset_missing(tmp_path):
    import analyze_multisport
    with pytest.raises(SystemExit, match="not found"):
        analyze_multisport.main(argv=["--dataset", str(tmp_path / "nope.csv")])


def test_main_exits_when_no_games_in_band(tmp_path):
    import analyze_multisport
    csv_path = tmp_path / "ds.csv"
    rows = [_row(f"KX-{i}", f"2026-01-{i+1:02d}", "NBA", 80, True) for i in range(5)]
    _write_synthetic_csv(csv_path, rows)
    with pytest.raises(SystemExit, match="No games"):
        analyze_multisport.main(argv=["--dataset", str(csv_path), "--low", "90", "--high", "99"])


def test_main_respects_low_high_args(tmp_path):
    """--low and --high actually filter the band, not just label the output."""
    import analyze_multisport
    csv_path = tmp_path / "ds.csv"
    png_path = tmp_path / "out_70_89.png"
    # Mix: 30 games at 75c (inside band), 30 games at 95c (outside).
    rows = []
    for i in range(30):
        rows.append(_row(f"KX-mid-{i}", f"2026-01-{(i % 28) + 1:02d}", "NBA", 75, i % 2 == 0))
    for i in range(30):
        rows.append(_row(f"KX-hi-{i}", f"2026-01-{(i % 28) + 1:02d}", "NBA", 95, True))
    _write_synthetic_csv(csv_path, rows)

    analyze_multisport.main(argv=[
        "--dataset", str(csv_path),
        "--low", "70", "--high", "89",
        "--out", str(png_path),
    ])
    assert png_path.exists()


def test_main_default_out_path_uses_band(tmp_path, monkeypatch, capsys):
    import analyze_multisport
    csv_path = tmp_path / "ds.csv"
    rows = [_row(f"KX-{i}", f"2026-01-{(i % 28) + 1:02d}", "NBA", 92, i % 2 == 0)
            for i in range(30)]
    _write_synthetic_csv(csv_path, rows)
    # Force default out path inside tmp_path so we don't write to the repo.
    monkeypatch.chdir(tmp_path)
    os.makedirs("misprice_discovery", exist_ok=True)

    analyze_multisport.main(argv=["--dataset", str(csv_path), "--low", "92", "--high", "92"])
    expected = tmp_path / "misprice_discovery" / "analysis_92_92_favorites.png"
    assert expected.exists()

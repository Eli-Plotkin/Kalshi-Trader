"""Tests for edge_validation: selection must be causal, stats must match known values."""

from __future__ import annotations

import csv

import matplotlib
matplotlib.use("Agg")

import numpy as np
import pandas as pd
import pytest

from edge_validation import (
    calibration_table,
    band_loss_pvalue,
    fit_bias_curve,
    null_model_pvalues,
    null_win_probs,
    run_null_tests,
    in_band,
    kalshi_fee_cents,
    load_nba_dataset,
    main,
    nested_walk_forward,
    poisson_binomial_cdf,
    select_low_edge,
    wilson_interval,
)


def _games(rows):
    """rows: (date, price, won). Builds the frame load_nba_dataset would return."""
    df = pd.DataFrame(rows, columns=["Date", "price", "won"])
    df["Date"] = pd.to_datetime(df["Date"])
    gross = np.where(df["won"], 100.0 - df["price"], -df["price"])
    df["net"] = gross - kalshi_fee_cents(df["price"])
    return df.sort_values("Date").reset_index(drop=True)


def test_fee_matches_kalshi_formula():
    assert kalshi_fee_cents(50) == pytest.approx(1.75)
    assert kalshi_fee_cents(100) == pytest.approx(0.0)


def test_select_low_edge_returns_none_when_nothing_profitable():
    df = _games([("2025-01-01", 92, False)] * 20)
    assert select_low_edge(df, [90, 91], 99, min_trades=5) is None


def test_select_low_edge_respects_min_trades():
    df = _games([("2025-01-01", 95, True)] * 4)
    assert select_low_edge(df, [90], 99, min_trades=5) is None
    assert select_low_edge(df, [90], 99, min_trades=4) == 90


def test_criteria_disagree_on_wide_vs_narrow_band():
    # 91-99: 10 wins at 95 (mean 4.7c, total 47c). Wide band adds 40 games at 86c
    # winning 90% of the time: lower mean (3.5c) but higher total (173c).
    rows = ([("2025-01-01", 95, True)] * 10 + [("2025-01-01", 86, True)] * 36
            + [("2025-01-01", 86, False)] * 4)
    df = _games(rows)
    assert select_low_edge(df, [85, 91], 99, 5, "total") == 85
    assert select_low_edge(df, [85, 91], 99, 5, "mean") == 91


def test_select_low_edge_rejects_unknown_criterion():
    with pytest.raises(ValueError):
        select_low_edge(_games([("2025-01-01", 95, True)]), [90], 99, 1, "bogus")


def test_walk_forward_never_uses_the_test_month():
    # Months 1-3 train; month 4 is a huge win streak the selector must not see
    # when choosing month 4's band, but month 5 may learn from it.
    rows = []
    for m in (1, 2, 3):
        rows += [(f"2025-{m:02d}-10", 95, False)] * 20          # losing history
    rows += [("2025-04-10", 95, True)] * 20                      # month 4 wins
    rows += [("2025-05-10", 95, True)] * 20
    df = _games(rows)
    wf = nested_walk_forward(df, fixed_low=91, high=99, lows=[91], min_trades=10,
                             min_train_months=3, criterion="mean")
    picks = dict(wf.chosen)
    assert picks["2025-04"] is None      # trained only on losing months
    assert picks["2025-05"] == 91 or picks["2025-05"] is None
    assert wf.trades.empty or (wf.trades["Date"] >= "2025-05-01").all()


def test_walk_forward_skips_warmup_months():
    df = _games([(f"2025-{m:02d}-10", 95, True) for m in range(1, 7) for _ in range(20)])
    wf = nested_walk_forward(df, fixed_low=91, high=99, lows=[91], min_trades=5,
                             min_train_months=3)
    assert wf.months_tested == 3


def test_wilson_interval_known_value():
    lo, hi = wilson_interval(0, 10)
    assert lo == pytest.approx(0.0, abs=1e-9)
    assert hi == pytest.approx(0.2775, abs=1e-3)
    assert all(np.isnan(wilson_interval(0, 0)))


def test_poisson_binomial_matches_binomial():
    from math import comb
    n, p, k = 10, 0.3, 4
    expected = sum(comb(n, i) * p**i * (1 - p) ** (n - i) for i in range(k + 1))
    assert poisson_binomial_cdf([p] * n, k) == pytest.approx(expected)


def test_poisson_binomial_all_zero_losses():
    # P(0 successes) = prod(1 - p)
    assert poisson_binomial_cdf([0.1, 0.2, 0.3], 0) == pytest.approx(0.9 * 0.8 * 0.7)


def test_calibration_table_buckets():
    df = _games([("2025-01-01", 91, True)] * 3 + [("2025-01-01", 91, False)])
    t = calibration_table(df, [90, 92])
    assert len(t) == 1 and t.loc[0, "n"] == 4
    assert t.loc[0, "win_rate"] == pytest.approx(75.0)
    assert t.loc[0, "excess_pts"] == pytest.approx(-16.0)


def test_null_pvalue_small_for_perfect_record_large_for_null_data():
    rng = np.random.default_rng(0)
    prices = rng.integers(91, 99, size=80)
    perfect = _games([("2025-01-01", p, True) for p in prices])
    wp = prices / 100.0
    p_fixed, p_search, *_ = null_model_pvalues(
        perfect, wp, low=91, high=99, lows=[91], min_trades=10, n_sims=500, seed=1)
    assert p_fixed < 0.05 and p_search < 0.05

    fair = _games([("2025-01-01", p, bool(rng.random() < p / 100)) for p in prices])
    p_fixed, *_ = null_model_pvalues(
        fair, wp, low=91, high=99, lows=[91], min_trades=10, n_sims=500, seed=1)
    assert p_fixed > 0.05


def test_fit_bias_curve_recovers_known_bias():
    # Simulate a market where favorites are systematically underpriced:
    # true P(win) = sigmoid(0.5 + 1.0 * logit(price)).
    rng = np.random.default_rng(3)
    prices = rng.integers(55, 97, size=20000)
    q = prices / 100.0
    true_p = 1 / (1 + np.exp(-(0.5 + np.log(q / (1 - q)))))
    won = rng.random(len(prices)) < true_p
    curve = fit_bias_curve(prices, won)
    assert curve.beta[0] == pytest.approx(0.5, abs=0.1)
    assert curve.beta[1] == pytest.approx(1.0, abs=0.1)
    assert curve(80) > 0.80                       # favorites win more than their price


def test_fit_bias_curve_is_identity_for_calibrated_market():
    rng = np.random.default_rng(4)
    prices = rng.integers(55, 97, size=20000)
    won = rng.random(len(prices)) < prices / 100.0
    curve = fit_bias_curve(prices, won)
    assert curve.beta[0] == pytest.approx(0.0, abs=0.1)
    assert curve.beta[1] == pytest.approx(1.0, abs=0.1)


def test_null_win_probs_breakeven_includes_fee_and_ex_model_needs_enough_data():
    small = _games([("2025-01-01", 92, True)] * 5)
    nulls = null_win_probs(small, search_floor=85)
    assert nulls["breakeven"][0] == pytest.approx((92 + kalshi_fee_cents(92)) / 100)
    assert "bias_ex" not in nulls                  # <30 games below the floor


def test_band_loss_pvalue_exact():
    band = _games([("2025-01-01", 90, True)] * 3)  # 0 losses
    assert band_loss_pvalue(band, [0.9, 0.9, 0.9]) == pytest.approx(0.9**3)


def test_run_null_tests_reports_each_model():
    rng = np.random.default_rng(5)
    rows = [("2025-01-01", int(p), bool(rng.random() < p / 100))
            for p in rng.integers(70, 99, size=400)]
    out = run_null_tests(_games(rows), low=91, high=99, lows=[88, 91], min_trades=10,
                         n_sims=100, seed=1)
    assert set(out) == {"breakeven", "bias_all", "bias_ex"}
    for res in out.values():
        assert 0 < res["band_win_prob"] < 1 and 0 < res["p_searched"] <= 1


def test_load_skips_description_row_and_dedupes(tmp_path):
    path = tmp_path / "d.csv"
    cols = ["Event_Ticker", "Date", "fav_avg_ask_cents", "Favorite_Won"]
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerow({c: f"Description: {c}" for c in cols})
        w.writerow({"Event_Ticker": "A", "Date": "2025-01-02", "fav_avg_ask_cents": 92, "Favorite_Won": True})
        w.writerow({"Event_Ticker": "A", "Date": "2025-01-02", "fav_avg_ask_cents": 92, "Favorite_Won": True})
        w.writerow({"Event_Ticker": "B", "Date": "2025-01-01", "fav_avg_ask_cents": 80, "Favorite_Won": False})
    df = load_nba_dataset(path)
    assert list(df["Event_Ticker"]) == ["B", "A"]            # sorted by date, deduped
    assert df.loc[1, "net"] == pytest.approx(8 - kalshi_fee_cents(92))
    assert df.loc[0, "net"] == pytest.approx(-80 - kalshi_fee_cents(80))


def test_in_band_inclusive():
    df = _games([("2025-01-01", p, True) for p in (90, 91, 99, 100)])
    assert sorted(in_band(df, 91, 99)["price"]) == [91, 99]


def test_main_runs_end_to_end(tmp_path, capsys):
    path = tmp_path / "d.csv"
    cols = ["Event_Ticker", "Date", "fav_avg_ask_cents", "Favorite_Won"]
    rng = np.random.default_rng(2)
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerow({c: f"Description: {c}" for c in cols})
        for i in range(300):
            price = int(rng.integers(80, 99))
            w.writerow({"Event_Ticker": f"E{i}",
                        "Date": (pd.Timestamp("2025-01-01") + pd.Timedelta(days=i)).date(),
                        "fav_avg_ask_cents": price,
                        "Favorite_Won": bool(rng.random() < price / 100)})
    out = tmp_path / "out.png"
    main(["--dataset", str(path), "--out", str(out), "--n-sims", "50"])
    assert out.exists()
    text = capsys.readouterr().out
    assert "NESTED WALK-FORWARD" in text and "NULL-MODEL" in text and "CALIBRATION" in text

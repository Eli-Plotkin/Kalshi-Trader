"""Edge validation: is the favorite-price-band edge real, or a product of how we picked it?

The band (e.g. 91-99c) was chosen by looking at the same games it is evaluated
on, so the in-sample win rate is optimistic. Three checks that account for that:

  1. Nested walk-forward   Re-pick the band using only *past* months, apply it to
                           the next month, aggregate. Tests the selection
                           procedure, not one hand-picked number. (Plain K-fold
                           is not used: it trains on the future and does not
                           re-run the band selection.)
  2. Null-model tests      Simulate seasons under a null win-probability model, run
                           the SAME band search on each, and count how often chance
                           alone looks as good as the observed result. Three nulls,
                           none of which assumes the market is fair:
                             breakeven  win prob = (price + fee): the band earns
                                        exactly zero. Rejecting = it is profitable.
                             bias_all   win prob follows a smooth calibration curve
                                        (logistic in logit(price)) fit on ALL games.
                                        Allows ordinary favorite-longshot bias;
                                        rejecting = the band beats that curve.
                             bias_ex    same curve fit only on games priced below
                                        the searched bands, then extrapolated up.
  3. Calibration           Win rate vs price in cent buckets, with Wilson CIs.

PnL is per contract, hold-to-settlement, net of Kalshi's taker fee
(0.07 * p * (1-p) per contract, unrounded).

Run:
  python misprice_discovery/edge_validation.py
  python misprice_discovery/edge_validation.py --low 91 --high 99 --n-sims 20000
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

DEFAULT_DATASET = "kalshi_nba_research_dataset.csv"
DEFAULT_OUT = "misprice_discovery/edge_validation.png"
DEFAULT_LOW = 91
DEFAULT_HIGH = 99
DEFAULT_SEARCH_LOWS = tuple(range(85, 96))   # candidate lower edges searched
DEFAULT_MIN_TRAIN_TRADES = 15                # a band needs this many past trades to be eligible
DEFAULT_MIN_TRAIN_MONTHS = 3
DEFAULT_SIMS = 5_000
DEFAULT_SEED = 7


# ----------------------------------------------------------------------------
# Data + PnL
# ----------------------------------------------------------------------------


def kalshi_fee_cents(price_cents):
    """Unrounded taker fee in cents per contract (matches sportsbook_sourced.pricing)."""
    p = np.asarray(price_cents, dtype=float) / 100.0
    return 100.0 * 0.07 * p * (1.0 - p)


def load_nba_dataset(path):
    """Load the NBA research CSV into price / won / net-PnL columns."""
    df = pd.read_csv(path)
    # Row 0 is a "Description: ..." row describing each column.
    df = df[~df["Event_Ticker"].astype(str).str.startswith("Description")].copy()
    df["Date"] = pd.to_datetime(df["Date"], errors="coerce")
    df["price"] = pd.to_numeric(df["fav_avg_ask_cents"], errors="coerce")
    df["won"] = df["Favorite_Won"].astype(str).str.lower().eq("true")
    df = df.dropna(subset=["Date", "price"]).drop_duplicates("Event_Ticker")
    df = df.sort_values("Date").reset_index(drop=True)
    gross = np.where(df["won"], 100.0 - df["price"], -df["price"])
    df["net"] = gross - kalshi_fee_cents(df["price"])
    return df


def in_band(df, low, high):
    return df[(df["price"] >= low) & (df["price"] <= high)]


# ----------------------------------------------------------------------------
# Band selection (the procedure under test)
# ----------------------------------------------------------------------------


CRITERIA = ("total", "mean")


def select_low_edge(train, lows, high, min_trades, criterion="total"):
    """Pick the lower band edge that scores best on `train`.

    criterion "total" maximizes total net PnL (favors wide, high-volume bands);
    "mean" maximizes net PnL per contract (favors narrow, high-edge bands).
    Only bands with positive mean net PnL and at least `min_trades` trades are
    eligible. Returns None if nothing qualifies (the strategy sits out).
    """
    if criterion not in CRITERIA:
        raise ValueError(f"criterion must be one of {CRITERIA}")
    best_low, best_score = None, 0.0
    for low in lows:
        band = in_band(train, low, high)
        if len(band) < min_trades or band["net"].mean() <= 0:
            continue
        score = band["net"].sum() if criterion == "total" else band["net"].mean()
        if score > best_score:
            best_low, best_score = low, score
    return best_low


# ----------------------------------------------------------------------------
# 1. Nested walk-forward
# ----------------------------------------------------------------------------


@dataclass
class WalkForwardResult:
    trades: pd.DataFrame           # out-of-sample trades taken, with the band used
    fixed_trades: pd.DataFrame     # OOS trades for the fixed low..high band, same months
    chosen: list                   # (month, chosen_low) per test month
    months_tested: int


def nested_walk_forward(df, *, fixed_low, high, lows, min_trades, min_train_months,
                        criterion="total"):
    """Expanding-window walk-forward with band re-selection each month.

    For each calendar month after `min_train_months` of history, choose the low
    edge from strictly earlier months only, then trade that band in the month.
    """
    months = df["Date"].dt.to_period("M")
    ordered = sorted(months.unique())
    taken, fixed, chosen = [], [], []
    tested = 0
    for i, month in enumerate(ordered):
        if i < min_train_months:
            continue
        tested += 1
        train = df[months < month]
        test = df[months == month]
        low = select_low_edge(train, lows, high, min_trades, criterion)
        chosen.append((str(month), low))
        if low is not None:
            taken.append(in_band(test, low, high).assign(band_low=low))
        fixed.append(in_band(test, fixed_low, high))
    empty = df.iloc[0:0]
    return WalkForwardResult(
        trades=pd.concat(taken) if taken else empty,
        fixed_trades=pd.concat(fixed) if fixed else empty,
        chosen=chosen,
        months_tested=tested,
    )


# ----------------------------------------------------------------------------
# 2. Null-model tests
# ----------------------------------------------------------------------------


def search_statistic(prices, net, lows, high, min_trades):
    """Best mean net PnL (cents/contract) over the searched bands."""
    best = -np.inf
    for low in lows:
        mask = (prices >= low) & (prices <= high)
        if mask.sum() >= min_trades:
            best = max(best, net[mask].mean())
    return best


def fit_bias_curve(prices, won, iters=50):
    """Logistic calibration curve: P(win) = sigmoid(a + b * logit(price/100)).

    a=0, b=1 is "price is the true probability". Other values let human bias
    (e.g. favorites underpriced, longshots overpriced) shift and tilt the
    curve smoothly. Fit by IRLS (Newton) in numpy. Returns a function
    price_cents -> win probability.
    """
    p = np.clip(np.asarray(prices, dtype=float) / 100.0, 0.01, 0.99)
    x = np.log(p / (1 - p))
    X = np.column_stack([np.ones_like(x), x])
    y = np.asarray(won, dtype=float)
    beta = np.array([0.0, 1.0])
    for _ in range(iters):
        mu = 1.0 / (1.0 + np.exp(-(X @ beta)))
        w = np.clip(mu * (1 - mu), 1e-9, None)
        step = np.linalg.solve(X.T @ (X * w[:, None]) + 1e-9 * np.eye(2), X.T @ (y - mu))
        beta = beta + step
        if np.abs(step).max() < 1e-10:
            break

    def curve(price_cents):
        q = np.clip(np.asarray(price_cents, dtype=float) / 100.0, 0.01, 0.99)
        return 1.0 / (1.0 + np.exp(-(beta[0] + beta[1] * np.log(q / (1 - q)))))

    curve.beta = beta
    return curve


def null_win_probs(df, search_floor):
    """Per-game win probabilities under each null model (name -> array)."""
    prices = df["price"].to_numpy()
    out = {"breakeven": (prices + kalshi_fee_cents(prices)) / 100.0}
    out["bias_all"] = fit_bias_curve(prices, df["won"])(prices)
    below = df["price"] < search_floor
    if below.sum() >= 30:
        out["bias_ex"] = fit_bias_curve(prices[below], df["won"][below])(prices)
    return out


def null_model_pvalues(df, win_prob, *, low, high, lows, min_trades, n_sims, seed):
    """P-values for the observed band edge when each game wins with `win_prob`.

    Returns (p_fixed, p_searched, observed_fixed, observed_searched):
      p_fixed     chance a band picked in advance looks this good by luck
      p_searched  chance the BEST band out of `lows` looks this good by luck --
                  the honest number, since we did search.
    """
    rng = np.random.default_rng(seed)
    prices = df["price"].to_numpy()
    fee = kalshi_fee_cents(prices)
    win_prob = np.asarray(win_prob, dtype=float)

    def net_for(won):
        return np.where(won, 100.0 - prices, -prices) - fee

    fixed_mask = (prices >= low) & (prices <= high)
    observed_net = df["net"].to_numpy()
    observed_fixed = observed_net[fixed_mask].mean()
    observed_searched = search_statistic(prices, observed_net, lows, high, min_trades)

    ge_fixed = ge_searched = 0
    for _ in range(n_sims):
        net = net_for(rng.random(len(prices)) < win_prob)
        if fixed_mask.any() and net[fixed_mask].mean() >= observed_fixed:
            ge_fixed += 1
        if search_statistic(prices, net, lows, high, min_trades) >= observed_searched:
            ge_searched += 1
    # +1 smoothing: a Monte Carlo p-value is never exactly zero.
    p_fixed = (ge_fixed + 1) / (n_sims + 1)
    p_searched = (ge_searched + 1) / (n_sims + 1)
    return p_fixed, p_searched, observed_fixed, observed_searched


def run_null_tests(df, *, low, high, lows, min_trades, n_sims, seed):
    """Per null model: predicted band win prob, expected losses, exact and searched p-values."""
    band_mask = ((df["price"] >= low) & (df["price"] <= high)).to_numpy()
    band = df[band_mask]
    results = {}
    for name, win_prob in null_win_probs(df, min(lows)).items():
        _, p_searched, _, _ = null_model_pvalues(
            df, win_prob, low=low, high=high, lows=lows,
            min_trades=min_trades, n_sims=n_sims, seed=seed)
        results[name] = {
            "band_win_prob": float(win_prob[band_mask].mean()),
            "exp_losses": float((1.0 - win_prob[band_mask]).sum()),
            "p_fixed_exact": band_loss_pvalue(band, win_prob[band_mask]),
            "p_searched": p_searched,
        }
    return results


# ----------------------------------------------------------------------------
# 3. Calibration
# ----------------------------------------------------------------------------


def wilson_interval(wins, n, z=1.96):
    if n == 0:
        return (np.nan, np.nan)
    phat = wins / n
    denom = 1 + z**2 / n
    centre = (phat + z**2 / (2 * n)) / denom
    half = z * np.sqrt(phat * (1 - phat) / n + z**2 / (4 * n**2)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def poisson_binomial_cdf(probs, k):
    """P(number of successes <= k) for independent Bernoulli(probs), exact DP."""
    dist = np.zeros(len(probs) + 1)
    dist[0] = 1.0
    for p in probs:
        dist[1:] = dist[1:] * (1 - p) + dist[:-1] * p
        dist[0] *= 1 - p
    return float(dist[: k + 1].sum())


def calibration_table(df, edges):
    """Win rate vs mean price per price bucket [edge_i, edge_{i+1})."""
    rows = []
    for lo, hi in zip(edges[:-1], edges[1:]):
        b = df[(df["price"] >= lo) & (df["price"] < hi)]
        if b.empty:
            continue
        wins = int(b["won"].sum())
        ci = wilson_interval(wins, len(b))
        rows.append({
            "bucket": f"{lo}-{hi - 1}",
            "n": len(b),
            "mean_price": b["price"].mean(),
            "win_rate": 100.0 * wins / len(b),
            "wilson_lo": 100.0 * ci[0],
            "wilson_hi": 100.0 * ci[1],
            "excess_pts": 100.0 * wins / len(b) - b["price"].mean(),
            "net_mean": b["net"].mean(),
        })
    return pd.DataFrame(rows)


def band_loss_pvalue(band, win_prob):
    """Exact P(this few losses or fewer | each game wins with win_prob). Small => band beats the null."""
    losses = int((~band["won"]).sum())
    return poisson_binomial_cdf(1.0 - np.asarray(win_prob, dtype=float), losses)


# ----------------------------------------------------------------------------
# Reporting
# ----------------------------------------------------------------------------


def _fmt_trades(label, t):
    if t.empty:
        return f"  {label:<34} no trades"
    return (f"  {label:<34} n={len(t):3d}  win={100*t['won'].mean():5.1f}%  "
            f"net={t['net'].mean():6.2f}c/ct  total={t['net'].sum():7.1f}c  "
            f"losses={int((~t['won']).sum())}")


def print_report(df, wfs, pvals, curves, calib, band, low, high):
    print(f"\nDataset: {len(df)} games, {df['Date'].min().date()} .. {df['Date'].max().date()}")
    print(f"Band under test: {low}-{high}c   (fees included)\n")

    print("1) NESTED WALK-FORWARD (band re-picked each month from past data only)")
    for criterion, wf in wfs.items():
        picks = [(m, l) for m, l in wf.chosen if l is not None]
        print(f"  [select by {criterion}] months tested: {wf.months_tested}, "
              f"band selected in {len(picks)}, sat out {wf.months_tested - len(picks)}")
        if picks:
            print("    selected low edges: " + ", ".join(f"{m}:{l}" for m, l in picks[:14])
                  + (" ..." if len(picks) > 14 else ""))
        print(_fmt_trades("  OOS, band re-selected monthly", wf.trades))
    first = next(iter(wfs.values()))
    print(_fmt_trades(f"  fixed {low}-{high}, same months*", first.fixed_trades))
    print(f"  *{low} was chosen using the full sample, so this row is NOT out-of-sample.")

    print("\n2) NULL-MODEL TESTS (none assume win probability == price)")
    n_loss = int((~band["won"]).sum())
    print(f"  observed {low}-{high}: {len(band)} games, {n_loss} losses, "
          f"net {band['net'].mean():.2f}c/ct")
    print(f"  {'null model':<11}{'pred. win%':>11}{'exp. losses':>12}{'P(fixed, exact)':>17}{'P(after search)':>17}")
    for name, res in pvals.items():
        print(f"  {name:<11}{100 * res['band_win_prob']:>11.1f}{res['exp_losses']:>12.2f}"
              f"{res['p_fixed_exact']:>17.4f}{res['p_searched']:>17.4f}")
    print("  breakeven: band earns exactly 0 after fees (small P => profitable).")
    print("  bias_all / bias_ex: smooth favorite-longshot-style bias is allowed (small P => band beats it).")
    print(f"  bias curve fit on all games: a={curves['bias_all'][0]:+.3f}, b={curves['bias_all'][1]:.3f} "
          "(a=0,b=1 would mean price is the true probability)")

    print("\n3) CALIBRATION (win rate vs price)")
    show = calib.copy()
    for col in ("mean_price", "win_rate", "wilson_lo", "wilson_hi", "excess_pts", "net_mean"):
        show[col] = show[col].round(2)
    print(show.to_string(index=False))


def plot_report(df, calib, wfs, low, high, out, curve=None):
    fig, axes = plt.subplots(1, 2, figsize=(13, 5))
    ax = axes[0]
    ax.errorbar(calib["mean_price"], calib["win_rate"],
                yerr=[calib["win_rate"] - calib["wilson_lo"], calib["wilson_hi"] - calib["win_rate"]],
                fmt="o", capsize=3, label="observed win rate (Wilson 95%)")
    lim = [calib["mean_price"].min() - 2, 100]
    ax.plot(lim, lim, "k--", lw=1, label="win rate == price (reference only)")
    if curve is not None:
        xs = np.linspace(lim[0], 99, 100)
        ax.plot(xs, 100 * curve(xs), color="C1", lw=1.5, label="fitted bias curve (all games)")
    ax.axvspan(low, high, alpha=0.08, color="green", label=f"{low}-{high}c band")
    ax.set(xlabel="mean entry price (c)", ylabel="win rate (%)", title="Calibration")
    ax.legend(fontsize=8)

    ax = axes[1]
    series = [(f"re-selected by {c}", wf.trades) for c, wf in wfs.items()]
    series.append((f"fixed {low}-{high} (in-sample pick)", next(iter(wfs.values())).fixed_trades))
    for label, t in series:
        if not t.empty:
            t = t.sort_values("Date")
            ax.plot(t["Date"], t["net"].cumsum(), label=f"{label} (n={len(t)})")
    ax.axhline(0, color="k", lw=0.8)
    ax.set(xlabel="date", ylabel="cumulative net PnL (c/contract)", title="Out-of-sample walk-forward")
    ax.legend(fontsize=8)
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(out, dpi=130)
    plt.close(fig)


def _parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--dataset", default=DEFAULT_DATASET)
    p.add_argument("--out", default=DEFAULT_OUT)
    p.add_argument("--low", type=int, default=DEFAULT_LOW)
    p.add_argument("--high", type=int, default=DEFAULT_HIGH)
    p.add_argument("--n-sims", type=int, default=DEFAULT_SIMS)
    p.add_argument("--min-trades", type=int, default=DEFAULT_MIN_TRAIN_TRADES)
    p.add_argument("--min-train-months", type=int, default=DEFAULT_MIN_TRAIN_MONTHS)
    p.add_argument("--seed", type=int, default=DEFAULT_SEED)
    return p.parse_args(argv)


def main(argv=None):
    args = _parse_args(argv)
    df = load_nba_dataset(args.dataset)
    lows = DEFAULT_SEARCH_LOWS
    wfs = {
        criterion: nested_walk_forward(
            df, fixed_low=args.low, high=args.high, lows=lows,
            min_trades=args.min_trades, min_train_months=args.min_train_months,
            criterion=criterion,
        )
        for criterion in CRITERIA
    }
    pvals = run_null_tests(df, low=args.low, high=args.high, lows=lows,
                           min_trades=args.min_trades, n_sims=args.n_sims, seed=args.seed)
    curve_all = fit_bias_curve(df["price"], df["won"])
    curves = {"bias_all": curve_all.beta}
    calib = calibration_table(df[df["price"] >= 80], list(range(80, 100)) + [100])
    band = in_band(df, args.low, args.high)
    print_report(df, wfs, pvals, curves, calib, band, args.low, args.high)
    plot_report(df, calib, wfs, args.low, args.high, args.out, curve_all)
    print(f"\nSaved plot: {args.out}")


if __name__ == "__main__":
    main()

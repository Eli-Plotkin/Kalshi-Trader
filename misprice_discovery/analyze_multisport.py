"""Statistical analysis: do favorites in a chosen price band have positive EV?

Two charts in one figure:
  (1) Bootstrap distribution of mean PnL — the formal significance test
  (2) Cumulative PnL over time vs a Monte Carlo null-hypothesis envelope —
      the visceral test that shows whether the edge accumulates over time

The strategy under test: enter every favorite priced in [LOW, HIGH] cents at
the pre-game avg ask, hold to settlement, +(100-entry)¢ if won, -(entry)¢ if
lost. LOW/HIGH come from `--low`/`--high` flags or `strategy_config.py`.

Run:
    python misprice_discovery/analyze_multisport.py
    python misprice_discovery/analyze_multisport.py --low 70 --high 99
Outputs:
    misprice_discovery/analysis_<low>_<high>_favorites.png
"""

from __future__ import annotations

import argparse
import os

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

import strategy_config

DATASET = "kalshi_multisport_research_dataset.csv"
OUT_PNG = f"misprice_discovery/analysis_{strategy_config.PRICE_MIN}_{strategy_config.PRICE_MAX}_favorites.png"

PRICE_LOW = strategy_config.PRICE_MIN
PRICE_HIGH = strategy_config.PRICE_MAX
N_BOOTSTRAP = 10_000
N_MONTE_CARLO = 1_000
ALPHA = 0.05  # 95% CIs

SPORT_COLORS = {
    "NBA": "#E03A3E",
    "NFL": "#013369",
    "MLB": "#D50032",
    "NHL": "#111111",
}


def load_dataset(path):
    """Load CSV, skip the description row, type-coerce."""
    df = pd.read_csv(path)
    # Drop the description row (every cell starts with "Description: ").
    df = df[~df["Event_Ticker"].astype(str).str.startswith("Description: ")].copy()
    df["Date"] = pd.to_datetime(df["Date"], errors="coerce")
    df["Favorite_Avg_Ask_Cents"] = pd.to_numeric(
        df["Favorite_Avg_Ask_Cents"], errors="coerce"
    )
    df["Favorite_Hold_To_Settle_PnL_Cents"] = pd.to_numeric(
        df["Favorite_Hold_To_Settle_PnL_Cents"], errors="coerce"
    )
    df["Favorite_Won"] = df["Favorite_Won"].astype(str).str.lower().eq("true")
    df = df.dropna(subset=[
        "Date", "Favorite_Avg_Ask_Cents", "Favorite_Hold_To_Settle_PnL_Cents",
    ])
    return df


def filter_strategy(df, low, high):
    mask = (df["Favorite_Avg_Ask_Cents"] >= low) & (df["Favorite_Avg_Ask_Cents"] <= high)
    return df[mask].sort_values("Date").reset_index(drop=True)


def bootstrap_mean_pnl(pnl, n_resamples, rng):
    """Resample with replacement; return array of bootstrap means."""
    n = len(pnl)
    idx = rng.integers(0, n, size=(n_resamples, n))
    return pnl.values[idx].mean(axis=1)


def null_envelope(pnl, n_simulations, rng):
    """Monte Carlo: simulate cumulative PnL under H0 (true mean = 0).

    Centers the observed PnL series at zero (preserves variance), then draws
    N i.i.d. samples with replacement from those zero-mean residuals per
    simulation and accumulates them. Returns 2.5/97.5 percentile envelopes at
    each game index — the band where the cumulative PnL would plausibly land
    if the true edge were zero.

    Note: we sample with replacement (not permutation). Permutation forces the
    sum at index N to exactly zero, which artificially collapses the envelope
    at the tail — bootstrap with replacement correctly preserves the random
    walk's sqrt(N) growth.
    """
    zero_mean = pnl.values - pnl.values.mean()
    n = len(zero_mean)
    sims = np.empty((n_simulations, n))
    for i in range(n_simulations):
        sample = rng.choice(zero_mean, size=n, replace=True)
        sims[i] = np.cumsum(sample)
    lower = np.percentile(sims, 100 * ALPHA / 2, axis=0)
    upper = np.percentile(sims, 100 * (1 - ALPHA / 2), axis=0)
    return lower, upper


def _bootstrap_stats(pnl, rng):
    """Return (observed_mean, ci_low, ci_high, p_value) for a PnL series."""
    means = bootstrap_mean_pnl(pnl, N_BOOTSTRAP, rng)
    observed = pnl.mean()
    ci_low, ci_high = np.percentile(means, [100 * ALPHA / 2, 100 * (1 - ALPHA / 2)])
    p_value = (means <= 0).mean()
    return observed, ci_low, ci_high, p_value


def plot_bootstrap(ax, pnl, rng):
    means = bootstrap_mean_pnl(pnl, N_BOOTSTRAP, rng)
    observed = pnl.mean()
    ci_low, ci_high = np.percentile(means, [100 * ALPHA / 2, 100 * (1 - ALPHA / 2)])
    p_value = (means <= 0).mean()

    ax.hist(means, bins=60, color="#4C72B0", edgecolor="white", alpha=0.85)
    ax.axvline(0, color="red", linewidth=1.5, linestyle="--", label="EV = 0")
    ax.axvline(observed, color="black", linewidth=2, label=f"observed mean = {observed:.2f}¢")
    ax.axvspan(ci_low, ci_high, color="black", alpha=0.08, label=f"95% CI = [{ci_low:.2f}¢, {ci_high:.2f}¢]")

    significance = "SIGNIFICANT at p<0.05" if ci_low > 0 else "NOT significant at p<0.05"
    ax.set_title(
        f"POOLED bootstrap mean PnL — all sports, favorites {PRICE_LOW}-{PRICE_HIGH}¢ "
        f"(N={len(pnl):,}, {significance})",
        fontsize=12,
    )
    ax.set_xlabel("Mean PnL per game (cents)")
    ax.set_ylabel(f"Frequency across {N_BOOTSTRAP:,} bootstrap resamples")
    ax.legend(loc="upper left", fontsize=9)
    ax.text(
        0.98, 0.97,
        f"p(mean ≤ 0) = {p_value:.4f}",
        transform=ax.transAxes,
        ha="right", va="top",
        fontsize=10,
        bbox=dict(boxstyle="round,pad=0.4", facecolor="white", edgecolor="gray"),
    )


def plot_forest_per_sport(ax, df, rng):
    """Forest plot: one horizontal row per sport (plus Pooled) showing each
    group's observed mean PnL and 95% bootstrap CI on a shared x-axis. Any
    sport whose CI sits entirely right of 0 has statistically significant
    positive EV; any sport whose CI crosses 0 does not.
    """
    rows = []  # (label, observed, ci_low, ci_high, p, n, color, significant)
    for sport in sorted(df["Sport"].unique()):
        sub = df[df["Sport"] == sport]["Favorite_Hold_To_Settle_PnL_Cents"]
        if len(sub) < 5:
            continue
        observed, lo, hi, p = _bootstrap_stats(sub, rng)
        significant = lo > 0 or hi < 0
        rows.append((sport, observed, lo, hi, p, len(sub), SPORT_COLORS.get(sport, "#888"), significant))

    pooled_pnl = df["Favorite_Hold_To_Settle_PnL_Cents"]
    observed, lo, hi, p = _bootstrap_stats(pooled_pnl, rng)
    significant = lo > 0 or hi < 0
    rows.append(("POOLED", observed, lo, hi, p, len(pooled_pnl), "#222", significant))

    y_positions = list(range(len(rows)))[::-1]  # top-to-bottom display order

    for y, (label, obs, lo, hi, p, n, color, sig) in zip(y_positions, rows):
        ax.hlines(y, lo, hi, color=color, linewidth=2.5 if sig else 1.5,
                  alpha=1.0 if sig else 0.5)
        ax.plot(obs, y, "o", color=color, markersize=10 if sig else 7,
                markeredgecolor="black" if sig else "none", markeredgewidth=1.5)
        sig_marker = "✓ significant" if sig else "not significant"
        annotation = (
            f"  {obs:+.2f}¢  [{lo:+.2f}, {hi:+.2f}]  "
            f"N={n:,}  p={p:.3f}  {sig_marker}"
        )
        ax.text(hi, y, annotation, va="center", fontsize=9,
                color="black" if sig else "gray")

    ax.axvline(0, color="red", linewidth=1.5, linestyle="--", alpha=0.8)
    ax.set_yticks(y_positions)
    ax.set_yticklabels([r[0] for r in rows], fontsize=11)
    ax.set_xlabel("Bootstrap mean PnL per game (cents) — 95% CI")
    ax.set_title(
        f"PER-SPORT bootstrap CIs — favorites priced {PRICE_LOW}-{PRICE_HIGH}¢ "
        f"(thick line + filled marker = CI excludes 0 = significant)",
        fontsize=12,
    )
    # Pad right so the annotations don't get clipped.
    xmin, xmax = ax.get_xlim()
    ax.set_xlim(xmin, xmax + (xmax - xmin) * 0.55)
    ax.set_ylim(-0.5, len(rows) - 0.5)


def plot_cumulative(ax, df, rng):
    pnl = df["Favorite_Hold_To_Settle_PnL_Cents"]
    cum = pnl.cumsum()
    x = np.arange(1, len(pnl) + 1)

    lower, upper = null_envelope(pnl, N_MONTE_CARLO, rng)
    ax.fill_between(
        x, lower, upper,
        color="gray", alpha=0.25,
        label="95% null-hypothesis envelope (true EV = 0)",
    )
    ax.axhline(0, color="black", linewidth=0.8, alpha=0.5)

    # Pooled line.
    ax.plot(x, cum.values, color="#222", linewidth=1.6, label="cumulative PnL (all sports)")

    # Per-sport overlay so it's visible whether one sport drives the edge.
    for sport, sub in df.groupby("Sport", sort=False):
        sport_cum = sub["Favorite_Hold_To_Settle_PnL_Cents"].cumsum()
        sport_x = np.arange(1, len(sport_cum) + 1)
        ax.plot(
            sport_x, sport_cum.values,
            color=SPORT_COLORS.get(sport, "#888"),
            linewidth=1.0, alpha=0.75,
            label=f"{sport} (N={len(sub):,})",
        )

    final = cum.iloc[-1]
    ax.set_title(
        f"Cumulative PnL of $1/game on every {PRICE_LOW}-{PRICE_HIGH}¢ favorite "
        f"(final: {final:+.0f}¢ over {len(pnl):,} bets)",
        fontsize=12,
    )
    ax.set_xlabel("Bet index (chronological)")
    ax.set_ylabel("Cumulative PnL (cents)")
    ax.legend(loc="upper left", fontsize=9)


def _parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--low", type=int, default=PRICE_LOW,
                        help=f"Lower price bound in cents (default {PRICE_LOW}).")
    parser.add_argument("--high", type=int, default=PRICE_HIGH,
                        help=f"Upper price bound in cents (default {PRICE_HIGH}).")
    parser.add_argument("--dataset", default=DATASET,
                        help=f"Path to research-dataset CSV (default {DATASET}).")
    parser.add_argument("--out", default=None,
                        help="Path to output PNG (default: derived from --low/--high).")
    return parser.parse_args(argv)


def main(argv=None):
    args = _parse_args(argv)
    low, high = args.low, args.high
    dataset_path = args.dataset
    out_path = args.out or f"misprice_discovery/analysis_{low}_{high}_favorites.png"

    # Update module-level constants so plot helpers pick up the new band.
    global PRICE_LOW, PRICE_HIGH
    PRICE_LOW, PRICE_HIGH = low, high

    if not os.path.exists(dataset_path):
        raise SystemExit(f"Dataset not found: {dataset_path} — run build_multisport first.")

    df = load_dataset(dataset_path)
    strategy = filter_strategy(df, low, high)
    if strategy.empty:
        raise SystemExit(f"No games in {low}-{high}¢ range yet.")

    print(f"Loaded {len(df):,} games; {len(strategy):,} in {low}-{high}¢ range")
    print("Per-sport counts in strategy band:")
    print(strategy.groupby("Sport").size().to_string())

    rng = np.random.default_rng(seed=42)
    fig, (ax_pool, ax_forest, ax_cum) = plt.subplots(3, 1, figsize=(14, 14))
    plot_bootstrap(ax_pool, strategy["Favorite_Hold_To_Settle_PnL_Cents"], rng)
    plot_forest_per_sport(ax_forest, strategy, rng)
    plot_cumulative(ax_cum, strategy, rng)
    fig.tight_layout()
    fig.savefig(out_path, dpi=150, bbox_inches="tight")
    print(f"\nSaved figure: {out_path}")


if __name__ == "__main__":
    main()

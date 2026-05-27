"""Walk-forward validation: the honest test of whether the edge is real.

At each bet, decide bet size using ONLY games that happened before it.
No peeking at future data, no using the full-sample mean. This is what
actually trading the strategy would look like.

What this answers:
  - Does the in-sample edge survive when you can't cheat by seeing the future?
  - How does the Kelly recommendation evolve as data accumulates?
  - Does the bootstrap CI tighten or shift over time?

Three sizing strategies compared:
  Walk-forward Quarter Kelly   recompute Kelly from past PnL, take 25%
  Walk-forward CI-lower Kelly  use bootstrap CI lower bound (robust)
  In-sample Quarter Kelly      cheats by using full-sample Kelly (reference)

Run:
  python misprice_discovery/walk_forward_validation.py
  python misprice_discovery/walk_forward_validation.py --warmup 30
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from analyze_multisport import (
    ALPHA,
    bootstrap_mean_pnl,
    filter_strategy,
    load_dataset,
)
from kelly_simulator import (
    log_sharpe,
    max_drawdown_pct,
    pct_time_underwater,
    simulate_bankroll,
)

DEFAULT_DATASET = "kalshi_multisport_research_dataset.csv"
DEFAULT_OUT = "misprice_discovery/walk_forward_validation.png"
import os

import strategy_config

DEFAULT_SPORT = os.getenv("STRATEGY_SPORT", "NBA")
DEFAULT_LOW = strategy_config.PRICE_MIN
DEFAULT_HIGH = strategy_config.PRICE_MAX
DEFAULT_START = 1_000.0
DEFAULT_WARMUP = 20
N_BOOTSTRAP_WF = 1_000  # lower than 10k since we re-run it per bet


# ----------------------------------------------------------------------------
# Walk-forward sizing functions
# ----------------------------------------------------------------------------


def kelly_from_history(pnl_history, entries_history, multiplier, use_ci_lower):
    """Compute the recommended Kelly fraction given only past data.

    Returns 0 if history is too short or edge is negative.
    """
    if len(pnl_history) < 5:
        return 0.0
    pnl = np.asarray(pnl_history, dtype=float)
    avg_entry = float(np.mean(entries_history))
    max_profit = max(100 - avg_entry, 1e-6)

    if use_ci_lower:
        rng = np.random.default_rng(0)
        means = bootstrap_mean_pnl(pd.Series(pnl), N_BOOTSTRAP_WF, rng)
        edge = float(np.percentile(means, 100 * ALPHA / 2))
    else:
        edge = float(pnl.mean())

    fraction = max(multiplier * edge / max_profit, 0.0)
    return min(fraction, 1.0)


def simulate_walk_forward(entries, outcomes, multiplier, use_ci_lower,
                          starting_bankroll, warmup):
    """Walk-forward simulation: at each bet, size using prior history only.

    Returns (equity_curve, fraction_curve, edge_estimate_curve).
    Length of all three is N+1 (equity) or N (fractions, edges) where N is the
    number of bets. Fractions are 0 during the warmup period.
    """
    bankroll = float(starting_bankroll)
    equity = [bankroll]
    fractions = []
    edge_estimates = []
    pnl_history = []
    entries_history = []

    for i, (c, won) in enumerate(zip(entries, outcomes)):
        pnl_realized = (100 - c) if won else -c

        if i < warmup:
            f = 0.0
            edge_estimates.append(np.nan)
        else:
            f = kelly_from_history(
                pnl_history, entries_history,
                multiplier=multiplier, use_ci_lower=use_ci_lower,
            )
            edge_estimates.append(np.mean(pnl_history) if pnl_history else np.nan)

        fractions.append(f)

        stake = f * bankroll
        if won:
            bankroll = bankroll + stake * (100 - c) / c
        else:
            bankroll = bankroll - stake
        bankroll = max(bankroll, 0.0)
        equity.append(bankroll)

        pnl_history.append(pnl_realized)
        entries_history.append(c)

    return np.array(equity), np.array(fractions), np.array(edge_estimates)


# ----------------------------------------------------------------------------
# Reporting + plotting
# ----------------------------------------------------------------------------


@dataclass
class WFResult:
    name: str
    equity: np.ndarray
    fractions: np.ndarray
    edges: np.ndarray
    final: float
    total_return_pct: float
    max_dd_pct: float
    sharpe: float
    pct_under: float
    avg_active_fraction: float  # mean fraction over non-warmup bets


def summarize(name, equity, fractions, edges, starting_bankroll):
    active = fractions[fractions > 0]
    final = float(equity[-1])
    return WFResult(
        name=name,
        equity=equity,
        fractions=fractions,
        edges=edges,
        final=final,
        total_return_pct=(final / starting_bankroll - 1) * 100,
        max_dd_pct=max_drawdown_pct(equity),
        sharpe=log_sharpe(equity),
        pct_under=pct_time_underwater(equity),
        avg_active_fraction=float(active.mean()) if len(active) else 0.0,
    )


def print_summary(results, sport, low, high, starting_bankroll, warmup):
    print(f"\n{'═'*82}")
    print(f"Walk-forward validation: {sport} favorites {low}-{high}¢")
    print(f"  Starting bankroll: ${starting_bankroll:,.0f}")
    print(f"  Warmup (no bets):  {warmup} games")
    print(f"{'═'*82}")
    print(f"{'Strategy':<28} {'Avg size':>10} {'Final $':>12} "
          f"{'Return':>9} {'Max DD':>9} {'Sharpe':>8}")
    print("─" * 82)
    for r in results:
        print(f"{r.name:<28} {r.avg_active_fraction*100:>8.1f}%  "
              f"${r.final:>10,.0f}  {r.total_return_pct:>+7.1f}%  "
              f"{r.max_dd_pct:>6.1f}%  {r.sharpe:>7.2f}")


def plot_validation(results, in_sample_equity, sport, low, high, warmup,
                    out_path):
    fig, (ax_eq, ax_frac, ax_edge) = plt.subplots(3, 1, figsize=(14, 13))

    palette = {
        "Walk-forward Quarter Kelly":  "#2CA02C",
        "Walk-forward CI-lower Kelly": "#1F77B4",
        "Walk-forward 5% cap":         "#9467BD",
    }

    # === Panel 1: Equity curves ===
    ax_eq.plot(in_sample_equity, color="#D62728", linestyle="--", linewidth=1.5,
               label=f"In-sample Quarter Kelly (uses future data) — "
                     f"final ${in_sample_equity[-1]:,.0f}")
    for r in results:
        color = palette.get(r.name, "#444")
        ax_eq.plot(
            r.equity, color=color, linewidth=2,
            label=f"{r.name} — final ${r.final:,.0f} (MDD {r.max_dd_pct:.0f}%)",
        )
    ax_eq.axvline(warmup, color="gray", linestyle=":", linewidth=1,
                  label=f"warmup ends (game {warmup})")
    ax_eq.axhline(in_sample_equity[0], color="black", linestyle="--",
                  linewidth=0.6, alpha=0.5)
    ax_eq.set_ylabel("Bankroll ($)")
    ax_eq.set_title(
        f"Walk-forward equity curves — {sport} {low}-{high}¢  "
        f"(if walk-forward tracks in-sample, the edge is real and stable)",
        fontsize=12,
    )
    ax_eq.legend(loc="upper left", fontsize=9)
    ax_eq.grid(alpha=0.3)

    # === Panel 2: Recommended Kelly fraction over time ===
    for r in results:
        color = palette.get(r.name, "#444")
        ax_frac.plot(r.fractions * 100, color=color, linewidth=2, label=r.name)
    ax_frac.axvline(warmup, color="gray", linestyle=":", linewidth=1)
    ax_frac.set_ylabel("Recommended bet size (% of bankroll)")
    ax_frac.set_title(
        "Evolving bet-size recommendation — stable line = strategy has settled; "
        "drift = still learning",
        fontsize=12,
    )
    ax_frac.legend(loc="upper right", fontsize=9)
    ax_frac.grid(alpha=0.3)

    # === Panel 3: Rolling edge estimate (mean PnL from history) ===
    # All walk-forward strategies share the same edge estimate; plot one.
    if results:
        primary = results[0]
        valid = ~np.isnan(primary.edges)
        ax_edge.plot(
            np.where(valid)[0], primary.edges[valid],
            color="#222", linewidth=2,
            label="Rolling mean PnL estimate (cents per bet)",
        )
    ax_edge.axhline(0, color="red", linestyle="--", linewidth=1, alpha=0.7,
                    label="break-even (EV = 0)")
    ax_edge.axvline(warmup, color="gray", linestyle=":", linewidth=1)
    ax_edge.set_xlabel("Bet index (chronological)")
    ax_edge.set_ylabel("Estimated edge (cents)")
    ax_edge.set_title(
        "Edge estimate over time — should stabilize as data accumulates",
        fontsize=12,
    )
    ax_edge.legend(loc="lower right", fontsize=9)
    ax_edge.grid(alpha=0.3)

    fig.tight_layout()
    fig.savefig(out_path, dpi=150, bbox_inches="tight")
    print(f"\nSaved figure: {out_path}")


# ----------------------------------------------------------------------------
# Fixed-size walk-forward (for the 5% cap baseline)
# ----------------------------------------------------------------------------


def simulate_fixed_fraction(entries, outcomes, fraction, starting_bankroll, warmup):
    """5% cap is sizing-rule-independent of history; still respect warmup."""
    bankroll = float(starting_bankroll)
    equity = [bankroll]
    fractions = []
    edges = []
    pnl_history = []

    for i, (c, won) in enumerate(zip(entries, outcomes)):
        if i < warmup:
            f = 0.0
            edges.append(np.nan)
        else:
            f = fraction
            edges.append(np.mean(pnl_history) if pnl_history else np.nan)
        fractions.append(f)

        stake = f * bankroll
        if won:
            bankroll = bankroll + stake * (100 - c) / c
        else:
            bankroll = bankroll - stake
        bankroll = max(bankroll, 0.0)
        equity.append(bankroll)
        pnl_history.append((100 - c) if won else -c)

    return np.array(equity), np.array(fractions), np.array(edges)


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------


def _parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--dataset", default=DEFAULT_DATASET)
    parser.add_argument("--out", default=DEFAULT_OUT)
    parser.add_argument("--sport", default=DEFAULT_SPORT)
    parser.add_argument("--low", type=int, default=DEFAULT_LOW)
    parser.add_argument("--high", type=int, default=DEFAULT_HIGH)
    parser.add_argument("--start", type=float, default=DEFAULT_START)
    parser.add_argument("--warmup", type=int, default=DEFAULT_WARMUP,
                        help="Number of initial games used to build history "
                             "before betting starts.")
    return parser.parse_args(argv)


def main(argv=None):
    args = _parse_args(argv)
    df = load_dataset(args.dataset)
    band = filter_strategy(df, args.low, args.high)
    band = band[band["Sport"] == args.sport].reset_index(drop=True)
    if band.empty:
        raise SystemExit(
            f"No {args.sport} games in {args.low}-{args.high}¢ band."
        )
    if len(band) <= args.warmup + 5:
        raise SystemExit(
            f"Only {len(band)} {args.sport} games available — need >{args.warmup + 5} "
            f"to do meaningful walk-forward (with --warmup {args.warmup})."
        )

    entries = band["Favorite_Avg_Ask_Cents"].astype(float).values
    outcomes = band["Favorite_Won"].astype(bool).values

    # In-sample reference: Quarter Kelly using FULL-SAMPLE edge (cheating).
    full_mean = band["Favorite_Hold_To_Settle_PnL_Cents"].mean()
    full_avg_entry = entries.mean()
    full_max_profit = max(100 - full_avg_entry, 1e-6)
    in_sample_quarter = max(0.25 * full_mean / full_max_profit, 0.0)
    in_sample_equity = simulate_bankroll(
        entries, outcomes, in_sample_quarter, args.start,
    )

    # Walk-forward strategies.
    runs = []
    eq, frac, edges = simulate_walk_forward(
        entries, outcomes, multiplier=0.25, use_ci_lower=False,
        starting_bankroll=args.start, warmup=args.warmup,
    )
    runs.append(summarize("Walk-forward Quarter Kelly", eq, frac, edges, args.start))

    eq, frac, edges = simulate_walk_forward(
        entries, outcomes, multiplier=1.0, use_ci_lower=True,
        starting_bankroll=args.start, warmup=args.warmup,
    )
    runs.append(summarize("Walk-forward CI-lower Kelly", eq, frac, edges, args.start))

    eq, frac, edges = simulate_fixed_fraction(
        entries, outcomes, 0.05, args.start, args.warmup,
    )
    runs.append(summarize("Walk-forward 5% cap", eq, frac, edges, args.start))

    # In-sample reference summary line.
    print(f"\nIn-sample Quarter Kelly (uses ALL data, including future):")
    print(f"  fraction = {in_sample_quarter*100:.1f}%/bet")
    print(f"  final    = ${in_sample_equity[-1]:,.0f}  "
          f"(return {(in_sample_equity[-1]/args.start - 1)*100:+.1f}%)")

    print_summary(runs, args.sport, args.low, args.high, args.start, args.warmup)

    # Interpretive headline.
    wf_quarter = runs[0]
    ratio = wf_quarter.final / in_sample_equity[-1] if in_sample_equity[-1] > 0 else 0
    print(f"\nWalk-forward Quarter Kelly captured "
          f"{ratio*100:.0f}% of in-sample return — "
          f"{'edge is robust' if ratio > 0.6 else 'edge may not survive out-of-sample'}.")

    plot_validation(runs, in_sample_equity, args.sport, args.low, args.high,
                    args.warmup, args.out)


if __name__ == "__main__":
    main()

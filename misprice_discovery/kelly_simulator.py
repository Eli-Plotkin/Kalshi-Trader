"""Simulate bankroll trajectory under various Kelly sizing strategies.

For a chosen sport + price band, replays your actual historical PnL sequence
through different bet-sizing rules. Answers: "if I'd bet X% of bankroll on
every favorite in [LOW, HIGH]¢ since the dataset begins, how would I have done?"

Sport and band default to `strategy_config.py` values (overridable via CLI).

Six sizing strategies compared by default:
  Full Kelly      f = mean_PnL / max_profit (point estimate)
  Half Kelly      0.5x full Kelly
  Quarter Kelly   0.25x full Kelly
  CI-lower Kelly  Kelly using lower bootstrap CI bound (robust)
  5% cap          fixed 5% of bankroll per bet
  1% cap          fixed 1% (very conservative control)

Per strategy we report: final bankroll, total return, max drawdown,
log-Sharpe ratio, and fraction of time underwater.

Run:
  python misprice_discovery/kelly_simulator.py
  python misprice_discovery/kelly_simulator.py --sport NFL --low 90 --high 99
  python misprice_discovery/kelly_simulator.py --start 10000   # custom bankroll
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from analyze_multisport import (
    ALPHA,
    N_BOOTSTRAP,
    bootstrap_mean_pnl,
    filter_strategy,
    load_dataset,
)

DEFAULT_DATASET = "kalshi_multisport_research_dataset.csv"
DEFAULT_OUT = "misprice_discovery/kelly_bankroll_simulation.png"
import os

import strategy_config

DEFAULT_SPORT = os.getenv("STRATEGY_SPORT", "NBA")
DEFAULT_LOW = strategy_config.PRICE_MIN
DEFAULT_HIGH = strategy_config.PRICE_MAX
DEFAULT_START = 1_000.0  # dollars

# Plot palette.
STRATEGY_COLORS = {
    "Full Kelly":     "#D62728",  # red — most aggressive
    "Half Kelly":     "#FF7F0E",
    "Quarter Kelly":  "#2CA02C",
    "CI-lower Kelly": "#1F77B4",
    "5% cap":         "#9467BD",
    "1% cap":         "#7F7F7F",
}


# ----------------------------------------------------------------------------
# Pure simulation primitives — tested independently
# ----------------------------------------------------------------------------


@dataclass
class SimResult:
    name: str
    fraction: float
    equity: np.ndarray         # length N+1, starts at starting_bankroll
    final: float
    total_return_pct: float
    max_drawdown_pct: float
    log_sharpe: float
    pct_time_underwater: float


def simulate_bankroll(entries_cents, outcomes, fraction, starting_bankroll):
    """Replay bets in order. At each bet:
        stake = fraction * current_bankroll      (dollars at risk)
        shares = stake / (entry / 100)           (Kalshi quotes in cents)
        if win:  bankroll += stake * (100-entry)/entry
        if lose: bankroll -= stake

    Returns the equity curve as a 1-D array of length N+1 (includes start).
    """
    bankroll = float(starting_bankroll)
    equity = [bankroll]
    for c, won in zip(entries_cents, outcomes):
        stake = fraction * bankroll
        if won:
            bankroll = bankroll + stake * (100 - c) / c
        else:
            bankroll = bankroll - stake
        # Bankroll can't go below 0 under fraction <= 1; floor for safety.
        bankroll = max(bankroll, 0.0)
        equity.append(bankroll)
    return np.array(equity)


def max_drawdown_pct(equity):
    """Peak-to-trough drawdown as a positive percentage."""
    if len(equity) < 2:
        return 0.0
    running_peak = np.maximum.accumulate(equity)
    drawdowns = (running_peak - equity) / running_peak
    return float(drawdowns.max() * 100.0)


def log_sharpe(equity):
    """Sharpe-like ratio on per-bet log returns. Annualization is omitted —
    bets aren't time-uniform; relative ranking across strategies is what matters."""
    if len(equity) < 3:
        return 0.0
    # Drop any zeros that would blow up log.
    eq = np.where(equity > 0, equity, np.nan)
    log_returns = np.diff(np.log(eq))
    log_returns = log_returns[~np.isnan(log_returns)]
    if len(log_returns) < 2 or log_returns.std(ddof=1) == 0:
        return 0.0
    return float(log_returns.mean() / log_returns.std(ddof=1) * np.sqrt(len(log_returns)))


def pct_time_underwater(equity):
    """Percentage of bets where the bankroll was below its running peak."""
    if len(equity) < 2:
        return 0.0
    running_peak = np.maximum.accumulate(equity)
    underwater = equity < running_peak
    return float(underwater.mean() * 100.0)


def evaluate(name, fraction, entries, outcomes, starting_bankroll):
    """Run a single strategy and package its stats."""
    equity = simulate_bankroll(entries, outcomes, fraction, starting_bankroll)
    final = float(equity[-1])
    return SimResult(
        name=name,
        fraction=fraction,
        equity=equity,
        final=final,
        total_return_pct=(final / starting_bankroll - 1) * 100,
        max_drawdown_pct=max_drawdown_pct(equity),
        log_sharpe=log_sharpe(equity),
        pct_time_underwater=pct_time_underwater(equity),
    )


# ----------------------------------------------------------------------------
# Kelly fraction derivation from the historical sample
# ----------------------------------------------------------------------------


def derive_kelly_fractions(pnl_series, entries_series, rng):
    """Compute full-Kelly + CI-lower Kelly from the observed PnL distribution.

    Kelly fraction for a binary contract: f = mean_PnL / max_profit_per_share.
    We use the *band-average* max profit (100 - avg_entry) so a single fixed
    fraction applies across all bets in the band.
    """
    mean_pnl = pnl_series.mean()
    avg_entry = entries_series.mean()
    max_profit = max(100 - avg_entry, 1e-6)

    boot_means = bootstrap_mean_pnl(pnl_series, N_BOOTSTRAP, rng)
    ci_low = np.percentile(boot_means, 100 * ALPHA / 2)

    full = max(mean_pnl / max_profit, 0.0)
    ci_lower = max(ci_low / max_profit, 0.0)
    return {
        "mean_pnl": float(mean_pnl),
        "avg_entry": float(avg_entry),
        "max_profit": float(max_profit),
        "full_kelly": float(min(full, 1.0)),
        "ci_lower_kelly": float(min(ci_lower, 1.0)),
        "ci_low_pnl": float(ci_low),
    }


def make_strategies(kelly_info):
    """Return list of (name, fraction) pairs in display order."""
    full = kelly_info["full_kelly"]
    ci = kelly_info["ci_lower_kelly"]
    return [
        ("Full Kelly", full),
        ("Half Kelly", full * 0.5),
        ("Quarter Kelly", full * 0.25),
        ("CI-lower Kelly", ci),
        ("5% cap", 0.05),
        ("1% cap", 0.01),
    ]


# ----------------------------------------------------------------------------
# Reporting
# ----------------------------------------------------------------------------


def print_summary(kelly_info, results, starting_bankroll, sport, low, high):
    print(f"\n{'═'*78}")
    print(f"Kelly simulation: {sport} favorites {low}-{high}¢")
    print(f"{'═'*78}")
    print(f"Mean PnL per bet:       {kelly_info['mean_pnl']:+.2f}¢")
    print(f"Average entry price:    {kelly_info['avg_entry']:.2f}¢")
    print(f"Max profit per share:   {kelly_info['max_profit']:.2f}¢")
    print(f"Full-Kelly fraction:    {kelly_info['full_kelly']*100:.1f}% of bankroll/bet")
    print(f"CI-lower Kelly:         {kelly_info['ci_lower_kelly']*100:.1f}% of bankroll/bet")
    print(f"Starting bankroll:      ${starting_bankroll:,.0f}")
    print(f"Trades simulated:       {len(results[0].equity) - 1}")
    print()
    print(f"{'Strategy':<16} {'Bet size':>9} {'Final $':>12} "
          f"{'Return':>9} {'Max DD':>9} {'Sharpe':>8} {'%Under':>8}")
    print("─" * 78)
    for r in results:
        print(f"{r.name:<16} {r.fraction*100:>7.1f}%  ${r.final:>10,.0f}  "
              f"{r.total_return_pct:>+7.1f}%  {r.max_drawdown_pct:>6.1f}%  "
              f"{r.log_sharpe:>7.2f}  {r.pct_time_underwater:>6.1f}%")


def plot_simulation(results, kelly_info, starting_bankroll, sport, low, high, out_path):
    fig, (ax_log, ax_lin) = plt.subplots(2, 1, figsize=(14, 11))

    for r in results:
        color = STRATEGY_COLORS.get(r.name, "#000")
        # Truncate the log axis at $1 to keep "bust" strategies visible.
        equity_for_log = np.where(r.equity > 0, r.equity, 1.0)
        label = (
            f"{r.name}  ({r.fraction*100:.1f}%/bet)  "
            f"final ${r.final:,.0f}  MDD {r.max_drawdown_pct:.0f}%"
        )
        ax_log.plot(equity_for_log, color=color, linewidth=2, label=label)
        ax_lin.plot(r.equity, color=color, linewidth=1.5, label=r.name)

    for ax in (ax_log, ax_lin):
        ax.axhline(starting_bankroll, color="black", linestyle="--",
                   linewidth=0.8, alpha=0.5, label="starting bankroll")
        ax.set_xlabel("Bet index (chronological)")
        ax.grid(True, alpha=0.3)

    ax_log.set_yscale("log")
    ax_log.set_ylabel("Bankroll ($, log scale)")
    ax_log.set_title(
        f"Bankroll trajectory under each Kelly sizing — "
        f"{sport} favorites {low}-{high}¢ "
        f"(N={len(results[0].equity)-1}, full Kelly = "
        f"{kelly_info['full_kelly']*100:.1f}%/bet)",
        fontsize=12,
    )
    ax_log.legend(loc="upper left", fontsize=8)

    ax_lin.set_ylabel("Bankroll ($, linear)")
    ax_lin.set_title(
        "Same trajectories on linear scale — drawdowns become visceral here",
        fontsize=12,
    )
    ax_lin.legend(loc="upper left", fontsize=9)

    fig.tight_layout()
    fig.savefig(out_path, dpi=150, bbox_inches="tight")
    print(f"\nSaved figure: {out_path}")


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------


def _parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--dataset", default=DEFAULT_DATASET)
    parser.add_argument("--out", default=DEFAULT_OUT)
    parser.add_argument("--sport", default=DEFAULT_SPORT,
                        help=f"Sport to simulate (default {DEFAULT_SPORT}).")
    parser.add_argument("--low", type=int, default=DEFAULT_LOW)
    parser.add_argument("--high", type=int, default=DEFAULT_HIGH)
    parser.add_argument("--start", type=float, default=DEFAULT_START,
                        help=f"Starting bankroll in dollars (default {DEFAULT_START:.0f}).")
    return parser.parse_args(argv)


def main(argv=None):
    args = _parse_args(argv)
    df = load_dataset(args.dataset)
    band = filter_strategy(df, args.low, args.high)
    band = band[band["Sport"] == args.sport].reset_index(drop=True)
    if band.empty:
        raise SystemExit(
            f"No {args.sport} games in {args.low}-{args.high}¢ band. "
            f"Available counts:\n{df.groupby('Sport').size().to_string()}"
        )

    entries = band["Favorite_Avg_Ask_Cents"].astype(float).values
    outcomes = band["Favorite_Won"].astype(bool).values
    pnl = band["Favorite_Hold_To_Settle_PnL_Cents"]

    rng = np.random.default_rng(42)
    kelly_info = derive_kelly_fractions(pnl, band["Favorite_Avg_Ask_Cents"], rng)
    strategies = make_strategies(kelly_info)
    results = [evaluate(name, frac, entries, outcomes, args.start)
               for name, frac in strategies]

    print_summary(kelly_info, results, args.start, args.sport, args.low, args.high)
    plot_simulation(results, kelly_info, args.start, args.sport, args.low, args.high,
                    args.out)


if __name__ == "__main__":
    main()

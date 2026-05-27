"""Friction-adjusted simulation: does the edge survive real-world costs?

Applies two friction sources to every bet:

  slippage    extra cents paid above the dataset's 15-min average ask
              (the dataset uses an average, real execution hits the
              prevailing ask which is usually 1-2¢ higher)

  fee         cents deducted per round-trip trade
              (Kalshi's fee structure varies; 0.5-1¢ is a reasonable estimate)

The simulator sweeps a range of friction levels and reports the break-even
point — the friction at which the edge dies.

Strategy under test: walk-forward fixed-cap sizing on favorites in the
configured price band. Defaults come from `strategy_config.py`.

Run:
  python misprice_discovery/friction_simulator.py
  python misprice_discovery/friction_simulator.py --sport NBA --low 90 --high 99
  python misprice_discovery/friction_simulator.py --max-friction 4 --steps 9
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from analyze_multisport import filter_strategy, load_dataset
from kelly_simulator import log_sharpe, max_drawdown_pct, pct_time_underwater

DEFAULT_DATASET = "kalshi_multisport_research_dataset.csv"
DEFAULT_OUT = "misprice_discovery/friction_simulator.png"
import os

import strategy_config

DEFAULT_SPORT = os.getenv("STRATEGY_SPORT", "NBA")
DEFAULT_LOW = strategy_config.PRICE_MIN
DEFAULT_HIGH = strategy_config.PRICE_MAX
DEFAULT_START = 1_000.0
DEFAULT_FRACTION = 0.05      # 5% cap — the walk-forward winner
DEFAULT_WARMUP = 20
DEFAULT_MAX_FRICTION = 3.0   # cents — sweep from 0 to this
DEFAULT_STEPS = 7


# ----------------------------------------------------------------------------
# Pure friction math — tested independently
# ----------------------------------------------------------------------------


def apply_friction(entries, outcomes, slippage_cents, fee_cents):
    """Return friction-adjusted (effective_entries, pnl_per_share).

    effective_entry = original_entry + slippage     (you paid more than the dataset says)
    won  : pnl = (100 - effective_entry) - fee
    lost : pnl = -effective_entry - fee
    """
    entries = np.asarray(entries, dtype=float)
    outcomes = np.asarray(outcomes, dtype=bool)
    effective_entries = entries + slippage_cents
    pnl = np.where(
        outcomes,
        (100.0 - effective_entries) - fee_cents,
        -effective_entries - fee_cents,
    )
    return effective_entries, pnl


def simulate_with_friction(entries, outcomes, fraction, slippage, fee,
                           starting_bankroll, warmup):
    """Replay bets at fixed fractional sizing with friction applied.

    Returns the equity curve (length N+1).
    """
    effective_entries, _ = apply_friction(entries, outcomes, slippage, fee)
    bankroll = float(starting_bankroll)
    equity = [bankroll]

    for i, (c_eff, won) in enumerate(zip(effective_entries, outcomes)):
        if i < warmup:
            equity.append(bankroll)
            continue
        # Stake sized by effective price (you actually paid c_eff cents/share).
        stake = fraction * bankroll
        if c_eff <= 0:
            # Pathological floor: skip rather than divide by zero. Real friction
            # never makes the entry negative.
            equity.append(bankroll)
            continue
        # c_eff > 100 is allowed — that's the model of "friction so brutal that
        # even a win is a net loss" and the math handles it correctly via
        # (100 - c_eff) being negative.
        if won:
            bankroll = bankroll + stake * (100 - c_eff) / c_eff - fee * (stake / c_eff)
        else:
            bankroll = bankroll - stake - fee * (stake / c_eff)
        bankroll = max(bankroll, 0.0)
        equity.append(bankroll)

    return np.array(equity)


# ----------------------------------------------------------------------------
# Sweep across friction levels
# ----------------------------------------------------------------------------


@dataclass
class FrictionPoint:
    total_friction: float       # slippage + fee, cents
    slippage: float
    fee: float
    final: float
    return_pct: float
    max_dd_pct: float
    sharpe: float
    profitable: bool


def sweep_friction(entries, outcomes, fraction, max_friction, steps,
                   starting_bankroll, warmup, fee_share=0.0):
    """Sweep total friction from 0 to max_friction in `steps` linear steps.

    `fee_share` ∈ [0, 1] controls how much of the total friction is modeled as
    a per-trade fee (cents subtracted from PnL) vs as slippage (cents added to
    entry price). Default 0 = all slippage, which is the more impactful path
    for high-priced favorites.
    """
    levels = np.linspace(0, max_friction, steps)
    results = []
    for total in levels:
        fee = total * fee_share
        slippage = total - fee
        equity = simulate_with_friction(
            entries, outcomes, fraction, slippage, fee,
            starting_bankroll, warmup,
        )
        final = float(equity[-1])
        results.append(FrictionPoint(
            total_friction=float(total),
            slippage=float(slippage),
            fee=float(fee),
            final=final,
            return_pct=(final / starting_bankroll - 1) * 100,
            max_dd_pct=max_drawdown_pct(equity),
            sharpe=log_sharpe(equity),
            profitable=final > starting_bankroll,
        ))
    return results


def find_breakeven(points):
    """Linear interpolation between the last profitable and first unprofitable
    friction levels. Returns None if all levels are profitable or all are not.
    """
    profitable = [p for p in points if p.profitable]
    unprofitable = [p for p in points if not p.profitable]
    if not profitable or not unprofitable:
        return None
    last_good = max(profitable, key=lambda p: p.total_friction)
    first_bad = min(
        (p for p in unprofitable if p.total_friction > last_good.total_friction),
        key=lambda p: p.total_friction,
        default=None,
    )
    if first_bad is None:
        return None
    # Linear interp in friction-vs-return space; return crosses 0 at break-even.
    r1, r2 = last_good.return_pct, first_bad.return_pct
    f1, f2 = last_good.total_friction, first_bad.total_friction
    if r1 == r2:
        return (f1 + f2) / 2
    t = r1 / (r1 - r2)  # fraction of distance from f1 to f2 where return hits 0
    return f1 + t * (f2 - f1)


# ----------------------------------------------------------------------------
# Reporting + plots
# ----------------------------------------------------------------------------


def print_summary(points, breakeven, sport, low, high, fraction, starting_bankroll):
    print(f"\n{'═'*78}")
    print(f"Friction sweep: {sport} favorites {low}-{high}¢ "
          f"at {fraction*100:.0f}% cap sizing")
    print(f"{'═'*78}")
    print(f"{'Total':>7} {'Slippage':>10} {'Fee':>6} {'Final $':>11} "
          f"{'Return':>9} {'Max DD':>8} {'Sharpe':>8} {'Verdict':>14}")
    print("─" * 78)
    for p in points:
        verdict = "✓ profitable" if p.profitable else "✗ losing"
        print(f"{p.total_friction:>6.1f}¢ {p.slippage:>9.1f}¢ {p.fee:>5.1f}¢ "
              f"${p.final:>9,.0f}  {p.return_pct:>+7.1f}%  "
              f"{p.max_dd_pct:>6.1f}%  {p.sharpe:>7.2f}  {verdict:>14}")

    print()
    if breakeven is not None:
        print(f"Break-even friction: ~{breakeven:.2f}¢ per share")
        print(f"  → Edge survives if real-world friction stays below this level.")
    elif all(p.profitable for p in points):
        print(f"Strategy stays profitable across the entire {points[-1].total_friction:.0f}¢ "
              f"sweep — break-even is beyond it.")
    else:
        print("Strategy never profitable in this sweep (edge dies even at 0 friction).")


def plot_friction(points, equity_curves_by_friction, sport, low, high,
                  fraction, starting_bankroll, breakeven, out_path):
    fig, (ax_equity, ax_sweep) = plt.subplots(2, 1, figsize=(14, 11))

    cmap = plt.cm.RdYlGn_r
    max_f = max(p.total_friction for p in points) or 1.0
    for friction, equity in equity_curves_by_friction:
        color = cmap(friction / max_f)
        ax_equity.plot(
            equity, color=color, linewidth=2,
            label=f"{friction:.1f}¢ friction — final ${equity[-1]:,.0f}",
        )
    ax_equity.axhline(starting_bankroll, color="black", linestyle="--",
                      linewidth=0.8, alpha=0.5)
    ax_equity.set_xlabel("Bet index (chronological)")
    ax_equity.set_ylabel("Bankroll ($)")
    ax_equity.set_title(
        f"Equity curves at increasing friction — {sport} {low}-{high}¢ at "
        f"{fraction*100:.0f}% cap "
        f"(green = low friction, red = high; flat = edge gone)",
        fontsize=12,
    )
    ax_equity.legend(loc="upper left", fontsize=9)
    ax_equity.grid(alpha=0.3)

    frictions = [p.total_friction for p in points]
    returns = [p.return_pct for p in points]
    colors = ["#2CA02C" if p.profitable else "#D62728" for p in points]
    ax_sweep.plot(frictions, returns, color="#222", linewidth=2, alpha=0.4, zorder=1)
    ax_sweep.scatter(frictions, returns, c=colors, s=80, zorder=2,
                     edgecolor="black", linewidth=1)
    ax_sweep.axhline(0, color="red", linestyle="--", linewidth=1,
                     label="break-even (return = 0%)")
    if breakeven is not None:
        ax_sweep.axvline(breakeven, color="orange", linestyle=":", linewidth=2,
                         label=f"break-even friction ≈ {breakeven:.2f}¢")
    ax_sweep.set_xlabel("Total friction per share (cents)")
    ax_sweep.set_ylabel("Total return %")
    ax_sweep.set_title(
        "Friction sweep — at what cost level does the edge die?",
        fontsize=12,
    )
    ax_sweep.legend(loc="upper right", fontsize=10)
    ax_sweep.grid(alpha=0.3)

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
    parser.add_argument("--sport", default=DEFAULT_SPORT)
    parser.add_argument("--low", type=int, default=DEFAULT_LOW)
    parser.add_argument("--high", type=int, default=DEFAULT_HIGH)
    parser.add_argument("--start", type=float, default=DEFAULT_START)
    parser.add_argument("--fraction", type=float, default=DEFAULT_FRACTION,
                        help=f"Bet-size cap (default {DEFAULT_FRACTION}).")
    parser.add_argument("--warmup", type=int, default=DEFAULT_WARMUP)
    parser.add_argument("--max-friction", type=float, default=DEFAULT_MAX_FRICTION,
                        help=f"Top of friction sweep in cents "
                             f"(default {DEFAULT_MAX_FRICTION}).")
    parser.add_argument("--steps", type=int, default=DEFAULT_STEPS,
                        help=f"Friction levels to evaluate "
                             f"(default {DEFAULT_STEPS}).")
    parser.add_argument("--fee-share", type=float, default=0.0,
                        help="Fraction of total friction modeled as per-trade fee "
                             "vs slippage (default 0 = all slippage).")
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

    entries = band["Favorite_Avg_Ask_Cents"].astype(float).values
    outcomes = band["Favorite_Won"].astype(bool).values

    points = sweep_friction(
        entries, outcomes,
        fraction=args.fraction,
        max_friction=args.max_friction,
        steps=args.steps,
        starting_bankroll=args.start,
        warmup=args.warmup,
        fee_share=args.fee_share,
    )

    # Equity curves at every friction level (for the top panel).
    equity_curves = []
    for p in points:
        eq = simulate_with_friction(
            entries, outcomes, args.fraction, p.slippage, p.fee,
            args.start, args.warmup,
        )
        equity_curves.append((p.total_friction, eq))

    breakeven = find_breakeven(points)
    print_summary(points, breakeven, args.sport, args.low, args.high,
                  args.fraction, args.start)
    plot_friction(points, equity_curves, args.sport, args.low, args.high,
                  args.fraction, args.start, breakeven, args.out)


if __name__ == "__main__":
    main()

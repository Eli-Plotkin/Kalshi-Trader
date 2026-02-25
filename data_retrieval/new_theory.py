import os
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt

# ================= CONFIG =================
_DIR = os.path.dirname(os.path.abspath(__file__))
CSV_PATH = os.path.join(_DIR, "kalshi_nba_arbitrage_data.csv")

FEE = 0.02            # Kalshi 2% fee on winnings
N_BOOT = 5000         # bootstrap iterations for CIs
STARTING_BANKROLL = 1000.0
MAX_KELLY = 0.05      # cap at 5% of bankroll per bet
N_SIM = 1000          # Monte Carlo seasons

CASHOUT_TARGETS = [0.25, 0.50, 0.75, 1.0, 1.5, 2.0]

PRICE_BUCKETS = [
    (10, 20, "10–20¢"),
    (20, 25, "20–25¢"),
    (25, 30, "25–30¢"),
    (30, 35, "30–35¢"),
    (35, 40, "35–40¢"),
    (40, 45, "40–45¢"),
    (45, 55, "45–55¢"),
]


# ================= PER-GAME RETURN CALCULATORS =================

def returns_hold_underdog(df):
    """Return per dollar invested: buy underdog YES, hold to settlement."""
    P = df['Und_Start_Price'].values / 100.0   # as fraction (0–1)
    won = (df['Winner'].str.lower().str.strip() == 'underdog').values
    # If win: profit = (1-P)/P * (1-fee) ; if lose: -1
    return np.where(won, (1 - P) / P * (1 - FEE), -1.0)


def returns_hold_favorite(df):
    """Return per dollar invested: buy favorite YES, hold to settlement.
    Approximate fav price = 1 - und_price."""
    und_P = df['Und_Start_Price'].values / 100.0
    fav_P = 1.0 - und_P
    won = (df['Winner'].str.lower().str.strip() == 'favorite').values
    return np.where(won, und_P / fav_P * (1 - FEE), -1.0)


def returns_cashout(df, target):
    """Return per dollar invested: buy underdog YES, sell when price hits +target%.
    If price never reaches target, you're left holding a loser."""
    P = df['Und_Start_Price'].values / 100.0
    best = df['Und_Best_Price'].values / 100.0
    hit = best >= P * (1 + target)
    return np.where(hit, target * (1 - FEE), -1.0)


# ================= HELPERS =================

def bootstrap_ci(rets, n_boot=N_BOOT, alpha=0.05):
    means = np.array([np.mean(np.random.choice(rets, len(rets))) for _ in range(n_boot)])
    return float(np.percentile(means, alpha / 2 * 100)), float(np.percentile(means, (1 - alpha / 2) * 100))


def kelly_fraction(win_rate, payout_b):
    """Half-Kelly, capped. payout_b = profit per $1 if win."""
    if payout_b <= 0 or win_rate <= 0:
        return 0.0
    f = (payout_b * win_rate - (1 - win_rate)) / payout_b
    return min(max(f, 0.0) * 0.5, MAX_KELLY)


def simulate_kelly_season(rets, win_flags, payout_b, n_games_per_season=200):
    """Simulate one season sampling n_games_per_season games from the dataset."""
    win_rate = np.mean(win_flags)
    f = kelly_fraction(win_rate, payout_b)
    bankroll = STARTING_BANKROLL
    idx = np.random.choice(len(rets), size=n_games_per_season, replace=True)
    for i in idx:
        if bankroll < 1:
            break
        stake = bankroll * f
        bankroll += stake * rets[i]
    return bankroll


def simulate_flat_season(rets, bet_size, n_games_per_season=200):
    """Simulate one season with flat bet sizing."""
    bankroll = STARTING_BANKROLL
    idx = np.random.choice(len(rets), size=n_games_per_season, replace=True)
    for i in idx:
        bankroll += bet_size * rets[i]
        if bankroll <= 0:
            return 0.0
    return bankroll


# ================= ANALYSIS =================

def analyze_strategy(label, rets):
    """Given an array of per-dollar returns, compute key stats."""
    ev = float(np.mean(rets))
    lo, hi = bootstrap_ci(rets)
    win_rate = float(np.mean(rets > 0))
    n = len(rets)
    verdict = "WINNING" if lo > 0 else ("UNCERTAIN" if hi > 0 else "LOSING")
    return dict(label=label, n=n, ev=ev, ci_lo=lo, ci_hi=hi, win_rate=win_rate, verdict=verdict)


def run_analysis():
    print("=" * 75)
    print("  NBA KALSHI STRATEGY ANALYSIS — EXHAUSTIVE BACKTEST")
    print("=" * 75)

    if not os.path.exists(CSV_PATH):
        print(f"ERROR: {CSV_PATH} not found. Run get_kalshi_nba_data.py first.")
        return

    df = pd.read_csv(CSV_PATH)
    df['Und_Start_Price'] = pd.to_numeric(df['Und_Start_Price'], errors='coerce')
    df['Und_Best_Price'] = pd.to_numeric(df['Und_Best_Price'], errors='coerce')
    df = df.dropna(subset=['Und_Start_Price', 'Und_Best_Price']).reset_index(drop=True)
    df = df[df['Und_Start_Price'] > 0].reset_index(drop=True)

    print(f"\nLoaded {len(df)} games  |  "
          f"Underdog wins: {(df['Winner'].str.lower().str.strip()=='underdog').sum()} "
          f"({(df['Winner'].str.lower().str.strip()=='underdog').mean():.1%})\n")

    # ================================================================
    # SECTION 1: PRICE BUCKET WIN RATE vs IMPLIED
    # ================================================================
    print("─" * 75)
    print("  SECTION 1: UNDERDOG WIN RATE vs KALSHI IMPLIED PROBABILITY")
    print("─" * 75)
    print(f"  {'Bucket':>8} | {'N':>4} | {'Act Win%':>8} | {'Implied%':>8} | {'Edge':>7} | {'EV/$ Hold':>10}")
    print("  " + "-" * 65)

    bucket_results = []
    for lo_p, hi_p, label in PRICE_BUCKETS:
        sub = df[(df['Und_Start_Price'] >= lo_p) & (df['Und_Start_Price'] < hi_p)]
        if len(sub) < 5:
            continue
        rets = returns_hold_underdog(sub)
        avg_P = sub['Und_Start_Price'].mean() / 100.0
        act_wr = float((sub['Winner'].str.lower().str.strip() == 'underdog').mean())
        implied = avg_P
        edge = act_wr - implied
        ev = float(np.mean(rets))
        ci_lo, ci_hi = bootstrap_ci(rets)
        verdict = "WINNING" if ci_lo > 0 else ("UNCERTAIN" if ci_hi > 0 else "LOSING")
        bucket_results.append(dict(
            label=label, lo_p=lo_p, hi_p=hi_p, n=len(sub),
            act_wr=act_wr, implied=implied, edge=edge,
            ev=ev, ci_lo=ci_lo, ci_hi=ci_hi, verdict=verdict,
            rets=rets,
        ))
        flag = " <<<" if ci_lo > 0 else (" ?" if ci_hi > 0 else "")
        print(f"  {label:>8} | {len(sub):>4} | {act_wr:>7.1%} | {implied:>7.1%} | "
              f"{edge:>+6.1%} | {ev:>+9.3f}{flag}")

    # ================================================================
    # SECTION 2: BUY FAVORITE — HOLD TO SETTLEMENT
    # ================================================================
    print("\n" + "─" * 75)
    print("  SECTION 2: BUY FAVORITE YES — HOLD TO SETTLEMENT")
    print("─" * 75)
    print(f"  {'Bucket':>8} | {'N':>4} | {'Act Win%':>8} | {'Implied%':>8} | {'Edge':>7} | {'EV/$ Hold':>10}")
    print("  " + "-" * 65)

    fav_bucket_results = []
    for lo_p, hi_p, label in PRICE_BUCKETS:
        sub = df[(df['Und_Start_Price'] >= lo_p) & (df['Und_Start_Price'] < hi_p)]
        if len(sub) < 5:
            continue
        rets = returns_hold_favorite(sub)
        avg_und = sub['Und_Start_Price'].mean() / 100.0
        fav_implied = 1.0 - avg_und
        act_wr = float((sub['Winner'].str.lower().str.strip() == 'favorite').mean())
        edge = act_wr - fav_implied
        ev = float(np.mean(rets))
        ci_lo, ci_hi = bootstrap_ci(rets)
        verdict = "WINNING" if ci_lo > 0 else ("UNCERTAIN" if ci_hi > 0 else "LOSING")
        fav_bucket_results.append(dict(
            label=label, n=len(sub), act_wr=act_wr, fav_implied=fav_implied,
            edge=edge, ev=ev, ci_lo=ci_lo, ci_hi=ci_hi, verdict=verdict, rets=rets,
        ))
        flag = " <<<" if ci_lo > 0 else (" ?" if ci_hi > 0 else "")
        print(f"  {label:>8} | {len(sub):>4} | {act_wr:>7.1%} | {fav_implied:>7.1%} | "
              f"{edge:>+6.1%} | {ev:>+9.3f}{flag}")

    # ================================================================
    # SECTION 3: CASHOUT STRATEGIES — FULL DATASET + KEY BUCKETS
    # ================================================================
    print("\n" + "─" * 75)
    print("  SECTION 3: CASHOUT STRATEGIES (buy underdog, sell at +X% swing)")
    print("  Break-even win rate = 1 / (X*0.98 + 1)")
    print("─" * 75)

    sub_ranges = [
        ("ALL prices", df),
        ("15–25¢", df[(df['Und_Start_Price'] >= 15) & (df['Und_Start_Price'] < 25)]),
        ("25–35¢", df[(df['Und_Start_Price'] >= 25) & (df['Und_Start_Price'] < 35)]),
    ]

    print(f"  {'Range':>12} | {'Target':>7} | {'Hit%':>6} | {'Break-evn':>9} | {'EV/$':>7} | {'95% CI':>20} | Verdict")
    print("  " + "-" * 80)

    cashout_results = []
    for range_label, sub in sub_ranges:
        if len(sub) < 10:
            continue
        for target in CASHOUT_TARGETS:
            rets = returns_cashout(sub, target)
            hit_rate = float(np.mean(rets > 0))
            breakeven = 1.0 / (target * (1 - FEE) + 1)
            ev = float(np.mean(rets))
            ci_lo, ci_hi = bootstrap_ci(rets)
            verdict = "WINNING" if ci_lo > 0 else ("UNCERTAIN" if ci_hi > 0 else "LOSING")
            flag = " <<<" if ci_lo > 0 else (" ?" if ci_hi > 0 else "")
            print(f"  {range_label:>12} | {int(target*100):>6}% | {hit_rate:>5.1%} | "
                  f"{breakeven:>8.1%} | {ev:>+6.3f} | "
                  f"[{ci_lo:>+6.3f}, {ci_hi:>+6.3f}] | {verdict}{flag}")
            cashout_results.append(dict(
                range_label=range_label, target=target, hit_rate=hit_rate,
                breakeven=breakeven, ev=ev, ci_lo=ci_lo, ci_hi=ci_hi,
                verdict=verdict, rets=rets, n=len(sub),
            ))
        print()

    # ================================================================
    # SECTION 4: BEST STRATEGY SIMULATION
    # ================================================================
    print("─" * 75)
    print("  SECTION 4: BEST STRATEGY — MONTE CARLO SIMULATION")
    print("─" * 75)

    # Gather all strategies with positive lower CI
    candidates = []
    for r in bucket_results:
        if r['ci_lo'] > 0:
            candidates.append(("Hold Und " + r['label'], r['rets'], r['ev']))
    for r in fav_bucket_results:
        if r['ci_lo'] > 0:
            candidates.append(("Hold Fav " + r['label'], r['rets'], r['ev']))
    for r in cashout_results:
        if r['ci_lo'] > 0:
            candidates.append((f"Cashout {int(r['target']*100)}% {r['range_label']}", r['rets'], r['ev']))

    # Also include best UNCERTAIN strategies (highest EV with ci_hi > 0)
    uncertain = []
    for r in bucket_results:
        if r['ci_hi'] > 0 and r['ci_lo'] <= 0:
            uncertain.append(("Hold Und " + r['label'], r['rets'], r['ev']))
    for r in fav_bucket_results:
        if r['ci_hi'] > 0 and r['ci_lo'] <= 0:
            uncertain.append(("Hold Fav " + r['label'], r['rets'], r['ev']))

    if not candidates:
        print("\n  No strategy with positive 95% CI lower bound found.")
        if uncertain:
            print("  Best UNCERTAIN strategies:")
            for label, rets, ev in sorted(uncertain, key=lambda x: -x[2])[:3]:
                print(f"    {label}: EV/$ = {ev:+.3f}")
        # Still simulate the best uncertain strategy
        if uncertain:
            candidates = [sorted(uncertain, key=lambda x: -x[2])[0]]
            candidates[0] = (candidates[0][0] + " [UNCERTAIN]", candidates[0][1], candidates[0][2])

    if not candidates:
        print("  Nothing to simulate.")
        return

    # Pick top 3 by EV
    candidates.sort(key=lambda x: -x[2])
    simulate_set = candidates[:3]

    graph_data = []
    for label, rets, ev in simulate_set:
        win_flags = (rets > 0).astype(int)
        # Typical payout for Kelly sizing: use median win return
        win_rets = rets[rets > 0]
        payout_b = float(np.median(win_rets)) if len(win_rets) > 0 else 1.0
        n_games = len(rets)

        flat_bet = min(20.0, STARTING_BANKROLL * 0.02)

        kelly_finals, flat_finals = [], []
        sample_curves = []
        for sim in range(N_SIM):
            kelly_finals.append(simulate_kelly_season(rets, win_flags, payout_b, n_games))
            flat_finals.append(simulate_flat_season(rets, flat_bet, n_games))
            if sim < 50:
                # Collect one equity curve for graphing
                bankroll = STARTING_BANKROLL
                f = kelly_fraction(np.mean(win_flags), payout_b)
                curve = [bankroll]
                idx = np.random.choice(len(rets), size=n_games, replace=True)
                for i in idx:
                    if bankroll < 1:
                        break
                    bankroll += bankroll * f * rets[i]
                    curve.append(bankroll)
                sample_curves.append(curve)

        kelly_profits = np.array(kelly_finals) - STARTING_BANKROLL
        flat_profits = np.array(flat_finals) - STARTING_BANKROLL
        bust_kelly = np.mean(np.array(kelly_finals) < STARTING_BANKROLL)
        bust_flat = np.mean(np.array(flat_finals) < STARTING_BANKROLL)

        print(f"\n  Strategy: {label}")
        print(f"  N={n_games} games  |  EV/$ = {ev:+.3f}  |  Win rate = {np.mean(win_flags):.1%}")
        print(f"  Kelly sim  → Median profit: ${np.median(kelly_profits):+.0f}  "
              f"95% CI: [${np.percentile(kelly_profits,2.5):+.0f}, ${np.percentile(kelly_profits,97.5):+.0f}]  "
              f"Bust: {bust_kelly:.1%}")
        print(f"  Flat ${flat_bet:.0f}/bet → Median profit: ${np.median(flat_profits):+.0f}  "
              f"95% CI: [${np.percentile(flat_profits,2.5):+.0f}, ${np.percentile(flat_profits,97.5):+.0f}]  "
              f"Bust: {bust_flat:.1%}")

        graph_data.append(dict(
            label=label, curves=sample_curves,
            kelly_profits=kelly_profits, flat_profits=flat_profits,
        ))

    # ================================================================
    # SECTION 5: GRAPH
    # ================================================================
    plt.style.use('dark_background')
    n_strats = len(graph_data)
    fig, axes = plt.subplots(n_strats, 2, figsize=(16, 5 * n_strats))
    if n_strats == 1:
        axes = [axes]

    fig.suptitle("NBA Kalshi Strategy Backtest", fontsize=14, y=1.01)

    for i, (data, (label, rets, ev)) in enumerate(zip(graph_data, simulate_set)):
        ax_curve, ax_dist = axes[i]

        # Left: equity curves
        curves = data['curves']
        max_len = max(len(c) for c in curves)
        padded = np.array([c + [c[-1]] * (max_len - len(c)) for c in curves])
        x_vals = np.arange(max_len)

        for c in curves:
            ax_curve.plot(c, color='lime', alpha=0.15, linewidth=0.6)
        ax_curve.fill_between(x_vals,
                              np.percentile(padded, 5, axis=0),
                              np.percentile(padded, 95, axis=0),
                              color='cyan', alpha=0.2, label='90% band')
        ax_curve.plot(np.mean(padded, axis=0), color='white', linewidth=2, label='Mean')
        ax_curve.axhline(STARTING_BANKROLL, color='red', linestyle='--', linewidth=1.2, label='Breakeven')
        ax_curve.set_title(f"{label}  |  EV/$ = {ev:+.3f}", fontsize=10)
        ax_curve.set_ylabel("Bankroll ($)")
        ax_curve.set_xlabel("Bets Placed")
        ax_curve.legend(fontsize=8)
        ax_curve.grid(True, alpha=0.2)

        # Right: profit distribution
        kp = data['kelly_profits']
        fp = data['flat_profits']
        ax_dist.hist(kp, bins=50, alpha=0.6, color='cyan', label=f'Kelly (med ${np.median(kp):+.0f})')
        ax_dist.hist(fp, bins=50, alpha=0.6, color='lime', label=f'Flat (med ${np.median(fp):+.0f})')
        ax_dist.axvline(0, color='red', linestyle='--', linewidth=1.5, label='Breakeven')
        ax_dist.set_title(f"Season P&L Distribution ({N_SIM} simulations)", fontsize=10)
        ax_dist.set_xlabel("Profit ($)")
        ax_dist.set_ylabel("Count")
        ax_dist.legend(fontsize=8)
        ax_dist.grid(True, alpha=0.2)

    plt.tight_layout()
    graph_path = os.path.join(_DIR, "strategy_analysis.png")
    plt.savefig(graph_path, dpi=150, bbox_inches='tight')
    print(f"\nGraph saved → {graph_path}")


if __name__ == "__main__":
    run_analysis()

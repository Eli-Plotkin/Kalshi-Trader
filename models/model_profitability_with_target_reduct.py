import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.ticker as ticker

# --- CONFIGURATION ---
FILE_PATH = 'kalshi_nba_arbitrage_data.csv'
INITIAL_STAKE = 100.0    # Bet $100 on the Underdog to start
TARGET_REDUCTION = .2
SLIPPAGE = 0.00          # We assume Limit Orders fill at exactly 40 if the price goes lower
                         # (Slippage is already accounted for in your conservative Min_Price data)

# Load Data
try:
    df = pd.read_csv(FILE_PATH)
    # Convert date and sort to ensure time-series accuracy
    df['Date'] = pd.to_datetime(df['Date'])
    df = df.sort_values(by='Date').reset_index(drop=True)
except FileNotFoundError:
    print("Error: CSV file not found.")
    exit()

cumulative_profit = []
plotting_dates = []
current_bankroll = 0.0
total_hedges = 0
wins = 0
losses = 0
profit_from_hedges = 0.0

print(f"{'DATE':<12} | {'MATCHUP':<35} | {'ACTION':<10} | {'RESULT':<10} | {'P&L'}")
print("-" * 90)

for i, row in df.iterrows():
    # 1. INITIAL BET (Underdog)
    # Your scraper now records the ASK price in Und_Start_Price, so we use it directly.
    entry_price = row['Und_Start_Price'] / 100
    if entry_price >= 0.98 or entry_price <= 0.01: 
        continue # Skip bad data rows
        
    shares_bought = INITIAL_STAKE / entry_price
    max_payout = shares_bought # If Und wins, we get $1.00 * shares

    target = row['Fav_Start_Price'] / 100 - TARGET_REDUCTION
    
    # 2. CHECK HEDGE CONDITION
    # Did the Favorite's price dip below our target (40 cents)?
    # We use the Conservative Min Price you calculated.
    market_low = row['Fav_Min_Price'] / 100
    
    # Logic: If the market dipped to 0.35, our Limit Order at 0.40 would definitely fill.
    hedge_triggered = market_low <= target
    
    game_pnl = 0.0
    
    if hedge_triggered:
        # --- SCENARIO A: HEDGE EXECUTED ---
        total_hedges += 1
        
        # Math: We sell off enough upside to guarantee a risk-free profit (or minimized loss)
        # We buy 'shares_bought' of the Favorite at 0.40.
        # Cost = Shares * 0.40
        hedge_cost = shares_bought * target
        
        # PnL = Revenue - Total Cost
        # Since we own BOTH sides (Und + Fav) for the same # of shares, we are guaranteed 
        # to win exactly 1 side.
        # Revenue = shares_bought * $1.00
        # Total Cost = INITIAL_STAKE + hedge_cost
        
        revenue = shares_bought * 1.00
        total_cost = INITIAL_STAKE + hedge_cost
        game_pnl = revenue - total_cost

        profit_from_hedges += game_pnl
        
        action = "HEDGED"
        result_str = "LOCKED"
        
    else:
        # --- SCENARIO B: NO HEDGE (RIDE THE UNDERDOG) ---
        action = "NO FILL"
        if row['Winner'] == 'Underdog':
            revenue = shares_bought * 1.00
            game_pnl = revenue - INITIAL_STAKE
            result_str = "WIN"
            wins += 1
        else:
            game_pnl = -INITIAL_STAKE
            result_str = "LOSS"
            losses += 1

    current_bankroll += game_pnl
    plotting_dates.append(row["Date"])
    cumulative_profit.append(current_bankroll)
    
    # Print the first 10 and interesting ones
    if i < 10 or game_pnl > 100:
        matchup = f"{row['Favorite_Team']} vs {row['Underdog_Team']}"
        print(f"{row['Date'].strftime('%Y-%m-%d'):<12} | {matchup:<35} | {action:<10} | {result_str:<10} | ${game_pnl:+.2f}")

# --- PLOTTING ---
plt.figure(figsize=(12, 6))
plt.plot(plotting_dates, cumulative_profit, color='green', linewidth=2)
plt.axhline(0, color='black', linewidth=1, linestyle='--')

# Styling
plt.title(f'Strategy Performance: Buy Hedge @ {TARGET_REDUCTION*100:.0f}% Dip', fontsize=14)
plt.ylabel('Cumulative Profit ($)', fontsize=12)
plt.xlabel('Date', fontsize=12)
plt.grid(True, alpha=0.3)

# Add stats to the plot
stats_text = (
    f"Total Games: {len(df)}\n"
    f"Hedges Triggered: {total_hedges} ({(total_hedges/len(df)):.1%})\n"
    f"Final P&L: ${current_bankroll:,.2f}\n"
    f"Profit Per Hedge: ${profit_from_hedges/total_hedges:,.2f}"
)
plt.gcf().text(0.15, 0.75, stats_text, fontsize=10, 
               bbox=dict(facecolor='white', alpha=0.8, edgecolor='gray'))

plt.tight_layout()
plt.show()
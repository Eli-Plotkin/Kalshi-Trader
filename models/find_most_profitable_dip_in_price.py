import pandas as pd
import numpy as np
import matplotlib.pyplot as plt

# --- 1. SETUP ---
file_path = 'kalshi_nba_arbitrage_data.csv'

# Strategy Settings
INITIAL_STAKE = 100.0  
# Set to 0 because your data already accounts for slippage (Conservative Min) and spread (Ask Price)
SLIPPAGE = 0.00        
ENTRY_SPREAD_TAX = 0.00 
FEE = 0.00             

# Load Data
try:
    df = pd.read_csv(file_path)
    print("Data loaded successfully.")
except FileNotFoundError:
    print("Error: File not found.")
    exit()

# --- 2. THE OPTIMIZATION LOOP ---
# Testing Multipliers from 0.10 (10%) to 0.60 (60%) of the starting price
trigger_dips = np.linspace(0.10, 0.60, 51)
profits = []

# Basic validation
required_cols = ['Und_Start_Price', 'Fav_Min_Price', 'Fav_Start_Price', 'Winner']
missing = [c for c in required_cols if c not in df.columns]
if missing:
    raise ValueError(f"Missing columns: {missing}")

for multiplier in trigger_dips:
    total_pnl = 0
    
    for i, row in df.iterrows():
        # A. Setup the Underdog Bet (Using Ask Price from CSV)
        und_price = (row['Und_Start_Price'] / 100) + ENTRY_SPREAD_TAX
        if und_price >= 0.99: und_price = 0.99
        und_payout = INITIAL_STAKE / und_price

        if und_price < .3:
            continue
        
        # B. Calculate Target Price
        # Logic: If Fav starts at 0.80 and Multiplier is 0.5, we buy if they dip to 0.40
        target_price = (row["Fav_Start_Price"] / 100) - multiplier
        if target_price < 0.01:
            continue

        # C. Check Execution
        # We use the Conservative Min Price (which already buffers for liquidity)
        min_purchasable = (row["Fav_Min_Price"] / 100) + SLIPPAGE
        
        if min_purchasable <= target_price:
            # --- SCENARIO 1: HEDGE (Arbitrage Lock) ---
            # Buy enough Fav contracts to match the Und payout
            hedge_contracts = und_payout 
            hedge_cost = hedge_contracts * target_price
            
            # Profit = Guaranteed Payout - Total Cost
            profit = (und_payout * (1 - FEE)) - INITIAL_STAKE - hedge_cost
            
        else:
            # --- SCENARIO 2: NO HEDGE (Naked Bet) ---
            if row['Winner'] == "Underdog":
                profit = (und_payout * (1 - FEE)) - INITIAL_STAKE
            else:
                profit = -INITIAL_STAKE
        
        total_pnl += profit
        
    profits.append(total_pnl)

# --- 3. PLOTTING THE RESULTS ---
plt.figure(figsize=(12, 6))
plt.plot(trigger_dips, profits, color='blue', linewidth=2, label='Strategy P&L')
plt.axhline(0, color='red', linestyle='--', label='Break Even')

# Find the Peak
max_idx = np.argmax(profits)
optimal_dip = trigger_dips[max_idx]
max_profit = profits[max_idx]

plt.scatter(optimal_dip, max_profit, color='green', s=100, zorder=5)
plt.annotate(f'OPTIMAL DIP: {optimal_dip:.2f}\nMax Profit: ${max_profit:,.0f}', 
             (optimal_dip, max_profit), xytext=(optimal_dip+0.05, max_profit),
             arrowprops=dict(facecolor='black', shrink=0.05))

plt.title(f"Optimization Results: Buying the Dip at 'x' Multiplier of Start Price")
plt.xlabel("Target Dip (e.g., 0.5 =  Fav Start Price - .5)")
plt.ylabel("Total Strategy Profit ($)")
plt.grid(True, alpha=0.3)
plt.legend()
plt.show()

print(f"CONCLUSION: The data suggests you should set your buy order at {optimal_dip:.2f} less than the starting price.")
print(f"Example: If Favorite starts at 80 cents, buy at {int(80 - optimal_dip)} cents.")
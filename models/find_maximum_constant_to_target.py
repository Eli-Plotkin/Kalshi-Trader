import pandas as pd
import numpy as np
import matplotlib.pyplot as plt

# --- 1. SETUP ---
# Replace with your actual CSV filename
file_path = 'kalshi_nba_arbitrage_data.csv' 

# Strategy Settings
INITIAL_STAKE = 100.0  # Bet on Underdog
SLIPPAGE = 0.03        # Conservative: We assume we buy 3 cents worse than the Low
FEE = 0.00             # Exchange fees (0.02 for 2% etc, if applicable)

# Load Data (Handle error if file not found)
try:
    df = pd.read_csv(file_path)
    print("Data loaded successfully.")
except FileNotFoundError:
    print("Error: File not found. Please export your .numbers file to CSV.")
    # Creating Dummy Data for Demonstration purposes so the code runs

# --- 2. THE OPTIMIZATION LOOP ---
# We test every trigger price from 0.05 to 0.95
trigger_price_differentials = np.linspace(0.01, 0.99, 99)
profits = []

required_cols = ['Und_Start_Price', 'Fav_Min_Price', 'Winner']
missing_cols = [c for c in required_cols if c not in df.columns]
assert not missing_cols, f"CRITICAL ERROR: Missing columns: {missing_cols}. Check your CSV headers."

try:
    assert pd.api.types.is_numeric_dtype(df['Und_Start_Price']), "Und_Start_Price is not numeric!"
    assert pd.api.types.is_numeric_dtype(df['Fav_Min_Price']), "Fav_Min_Price is not numeric!"
except AssertionError as e:
    print(f"DATA TYPE ERROR: {e}")
    print("Sample data:", df[['Und_Start_Price', 'Fav_Min_Price']].head())
    raise


for target_differential in trigger_price_differentials:
    total_pnl = 0
    
    for i, row in df.iterrows():
        assert row['Winner'] == "Underdog" or row['Winner'] == "Favorite", "Invalid Winner"

        # A. Setup the Underdog Bet
        ENTRY_SPREAD_TAX = 0.04 
        und_price = (row['Und_Start_Price'] / 100) + ENTRY_SPREAD_TAX

        # Safety check: Price cannot exceed 0.99
        if und_price >= 0.99: und_price = 0.99
        und_payout = INITIAL_STAKE / und_price
        
        #A. Calculate Target price
        target_price = (row["Fav_Start_Price"] / 100) * target_differential

        min_purchasable = (row["Fav_Min_Price"] / 100) + SLIPPAGE
        
        # Condition: Did the dip go LOW enough to hit our target?
        if min_purchasable <= target_price:
            # --- SCENARIO 1: HEDGE (GREEN BOOK) ---
            # We assume strict arbitrage: Equalize payouts.
            # Math: To get 'und_payout' on the Fav side, we need to buy 'und_payout' number of contracts.
            # (Since 1 contract pays $1).
            hedge_contracts = und_payout 
            hedge_cost = hedge_cost = hedge_contracts * target_price
            
            # Profit = Total Revenue (Payout) - Total Cost (Stake + Hedge)
            # Since payouts are equalized, we just take the Payout - Costs
            profit = (und_payout * (1 - FEE)) - INITIAL_STAKE - hedge_cost
            
        else:
            # --- SCENARIO 2: NO HEDGE (RIDE IT OUT) ---
            if row['Winner'] == "Underdog":
                profit = (und_payout * (1 - FEE)) - INITIAL_STAKE
            else:
                profit = -INITIAL_STAKE
        
        total_pnl += profit
        
    profits.append(total_pnl)


# --- 3. PLOTTING THE RESULTS ---
plt.figure(figsize=(12, 6))
plt.plot(trigger_price_differentials, profits, color='blue', linewidth=2, label='Strategy P&L')
plt.axhline(0, color='red', linestyle='--', label='Break Even')

# Find the Peak
max_idx = np.argmax(profits)
optimal_price_differential = trigger_price_differentials[max_idx]
max_profit = profits[max_idx]

plt.scatter(optimal_price_differential, max_profit, color='green', s=100, zorder=5)
plt.annotate(f'OPTIMAL DIP: {optimal_price_differential:.2f}\nMax Profit: ${max_profit:,.0f}', 
             (optimal_price_differential, max_profit), xytext=(optimal_price_differential+0.05, max_profit),
             arrowprops=dict(facecolor='black', shrink=0.05))

plt.title(f"Optimization Results: Best Price to Buy the Favorite (Initial Stake ${INITIAL_STAKE})")
plt.xlabel("Target Differential from Original Price (The Dip)")
plt.ylabel("Total Strategy Profit ($)")
plt.grid(True, alpha=0.3)
plt.legend()
plt.show()

if optimal_price_differential == 0:
    odds_str = "N/A"
elif optimal_price_differential >= 0.50:
    # Formula for Favorites (e.g., 0.60 -> -150)
    # Odds = - (Price / (1 - Price)) * 100
    us_odds = -int((optimal_price_differential / (1 - optimal_price_differential)) * 100)
    odds_str = f"{us_odds}"
else:
    # Formula for Underdogs (e.g., 0.40 -> +150)
    # Odds = + ((1 - Price) / Price) * 100
    us_odds = int(((1 - optimal_price_differential) / optimal_price_differential) * 100)
    odds_str = f"+{us_odds}"

print(f"CONCLUSION: The data suggests you should set your buy order at {optimal_price_differential:.2f} ({odds_str} odds).")

if max_profit < 0:
    print("WARNING: The peak is below $0. This strategy is not profitable with current slippage/fees.")
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt

# --- 1. SETUP ---
file_path = 'kalshi_nba_arbitrage_data.csv' 

# Strategy Settings
INITIAL_STAKE = 100.0   
ENTRY_FEE_BUFFER = 0.04  # 4 cents (Spread + Taker Fees)
HEDGE_SLIPPAGE = 0.01    # 1 cent slippage on panic hedges

# Load Data
try:
    df = pd.read_csv(file_path)
    print("Data loaded successfully.")
except FileNotFoundError:
    print(f"Error: '{file_path}' not found.")
    exit()

processed_data = []

# --- 2. THE STRATEGY LOOP ---
for i, row in df.iterrows():
    
    # --- A. INITIAL ENTRY (UNDERDOG) ---
    # We calculate the execution price including the spread/fee buffer
    raw_und_price = row["Und_Start_Price"] / 100
    exec_und_price = min(0.99, raw_und_price + ENTRY_FEE_BUFFER)
    
    # FILTER: Skip if Underdog is too expensive (> 35 cents)
    if exec_und_price > 0.35: 
        continue

    # Calculate Contracts Owned
    # Note: In real life you buy integer contracts. We use float for precision here.
    und_contracts = INITIAL_STAKE / exec_und_price
    max_und_revenue = und_contracts * 1.00  # Kalshi pays $1 per contract

    # --- B. MARKET DATA ---
    fav_start_price = row["Fav_Start_Price"] / 100
    fav_low_of_day = row["Fav_Min_Price"] / 100
    
    # Initialize Variables
    H1_cost = 0.0
    fav_contracts = 0.0
    
    # --- C. HEDGE #1 (Safety Net) ---
    # Trigger: Favorite drops 20 cents from open
    trigger_price = fav_start_price - 0.58
    
    # Execution Logic:
    # 1. Trigger price must be valid (> 1 cent)
    # 2. Market must have actually dropped to this price (fav_low_of_day <= trigger)
    if trigger_price > 0.01 and fav_low_of_day <= trigger_price:
        
        # REALITY CHECK: You likely pay slippage when panic-buying a crash.
        # So your execution price is slightly worse than the trigger price.
        exec_hedge_price = trigger_price + HEDGE_SLIPPAGE 
        
        # Breakeven Math:
        # We need enough Fav contracts to cover the INITIAL_STAKE + HEDGE_COST.
        # Equation: (Contracts * 1.00) = INITIAL_STAKE + (Contracts * Exec_Price)
        # Solve for Contracts: Contracts = INITIAL_STAKE / (1 - Exec_Price)
        
        needed_fav_contracts = INITIAL_STAKE / (1 - exec_hedge_price)
        H1_cost_needed = needed_fav_contracts * exec_hedge_price
        
        # LOGIC CHECK: Do not hedge if it costs more than our max potential profit.
        # Max profit = (Underdog Revenue - Initial Stake).
        # We clamp the hedge cost to this "house money".
        available_profit = max(0.0, max_und_revenue - INITIAL_STAKE)
        
        H1_cost = min(H1_cost_needed, available_profit)
        
        # Buy the contracts
        if H1_cost > 0:
            fav_contracts += H1_cost / exec_hedge_price

    # --- D. CALCULATE P&L ---
    total_spend = INITIAL_STAKE + H1_cost
    
    if row["Winner"] == "Underdog":
        revenue = max_und_revenue
    else:
        # If Favorite wins, our revenue comes strictly from hedge contracts (if any)
        revenue = fav_contracts * 1.00

    profit = revenue - total_spend

    # Store Data
    row_data = row.to_dict()
    row_data['Game_PnL'] = profit
    row_data['Hedge_Triggered'] = (H1_cost > 0) # Useful flag for analysis
    processed_data.append(row_data)

# --- 3. RESULTS ---
results_df = pd.DataFrame(processed_data)

if results_df.empty:
    print("No games met criteria.")
else:
    results_df['Cumulative_PnL'] = results_df['Game_PnL'].cumsum()
    
    total_profit = results_df['Game_PnL'].sum()
    roi = (total_profit / (len(results_df) * INITIAL_STAKE)) * 100
    win_rate = (len(results_df[results_df['Game_PnL'] > 0]) / len(results_df)) * 100
    hedge_rate = (results_df['Hedge_Triggered'].sum() / len(results_df)) * 100

    print("-" * 40)
    print(f"Games Played: {len(results_df)}")
    print(f"Hedges Triggered: {results_df['Hedge_Triggered'].sum()} ({hedge_rate:.1f}%)")
    print(f"Win Rate (Profitable Games): {win_rate:.1f}%")
    print(f"Total Profit: ${total_profit:,.2f}")
    print(f"ROI: {roi:.2f}%")
    print("-" * 40)

    # Plot
    plt.figure(figsize=(12, 6))
    plt.plot(results_df.index, results_df['Cumulative_PnL'], label='Equity Curve', color='#1f77b4', linewidth=2)
    plt.fill_between(results_df.index, results_df['Cumulative_PnL'], 0, where=(results_df['Cumulative_PnL']>=0), color='green', alpha=0.1)
    plt.fill_between(results_df.index, results_df['Cumulative_PnL'], 0, where=(results_df['Cumulative_PnL']<0), color='red', alpha=0.1)
    plt.axhline(0, color='black', linestyle='--')
    plt.title('Strategy Performance (Underdog <= 0.35 | Defensive Hedge Only)')
    plt.xlabel('Games')
    plt.ylabel('Profit ($)')
    plt.grid(True, alpha=0.3)
    plt.show()
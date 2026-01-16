import os
import pandas as pd
import gspread
from google.oauth2.service_account import Credentials
import numpy as np
import matplotlib.pyplot as plt

# ================= SECURE CONFIG (DOWNLOADS) =================
# Updated to match your other script's secure location
DOWNLOADS_PATH = "/Users/jamesforman/Downloads"
GOOGLE_CREDS_PATH = os.path.join(DOWNLOADS_PATH, "credentials.json")

SPREADSHEET_NAME = "NBA Arbitrage"
SOURCE_TAB = "Data Scrape 2"
TARGET_TAB = "New Data"

STARTING_BANKROLL = 1000.0  
ROI_TARGETS = [0.5, 0.75, 1.0, 1.25, 1.5, 1.75]

# Filter Range
ENTRY_MIN, ENTRY_MAX = 20, 35

# ================= MATH FUNCTIONS =================
def calculate_compounding_ror(outcomes, f):
    """Calculates Probability of >90% Loss based on Log-Returns."""
    if f <= 0 or len(outcomes) == 0: return 100.0
    log_returns = np.log(1 + f * outcomes)
    mu = np.mean(log_returns)
    sigma_sq = np.var(log_returns)
    if mu <= 0: return 100.0
    z = np.log(10) # 90% loss threshold
    ror = np.exp(- (2 * mu / sigma_sq) * z)
    return round(min(1.0, ror) * 100, 2)

def calculate_max_dd(curve):
    """Calculates the maximum peak-to-trough percentage drop."""
    if len(curve) == 0: return 0.0
    peak = np.maximum.accumulate(curve)
    drawdowns = (curve - peak) / peak
    return round(np.min(drawdowns) * 100, 2)

# ================= MAIN MODEL =================
def run_model():
    # 1. SETUP - Updated to use the secure path in Downloads
    try:
        creds = Credentials.from_service_account_file(
            GOOGLE_CREDS_PATH, 
            scopes=["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
        )
        gc = gspread.authorize(creds)
        sh = gc.open(SPREADSHEET_NAME)
    except FileNotFoundError:
        print(f"CRITICAL ERROR: Could not find credentials.json in {DOWNLOADS_PATH}")
        return

    df = pd.DataFrame(sh.worksheet(SOURCE_TAB).get_all_records())
    
    # 2. FILTER LOGIC
    df['Und_Start_Price'] = pd.to_numeric(df['Und_Start_Price'], errors='coerce')
    df_model = df[(df['Und_Start_Price'] >= ENTRY_MIN) & (df['Und_Start_Price'] <= ENTRY_MAX)].copy()

    if len(df_model) == 0:
        print(f"No games found in the {ENTRY_MIN}-{ENTRY_MAX} price range.")
        return

    entry_prices = df_model['Und_Start_Price'].to_numpy()
    best_prices = df_model['Und_Best_Price'].to_numpy()
    winners = df_model['Winner'].to_numpy()
    underdogs = df_model['Underdog_Team'].to_numpy()

    # 3. DEFINE HEADERS
    summary_table = [[
        "ROI Target", "Win Rate %",
        "1% Fixed", "1% Final $", "1% RoR %", "1% Max DD %",
        "Qtr Kelly %", "Qtr Final $", "Qtr RoR %", "Qtr Max DD %",
        "Half Kelly %", "Half Final $", "Half RoR %", "Half Max DD %",
        "Full Kelly %", "Full Final $", "Full RoR %", "Full Max DD %"
    ]]

    plot_data = {}
    best_qtr_final = -1

    # 4. RUN SIMULATION LOOP
    for t in ROI_TARGETS:
        target_price = entry_prices * (1 + t)
        outcomes = np.where((best_prices >= target_price) & (target_price <= 100), t,
                   np.where(winners == underdogs, (100 - entry_prices) / entry_prices, -1.0))
        
        p = np.mean(outcomes > 0)
        avg_win = np.mean(outcomes[outcomes > 0]) if any(outcomes > 0) else 0
        f_star = max(0, ((avg_win * p) - (1 - p)) / avg_win) if avg_win > 0 else 0

        def get_tier_stats(frac):
            curve = STARTING_BANKROLL * np.cumprod(1 + (frac * outcomes))
            ror = calculate_compounding_ror(outcomes, frac)
            dd = calculate_max_dd(curve)
            return round(curve[-1], 2), ror, dd, curve

        f1_fin, f1_ror, f1_dd, f1_curve = get_tier_stats(0.01)
        q_fin, q_ror, q_dd, q_curve = get_tier_stats(f_star / 4)
        h_fin, h_ror, h_dd, h_curve = get_tier_stats(f_star / 2)
        full_fin, full_ror, full_dd, full_curve = get_tier_stats(f_star)

        summary_table.append([
            f"{int(t*100)}%", f"{round(p*100, 2)}%",
            "1.00%", f"${f1_fin}", f"{f1_ror}%", f"{f1_dd}%",
            f"{round((f_star/4)*100, 2)}%", f"${q_fin}", f"{q_ror}%", f"{q_dd}%",
            f"{round((f_star/2)*100, 2)}%", f"${h_fin}", f"{h_ror}%", f"{h_dd}%",
            f"{round(f_star*100, 2)}%", f"${full_fin}", f"{full_ror}%", f"{full_dd}%"
        ])

        if q_fin > best_qtr_final:
            best_qtr_final = q_fin
            plot_data = {"1% Fixed": f1_curve, "Quarter Kelly": q_curve, "Half Kelly": h_curve, "Full Kelly": full_curve}

    # 5. GENERATE PLOT
    plt.style.use('dark_background')
    plt.figure(figsize=(12, 7))
    for name, curve in plot_data.items():
        width = 3 if name == "Quarter Kelly" else 1.5
        plt.plot(curve, label=name, linewidth=width, alpha=0.9)
        
    plt.yscale('log')
    plt.title(f'Strategy Performance ({len(df_model)} Trades @ {ENTRY_MIN}-{ENTRY_MAX} Range)', fontsize=14)
    plt.ylabel('Bankroll ($) - Log Scale')
    plt.xlabel('Trade Sequence')
    plt.legend()
    plt.grid(True, which="both", alpha=0.2)
    plt.savefig("strategy_risk_analysis.png")

    # 6. EXPORT TO GOOGLE SHEETS
    ws = sh.worksheet(TARGET_TAB)
    ws.clear()
    ws.update(range_name='A1', values=summary_table)
    print(f"Update Complete: Processed {len(df_model)} games. Chart saved and Sheets updated.")

if __name__ == "__main__":
    run_model()
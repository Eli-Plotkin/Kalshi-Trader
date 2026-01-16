import os
import time
import base64
import json
import requests
import gspread
from google.oauth2.service_account import Credentials
from datetime import datetime
from collections import defaultdict
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding as asymmetric_padding
from gspread_formatting import *

# ================= SECURE CONFIG (DOWNLOADS) =================
# We look in Downloads so these files are NEVER in your Git folder.
SECURE_DIR = "/Users/jamesforman/Downloads"

KALSHI_CONFIG_PATH = os.path.join(SECURE_DIR, "kalshi_config.json")
PRIVATE_KEY_PATH = os.path.join(SECURE_DIR, "bet-tracker.txt")
GOOGLE_CREDS_PATH = os.path.join(SECURE_DIR, "credentials.json")

# Google Sheets Details
SHEET_NAME = "NBA Arbitrage"
WORKSHEET_TITLE = "Live Data"

# ================= CREDENTIAL LOADING =================
try:
    # 1. Load the Kalshi API Key ID
    with open(KALSHI_CONFIG_PATH, "r") as f:
        API_KEY_ID = json.load(f).get("api_key_id")
    
    # 2. Load the Big Private Key (RSA)
    with open(PRIVATE_KEY_PATH, "rb") as key_file:
        SIGNER = serialization.load_pem_private_key(key_file.read(), password=None)
except Exception as e:
    print(f"CRITICAL ERROR: Files missing in Downloads or corrupted. {e}")
    exit(1)

# ================= KALSHI AUTH & API =================
def sign_request(method, path):
    """Signs the Kalshi request using RSA-PSS SHA256."""
    timestamp = str(int(time.time() * 1000))
    clean_path = path.split('?')[0]  # Sign the path without query params
    msg = f"{timestamp}{method}{clean_path}"
    
    signature = SIGNER.sign(
        msg.encode('utf-8'),
        asymmetric_padding.PSS(
            mgf=asymmetric_padding.MGF1(hashes.SHA256()), 
            salt_length=asymmetric_padding.PSS.MAX_LENGTH
        ),
        hashes.SHA256()
    )
    return base64.b64encode(signature).decode('utf-8'), timestamp

def kalshi_get(endpoint, params=None):
    """Authenticated GET request to Kalshi."""
    sig, ts = sign_request("GET", endpoint)
    headers = {
        "KALSHI-ACCESS-KEY": API_KEY_ID, 
        "KALSHI-ACCESS-SIGNATURE": sig, 
        "KALSHI-ACCESS-TIMESTAMP": ts
    }
    url = f"https://api.elections.kalshi.com{endpoint}"
    r = requests.get(url, headers=headers, params=params)
    return r.json() if r.status_code == 200 else None

# ================= GOOGLE SHEETS LOGIC =================
def update_google_sheet(rows, balance, total_deposits, account_roi):
    scopes = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
    creds = Credentials.from_service_account_file(GOOGLE_CREDS_PATH, scopes=scopes)
    client = gspread.authorize(creds)

    try:
        spreadsheet = client.open(SHEET_NAME)
        sheet = spreadsheet.worksheet(WORKSHEET_TITLE)
    except Exception as e:
        print(f"Sheet Error: {e}")
        return

    sheet.clear()
    
    # Dashboard Section
    dashboard_data = [
        ['CURRENT PORTFOLIO VALUE:', f'${balance:,.2f}'],
        ['MANUAL TOTAL DEPOSITS:', f'${total_deposits:,.2f}'],
        ['TOTAL NET PROFIT:', f'${(balance - total_deposits):,.2f}'],
        ['ACCOUNT NET ROI %:', f'{account_roi:,.2f}%'],
        ['Last Updated:', datetime.now().strftime("%Y-%m-%d %H:%M:%S")]
    ]
    # Using named arguments to be compatible with gspread v6+
    sheet.update(range_name='A1:B5', values=dashboard_data)

    # Table Header
    header_row = 7
    headers = ["Date", "Market", "Status", "Total Bought $", "Avg Buy Price", "Avg Sell Price", "PnL $", "Trade ROI %"]
    sheet.update(range_name=f'A{header_row}:H{header_row}', values=[headers])

    # Table Data
    first_data_row = header_row + 1
    data_to_append = [[
        r["Date"], r["Market"], r["Status"], r["Total_Bought_$"], 
        r["Avg_Buy_Price"], r["Avg_Sell_Price"], r["PnL_$"], r["Percent_Gain"]
    ] for r in rows]
    
    if data_to_append:
        last_data_row = first_data_row + len(data_to_append) - 1
        sheet.update(
            range_name=f'A{first_data_row}:H{last_data_row}', 
            values=data_to_append, 
            value_input_option='USER_ENTERED'
        )

        # Totals Row
        total_row_idx = last_data_row + 1
        totals_row = [
            "TOTALS", "", "", 
            f"=SUM(D{first_data_row}:D{last_data_row})", "", "", 
            f"=SUM(G{first_data_row}:G{last_data_row})", 
            f"=IF(D{total_row_idx}>0, G{total_row_idx}/D{total_row_idx}, 0)"
        ]
        sheet.update(range_name=f'A{total_row_idx}:H{total_row_idx}', values=[totals_row], value_input_option='USER_ENTERED')

        # Formatting
        format_cell_range(sheet, 'A1:A5', cellFormat(textFormat=textFormat(bold=True)))
        format_cell_range(sheet, f'A{header_row}:H{header_row}', cellFormat(
            backgroundColor=color(0.2, 0.2, 0.2), 
            textFormat=textFormat(bold=True, foregroundColor=color(1, 1, 1)), 
            horizontalAlignment='CENTER'
        ))
        format_cell_range(sheet, f'D{first_data_row}:G{total_row_idx}', cellFormat(numberFormat={'type': 'CURRENCY', 'pattern': '$#,##0.00'}))
        format_cell_range(sheet, f'H{first_data_row}:H{total_row_idx}', cellFormat(numberFormat={'type': 'NUMBER', 'pattern': '0.00"%"'}))

# ================= DATA PROCESSING =================
def process_fills(fills):
    market_groups = defaultdict(list)
    for f in fills: 
        market_groups[f["market_ticker"]].append(f)
    
    rows = []
    for mt, fls in market_groups.items():
        if not mt.startswith("KXNBA"): continue
        m_info = kalshi_get(f"/trade-api/v2/markets/{mt}")
        if not m_info: continue
        market = m_info.get("market", {})

        def extract_price(fill): 
            return float(fill.get("yes_price") or fill.get("no_price") or fill.get("price") or 0)

        buys = [f for f in fls if f["action"] == "buy"]
        if not buys: continue
        
        total_qty = sum(int(f.get("count", 0)) for f in buys)
        total_cost = sum((extract_price(f) * int(f.get("count", 0))) / 100 for f in buys)
        avg_buy = total_cost / total_qty if total_qty > 0 else 0

        sells = [f for f in fls if f["action"] == "sell"]
        sell_payout = sum((extract_price(f) * int(f.get("count", 0))) / 100 for f in sells)
        avg_sell = sell_payout / total_qty if total_qty > 0 else 0

        pnl = (sell_payout - total_cost) if avg_sell > 0 else -total_cost
        rows.append({
            "Date": datetime.now().strftime("%Y-%m-%d"),
            "Market": market.get("title", mt),
            "Status": "Cashed Out" if avg_sell > 0 else "LOST/OPEN",
            "Total_Bought_$": round(total_cost, 2),
            "Avg_Buy_Price": round(avg_buy, 2),
            "Avg_Sell_Price": round(avg_sell, 2),
            "PnL_$": round(pnl, 2),
            "Percent_Gain": round((pnl / total_cost) * 100, 2) if total_cost > 0 else 0
        })
    return rows

def main():
    print("Checking keys and fetching Kalshi data...")
    balance_data = kalshi_get("/trade-api/v2/portfolio/balance")
    
    if not balance_data:
        print("Failed to fetch balance. Check your API Key ID and Private Key.")
        return

    current_balance = (balance_data.get("balance", 0) / 100)
    total_deposits = 10.00  # Update this manually when you deposit more!
    account_roi = ((current_balance - total_deposits) / total_deposits * 100) if total_deposits > 0 else 0

    data = kalshi_get("/trade-api/v2/portfolio/fills", {"limit": 100})
    if data and "fills" in data:
        processed_rows = process_fills(data["fills"])
        update_google_sheet(processed_rows, current_balance, total_deposits, account_roi)
        print(f"Success! Portfolio Balance: ${current_balance:,.2f}")

if __name__ == "__main__":
    main()
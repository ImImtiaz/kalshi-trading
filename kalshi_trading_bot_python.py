# Kalshi Trading Bot (Simplified Production Version)
# ==============================================
# Hybrid Strategy: Mean Reversion + Range + Time Decay

import time
import json
import requests
from datetime import datetime, timedelta

# ========================
# CONFIG LOADER
# ========================
with open("config.json") as f:
    config = json.load(f)

API_KEY = config["API_KEY"]
BASE_URL = "https://trading-api.kalshi.com/trade-api/v2"
TRADE_SIZE = config.get("trade_size", 10)
STOP_LOSS = config.get("stop_loss", 0.07)
TAKE_PROFIT = config.get("take_profit", 0.1)
PAPER = config.get("paper_trade", True)

# ========================
# API WRAPPER
# ========================
headers = {
    "Authorization": f"Bearer {API_KEY}",
    "Content-Type": "application/json"
}

def get_markets():
    r = requests.get(f"{BASE_URL}/markets", headers=headers)
    return r.json()


def get_orderbook(ticker):
    r = requests.get(f"{BASE_URL}/markets/{ticker}/orderbook", headers=headers)
    return r.json()


def place_order(ticker, side, price, size):
    if PAPER:
        print(f"[PAPER] {side} {ticker} @ {price} x {size}")
        return

    data = {
        "ticker": ticker,
        "side": side,
        "price": price,
        "size": size,
        "type": "limit"
    }
    r = requests.post(f"{BASE_URL}/orders", headers=headers, json=data)
    return r.json()

# ========================
# DATA ENGINE
# ========================
price_history = {}


def update_price_history(ticker, price):
    if ticker not in price_history:
        price_history[ticker] = []
    price_history[ticker].append((datetime.utcnow(), price))

    # Keep last 15 min
    cutoff = datetime.utcnow() - timedelta(minutes=15)
    price_history[ticker] = [p for p in price_history[ticker] if p[0] > cutoff]


def get_recent_prices(ticker):
    return [p[1] for p in price_history.get(ticker, [])]

# ========================
# STRATEGIES
# ========================

def mean_reversion(ticker, current_price):
    prices = get_recent_prices(ticker)
    if len(prices) < 5:
        return None

    trend = prices[-1] - prices[0]

    fair_value = 50
    if trend > 2:
        fair_value = 55
    elif trend < -2:
        fair_value = 45

    if current_price < fair_value - 10:
        return "buy_yes"
    elif current_price > fair_value + 10:
        return "buy_no"

    return None


def range_strategy(ticker, current_price):
    prices = get_recent_prices(ticker)
    if len(prices) < 5:
        return None

    low = min(prices)
    high = max(prices)

    if current_price <= low + 2:
        return "buy_yes"
    elif current_price >= high - 2:
        return "buy_no"

    return None


def time_decay(ticker, current_price, expiry):
    time_left = expiry - datetime.utcnow()

    if time_left < timedelta(minutes=15):
        prices = get_recent_prices(ticker)
        if len(prices) < 5:
            return None

        trend = prices[-1] - prices[0]
        if trend > 3:
            return "buy_yes"
        elif trend < -3:
            return "buy_no"

    return None

# ========================
# MAIN LOOP
# ========================

def run_bot():
    print("Starting Kalshi Bot...")

    while True:
        try:
            markets = get_markets()

            for m in markets.get("markets", [])[:5]:  # limit markets
                ticker = m["ticker"]
                expiry = datetime.fromisoformat(m["close_time"].replace("Z", ""))

                ob = get_orderbook(ticker)
                best_bid = ob.get("bids", [[0]])[0][0]
                best_ask = ob.get("asks", [[100]])[0][0]

                mid_price = (best_bid + best_ask) / 2

                update_price_history(ticker, mid_price)

                # Strategy selection
                action = (
                    time_decay(ticker, mid_price, expiry)
                    or mean_reversion(ticker, mid_price)
                    or range_strategy(ticker, mid_price)
                )

                if action:
                    print(f"{ticker} -> {action} @ {mid_price}")

                    if action == "buy_yes":
                        place_order(ticker, "yes", best_ask, TRADE_SIZE)
                    elif action == "buy_no":
                        place_order(ticker, "no", best_bid, TRADE_SIZE)

            time.sleep(5)

        except Exception as e:
            print("Error:", e)
            time.sleep(10)


if __name__ == "__main__":
    run_bot()

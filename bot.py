#####################################
# HYPER CANDLE-BREAK PAPER BOT (Render)
# by ChatGPT
#####################################

import time, datetime, requests, pandas as pd, numpy as np, os

# ==========================
# SETTINGS
# ==========================
DRY_RUN = True  # paper trading
ASSETS = ["ETHUSDT", "BTCUSDT"]  # balanced 2-asset mode
TF = "1h"   # timeframe
CANDLES = 200  # how many bars to fetch
CAPITAL = 10000
LEVERAGE = 5
RISK = 0.01
ATR_PCT_MIN = 0.0025
VOL_MULT = 1.2
EMA_PERIOD = 50

portfolio_value = CAPITAL
open_positions = {}
trade_log = []


# ==========================
# FETCH CANDLES
# ==========================
def fetch_ohlc(symbol):
    url = "https://api.delta.exchange/v2/history/candles"
    end = int(time.time())
    start = end - CANDLES * 3600  # 1h bars

    params = {
        "symbol": symbol,
        "resolution": TF,
        "start": start,
        "end": end
    }

    r = requests.get(url, params=params)
    data = r.json()["result"]

    df = pd.DataFrame(data, columns=["time","open","high","low","close","volume"])
    df["time"] = pd.to_datetime(df["time"], unit="s")
    df = df.sort_values("time").reset_index(drop=True)
    for c in ["open","high","low","close","volume"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


# ==========================
# INDICATORS
# ==========================
def indicators(df):
    df["body"] = (df["close"] - df["open"]).abs()
    df["range"] = df["high"] - df["low"]
    df["atr"] = df["range"].rolling(14).mean()
    df["atr_pct"] = df["atr"] / df["close"]
    df["vol_sma"] = df["volume"].rolling(20).mean()
    df["ema"] = df["close"].ewm(EMA_PERIOD).mean()
    return df


# ==========================
# PAPER EXECUTION ENGINE
# ==========================
def open_trade(symbol, direction, entry, stop):
    global portfolio_value
    risk_amount = portfolio_value * RISK
    distance = abs(entry - stop)
    qty = (risk_amount / distance) * LEVERAGE

    open_positions[symbol] = {
        "side": direction,
        "entry": entry,
        "stop": stop,
        "qty": qty
    }

    print(f"[OPEN] {symbol} {direction} @ {entry:.2f} | stop {stop:.2f} | qty {qty:.4f}")


def close_trade(symbol, price):
    global portfolio_value

    pos = open_positions[symbol]
    if pos["side"] == "LONG":
        pnl = (price - pos["entry"]) * pos["qty"]
    else:
        pnl = (pos["entry"] - price) * pos["qty"]

    portfolio_value += pnl
    print(f"[CLOSE] {symbol} pnl={pnl:.2f} new_balance={portfolio_value:.2f}")

    trade_log.append(pnl)
    del open_positions[symbol]


# ==========================
# STRATEGY LOOP (Candle-Break Hyper)
# ==========================
def run_cycle():
    global portfolio_value

    for sym in ASSETS:
        df = fetch_ohlc(sym)
        df = indicators(df)
        row = df.iloc[-1]
        prev = df.iloc[-2]

        # reject low volatility / low volume
        if row["atr_pct"] < ATR_PCT_MIN: continue
        if row["volume"] < row["vol_sma"] * VOL_MULT: continue

        # Candle-break logic
        # LONG
        if row["close"] > prev["high"] and row["close"] > row["ema"]:
            if sym not in open_positions:
                open_trade(sym, "LONG", row["close"], prev["low"])
        # SHORT
        elif row["close"] < prev["low"] and row["close"] < row["ema"]:
            if sym not in open_positions:
                open_trade(sym, "SHORT", row["close"], prev["high"])

        # Manage existing position
        if sym in open_positions:
            pos = open_positions[sym]

            # trailing stop updates
            if pos["side"] == "LONG":
                new_stop = prev["low"]
                pos["stop"] = max(pos["stop"], new_stop)
                if row["close"] < pos["stop"]:
                    close_trade(sym, row["close"])

            if pos["side"] == "SHORT":
                new_stop = prev["high"]
                pos["stop"] = min(pos["stop"], new_stop)
                if row["close"] > pos["stop"]:
                    close_trade(sym, row["close"])


# ==========================
# MAIN LOOP
# ==========================
print("BOT STARTED ✅ (paper mode)")

while True:
    print(f"[{datetime.datetime.utcnow().isoformat()}] Hyper cycle… balance={portfolio_value:.2f}")
    try:
        run_cycle()
    except Exception as e:
        print("ERR:", e)

    time.sleep(60)  # run every 1 minute

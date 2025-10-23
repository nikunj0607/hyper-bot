# ==============================
# HYPER MODE BOT WITH WEBHOOK
# ==============================
import requests, time, datetime, threading
from flask import Flask, request
import numpy as np
import pandas as pd

# ====== Load Secrets (Do NOT upload to GitHub) ======
from secrets import TG_TOKEN, CHAT_ID

app = Flask(__name__)

# ====== TELEGRAM SEND ======
def tg(msg):
    try:
        url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
        data = {"chat_id": CHAT_ID, "text": msg}
        requests.post(url, data=data)
    except:
        pass

# ========== DATA FETCH ==========
def get_ohlc(symbol):
    url = "https://api.delta.exchange/v2/history/candles"
    now = int(datetime.datetime.now().timestamp())
    params = {
        "symbol": symbol,
        "resolution":"1h",
        "start": now - 60*24*3600,
        "end": now
    }
    r = requests.get(url, params=params)
    df = pd.DataFrame(r.json()["result"], columns=["time","open","high","low","close","volume"])
    df["time"] = pd.to_datetime(df["time"], unit="s")
    return df

# ====== STRATEGY ======
def candle_break(df):
    signals = []
    for i in range(1,len(df)):
        last = df.iloc[i-1]
        cur  = df.iloc[i]

        if cur.close > last.high:
            signals.append("BUY")
        elif cur.close < last.low:
            signals.append("SELL")
        else:
            signals.append(None)

    return signals

# ====== GLOBAL ======
portfolio = 10000
IN_POSITION = False
POSITION_SIDE = None

last_signal = None

run_trading = True

# ===== HYPER LOOP =====
def hyper_loop():
    global portfolio, IN_POSITION, POSITION_SIDE, last_signal

    while True:
        if run_trading == False:
            tg("⏸ paused")
            time.sleep(60)
            continue

        df = get_ohlc("ETHUSDT")
        signals = candle_break(df)
        sig = signals[-1]

        # BUY
        if sig == "BUY" and POSITION_SIDE != "LONG":
            POSITION_SIDE = "LONG"
            IN_POSITION = True
            tg(f"🟢 LONG entry ({df.close.iloc[-1]:.2f})")

        # SELL
        elif sig == "SELL" and POSITION_SIDE != "SHORT":
            POSITION_SIDE = "SHORT"
            IN_POSITION = True
            tg(f"🔴 SHORT entry ({df.close.iloc[-1]:.2f})")

        # Print heartbeat to Render logs
        print(f"[{datetime.datetime.utcnow().isoformat()}] Hyper cycle… pos={POSITION_SIDE}")

        time.sleep(3600)  # 1h candles

# Run strategy thread
threading.Thread(target=hyper_loop, daemon=True).start()

# ==============================
# WEBHOOK ENDPOINT
# ==============================
@app.route(f"/{TG_TOKEN}", methods=["POST"])
def webhook():
    global run_trading
    data = request.get_json()

    if "message" in data:
        text = data["message"]["text"].lower()
        cid  = data["message"]["chat"]["id"]
        if cid != CHAT_ID:
            return "ignored"

        if text == "ping":
            tg("✅ alive")
        elif text == "pause":
            run_trading = False
            tg("⏸ paused")
        elif text == "resume":
            run_trading = True
            tg("▶ resumed")
        elif text == "status":
            tg(f"pos={POSITION_SIDE}")
        else:
            tg("❓ unknown")

    return "ok"

@app.route("/", methods=["GET"])
def home():
    return "hyper alive"

# ====== GUNICORN ENTRYPOINT ======
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)

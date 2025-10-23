import time, datetime, requests, pytz, os, threading
from flask import Flask

# ==============================
# ENVIRONMENT SECRETS (Render)
# ==============================
TG_TOKEN = os.environ.get("TG_TOKEN")
CHAT_ID  = int(os.environ.get("CHAT_ID"))


# ==============================
# BOT SETTINGS
# ==============================
ASSETS       = ["ETHUSDT", "BTCUSDT", "SOLUSDT", "BNBUSDT"]
TF           = "1h"
API          = "https://api.delta.exchange"
LOOP_SLEEP   = 20      # seconds between ticks
IN_POSITION  = {a: False for a in ASSETS}   # position tracker
run_trading  = True     # pause/resume flag


# ==============================
# TELEGRAM SEND
# ==============================
def tg(msg):
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    params = {"chat_id": CHAT_ID, "text": msg}
    try:
        requests.get(url, params=params)
    except:
        pass


# ==============================
# TELEGRAM RECEIVE
# ==============================
last_update_id = None
def listen():
    global last_update_id
    url = f"https://api.telegram.org/bot{TG_TOKEN}/getUpdates"
    try:
        r = requests.get(url).json()
    except:
        return None

    updates = r.get("result", [])
    if not updates: return None

    upd = updates[-1]
    uid = upd["update_id"]

    if last_update_id is None:
        last_update_id = uid
        return None

    if uid == last_update_id:
        return None

    last_update_id = uid
    text = upd["message"]["text"].lower()
    cid  = upd["message"]["chat"]["id"]

    if cid != CHAT_ID:    # block strangers
        return None

    return text


# ==============================
# GET LATEST CANDLES
# ==============================
def candle(asset):
    url = f"{API}/v2/history/candles"
    p = {
        "symbol":     asset,
        "resolution": TF,
        "start":      int(time.time()) - 100000,
        "end":        int(time.time())
    }
    r = requests.get(url, params=p).json()
    if "result" not in r: return None
    d = r["result"]
    if len(d) < 3: return None
    return d[-3], d[-2]    # c2 closed, c1 break candle


# ==============================
# HYPER-MODE ENTRY LOGIC
# ==============================
def check_entry(asset):
    data = candle(asset)
    if not data: return None

    c2, c1 = data

    # BUY
    if c2["close"] > c2["open"] and c1["high"] > c2["high"]:
        return "BUY"

    # SELL
    if c2["close"] < c2["open"] and c1["low"] < c2["low"]:
        return "SELL"

    return None


# ==============================
# MAIN LOOP
# ==============================
def loop():
    global run_trading

    while True:

        # ---- Telegram commands ----
        cmd = listen()
        if cmd:
            if cmd == "ping":
                tg("✅ Bot alive!")
            elif cmd == "pause":
                run_trading = False
                tg("⏸ Trading paused")
            elif cmd == "resume":
                run_trading = True
                tg("▶ Trading resumed")
            elif cmd == "status":
                tg(f"Positions: {IN_POSITION}")
            elif cmd == "help":
                tg("/ping /pause /resume /status")
            else:
                tg("❓ Unknown command")

        # ---- paused? ----
        if not run_trading:
            time.sleep(LOOP_SLEEP)
            continue

        # ---- asset loop ----
        for a in ASSETS:
            sig = check_entry(a)
            if not sig: continue

            if (sig == "BUY") and (not IN_POSITION[a]):
                IN_POSITION[a] = True
                tg(f"🟢 BUY {a}")

            if (sig == "SELL") and (IN_POSITION[a]):
                IN_POSITION[a] = False
                tg(f"🔴 SELL {a}")

        print(f"[{datetime.datetime.utcnow().isoformat()}] heartbeat")
        time.sleep(LOOP_SLEEP)


# ==============================
# FLASK SERVER (Render Health)
# ==============================
app = Flask(__name__)

@app.route("/")
def home():
    return "hyper alive"


# ==============================
# BACKGROUND THREAD START
# ==============================
t = threading.Thread(target=loop)
t.daemon = True
t.start()

from flask import Flask, render_template
import json
import time
from datetime import datetime
from threading import Thread
import random  # TEMP: simulating strategy fill

app = Flask(__name__)

# ========== TRADE LOG HELPERS ==========

def read_trades():
    try:
        return json.load(open("trades.json"))
    except:
        return []

def write_trades(data):
    json.dump(data, open("trades.json", "w"), indent=4)

def log_trade(symbol, side, price, qty):
    trades = read_trades()
    trade = {
        "time": str(datetime.now())[:19],
        "symbol": symbol,
        "side": side,
        "price": price,
        "qty": qty,
        "status": "OPEN"
    }
    trades.append(trade)
    write_trades(trades)

def close_trade(symbol, exit_price):
    trades = read_trades()
    for t in trades:
        if t["symbol"] == symbol and t["status"] == "OPEN":
            t["exit_price"] = exit_price
            t["pnl"] = round((exit_price - t["price"]) * t["qty"], 2)
            t["status"] = "CLOSED"
            break
    write_trades(trades)

# ========== STRATEGY LOOP ==========

def strategy_loop():
    while True:
        # ------------------------------
        # Replace this block with REAL logic
        # ------------------------------
        
        # Fake signal generator (demo)
        if random.randint(1, 8) == 3:
            # create new trade
            log_trade("SBIN", "BUY", random.randint(500, 600), 1)

        # Fake exit for open trades
        trades = read_trades()
        for t in trades:
            if t["status"] == "OPEN" and random.randint(1, 8) == 5:
                close_trade(t["symbol"], t["price"] + random.randint(-10, 10))

        time.sleep(10)  # run every 10 sec

# ========== UI ROUTE ==========

@app.route("/")
def dashboard():
    trades = read_trades()
    return render_template("dashboard.html", trades=trades)

# ========== STARTUP ==========

if __name__ == "__main__":
    t = Thread(target=strategy_loop)
    t.daemon = True
    t.start()
    app.run(host="0.0.0.0", port=5000)

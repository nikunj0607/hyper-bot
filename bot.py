# bot.py – Hyper Mode UI + paper trading (no Telegram)

import os, time, math, json, threading, datetime
from typing import List, Dict, Any, Optional
import requests
from flask import Flask, jsonify, render_template_string

# ======= SETTINGS =======
ASSETS = ["ETHUSDT", "BTCUSDT", "SOLUSDT", "BNBUSDT"]
TIMEFRAME = "1h"
MODE = "paper"
LEVERAGE = 5
RISK_PCT = 0.01
SLIPPAGE = 0.0002
FEE = 0.0005
HISTORY_DAYS = 120
REFRESH_SECONDS = 60
MIN_TRADE_GAP = 1
EMA_TREND_LEN = 200
ATR_LEN = 14
ATR_PCT_MIN = 0.004
ATR_PCT_MAX = 0.06
VOL_SMA = 20
VOL_MULT = 1.2
MAX_OPEN_TRADES = 4
START_EQUITY = float(os.getenv("START_EQUITY", "10000"))

# ======= Flask =======
app = Flask(__name__)

@app.route("/")
def root():
    return "hyper-ui ok"

@app.route("/status")
def status():
    return jsonify(bot_status())

@app.route("/trades")
def trades():
    return jsonify(read_trades())

@app.route("/dashboard")
def dashboard():
    trades = read_trades()
    return render_template_string(HTML_TABLE, trades=trades)

# ======= File-backed trade log =======

def read_trades():
    try:
        return json.load(open("trades.json"))
    except:
        return []

def write_trades(data):
    json.dump(data, open("trades.json", "w"), indent=4)

def log_entry(symbol, side, entry, qty):
    t = read_trades()
    t.append({
        "time": str(datetime.datetime.utcnow())[:19],
        "symbol": symbol,
        "side": side,
        "entry": entry,
        "qty": qty,
        "status": "OPEN"
    })
    write_trades(t)

def log_exit(symbol, exit_px, pnl):
    t = read_trades()
    # close last OPEN for this symbol
    for row in reversed(t):
        if row["symbol"] == symbol and row["status"] == "OPEN":
            row["exit"] = exit_px
            row["pnl"] = round(pnl,2)
            row["status"] = "CLOSED"
            break
    write_trades(t)

# ======= Utilities =======
def now_utc(): return datetime.datetime.utcnow().isoformat(timespec="seconds")+"Z"

def safe_get(url, params=None, timeout=20):
    r = requests.get(url, params=params or {}, timeout=timeout)
    r.raise_for_status()
    return r

# ====== Delta ======
BASE = "https://api.india.delta.exchange"
def fetch_candles(symbol, resolution, days):
    end_ts = int(time.time())
    start_ts = end_ts - days*24*3600
    url = f"{BASE}/v2/history/candles"
    params = {"symbol": symbol,"resolution": resolution,"start": start_ts,"end": end_ts}
    rows = safe_get(url, params).json().get("result",[])
    o=[]
    for r in rows:
        o.append({"time":int(r[0]),"open":float(r[1]),"high":float(r[2]),"low":float(r[3]),
                  "close":float(r[4]),"volume":float(r[5])})
    o.sort(key=lambda x:x["time"])
    return o

# ===== indicators =====
def ema_series(values, span):
    if not values: return []
    alpha=2/(span+1)
    out=[values[0]]
    prev=values[0]
    for v in values[1:]:
        prev=alpha*v+(1-alpha)*prev
        out.append(prev)
    return out

def sma(vals,n):
    out=[];s=0;q=[]
    for v in vals:
        q.append(v);s+=v
        if len(q)>n: s-=q.pop(0)
        out.append(s/len(q))
    return out

def true_range(o,h,l,c):
    out=[]
    for i in range(len(c)):
        if i==0: out.append(h[i]-l[i])
        else:
            out.append(max(h[i]-l[i],abs(h[i]-c[i-1]),abs(l[i]-c[i-1])))
    return out

# ===== strategy state =====
class Position:
    def __init__(self, side, entry, stop, qty, ref_index):
        self.side=side
        self.entry=entry
        self.stop=stop
        self.qty=qty
        self.ref_index=ref_index

class SymbolState:
    def __init__(self,s):
        self.symbol=s
        self.candles=[]
        self.pos=None
        self.last_trade_index=-9999

portfolio_value=START_EQUITY
equity_peak=START_EQUITY
symbols={s:SymbolState(s) for s in ASSETS}
lock=threading.Lock()

# ===== indicators build =====
def build_indicators(c):
    closes=[x["close"] for x in c]
    highs=[x["high"] for x in c]
    lows=[x["low"] for x in c]
    vols=[x["volume"] for x in c]
    e200=ema_series(closes,EMA_TREND_LEN)
    tr=true_range([x["open"] for x in c],highs,lows,closes)
    atr=sma(tr,ATR_LEN)
    atrp=[(atr[i]/closes[i] if closes[i]!=0 else None) for i in range(len(c))]
    vsma=sma(vols,VOL_SMA)
    return {"ema200":e200,"atr":atr,"atr_pct":atrp,"vol_sma":vsma}

# ===== position handling =====
def enter_position(sym, side, entry_px, stop_px, risk_cash):
    global portfolio_value
    dist=abs(entry_px-stop_px)
    dist=max(dist,entry_px*0.0001)
    qty=(risk_cash*LEVERAGE)/dist
    fee=entry_px*abs(qty)*FEE
    portfolio_value-=fee
    sym.pos=Position(side,entry_px,stop_px,qty,len(sym.candles)-1)
    print(f"[{now_utc()}] ENTER {sym.symbol} {side} @ {entry_px:.2f}")
    log_entry(sym.symbol,side,entry_px,qty)

def exit_position(sym,exit_px, reason):
    global portfolio_value,equity_peak
    if not sym.pos: return
    pos=sym.pos
    pnl=(exit_px-pos.entry)*pos.qty if pos.side=="long" else (pos.entry-exit_px)*pos.qty
    fee=(pos.entry+exit_px)*abs(pos.qty)*FEE
    pnl-=fee
    portfolio_value+=pnl
    equity_peak=max(equity_peak,portfolio_value)
    print(f"[{now_utc()}] EXIT {sym.symbol} {pos.side} @ {exit_px:.2f} pnl={pnl:.2f} ({reason})")
    log_exit(sym.symbol,exit_px,pnl)
    sym.pos=None
    sym.last_trade_index=len(sym.candles)-1

# ===== strategy engine =====
# (UNCHANGED LOGIC — EXACTLY SAME)
# ... (I keep all strategy_step(), refresh, main_loop same)
def strategy_step(sym: SymbolState):
    global portfolio_value
    c = sym.candles
    if len(c) < max(EMA_TREND_LEN+5, VOL_SMA+5, ATR_LEN+5):
        return
    ind = build_indicators(c)
    ema200 = ind["ema200"]
    atr_pct = ind["atr_pct"]
    vol_sma = ind["vol_sma"]

    i = len(c) - 1
    prev = i - 1
    if prev <= 0: return

    prev_bar = c[prev]
    prev_green = prev_bar["close"] > prev_bar["open"]
    prev_red   = prev_bar["close"] < prev_bar["open"]

    e200 = ema200[prev]
    atrp = atr_pct[prev]
    vavg = vol_sma[prev]
    if e200 is None or atrp is None or vavg is None:
        return

    cur_bar = c[i]
    broke_up = (cur_bar["high"] > prev_bar["high"])
    broke_dn = (cur_bar["low"]  < prev_bar["low"])

    vol_ok = prev_bar["volume"] > vavg * VOL_MULT
    atr_ok = (ATR_PCT_MIN <= atrp <= ATR_PCT_MAX)

    open_positions = sum(1 for s in symbols.values() if s.pos)
    can_open = open_positions < MAX_OPEN_TRADES

    # manage open
    if sym.pos:
        if sym.pos.side == "long" and prev_green:
            sym.pos.stop = max(sym.pos.stop, prev_bar["low"])
        if sym.pos.side == "short" and prev_red:
            sym.pos.stop = min(sym.pos.stop, prev_bar["high"])

        if sym.pos.side=="long" and cur_bar["low"] <= sym.pos.stop:
            exit_position(sym, sym.pos.stop, "stop")
            return
        if sym.pos.side=="short" and cur_bar["high"] >= sym.pos.stop:
            exit_position(sym, sym.pos.stop, "stop")
            return

        if sym.pos.side=="long" and broke_dn and prev_red:
            exit_position(sym, prev_bar["low"], "flip")
            if can_open and (prev_bar["close"] < e200) and vol_ok and atr_ok and (prev - sym.last_trade_index >= MIN_TRADE_GAP):
                entry = prev_bar["low"]*(1-SLIPPAGE)
                stop  = prev_bar["high"]
                enter_position(sym,"short",entry,stop,portfolio_value*RISK_PCT)
            return

        if sym.pos.side=="short" and broke_up and prev_green:
            exit_position(sym, prev_bar["high"], "flip")
            if can_open and (prev_bar["close"] > e200) and vol_ok and atr_ok and (prev - sym.last_trade_index >= MIN_TRADE_GAP):
                entry = prev_bar["high"]*(1+SLIPPAGE)
                stop  = prev_bar["low"]
                enter_position(sym,"long",entry,stop,portfolio_value*RISK_PCT)
            return

        return

    # fresh entries
    if not can_open: return
    if prev - sym.last_trade_index < MIN_TRADE_GAP: return

    if prev_green and broke_up and (prev_bar["close"] > e200) and vol_ok and atr_ok:
        entry = prev_bar["high"]*(1+SLIPPAGE)
        stop  = prev_bar["low"]
        enter_position(sym,"long",entry,stop,portfolio_value*RISK_PCT)
        return

    if prev_red and broke_dn and (prev_bar["close"] < e200) and vol_ok and atr_ok:
        entry = prev_bar["low"]*(1-SLIPPAGE)
        stop  = prev_bar["high"]
        enter_position(sym,"short",entry,stop,portfolio_value*RISK_PCT)
        return


def load_all_history():
    for s in ASSETS:
        try:
            candles = fetch_candles(s, TIMEFRAME, HISTORY_DAYS)
            symbols[s].candles = candles
            print(f"[{now_utc()}] Loaded {len(candles)} candles {s}")
        except Exception as e:
            print(f"[{now_utc()}] Load error {s}: {e}")


def refresh_latest_bar(sym: SymbolState):
    try:
        recent = fetch_candles(sym.symbol, TIMEFRAME, 5)
        if not recent: return
        have = {x["time"]: x for x in sym.candles[-500:]}
        for row in recent:
            have[row["time"]] = row
        merged = list(have.values())
        merged.sort(key=lambda x:x["time"])
        sym.candles = merged
    except Exception as e:
        print(f"[{now_utc()}] refresh error {sym.symbol}: {e}")


def dd_percent():
    if equity_peak<=0: return 0
    return (portfolio_value/equity_peak - 1)*100


def bot_status():
    open_pos=[]
    for s in symbols.values():
        if s.pos:
            open_pos.append({
                "symbol":s.symbol,"side":s.pos.side,
                "entry":s.pos.entry,"stop":s.pos.stop,"qty":s.pos.qty
            })
    return {
        "mode":MODE,
        "tframe":TIMEFRAME,
        "equity":round(portfolio_value,2),
        "dd%":round(dd_percent(),2),
        "open_positions":open_pos,
        "assets":ASSETS
    }


def main_loop():
    global portfolio_value
    print(f"[{now_utc()}] Hyper loop started tf={TIMEFRAME} assets={ASSETS} equity={portfolio_value:.2f}")

    load_all_history()

    while True:
        try:
            with lock:
                for s in ASSETS:
                    refresh_latest_bar(symbols[s])
                for s in ASSETS:
                    strategy_step(symbols[s])
            print(f"[{now_utc()}] tick eq={portfolio_value:.2f} dd%={dd_percent():.2f}")
        except Exception as e:
            print(f"[{now_utc()}] loop error: {e}")

        time.sleep(REFRESH_SECONDS)


def start_background():
    t = threading.Thread(target=main_loop, daemon=True)
    t.start()

start_background()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT","10000")))

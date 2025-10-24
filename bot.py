# ======================================================
# Hyper Bot — FULL LIVE MAINNET multi-asset version
# ======================================================

import os, time, json, threading, datetime
import requests
import hashlib, hmac
from urllib.parse import urlencode
from flask import Flask, render_template, send_file
import matplotlib.pyplot as plt
from io import BytesIO

# ===== TIME =====
def now_ist():
    return (datetime.datetime.now(datetime.timezone.utc)
            + datetime.timedelta(hours=5, minutes=30)
            ).strftime("%Y-%m-%d %H:%M:%S")

def now_utc():
    return datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")

# ===== BUDGET =====
RUPEES_BUDGET = 5000
INR_PER_USD   = 83
portfolio     = RUPEES_BUDGET / INR_PER_USD
peak          = portfolio

# ===== MAIN SETTINGS =====
ASSETS = ["BTCUSD", "ETHUSD", "SOLUSD", "BNBUSD"]
TIMEFRAME = "1h"
LEVERAGE = 5
RISK_PCT = 0.01
SLIPPAGE = 0.0002
FEE = 0.0005
HISTORY_DAYS = 120
REFRESH_SECONDS = 60
EMA_TREND_LEN = 200
ATR_LEN = 14
ATR_PCT_MIN = 0.004
ATR_PCT_MAX = 0.06
VOL_SMA = 20
VOL_MULT = 1.2
MAX_OPEN_TRADES = 4

# ===== LIVE SWITCHES =====
LIVE_TRADING = True
USE_TESTNET  = False   # <-- MAINNET

HISTORY_URL = "https://api.india.delta.exchange"
ORDER_URL   = "https://api.india.delta.exchange"

# ===== KEYS =====
API_KEY    = os.environ.get("DELTA_API_KEY","")
API_SECRET = os.environ.get("DELTA_API_SECRET","")

# ===== SIGN =====
def sign_headers(method,path,params,body):
    ts=str(int(time.time()))
    query=urlencode(params or {})
    payload=method+ts+path+(query if query else "")+(json.dumps(body,separators=(',',':')) if body else "")
    sig=hmac.new(API_SECRET.encode(),payload.encode(),"sha256").hexdigest()
    return {"api-key":API_KEY,"timestamp":ts,"signature":sig,"Accept":"application/json","Content-Type":"application/json"}

# ===== PRODUCT ID =====
_product_cache={}
def product_id(symbol):
    if symbol in _product_cache: return _product_cache[symbol]
    path="/v2/products"
    r=requests.get(ORDER_URL+path,headers=sign_headers("GET",path,None,None))
    for p in r.json().get("result",[]):
        if p["symbol"]==symbol:
            _product_cache[symbol]=p["id"]
            return p["id"]
    raise Exception("product not found")

# ===== LIVE ORDER =====
def place_order(symbol,side,qty,reduce=False):
    if qty<=0: return
    if not LIVE_TRADING:
        print("[SIM]",side,symbol,qty);return
    pid=product_id(symbol)
    body={"product_id":pid,"side":"buy" if side=="long" else "sell",
          "order_type":"market_order","size":round(qty,6),"reduce_only":reduce}
    path="/v2/orders"
    r=requests.post(ORDER_URL+path,headers=sign_headers("POST",path,None,body),data=json.dumps(body))
    print("[LIVE ORDER]",r.json())

def close_order(symbol,qty,side):
    opp="sell" if side=="long" else "buy"
    place_order(symbol,opp,abs(qty),reduce=True)

# ===== TRADES FILE =====
def read_trades():
    try:return json.load(open("trades.json"))
    except:return []

def write_trades(x):
    json.dump(x,open("trades.json","w"),indent=4)

def log_entry(sym,side,entry,qty):
    t=read_trades()
    for r in t:
        if r["symbol"]==sym and r["status"]=="OPEN":return
    t.append({"time":now_ist(),"symbol":sym,"side":side,"entry":entry,"qty":qty,"status":"OPEN"})
    write_trades(t)

def log_exit(sym,px,pnl):
    t=read_trades()
    for r in t[::-1]:
        if r["symbol"]==sym and r["status"]=="OPEN":
            r["exit"]=px;r["pnl"]=round(pnl,2);r["status"]="CLOSED";break
    write_trades(t)

# ===== FETCH CANDLES =====
def fetch_candles(symbol, res, days):
    end = int(time.time())
    start = end - days * 86400

    url = f"{HISTORY_URL}/v2/history/candles"
    p = {
        "symbol": symbol,
        "resolution": res,
        "start": start,
        "end": end
    }

    r = requests.get(url, params=p, headers={"Accept":"application/json"})
    js = r.json().get("result", [])

    out = []
    for c in js:
        # LIST format
        if isinstance(c, list) and len(c) >= 6:
            out.append({
                "open":   c[0],
                "high":   c[1],
                "low":    c[2],
                "close":  c[3],
                "volume": c[4],
                "time":   c[5]
            })
        # DICT format
        elif isinstance(c, dict):
            out.append({
                "open":   c.get("open", 0),
                "high":   c.get("high", 0),
                "low":    c.get("low", 0),
                "close":  c.get("close", 0),
                "volume": c.get("volume", 0),
                "time":   c.get("time", 0)
            })
        # Unknown format (skip)
        else:
            print("⚠️ Unknown candle format:", c)

    return out


# ===== INDICATORS =====
def ema(vals,n):
    out=[];a=2/(n+1);p=vals[0]
    for v in vals:p=a*v+(1-a)*p;out.append(p)
    return out

def sma(values, n):
    out = []
    window = []
    s = 0
    for v in values:
        window.append(v)
        s += v
        if len(window) > n:
            s -= window.pop(0)
        out.append(s / len(window))
    return out



# ===== STATE =====
class P:pass
class S:pass
symbols={s:S() for s in ASSETS}
for s in symbols.values():
    s.candles=[];s.pos=None

# ===== ENTER/EXIT =====
def enter(sym,side,entry,stop):
    global portfolio
    if sym.pos:return

    dist=max(abs(entry-stop),entry*0.0001)
    qty=(portfolio*RISK_PCT*LEVERAGE)/dist

    # clamp by rupee budget
    max_notional=RUPEES_BUDGET*LEVERAGE
    if abs(qty*entry)>max_notional:
        qty*=max_notional/(abs(qty*entry))

    portfolio-=entry*abs(qty)*FEE

    sym.pos=P();sym.pos.side=side;sym.pos.entry=entry;sym.pos.stop=stop;sym.pos.qty=qty
    log_entry(sym.symbol,side,entry,qty)
    place_order(sym.symbol,side,qty)

def exit(sym,px):
    global portfolio,peak
    p=sym.pos;pnl=(px-p.entry)*p.qty if p.side=="long" else (p.entry-px)*p.qty
    fee=(p.entry+px)*abs(p.qty)*FEE;pnl-=fee
    portfolio+=pnl;peak=max(peak,portfolio)
    log_exit(sym.symbol,px,pnl)
    close_order(sym.symbol,p.qty,p.side)
    sym.pos=None

# ===== STRATEGY =====
def step(sym):
    c=sym.candles
    if len(c) < EMA_TREND_LEN + 5: 
        return
    closes=[x["close"] for x in c]
    vols=[x["volume"]for x in c]
    highs=[x["high"]for x in c]
    lows=[x["low"]for x in c]

    e200=ema(closes,200)
    vsma=sma(vols,20)

    i=len(c)-1;p=i-1
    pb,cb=c[p],c[i]
    pg=pb["close"]>pb["open"];pr=pb["close"]<pb["open"]
    up=cb["high"]>pb["high"];dn=cb["low"]<pb["low"]
    vol_ok = vsma[p] > 0 and pb["volume"] > vsma[p] * VOL_MULT


    # manage
    if sym.pos:
        if sym.pos.side=="long" and cb["low"]<=sym.pos.stop:exit(sym,sym.pos.stop);return
        if sym.pos.side=="short" and cb["high"]>=sym.pos.stop:exit(sym,sym.pos.stop);return
        return

    # fresh signals
    if pg and up and pb["close"]>e200[p] and vol_ok:
        enter(sym,"long",pb["high"]*(1+SLIPPAGE),pb["low"])
    if pr and dn and pb["close"]<e200[p] and vol_ok:
        enter(sym,"short",pb["low"]*(1-SLIPPAGE),pb["high"])

# ===== LOOP =====
def refresh(sym):
    try:
        rec = fetch_candles(sym.symbol, TIMEFRAME, 1)
        if not rec:
            return
        hist = {x["time"]: x for x in sym.candles[-400:]}
        for x in rec:
            hist[x["time"]] = x
        arr = list(hist.values())
        arr.sort(key=lambda x: x["time"])
        sym.candles = arr
    except Exception as e:
        print("refresh err", sym.symbol, e)


def loop():
    global portfolio
    for s in ASSETS:
        symbols[s].symbol=s
        symbols[s].candles=fetch_candles(s,TIMEFRAME,HISTORY_DAYS)
        print("[load]",s,len(symbols[s].candles))
    while True:
        for s in ASSETS: refresh(symbols[s])
        for s in ASSETS: step(symbols[s])
        print(now_utc(),"eq=",portfolio)
        time.sleep(REFRESH_SECONDS)

threading.Thread(target=loop,daemon=True).start()

# ===== UI =====
app=Flask(__name__)

@app.route("/dashboard")
def dash():
    t=read_trades()
    for x in t:
        if x["status"]=="OPEN":
            sym=symbols[x["symbol"]]
            last=sym.candles[-1]["close"]
            if x["side"]=="long":x["floating"]=round((last-x["entry"])*x["qty"],2)
            else:x["floating"]=round((x["entry"]-last)*x["qty"],2)
        else:x["floating"]="-"
    eq_usd=round(portfolio,2)
    eq_inr=round(portfolio*INR_PER_USD,2)
    return render_template("dashboard.html",trades=t,eq_usd=eq_usd,eq_inr=eq_inr)

@app.route("/equity")
def eq():
    return "Equity curve coming soon"

if __name__=="__main__":
    app.run(host="0.0.0.0",port=10000)

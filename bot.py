# ==============================
# HYPER LIVE (PAPER) – 1h, Alerts ON, Webhook control
# Assets: ETHUSDT, BTCUSDT, SOLUSDT, BNBUSDT
# Strategy: Candle-break with regime/volume/ATR filters,
#           1% risk/trade (paper), TP1/TP2, trailing stop, flip on opposite break
# ==============================

import os, time, json, datetime, threading
import requests
import pandas as pd
import numpy as np
from flask import Flask, request

# ----------- secrets from env (Render -> Environment) -----------
TG_TOKEN = os.environ.get("TG_TOKEN")           # e.g. 123456:ABC...
CHAT_ID  = int(os.environ.get("CHAT_ID", "0"))  # e.g. 123456789

# ----------- config -----------
ASSETS          = ["ETHUSDT", "BTCUSDT", "SOLUSDT", "BNBUSDT"]
TF              = "1h"                 # timeframe
BASE_URL        = "https://api.delta.exchange"
LOOKBACK_DAYS   = 120                  # history window
SLEEP_SEC       = 60                   # main loop tick
RISK_PER_TRADE  = 0.01                 # 1% risk per trade (paper)
LEVERAGE_NOTE   = 5                    # for info only in alerts
FEE             = 0.0005               # 5 bps/side (paper)
SLIPPAGE        = 0.0002               # 2 bps
CAPITAL_START   = 10_000.0             # paper equity start
STATE_FILE      = "state.json"         # persisted state

# filters
VOL_MULT        = 1.2                   # vol must be > 1.2x 20SMA
ATR_PCT_MIN     = 0.004                 # 0.4% daily-ish per 1h bar proxy
TRAIL_ATR_MULT  = 1.5                   # trailing stop = price - 1.5*ATR (long) / + for short

# take profit in R multiples (R = entry-stop distance)
TP1_R           = 1.0
TP2_R           = 2.0
TP1_SCALE       = 0.5                   # scale out 50% at TP1
TP2_SCALE       = 0.5                   # close remainder at TP2

# control flags
run_trading     = True

# ----------- app (Render) -----------
app = Flask(__name__)


# ----------- telegram helpers -----------
def tg(text: str):
    if not TG_TOKEN or not CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
        requests.post(url, data={"chat_id": CHAT_ID, "text": text})
    except Exception:
        pass


# ----------- persistence -----------
def load_state():
    state = {
        "equity": CAPITAL_START,
        "positions": {},   # per asset: dict(side, entry, stop, qty, tp1_hit, t_entry_time)
        "last_bar_time": {},  # per asset -> last processed candle timestamp (int)
        "pnl_realized": 0.0
    }
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, "r") as f:
                loaded = json.load(f)
            state.update(loaded)
    except Exception:
        pass
    return state

def save_state(state):
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f, default=float)
    except Exception:
        pass


STATE = load_state()  # global runtime state


# ----------- data -----------
def get_candles(symbol: str, tf: str, from_ts: int, to_ts: int):
    url = f"{BASE_URL}/v2/history/candles"
    params = {"symbol": symbol, "resolution": tf, "start": from_ts, "end": to_ts}
    r = requests.get(url, params=params, timeout=30)
    r.raise_for_status()
    data = r.json().get("result", [])
    if not data:
        return pd.DataFrame()
    df = pd.DataFrame(data, columns=["time","open","high","low","close","volume"])
    df["time"] = pd.to_datetime(df["time"], unit="s")
    for c in ["open","high","low","close","volume"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df.dropna().sort_values("time").reset_index(drop=True)

def fetch_history(symbol: str):
    now = int(time.time())
    start = now - LOOKBACK_DAYS * 24 * 3600
    df = get_candles(symbol, TF, start, now)
    if df.empty:
        return df
    # indicators
    df["ema200"] = df["close"].ewm(span=200, adjust=False).mean()
    df["vol_sma20"] = df["volume"].rolling(20, min_periods=20).mean()
    # ATR (classic)
    cprev = df["close"].shift(1)
    tr = pd.concat([(df["high"]-df["low"]).abs(),
                    (df["high"]-cprev).abs(),
                    (df["low"]-cprev).abs()], axis=1).max(axis=1)
    df["atr"] = tr.rolling(14, min_periods=14).mean()
    # ATR%
    df["atr_pct"] = df["atr"] / df["close"]
    return df


# ----------- strategy rules -----------
def candle_break_signal(df: pd.DataFrame):
    """Return last closed candle-based signal: BUY/SELL/None with filters."""
    if len(df) < 205:
        return None, None

    # use last two CLOSED candles: c2 = just closed, c3 = previous
    c3 = df.iloc[-3]  # setup candle
    c2 = df.iloc[-2]  # break detection (just closed)

    # filters
    regime_up   = c3.close > c3.ema200
    regime_down = c3.close < c3.ema200
    vol_ok      = c3.volume > VOL_MULT * max(1e-9, c3.vol_sma20)
    atr_ok      = c3.atr_pct >= ATR_PCT_MIN

    # LONG entry: green c3 + c2 breaks c3 high + filters in up regime
    if (c3.close > c3.open) and (c2.high > c3.high) and regime_up and vol_ok and atr_ok:
        stop = c3.low
        return "BUY", stop

    # SHORT entry: red c3 + c2 breaks c3 low + filters in down regime
    if (c3.close < c3.open) and (c2.low < c3.low) and regime_down and vol_ok and atr_ok:
        stop = c3.high
        return "SELL", stop

    return None, None


# ----------- sizing & PnL (paper) -----------
def fees_for(notional):
    return notional * FEE

def slippage_on(px, side):
    # simple spread: buy worse, sell worse
    return px * (1 + SLIPPAGE) if side == "BUY" else px * (1 - SLIPPAGE)

def enter_position(asset, side, entry_px, stop_px):
    """Risk-based qty: risk = equity*RISK, qty = risk / |entry-stop|."""
    risk_cap = STATE["equity"] * RISK_PER_TRADE
    dist = max(1e-9, abs(entry_px - stop_px))
    qty = risk_cap / dist

    # fees on entry (paper)
    notional = entry_px * qty
    cost = fees_for(notional)

    pos = {
        "side": side, "entry": entry_px, "stop": stop_px,
        "qty": qty, "tp1_hit": False, "t_entry_time": int(time.time())
    }
    STATE["positions"][asset] = pos
    STATE["equity"] -= cost  # pay entry fee in paper
    tg(f"⚡ {asset} {side} @ {entry_px:.2f} | stop {stop_px:.2f} | qty {qty:.6f} | lev~{LEVERAGE_NOTE}x (paper)")
    save_state(STATE)

def close_position(asset, exit_px, reason: str):
    pos = STATE["positions"].get(asset)
    if not pos:
        return
    side = pos["side"]
    qty  = pos["qty"]
    entry = pos["entry"]

    # PnL (paper futures style)
    pnl = (exit_px - entry) * qty if side == "BUY" else (entry - exit_px) * qty

    # fees (entry already paid; charge exit now)
    notional_exit = exit_px * qty
    fee_exit = fees_for(notional_exit)

    net = pnl - fee_exit
    STATE["equity"] += net
    STATE["pnl_realized"] += net
    tg(f"🧾 {asset} EXIT {reason} @ {exit_px:.2f} | PnL {net:+.2f} | Eq {STATE['equity']:.2f}")
    del STATE["positions"][asset]
    save_state(STATE)

def manage_open_position(asset, last_row):
    """TP1/TP2 & ATR trailing & hard SL & flip check."""
    pos = STATE["positions"].get(asset)
    if not pos:
        return

    side = pos["side"]
    qty  = pos["qty"]
    entry= pos["entry"]
    stop = pos["stop"]
    tp1_hit = pos["tp1_hit"]

    # R distance
    R = abs(entry - stop)

    # live ATR trailing (based on last_row.atr)
    if side == "BUY":
        trail = last_row.close - TRAIL_ATR_MULT * last_row.atr
        new_stop = max(stop, trail)
    else:
        trail = last_row.close + TRAIL_ATR_MULT * last_row.atr
        new_stop = min(stop, trail)

    if abs(new_stop - stop) > 1e-9:
        pos["stop"] = new_stop
        STATE["positions"][asset] = pos
        save_state(STATE)

    price = last_row.close

    # TP1/TP2
    if side == "BUY":
        if (not tp1_hit) and price >= entry + TP1_R * R:
            # scale out TP1
            exit_px = price
            part_qty = qty * TP1_SCALE
            # realize proportional PnL on the part
            notional = exit_px * part_qty
            pnl = (exit_px - entry) * part_qty - fees_for(notional)
            STATE["equity"] += pnl
            pos["qty"] = qty * (1 - TP1_SCALE)
            pos["tp1_hit"] = True
            tg(f"🎯 {asset} TP1 +{TP1_R:.1f}R @ {exit_px:.2f} | +{pnl:.2f}")
            save_state(STATE)
        elif price >= entry + TP2_R * R:
            close_position(asset, price, f"TP2 +{TP2_R:.1f}R")
            return

        # hard/trailed stop
        if price <= pos["stop"]:
            close_position(asset, pos["stop"], "SL (trail)")
            return

    else:  # SHORT
        if (not tp1_hit) and price <= entry - TP1_R * R:
            exit_px = price
            part_qty = qty * TP1_SCALE
            notional = exit_px * part_qty
            pnl = (entry - exit_px) * part_qty - fees_for(notional)
            STATE["equity"] += pnl
            pos["qty"] = qty * (1 - TP1_SCALE)
            pos["tp1_hit"] = True
            tg(f"🎯 {asset} TP1 +{TP1_R:.1f}R @ {exit_px:.2f} | +{pnl:.2f}")
            save_state(STATE)
        elif price <= entry - TP2_R * R:
            close_position(asset, price, f"TP2 +{TP2_R:.1f}R")
            return

        if price >= pos["stop"]:
            close_position(asset, pos["stop"], "SL (trail)")
            return


# ----------- main loop -----------
def process_asset(asset):
    df = fetch_history(asset)
    if df.empty or len(df) < 205:
        return

    # only run once per closed bar
    last_ts = int(df.iloc[-2].time.value // 10**9)  # last CLOSED candle timestamp
    if STATE["last_bar_time"].get(asset) == last_ts:
        return  # already processed this bar
    STATE["last_bar_time"][asset] = last_ts
    save_state(STATE)

    # manage open first (using last closed)
    manage_open_position(asset, df.iloc[-2])

    # entry/flip
    sig, stop = candle_break_signal(df)
    if not sig:
        return

    pos = STATE["positions"].get(asset)

    # flip logic: if opposite signal, close then open new
    if pos:
        current_side = pos["side"]
        if (current_side == "BUY" and sig == "SELL") or (current_side == "SELL" and sig == "BUY"):
            close_position(asset, df.iloc[-2].close, "Flip")
            # re-enter below

    # if flat, take entry
    if asset not in STATE["positions"]:
        entry_px = df.iloc[-2].close
        entry_px = slippage_on(entry_px, "BUY" if sig == "BUY" else "SELL")
        enter_position(asset, sig, entry_px, stop)


def hyper_worker():
    tg("🚀 Hyper LIVE (PAPER) started — 1h TF | Alerts ON")
    while True:
        try:
            if not run_trading:
                time.sleep(SLEEP_SEC)
                continue

            for a in ASSETS:
                process_asset(a)

            # heartbeat
            print(f"[{datetime.datetime.now(datetime.UTC).isoformat()}] cycle — Eq={STATE['equity']:.2f}")
        except Exception as e:
            tg(f"❗ worker error: {e}")
        finally:
            time.sleep(SLEEP_SEC)


# ----------- webhook routes -----------
@app.route("/", methods=["GET"])
def home():
    return "hyper alive"

@app.route(f"/{TG_TOKEN}", methods=["POST"])
def webhook():
    global run_trading
    try:
        data = request.get_json(force=True, silent=True) or {}
        msg  = data.get("message", {})
        text = (msg.get("text") or "").strip().lower()
        cid  = msg.get("chat", {}).get("id")
        if CHAT_ID and cid != CHAT_ID:
            return "ignored"

        if text in ("/ping", "ping"):
            tg("✅ alive")
        elif text in ("/pause", "pause"):
            run_trading = False
            tg("⏸ paused")
        elif text in ("/resume", "resume"):
            run_trading = True
            tg("▶ resumed")
        elif text in ("/status", "status"):
            open_pos = {k: v["side"] for k, v in STATE["positions"].items()}
            tg(f"📊 Eq={STATE['equity']:.2f} | Open={open_pos}")
        elif text in ("/help", "help"):
            tg("/ping /status /pause /resume")
        else:
            tg("❓ unknown. use /help")
    except Exception as e:
        tg(f"❗ webhook error: {e}")
    return "ok"


# ----------- background start -----------
t = threading.Thread(target=hyper_worker, daemon=True)
t.start()

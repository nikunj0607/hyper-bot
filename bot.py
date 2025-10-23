# bot.py
# Hyper Mode — 4 assets, 1h, paper trading, Telegram alerts (no pandas/numpy)
# Works on Render free plan (Flask + requests only)

import os, time, math, json, threading, datetime
from typing import List, Dict, Any, Optional
import requests
from flask import Flask, jsonify

# ======= SETTINGS (edit here if you want) =======
ASSETS = ["ETHUSDT", "BTCUSDT", "SOLUSDT", "BNBUSDT"]
TIMEFRAME = "1h"                 # fixed as per your ask
MODE = "paper"                   # paper | live (only paper implemented)
LEVERAGE = 5
RISK_PCT = 0.01                  # 1% per trade (paper)
SLIPPAGE = 0.0002                # 0.02% slippage
FEE = 0.0005                     # 0.05% per side
HISTORY_DAYS = 120              # how much to load initially
REFRESH_SECONDS = 60             # main loop heartbeat
MIN_TRADE_GAP = 1               # bars to wait after a flip to avoid whipsaw

# Filters (Hyper-ish)
EMA_TREND_LEN = 200             # regime filter
ATR_LEN = 14                    # volatility calc
ATR_PCT_MIN = 0.004             # 0.4% min vol
ATR_PCT_MAX = 0.06              # 6% max vol
VOL_SMA = 20                    # volume average window
VOL_MULT = 1.2                  # require vol > 1.2x avg
MAX_OPEN_TRADES = 4             # allow parallel across symbols (paper)

# Capital
START_EQUITY = float(os.getenv("START_EQUITY", "10000"))

# Telegram (read from secrets.py)
try:
    import secrets  # create your own secrets.py (see below)
    TG_TOKEN = getattr(secrets, "TELEGRAM_TOKEN", "")
    TG_CHAT_ID = getattr(secrets, "TELEGRAM_CHAT_ID", "")
except Exception:
    TG_TOKEN = ""
    TG_CHAT_ID = ""

# ======= Flask app (health + quick status) =======
app = Flask(__name__)

@app.route("/")
def root():
    return "hyper-bot ok"

@app.route("/status")
def status():
    return jsonify(bot_status())

# ======= Utilities =======
def now_utc() -> str:
    # for logs
    return datetime.datetime.utcnow().isoformat(timespec="seconds") + "Z"

def tg_send(text: str):
    if not TG_TOKEN or not TG_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": TG_CHAT_ID, "text": text[:4000]})
    except Exception:
        pass

def safe_get(url: str, params: Dict[str, Any] = None, timeout: int = 20):
    r = requests.get(url, params=params or {}, timeout=timeout)
    r.raise_for_status()
    return r

# ======= Data fetch (Delta Exchange) =======
BASE = "https://api.india.delta.exchange"
def fetch_candles(symbol: str, resolution: str, days: int) -> List[Dict[str, Any]]:
    """
    Returns list of dict: {time, open, high, low, close, volume}
    """
    end_ts = int(time.time())
    start_ts = end_ts - days * 24 * 3600
    url = f"{BASE}/v2/history/candles"
    params = {
        "symbol": symbol,
        "resolution": resolution,
        "start": start_ts,
        "end": end_ts
    }
    r = safe_get(url, params)
    js = r.json()
    rows = js.get("result", [])
    out = []
    for row in rows:
        # row: [time, open, high, low, close, volume]
        out.append({
            "time": int(row[0]),
            "open": float(row[1]),
            "high": float(row[2]),
            "low": float(row[3]),
            "close": float(row[4]),
            "volume": float(row[5])
        })
    out.sort(key=lambda x: x["time"])
    return out

# ======= Indicators (pure python) =======
def ema_series(values: List[float], span: int) -> List[float]:
    if not values: return []
    alpha = 2.0 / (span + 1.0)
    out = []
    prev = values[0]
    out.append(prev)
    for i in range(1, len(values)):
        prev = alpha * values[i] + (1 - alpha) * prev
        out.append(prev)
    return out

def true_range(o: List[float], h: List[float], l: List[float], c: List[float]) -> List[float]:
    out = []
    for i in range(len(c)):
        if i == 0:
            out.append(h[i] - l[i])
        else:
            x = max(h[i] - l[i], abs(h[i] - c[i-1]), abs(l[i] - c[i-1]))
            out.append(x)
    return out

def sma(values: List[float], n: int) -> List[Optional[float]]:
    out = []
    s = 0.0
    q = []
    for v in values:
        q.append(v)
        s += v
        if len(q) > n:
            s -= q.pop(0)
        out.append(s / len(q))
    return out

# ======= Strategy: Candle Break with Hyper Filters =======
"""
Rules (per your spec):
- Long setup: wait for a GREEN candle (close>open) to close.
  Enter long if next candle breaks that green candle's HIGH.
  Initial SL at that green candle's LOW.
  Trail SL to each new green candle's LOW while in long.
- Short setup: inverse with red candle.
- Hyper filters:
  * price above EMA200 for longs / below for shorts
  * ATR% in [ATR_PCT_MIN, ATR_PCT_MAX]
  * volume > VOL_MULT * SMA(VOL_SMA)
  * optional bar spacing to avoid immediate flip (MIN_TRADE_GAP)
"""

class Position:
    def __init__(self, side: str, entry: float, stop: float, qty: float, ref_index: int):
        self.side = side              # "long" or "short"
        self.entry = entry
        self.stop = stop
        self.qty = qty
        self.ref_index = ref_index    # candle index used for signal (for gap)
        self.open_time = int(time.time())

class SymbolState:
    def __init__(self, symbol: str):
        self.symbol = symbol
        self.candles: List[Dict[str, Any]] = []
        self.pos: Optional[Position] = None
        self.last_trade_index: int = -99999

# Global portfolio
portfolio_value = START_EQUITY
equity_peak = START_EQUITY
symbols: Dict[str, SymbolState] = {s: SymbolState(s) for s in ASSETS}
lock = threading.Lock()

def build_indicators(c: List[Dict[str, Any]]) -> Dict[str, List[Optional[float]]]:
    closes = [x["close"] for x in c]
    highs  = [x["high"]  for x in c]
    lows   = [x["low"]   for x in c]
    vols   = [x["volume"] for x in c]
    ema200 = ema_series(closes, EMA_TREND_LEN)
    tr = true_range([x["open"] for x in c], highs, lows, closes)
    atr = sma(tr, ATR_LEN)
    # ATR% relative to close
    atr_pct = []
    for i in range(len(c)):
        if atr[i] is None or closes[i] == 0:
            atr_pct.append(None)
        else:
            atr_pct.append(atr[i] / max(1e-9, closes[i]))
    vol_sma = sma(vols, VOL_SMA)
    return {
        "ema200": ema200,
        "atr": atr,
        "atr_pct": atr_pct,
        "vol_sma": vol_sma
    }

def enter_position(sym: SymbolState, side: str, entry_px: float, stop_px: float, risk_cash: float):
    global portfolio_value
    dist = abs(entry_px - stop_px)
    dist = max(dist, entry_px * 0.0001)  # min stop distance 1 bps
    # qty uses 1% risk with leverage
    qty = (risk_cash * LEVERAGE) / dist
    # fees on entry
    fee_cost = entry_px * abs(qty) * FEE
    portfolio_value -= fee_cost
    sym.pos = Position(side, entry_px, stop_px, qty, ref_index=len(sym.candles)-1)
    tg_send(f"🟢 ENTER {sym.symbol} {side.upper()} | entry={entry_px:.4f} stop={stop_px:.4f} qty={qty:.6f}")
    print(f"[{now_utc()}] ENTER {sym.symbol} {side} entry={entry_px:.4f} stop={stop_px:.4f} qty={qty:.6f}")

def exit_position(sym: SymbolState, exit_px: float, reason: str):
    global portfolio_value, equity_peak
    if not sym.pos:
        return
    pos = sym.pos
    # PnL
    pnl = (exit_px - pos.entry) * pos.qty if pos.side == "long" else (pos.entry - exit_px) * pos.qty
    fee_cost = (pos.entry + exit_px) * abs(pos.qty) * FEE
    pnl_after = pnl - fee_cost
    portfolio_value += pnl_after
    equity_peak = max(equity_peak, portfolio_value)
    tg_send(f"🔻 EXIT {sym.symbol} {pos.side.upper()} | exit={exit_px:.4f} PnL={pnl_after:.2f} ({reason}) | equity={portfolio_value:.2f}")
    print(f"[{now_utc()}] EXIT {sym.symbol} {pos.side} exit={exit_px:.4f} pnl={pnl_after:.2f} reason={reason} equity={portfolio_value:.2f}")
    sym.pos = None
    sym.last_trade_index = len(sym.candles)-1

def strategy_step(sym: SymbolState):
    global portfolio_value
    c = sym.candles
    if len(c) < max(EMA_TREND_LEN + 5, VOL_SMA + 5, ATR_LEN + 5):
        return
    ind = build_indicators(c)
    ema200 = ind["ema200"]
    atr_pct = ind["atr_pct"]
    vol_sma = ind["vol_sma"]

    i = len(c) - 1               # current (forming) candle
    prev = i - 1                 # last closed candle
    if prev <= 0: return

    # Last closed candle details
    prev_bar = c[prev]
    prev_green = prev_bar["close"] > prev_bar["open"]
    prev_red = prev_bar["close"] < prev_bar["open"]

    # regime/filters read at prev (close of signal candle)
    e200 = ema200[prev]
    atrp = atr_pct[prev]
    vavg = vol_sma[prev]
    if e200 is None or atrp is None or vavg is None:
        return

    # current highs/lows (we only simulate "break" within current bar)
    cur_bar = c[i]
    broke_up = (cur_bar["high"] > prev_bar["high"])
    broke_dn = (cur_bar["low"]  < prev_bar["low"])

    # Filters
    vol_ok = prev_bar["volume"] > vavg * VOL_MULT
    vol_ok = vol_ok if not math.isnan(prev_bar["volume"]) else False
    atr_ok = (ATR_PCT_MIN <= atrp <= ATR_PCT_MAX)

    # MAX concurrent positions — portfolio cap (paper)
    open_positions = sum(1 for s in symbols.values() if s.pos)
    can_open = open_positions < MAX_OPEN_TRADES

    # Manage open position
    if sym.pos:
        # trailing with each favorable candle’s extreme
        if sym.pos.side == "long" and prev_green:
            sym.pos.stop = max(sym.pos.stop, prev_bar["low"])
        if sym.pos.side == "short" and prev_red:
            sym.pos.stop = min(sym.pos.stop, prev_bar["high"])

        # stop hit?
        if sym.pos.side == "long" and cur_bar["low"] <= sym.pos.stop:
            exit_position(sym, sym.pos.stop, "stop")
            return
        if sym.pos.side == "short" and cur_bar["high"] >= sym.pos.stop:
            exit_position(sym, sym.pos.stop, "stop")
            return

        # opposite break = flip
        if sym.pos.side == "long" and broke_dn and prev_red:
            exit_position(sym, prev_bar["low"], "flip")
            # optional: open short (respect filters)
            if can_open and (prev_bar["close"] < e200) and vol_ok and atr_ok and (prev - sym.last_trade_index >= MIN_TRADE_GAP):
                entry = prev_bar["low"] * (1 - SLIPPAGE)
                stop = prev_bar["high"]
                enter_position(sym, "short", entry, stop, portfolio_value * RISK_PCT)
            return

        if sym.pos.side == "short" and broke_up and prev_green:
            exit_position(sym, prev_bar["high"], "flip")
            # optional: open long
            if can_open and (prev_bar["close"] > e200) and vol_ok and atr_ok and (prev - sym.last_trade_index >= MIN_TRADE_GAP):
                entry = prev_bar["high"] * (1 + SLIPPAGE)
                stop = prev_bar["low"]
                enter_position(sym, "long", entry, stop, portfolio_value * RISK_PCT)
            return

        return

    # No position: look for fresh entries
    if not can_open:
        return

    # Respect small gap since last trade to reduce noise
    if prev - sym.last_trade_index < MIN_TRADE_GAP:
        return

    # LONG
    if prev_green and broke_up and (prev_bar["close"] > e200) and vol_ok and atr_ok:
        entry = prev_bar["high"] * (1 + SLIPPAGE)
        stop = prev_bar["low"]
        enter_position(sym, "long", entry, stop, portfolio_value * RISK_PCT)
        return

    # SHORT
    if prev_red and broke_dn and (prev_bar["close"] < e200) and vol_ok and atr_ok:
        entry = prev_bar["low"] * (1 - SLIPPAGE)
        stop = prev_bar["high"]
        enter_position(sym, "short", entry, stop, portfolio_value * RISK_PCT)
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
    # For 1h we can just refetch last ~3 bars quickly
    try:
        recent = fetch_candles(sym.symbol, TIMEFRAME, 5)
        if not recent:
            return
        # merge by time (keep unique)
        have = {x["time"]: x for x in sym.candles[-500:]}  # small map
        for row in recent:
            have[row["time"]] = row
        merged = list(have.values())
        merged.sort(key=lambda x: x["time"])
        # keep last N
        sym.candles = (sym.candles[:-500] + merged) if len(sym.candles) > 500 else merged
    except Exception as e:
        print(f"[{now_utc()}] refresh error {sym.symbol}: {e}")

def dd_percent() -> float:
    if equity_peak <= 0: return 0.0
    return (portfolio_value / equity_peak - 1.0) * 100.0

def bot_status() -> Dict[str, Any]:
    open_pos = []
    for s in symbols.values():
        if s.pos:
            open_pos.append({
                "symbol": s.symbol, "side": s.pos.side,
                "entry": s.pos.entry, "stop": s.pos.stop, "qty": s.pos.qty
            })
    return {
        "mode": MODE,
        "tframe": TIMEFRAME,
        "equity": round(portfolio_value, 2),
        "dd%": round(dd_percent(), 2),
        "open_positions": open_pos,
        "assets": ASSETS
    }

def main_loop():
    global portfolio_value
    print(f"[{now_utc()}] Hyper loop started — mode={MODE} tf={TIMEFRAME} assets={ASSETS} equity={portfolio_value:.2f}")
    tg_send(f"🚀 Hyper bot started | mode={MODE} tf={TIMEFRAME} | assets={', '.join(ASSETS)} | equity={portfolio_value:.2f}")

    load_all_history()

    while True:
        try:
            with lock:
                for s in ASSETS:
                    refresh_latest_bar(symbols[s])
                for s in ASSETS:
                    strategy_step(symbols[s])

            print(f"[{now_utc()}] tick… equity={portfolio_value:.2f} dd%={dd_percent():.2f}")
        except Exception as e:
            print(f"[{now_utc()}] loop error: {e}")

        time.sleep(REFRESH_SECONDS)

# Start the background loop once Gunicorn boots the app
def start_background():
    t = threading.Thread(target=main_loop, daemon=True)
    t.start()

start_background()

if __name__ == "__main__":
    # local run: python bot.py
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))

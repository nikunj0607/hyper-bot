# bot.py
# Hyper Mode — 4 assets, 1h, paper trading, Telegram alerts
# Works on Render free plan (Flask + requests only, no pandas/numpy)

import os, time, math, json, threading, datetime
from typing import List, Dict, Any, Optional
import requests
from flask import Flask, jsonify

# ======= SETTINGS =======
ASSETS = ["ETHUSDT", "BTCUSDT", "SOLUSDT", "BNBUSDT"]
TIMEFRAME = "1h"                 # fixed per your ask
MODE = "paper"                   # paper | live (only paper implemented)
LEVERAGE = 5
RISK_PCT = 0.01                  # 1% risk-per-trade
SLIPPAGE = 0.0002                # 0.02% slippage
FEE = 0.0005                     # 0.05% per side
HISTORY_DAYS = 120               # initial history
REFRESH_SECONDS = 60             # main loop heartbeat
MIN_TRADE_GAP = 1                # bars to wait after a flip

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
    import secrets  # create your own secrets.py (see README)
    TG_TOKEN = getattr(secrets, "TELEGRAM_TOKEN", "")
    TG_CHAT_ID = getattr(secrets, "TELEGRAM_CHAT_ID", "")
except Exception:
    TG_TOKEN = ""
    TG_CHAT_ID = ""

# ======= Flask app =======
app = Flask(__name__)

@app.route("/")
def root():
    return "hyper-bot ok"

@app.route("/status")
def status():
    return jsonify(bot_status())

def now_utc() -> str:
    return datetime.datetime.utcnow().isoformat(timespec="seconds") + "Z"

def tg_send(text: str):
    if not TG_TOKEN or not TG_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": TG_CHAT_ID, "text": text[:4000]}, timeout=20)
    except Exception:
        pass

def safe_get(url: str, params: Dict[str, Any] = None, timeout: int = 25) -> requests.Response:
    r = requests.get(url, params=params or {}, timeout=timeout)
    r.raise_for_status()
    return r

# ======= Data fetch (Delta India) =======
BASE = "https://api.india.delta.exchange"

# Some markets on Delta are USD-quoted (no USDT). Try remap if first attempt fails.
SYMBOL_REMAP = {
    "ETHUSDT": "ETHUSD",
    "BTCUSDT": "BTCUSD",
    "SOLUSDT": "SOLUSD",
    "BNBUSDT": "BNBUSD",
}

def _parse_candles_rows(rows: List) -> List[Dict[str, Any]]:
    """Accept rows like [time, o, h, l, c] or [time, o, h, l, c, v]."""
    out = []
    for row in rows:
        # Sparkline sometimes returns list of lists or dicts; normalize
        if isinstance(row, dict):
            # try common keys
            t = int(row.get("time") or row.get("timestamp") or 0)
            o = float(row.get("open", 0))
            h = float(row.get("high", 0))
            l = float(row.get("low", 0))
            c = float(row.get("close", 0))
            v = float(row.get("volume", 0)) if row.get("volume") is not None else None
        else:
            # list/tuple variants
            t = int(row[0])
            o = float(row[1])
            h = float(row[2])
            l = float(row[3])
            c = float(row[4])
            v = float(row[5]) if len(row) > 5 else None
        out.append({"time": t, "open": o, "high": h, "low": l, "close": c, "volume": v})
    out.sort(key=lambda x: x["time"])
    return out

def fetch_candles(symbol: str, resolution: str, days: int) -> List[Dict[str, Any]]:
    """
    Robust loader:
    1) /v2/history/candles with given symbol
    2) /v2/history/candles with remapped symbol (ETHUSD etc.)
    3) /v2/history/sparkline (symbol or remap)
    Returns list of dict: {time, open, high, low, close, volume or None}
    """
    end_ts = int(time.time())
    start_ts = end_ts - days * 24 * 3600

    # 1) candles original
    try:
        url = f"{BASE}/v2/history/candles"
        params = {"symbol": symbol, "resolution": resolution, "start": start_ts, "end": end_ts}
        r = safe_get(url, params)
        js = r.json()
        rows = js.get("result", [])
        if rows:
            out = _parse_candles_rows(rows)
            print(f"[{now_utc()}] candles ok {symbol} n={len(out)}")
            return out
        else:
            print(f"[{now_utc()}] candles empty {symbol} payload={js}")
    except Exception as e:
        print(f"[{now_utc()}] candles error {symbol}: {e}")

    # 2) candles remapped
    alt = SYMBOL_REMAP.get(symbol, symbol)
    if alt != symbol:
        try:
            url = f"{BASE}/v2/history/candles"
            params = {"symbol": alt, "resolution": resolution, "start": start_ts, "end": end_ts}
            r = safe_get(url, params)
            js = r.json()
            rows = js.get("result", [])
            if rows:
                out = _parse_candles_rows(rows)
                print(f"[{now_utc()}] candles ok {symbol}->{alt} n={len(out)}")
                return out
            else:
                print(f"[{now_utc()}] candles empty {symbol}->{alt} payload={js}")
        except Exception as e:
            print(f"[{now_utc()}] candles error {symbol}->{alt}: {e}")

    # 3) sparkline original
    try:
        url = f"{BASE}/v2/history/sparkline"
        params = {"symbol": symbol, "resolution": resolution, "start": start_ts, "end": end_ts}
        r = safe_get(url, params)
        js = r.json()
        rows = js.get("result", []) or js.get("prices", []) or js.get("data", [])
        if rows:
            out = _parse_candles_rows(rows)
            print(f"[{now_utc()}] sparkline ok {symbol} n={len(out)}")
            return out
        else:
            print(f"[{now_utc()}] sparkline empty {symbol} payload={js}")
    except Exception as e:
        print(f"[{now_utc()}] sparkline error {symbol}: {e}")

    # 4) sparkline remapped
    if alt != symbol:
        try:
            url = f"{BASE}/v2/history/sparkline"
            params = {"symbol": alt, "resolution": resolution, "start": start_ts, "end": end_ts}
            r = safe_get(url, params)
            js = r.json()
            rows = js.get("result", []) or js.get("prices", []) or js.get("data", [])
            if rows:
                out = _parse_candles_rows(rows)
                print(f"[{now_utc()}] sparkline ok {symbol}->{alt} n={len(out)}")
                return out
            else:
                print(f"[{now_utc()}] sparkline empty {symbol}->{alt} payload={js}")
        except Exception as e:
            print(f"[{now_utc()}] sparkline error {symbol}->{alt}: {e}")

    # give up
    print(f"[{now_utc()}] fetch failed {symbol} (all fallbacks)")
    return []

# ======= Indicators (pure python) =======
def ema_series(values: List[float], span: int) -> List[float]:
    if not values: return []
    alpha = 2.0 / (span + 1.0)
    out = [values[0]]
    prev = values[0]
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

def sma(values: List[Optional[float]], n: int) -> List[Optional[float]]:
    out, s, q = [], 0.0, []
    for v in values:
        if v is None:
            out.append(None if not q else s / len(q))
            continue
        q.append(v); s += v
        if len(q) > n:
            s -= q.pop(0)
        out.append(s / len(q))
    return out

# ======= Strategy =======
class Position:
    def __init__(self, side: str, entry: float, stop: float, qty: float, ref_index: int):
        self.side = side
        self.entry = entry
        self.stop = stop
        self.qty = qty
        self.ref_index = ref_index
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
    opens  = [x["open"]   for x in c]
    highs  = [x["high"]   for x in c]
    lows   = [x["low"]    for x in c]
    closes = [x["close"]  for x in c]
    vols   = [x["volume"] for x in c]  # may be None

    ema200 = ema_series(closes, EMA_TREND_LEN)
    tr = true_range(opens, highs, lows, closes)
    atr = sma(tr, ATR_LEN)
    atr_pct = []
    for i in range(len(c)):
        if atr[i] is None or closes[i] == 0:
            atr_pct.append(None)
        else:
            atr_pct.append(atr[i] / max(1e-9, closes[i]))
    # volume SMA: if volume missing, keep None (later we’ll bypass volume filter)
    vol_sma = sma(vols, VOL_SMA) if any(v is not None for v in vols) else [None]*len(c)
    return {"ema200": ema200, "atr_pct": atr_pct, "vol_sma": vol_sma}

def enter_position(sym: SymbolState, side: str, entry_px: float, stop_px: float, risk_cash: float):
    global portfolio_value
    dist = abs(entry_px - stop_px)
    dist = max(dist, entry_px * 0.0001)  # min stop distance
    qty = (risk_cash * LEVERAGE) / dist
    fee_cost = entry_px * abs(qty) * FEE
    portfolio_value -= fee_cost
    sym.pos = Position(side, entry_px, stop_px, qty, ref_index=len(sym.candles)-1)
    tg_send(f"🟢 ENTER {sym.symbol} {side.upper()} | entry={entry_px:.4f} stop={stop_px:.4f} qty={qty:.6f}")
    print(f"[{now_utc()}] ENTER {sym.symbol} {side} entry={entry_px:.4f} stop={stop_px:.4f} qty={qty:.6f}")

def exit_position(sym: SymbolState, exit_px: float, reason: str):
    global portfolio_value, equity_peak
    if not sym.pos: return
    pos = sym.pos
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
    ema200 = ind["ema200"]; atr_pct = ind["atr_pct"]; vol_sma = ind["vol_sma"]

    i = len(c) - 1
    prev = i - 1
    if prev <= 0: return

    prev_bar = c[prev]
    prev_green = prev_bar["close"] > prev_bar["open"]
    prev_red   = prev_bar["close"] < prev_bar["open"]

    e200 = ema200[prev]
    atrp = atr_pct[prev]
    vavg = vol_sma[prev]

    if e200 is None or atrp is None:
        return

    cur_bar = c[i]
    broke_up = (cur_bar["high"] > prev_bar["high"])
    broke_dn = (cur_bar["low"]  < prev_bar["low"])

    # Volume filter: if exchange doesn’t send volume, bypass it
    if prev_bar.get("volume") is None or vavg is None:
        vol_ok = True
    else:
        vol_ok = prev_bar["volume"] > vavg * VOL_MULT

    atr_ok = (ATR_PCT_MIN <= atrp <= ATR_PCT_MAX)
    open_positions = sum(1 for s in symbols.values() if s.pos)
    can_open = open_positions < MAX_OPEN_TRADES

    # Manage open position
    if sym.pos:
        if sym.pos.side == "long" and prev_green:
            sym.pos.stop = max(sym.pos.stop, prev_bar["low"])
        if sym.pos.side == "short" and prev_red:
            sym.pos.stop = min(sym.pos.stop, prev_bar["high"])

        if sym.pos.side == "long" and cur_bar["low"] <= sym.pos.stop:
            exit_position(sym, sym.pos.stop, "stop"); return
        if sym.pos.side == "short" and cur_bar["high"] >= sym.pos.stop:
            exit_position(sym, sym.pos.stop, "stop"); return

        if sym.pos.side == "long" and broke_dn and prev_red:
            exit_position(sym, prev_bar["low"], "flip")
            if can_open and (prev_bar["close"] < e200) and vol_ok and atr_ok and (prev - sym.last_trade_index >= MIN_TRADE_GAP):
                entry = prev_bar["low"] * (1 - SLIPPAGE); stop = prev_bar["high"]
                enter_position(sym, "short", entry, stop, portfolio_value * RISK_PCT)
            return

        if sym.pos.side == "short" and broke_up and prev_green:
            exit_position(sym, prev_bar["high"], "flip")
            if can_open and (prev_bar["close"] > e200) and vol_ok and atr_ok and (prev - sym.last_trade_index >= MIN_TRADE_GAP):
                entry = prev_bar["high"] * (1 + SLIPPAGE); stop = prev_bar["low"]
                enter_position(sym, "long", entry, stop, portfolio_value * RISK_PCT)
            return

        return

    # No position: look for fresh entries
    if not can_open: return
    if prev - sym.last_trade_index < MIN_TRADE_GAP: return

    if prev_green and broke_up and (prev_bar["close"] > e200) and vol_ok and atr_ok:
        entry = prev_bar["high"] * (1 + SLIPPAGE); stop = prev_bar["low"]
        enter_position(sym, "long", entry, stop, portfolio_value * RISK_PCT); return

    if prev_red and broke_dn and (prev_bar["close"] < e200) and vol_ok and atr_ok:
        entry = prev_bar["low"] * (1 - SLIPPAGE); stop = prev_bar["high"]
        enter_position(sym, "short", entry, stop, portfolio_value * RISK_PCT); return

def load_all_history():
    for s in ASSETS:
        candles = fetch_candles(s, TIMEFRAME, HISTORY_DAYS)
        symbols[s].candles = candles
        print(f"[{now_utc()}] Loaded {len(candles)} candles {s}")

def refresh_latest_bar(sym: SymbolState):
    try:
        recent = fetch_candles(sym.symbol, TIMEFRAME, 5)
        if not recent: return
        have = {x["time"]: x for x in sym.candles[-1000:]}
        for row in recent: have[row["time"]] = row
        merged = list(have.values()); merged.sort(key=lambda x: x["time"])
        sym.candles = (sym.candles[:-1000] + merged) if len(sym.candles) > 1000 else merged
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
        "mode": MODE, "tframe": TIMEFRAME,
        "equity": round(portfolio_value, 2),
        "dd%": round(dd_percent(), 2),
        "open_positions": open_pos, "assets": ASSETS
    }

def main_loop():
    global portfolio_value
    print(f"[{now_utc()}] Hyper loop started — mode={MODE} tf={TIMEFRAME} assets={ASSETS} equity={portfolio_value:.2f}")
    tg_send(f"🚀 Hyper bot started | mode={MODE} tf={TIMEFRAME} | assets={', '.join(ASSETS)} | equity={portfolio_value:.2f}")
    load_all_history()
    while True:
        try:
            for s in ASSETS:
                refresh_latest_bar(symbols[s])
            for s in ASSETS:
                strategy_step(symbols[s])
            print(f"[{now_utc()}] tick… equity={portfolio_value:.2f} dd%={dd_percent():.2f}")
        except Exception as e:
            print(f"[{now_utc()}] loop error: {e}")
        time.sleep(REFRESH_SECONDS)

def start_background():
    t = threading.Thread(target=main_loop, daemon=True)
    t.start()

start_background()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))

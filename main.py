# main.py
# Google Cloud Run single-file app (no external deps)
# - Web UI with 26 buttons to control BTC/ETH positions on Binance USDT-M Futures
# - Auto position management: +0.25% add, 0.50% step-trailing SL
# - Indicator-based exits (ATR/CE/EMA/Swing) using Binance klines (1m/3m)
#
# ENV required:
#   BINANCE_API_KEY, BINANCE_API_SECRET
#   PORT (Cloud Run provides)
#
# SECURITY: Real-money trading. Test small. Manage API key/IP restrictions.

import os, hmac, hashlib, time, json, threading, urllib.parse, urllib.request, ssl, math
from http.server import BaseHTTPRequestHandler, HTTPServer

API_KEY = os.environ.get("BINANCE_API_KEY", "")
API_SECRET = os.environ.get("BINANCE_API_SECRET", "").encode()
BASE = "https://fapi.binance.com"  # USDT-M Futures

SYMBOLS = {
    "BTC": "BTCUSDT",
    "ETH": "ETHUSDT",
}

# ---- in-memory state (per instance) ----
# Cloud Run might recycle instances; this keeps state while instance alive.
state_lock = threading.Lock()
state = {
    "round_first_usdt": None,  # size of first order in the current round (shared between BTC/ETH per your spec)
    "positions": {
        # "BTC": {...}, "ETH": {...}
    },
    # exit conditions toggles
    "exits": {
        "BTC": {"TF1": {"ATR": False, "CE": False, "EMA13": False, "EMA26": False, "SwingHigh": False, "SwingLow": False},
                "TF3": {"ATR": False, "CE": False, "EMA26": False}},
        "ETH": {"TF1": {"ATR": False, "CE": False, "EMA13": False, "EMA26": False, "SwingHigh": False, "SwingLow": False},
                "TF3": {"ATR": False, "CE": False, "EMA26": False}},
    },
    # stop-limit target cache input
    "stop_limit_price": {"BTC": "", "ETH": ""},
    # bg thread control
    "bg_running": False
}

# per-symbol position schema in state["positions"]:
# {
#   "side": "LONG"|"SHORT",
#   "entry_price": float (weighted),
#   "base_usdt": float (size of first order for this symbol in this round),
#   "last_add_price": float (market price where last add happened),
#   "next_add_price": float (threshold for +0.25% step),
#   "sl_order_id": int|None,    # current SL order id (closePosition STOP_MARKET)
# }

# ----- HTTP helpers -----
def http_get(path, params=None, signed=False):
    url = BASE + path
    if params is None: params = {}
    if signed:
        params["timestamp"] = int(time.time() * 1000)
        query = urllib.parse.urlencode(params, doseq=True)
        sig = hmac.new(API_SECRET, query.encode(), hashlib.sha256).hexdigest()
        url += "?" + query + "&signature=" + sig
        headers = {"X-MBX-APIKEY": API_KEY}
    else:
        if params:
            url += "?" + urllib.parse.urlencode(params, doseq=True)
        headers = {}
    req = urllib.request.Request(url, headers=headers)
    ctx = ssl.create_default_context()
    with urllib.request.urlopen(req, context=ctx, timeout=20) as resp:
        return json.loads(resp.read().decode())

def http_delete(path, params=None, signed=False):
    url = BASE + path
    if params is None: params = {}
    if signed:
        params["timestamp"] = int(time.time() * 1000)
        query = urllib.parse.urlencode(params, doseq=True)
        sig = hmac.new(API_SECRET, query.encode(), hashlib.sha256).hexdigest()
        url += "?" + query + "&signature=" + sig
        headers = {"X-MBX-APIKEY": API_KEY}
    else:
        if params:
            url += "?" + urllib.parse.urlencode(params, doseq=True)
        headers = {}
    req = urllib.request.Request(url, headers=headers, method="DELETE")
    ctx = ssl.create_default_context()
    with urllib.request.urlopen(req, context=ctx, timeout=20) as resp:
        return json.loads(resp.read().decode())

def http_post(path, params=None, signed=False):
    url = BASE + path
    if params is None: params = {}
    headers = {}
    if signed:
        params["timestamp"] = int(time.time() * 1000)
        query = urllib.parse.urlencode(params, doseq=True)
        sig = hmac.new(API_SECRET, query.encode(), hashlib.sha256).hexdigest()
        body = (query + "&signature=" + sig).encode()
        headers = {"X-MBX-APIKEY": API_KEY, "Content-Type": "application/x-www-form-urlencoded"}
    else:
        body = urllib.parse.urlencode(params, doseq=True).encode()
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    ctx = ssl.create_default_context()
    with urllib.request.urlopen(req, context=ctx, timeout=20) as resp:
        return json.loads(resp.read().decode())

# ----- Binance utils -----
def get_wallet_usdt():
    acc = http_get("/fapi/v2/account", signed=True)
    # totalWalletBalance is a string
    return float(acc.get("totalWalletBalance", "0"))

def get_price(symbol):
    data = http_get("/fapi/v1/ticker/price", {"symbol": symbol})
    return float(data["price"])

def get_position_qty(symbol):
    # returns signed qty (positive for LONG, negative for SHORT)
    risks = http_get("/fapi/v2/positionRisk", {"symbol": symbol}, signed=True)
    if not risks: return 0.0
    posAmt = float(risks[0]["positionAmt"])
    return posAmt

def cancel_all(symbol):
    try:
        http_delete("/fapi/v1/allOpenOrders", {"symbol": symbol}, signed=True)
    except Exception:
        pass

def set_leverage(symbol, lev=5):
    try:
        http_post("/fapi/v1/leverage", {"symbol": symbol, "leverage": lev}, signed=True)
    except Exception:
        pass

def set_cross_margin(symbol):
    try:
        http_post("/fapi/v1/marginType", {"symbol": symbol, "marginType": "CROSSED"}, signed=True)
    except Exception:
        pass

def market_order(symbol, side, usdt_amount):
    # quantity = usdt_amount / price, rounded by stepSize
    price = get_price(symbol)
    # get lot size filter
    ex = http_get("/fapi/v1/exchangeInfo")
    filters = {}
    for s in ex["symbols"]:
        if s["symbol"] == symbol:
            for f in s["filters"]:
                filters[f["filterType"]] = f
            break
    step = float(filters["LOT_SIZE"]["stepSize"])
    minQty = float(filters["LOT_SIZE"]["minQty"])
    qty = usdt_amount / price
    # round down to step
    def round_step(q, step):
        return math.floor(q / step) * step
    qty = max(round_step(qty, step), minQty)

    # place MARKET
    res = http_post("/fapi/v1/order", {
        "symbol": symbol,
        "side": side,
        "type": "MARKET",
        "quantity": f"{qty:.8f}",
    }, signed=True)
    return res, price, qty

def close_all_market(symbol):
    # reduce-only market close: place opposite MARKET with reduceOnly=true and big qty
    qty = abs(get_position_qty(symbol))
    if qty <= 0:
        return {"status": "no_position"}
    side = "SELL" if qty > 0 else "BUY"  # note: Binance uses sign in qty; we’ll derive by side from qty sign
    # Need actual side from qty sign: if qty>0 -> LONG -> need SELL; if qty<0 -> SHORT -> need BUY
    if get_position_qty(symbol) > 0:
        side = "SELL"
    else:
        side = "BUY"
    res = http_post("/fapi/v1/order", {
        "symbol": symbol,
        "side": side,
        "type": "MARKET",
        "quantity": f"{qty:.8f}",
        "reduceOnly": "true"
    }, signed=True)
    return res

def set_close_position_sl(symbol, side, ref_price, sl_offset=0.005):
    """Create/replace STOP_MARKET closePosition at 0.50% against direction, reference=ref_price"""
    cancel_all(symbol)
    if side == "LONG":
        stopPrice = ref_price * (1 - sl_offset)
        sl_side = "SELL"
    else:
        stopPrice = ref_price * (1 + sl_offset)
        sl_side = "BUY"
    # STOP_MARKET with closePosition=true
    res = http_post("/fapi/v1/order", {
        "symbol": symbol,
        "side": sl_side,
        "type": "STOP_MARKET",
        "stopPrice": f"{stopPrice:.2f}",
        "closePosition": "true",
        "workingType": "CONTRACT_PRICE"
    }, signed=True)
    return res, stopPrice

# ----- Indicators (basic Python) -----
def ema(series, length):
    k = 2/(length+1)
    ema_val = None
    out = []
    for x in series:
        if ema_val is None:
            ema_val = x
        else:
            ema_val = x*k + ema_val*(1-k)
        out.append(ema_val)
    return out

def atr(h, l, c, length):
    trs = []
    prevc = c[0]
    for i in range(len(c)):
        tr = max(h[i]-l[i], abs(h[i]-prevc), abs(l[i]-prevc))
        trs.append(tr)
        prevc = c[i]
    # simple RMA of TR
    k = 1/length
    r = []
    val = None
    for tr in trs:
        if val is None:
            val = tr
        else:
            val = (1-k)*val + k*tr
        r.append(val)
    return r

def chandelier_exit(h, l, c, length=22, mult=3.0):
    import statistics
    # use ATR like TradingView default (RMA ATR)
    a = atr(h,l,c,length)
    # highest/lowest close over length
    ce_long = []
    ce_short = []
    for i in range(len(c)):
        s = max(0, i-length+1)
        long_base = max(c[s:i+1])  # use close base
        short_base = min(c[s:i+1])
        ce_long.append(long_base - mult*a[i])
        ce_short.append(short_base + mult*a[i])
    # direction
    dirv = [1]
    for i in range(1, len(c)):
        prev = dirv[-1]
        d = 1 if c[i] > ce_short[i-1] else (-1 if c[i] < ce_long[i-1] else prev)
        dirv.append(d)
    return ce_long, ce_short, dirv

def swing_hl(h, l, sh=5, sl=5):
    # latest “current” levels; for exit checks we just need current values vs price
    # find last pivot high/low confirmed
    curH = None
    curL = None
    for i in range(sl, len(h)-sl):
        okH = all(h[i] > h[i+k] and h[i] > h[i-k] for k in range(1, sh+1))
        okL = all(l[i] < l[i+k] and l[i] < l[i-k] for k in range(1, sl+1))
        if okH: curH = h[i]
        if okL: curL = l[i]
    return curH, curL

def fetch_klines(symbol, interval, limit=200):
    data = http_get("/fapi/v1/klines", {"symbol": symbol, "interval": interval, "limit": limit})
    # each item: [openTime, open, high, low, close, volume, ...]
    o = [float(x[1]) for x in data]
    h = [float(x[2]) for x in data]
    l = [float(x[3]) for x in data]
    c = [float(x[4]) for x in data]
    return o,h,l,c

def check_exit_conditions(coin):
    sym = SYMBOLS[coin]
    pos_qty = get_position_qty(sym)
    if pos_qty == 0:
        return False
    # determine side
    side = "LONG" if pos_qty > 0 else "SHORT"
    # TF1 (1m)
    tf1 = state["exits"][coin]["TF1"]
    tf3 = state["exits"][coin]["TF3"]

    now_price = get_price(sym)

    # 1m calculations
    o,h,l,c = fetch_klines(sym, "1m", limit=120)
    # 3m calculations
    o3,h3,l3,c3 = fetch_klines(sym, "3m", limit=120)

    # ATR TS (simple variant like Pine)
    # build ATR-TS line for both TF
    def atr_ts(h, l, c, atr_len=5, hhv=10, mult=3.0):
        a = atr(h,l,c,atr_len)
        basis = [h[i] - mult*a[i] for i in range(len(c))]
        out = []
        for i in range(len(c)):
            start = max(0, i-hhv+1)
            hh = max(basis[start:i+1])
            out.append(c[i] if i < 16 else hh)
        return out

    # EMA
    ema13 = ema(c, 13); ema26 = ema(c, 26)
    ema26_3 = ema(c3, 26)

    atrts_1 = atr_ts(h,l,c)
    atrts_3 = atr_ts(h3,l3,c3)

    # CE
    ceL, ceS, ceDir = chandelier_exit(h,l,c)
    ceL3, ceS3, ceDir3 = chandelier_exit(h3,l3,c3)

    # Swing
    curH, curL = swing_hl(h,l,sh=5,sl=5)

    # check TF1 flags:
    triggered = False
    # ATR TS cross
    if tf1["ATR"]:
        if side == "LONG" and c[-1] < atrts_1[-1]: triggered = True
        if side == "SHORT" and c[-1] > atrts_1[-1]: triggered = True
    # CE stop
    if tf1["CE"]:
        if side == "LONG" and c[-1] < ceL[-1]: triggered = True
        if side == "SHORT" and c[-1] > ceS[-1]: triggered = True
    # EMA13 / EMA26 stop (price crossing EMA)
    if tf1.get("EMA13", False):
        if side == "LONG" and c[-1] < ema13[-1]: triggered = True
        if side == "SHORT" and c[-1] > ema13[-1]: triggered = True
    if tf1.get("EMA26", False):
        if side == "LONG" and c[-1] < ema26[-1]: triggered = True
        if side == "SHORT" and c[-1] > ema26[-1]: triggered = True
    # Swing HL
    if tf1.get("SwingHigh", False) and curH is not None:
        if side == "SHORT" and now_price > curH: triggered = True
    if tf1.get("SwingLow", False) and curL is not None:
        if side == "LONG" and now_price < curL: triggered = True

    # TF3
    if not triggered:
        if tf3["ATR"]:
            if side == "LONG" and c3[-1] < atrts_3[-1]: triggered = True
            if side == "SHORT" and c3[-1] > atrts_3[-1]: triggered = True
        if tf3["CE"]:
            if side == "LONG" and c3[-1] < ceL3[-1]: triggered = True
            if side == "SHORT" and c3[-1] > ceS3[-1]: triggered = True
        if tf3.get("EMA26", False):
            if side == "LONG" and c3[-1] < ema26_3[-1]: triggered = True
            if side == "SHORT" and c3[-1] > ema26_3[-1]: triggered = True

    if triggered:
        close_all_market(sym)
        # reset local per-coin state
        with state_lock:
            state["positions"].pop(coin, None)
            # if both positions empty => reset round_first_usdt
            if not state["positions"]:
                state["round_first_usdt"] = None
        return True
    return False

# ----- Auto management thread -----
def bg_loop():
    while True:
        time.sleep(1.5)
        with state_lock:
            coins = list(state["positions"].keys())
        for coin in coins:
            try:
                sym = SYMBOLS[coin]
                pos_qty = get_position_qty(sym)
                if pos_qty == 0:
                    # position gone, cleanup
                    with state_lock:
                        state["positions"].pop(coin, None)
                        if not state["positions"]:
                            state["round_first_usdt"] = None
                    continue

                with state_lock:
                    ps = state["positions"][coin]
                side = ps["side"]  # LONG/SHORT
                price = get_price(sym)

                # check indicator exits if toggled
                try:
                    check_exit_conditions(coin)
                except Exception:
                    pass

                # add position at +0.25% steps
                direction = 1 if side == "LONG" else -1
                trigger_price = ps["next_add_price"]
                ok = (price >= trigger_price) if side == "LONG" else (price <= trigger_price)
                if ok:
                    # add market with base_usdt
                    res, exec_price, qty = market_order(sym, "BUY" if side=="LONG" else "SELL", ps["base_usdt"])
                    # update next_add threshold
                    move = 1.0025 if side=="LONG" else 0.9975
                    new_next = exec_price * move
                    # set step-trailing SL at 0.50% against from this exec price
                    set_close_position_sl(sym, side, exec_price, sl_offset=0.005)
                    with state_lock:
                        ps["last_add_price"] = exec_price
                        ps["next_add_price"] = new_next
                        state["positions"][coin] = ps
            except Exception:
                # keep loop alive
                continue

# ensure only one thread
def ensure_bg():
    with state_lock:
        if not state["bg_running"]:
            t = threading.Thread(target=bg_loop, daemon=True)
            t.start()
            state["bg_running"] = True

# ----- UI -----
HTML = """
<!doctype html>
<html>
<head>
<meta charset="utf-8" />
<title>Manual Futures Control</title>
<style>
body{font-family:ui-sans-serif,system-ui,Segoe UI,Arial;margin:20px;background:#0b0e11;color:#eaecef}
h2{margin:0 0 10px}
.panel{border-radius:14px;padding:16px;margin-bottom:22px}
.orange{background:#2b1a00;border:1px solid #6a3d00}
.blue{background:#001e2b;border:1px solid #004b6a}
.grid{display:grid;grid-template-columns:repeat(3, 1fr);gap:8px}
button{padding:10px 12px;border-radius:10px;border:1px solid #444;background:#111;color:#eaecef;cursor:pointer}
button:hover{background:#1a1f24}
.row{margin:10px 0}
input[type=text]{padding:8px;border-radius:8px;border:1px solid #444;background:#0c1116;color:#eaecef}
.small{font-size:12px;color:#9aa4ad}
label{display:block;margin-bottom:6px}
.section-title{font-weight:700;margin:8px 0}
.badge{display:inline-block;padding:2px 8px;border-radius:999px;background:#223;border:1px solid #445;font-size:12px;margin-left:8px}
</style>
</head>
<body>
<h1>Futures Control (USDⓈ-M)</h1>
<div class="panel orange">
  <h2>BTC Panel <span class="badge">Symbol: BTCUSDT</span></h2>
  <div class="grid">
    <form method="POST"><input type="hidden" name="cmd" value="btc_long"><button>1.1 Buy/Long</button></form>
    <form method="POST"><input type="hidden" name="cmd" value="btc_short"><button>1.2 Sell/Short</button></form>
    <div></div>

    <form method="POST"><input type="hidden" name="cmd" value="btc_tf1_atr"><button>2.1 ATR Stop TF1</button></form>
    <form method="POST"><input type="hidden" name="cmd" value="btc_tf1_ce"><button>2.2 CE Stop TF1</button></form>
    <form method="POST"><input type="hidden" name="cmd" value="btc_tf1_ema13"><button>2.3 EMA13 Stop TF1</button></form>
    <form method="POST"><input type="hidden" name="cmd" value="btc_tf1_ema26"><button>2.4 EMA26 Stop TF1</button></form>
    <form method="POST"><input type="hidden" name="cmd" value="btc_tf1_sh"><button>2.5 SwingHigh Stop TF1</button></form>
    <form method="POST"><input type="hidden" name="cmd" value="btc_tf1_sl"><button>2.6 SwingLow Stop TF1</button></form>

    <form method="POST"><input type="hidden" name="cmd" value="btc_tf3_atr"><button>3.1 ATR Stop TF3</button></form>
    <form method="POST"><input type="hidden" name="cmd" value="btc_tf3_ce"><button>3.2 CE Stop TF3</button></form>
    <form method="POST"><input type="hidden" name="cmd" value="btc_tf3_ema26"><button>3.3 EMA26 Stop TF3</button></form>

    <form method="POST"><input type="hidden" name="cmd" value="btc_stop_market"><button>4.1 Stop Market now</button></form>
    <form method="POST"><input type="hidden" name="cmd" value="btc_stop_limit"><label>4.2 Stop Limit (ใส่ราคา):</label>
        <input type="text" name="price" value="{btc_price}">
        <button>4.2 Stop Limit</button></form>
  </div>
  <div class="row small">สถานะ TF1/TF3 จะทำงานจนกว่าจะปิด Position</div>
</div>

<div class="panel blue">
  <h2>ETH Panel <span class="badge">Symbol: ETHUSDT</span></h2>
  <div class="grid">
    <form method="POST"><input type="hidden" name="cmd" value="eth_long"><button>5.1 Buy/Long</button></form>
    <form method="POST"><input type="hidden" name="cmd" value="eth_short"><button>5.2 Sell/Short</button></form>
    <div></div>

    <form method="POST"><input type="hidden" name="cmd" value="eth_tf1_atr"><button>6.1 ATR Stop TF1</button></form>
    <form method="POST"><input type="hidden" name="cmd" value="eth_tf1_ce"><button>6.2 CE Stop TF1</button></form>
    <form method="POST"><input type="hidden" name="cmd" value="eth_tf1_ema13"><button>6.3 EMA13 Stop TF1</button></form>
    <form method="POST"><input type="hidden" name="cmd" value="eth_tf1_ema26"><button>6.4 EMA26 Stop TF1</button></form>
    <form method="POST"><input type="hidden" name="cmd" value="eth_tf1_sh"><button>6.5 SwingHigh Stop TF1</button></form>
    <form method="POST"><input type="hidden" name="cmd" value="eth_tf1_sl"><button>6.6 SwingLow Stop TF1</button></form>

    <form method="POST"><input type="hidden" name="cmd" value="eth_tf3_atr"><button>7.1 ATR Stop TF3</button></form>
    <form method="POST"><input type="hidden" name="cmd" value="eth_tf3_ce"><button>7.2 CE Stop TF3</button></form>
    <form method="POST"><input type="hidden" name="cmd" value="eth_tf3_ema26"><button>7.3 EMA26 Stop TF3</button></form>

    <form method="POST"><input type="hidden" name="cmd" value="eth_stop_market"><button>8.1 Stop Market now</button></form>
    <form method="POST"><input type="hidden" name="cmd" value="eth_stop_limit"><label>8.2 Stop Limit (ใส่ราคา):</label>
        <input type="text" name="price" value="{eth_price}">
        <button>8.2 Stop Limit</button></form>
  </div>
  <div class="row small">สถานะ TF1/TF3 จะทำงานจนกว่าจะปิด Position</div>
</div>

<div class="row small">
หมายเหตุ: ระบบจะตั้ง Leverage=5 และ Cross Margin ให้โดยอัตโนมัติ (ปรับในโค้ดได้)
</div>

</body>
</html>
"""

def compute_first_order_usdt():
    # 8% of wallet; if not set in this round -> set and return; if set -> reuse
    with state_lock:
        if state["round_first_usdt"] is not None:
            return state["round_first_usdt"]
    bal = get_wallet_usdt()
    amount = round(bal * 0.08, 2)
    with state_lock:
        if state["round_first_usdt"] is None:
            state["round_first_usdt"] = amount
    return amount

def start_position(coin, side):
    sym = SYMBOLS[coin]
    set_cross_margin(sym)
    set_leverage(sym, 5)

    # if already have position -> reject (one position per coin)
    if get_position_qty(sym) != 0:
        return {"error": "Position already open for " + coin}

    base_usdt = compute_first_order_usdt()
    res, exec_price, qty = market_order(sym, "BUY" if side=="LONG" else "SELL", base_usdt)

    # set initial SL 0.50% opposite
    set_close_position_sl(sym, side, exec_price, sl_offset=0.005)

    # compute next +0.25% threshold
    move = 1.0025 if side=="LONG" else 0.9975
    next_add = exec_price * move

    with state_lock:
        state["positions"][coin] = {
            "side": side,
            "entry_price": exec_price,
            "base_usdt": base_usdt,
            "last_add_price": exec_price,
            "next_add_price": next_add,
            "sl_order_id": None
        }
    ensure_bg()
    return {"ok": True, "coin": coin, "side": side, "price": exec_price, "qty": qty, "base_usdt": base_usdt}

def set_stop_limit_close(coin, price):
    sym = SYMBOLS[coin]
    pos_qty = abs(get_position_qty(sym))
    if pos_qty == 0:
        return {"error": "No position to close"}
    # opposite side
    side = "SELL" if get_position_qty(sym) > 0 else "BUY"
    # LIMIT reduce-only
    ex = http_get("/fapi/v1/exchangeInfo")
    filters = {}
    for s in ex["symbols"]:
        if s["symbol"] == sym:
            for f in s["filters"]:
                filters[f["filterType"]] = f
            break
    tick = float(filters["PRICE_FILTER"]["tickSize"])
    def round_tick(p, t):
        return math.floor(p / t) * t
    px = round_tick(price, tick)
    res = http_post("/fapi/v1/order", {
        "symbol": sym,
        "side": side,
        "type": "LIMIT",
        "timeInForce": "GTC",
        "quantity": f"{pos_qty:.8f}",
        "price": f"{px:.2f}",
        "reduceOnly": "true"
    }, signed=True)
    return res

def toggle_exit(coin, key, tf):
    with state_lock:
        cur = state["exits"][coin][tf][key]
        state["exits"][coin][tf][key] = not cur
        return state["exits"][coin][tf][key]

class App(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/"):
            # simple dashboard
            with state_lock:
                btc_p = state["stop_limit_price"]["BTC"]
                eth_p = state["stop_limit_price"]["ETH"]
            html = HTML.format(btc_price=btc_p, eth_price=eth_p)
            self._ok(html, "text/html; charset=utf-8")
        else:
            self._notfound()

    def do_POST(self):
        length = int(self.headers.get('Content-Length', 0))
        data = self.rfile.read(length).decode()
        form = urllib.parse.parse_qs(data)
        cmd = (form.get("cmd",[""])[0]).strip()
        price = form.get("price", [""])[0].strip()

        msg = ""
        try:
            if cmd == "btc_long":
                msg = json.dumps(start_position("BTC","LONG"))
            elif cmd == "btc_short":
                msg = json.dumps(start_position("BTC","SHORT"))
            elif cmd == "eth_long":
                msg = json.dumps(start_position("ETH","LONG"))
            elif cmd == "eth_short":
                msg = json.dumps(start_position("ETH","SHORT"))

            elif cmd == "btc_stop_market":
                msg = json.dumps(close_all_market(SYMBOLS["BTC"]))
                self._reset_round_if_none()
            elif cmd == "eth_stop_market":
                msg = json.dumps(close_all_market(SYMBOLS["ETH"]))
                self._reset_round_if_none()

            elif cmd == "btc_stop_limit":
                try:
                    p = float(price)
                    with state_lock: state["stop_limit_price"]["BTC"] = price
                    msg = json.dumps(set_stop_limit_close("BTC", p))
                except:
                    msg = json.dumps({"error":"invalid price"})
            elif cmd == "eth_stop_limit":
                try:
                    p = float(price)
                    with state_lock: state["stop_limit_price"]["ETH"] = price
                    msg = json.dumps(set_stop_limit_close("ETH", p))
                except:
                    msg = json.dumps({"error":"invalid price"})

            # toggles:
            elif cmd == "btc_tf1_atr": msg = json.dumps({"BTC TF1 ATR": toggle_exit("BTC","ATR","TF1")})
            elif cmd == "btc_tf1_ce": msg = json.dumps({"BTC TF1 CE": toggle_exit("BTC","CE","TF1")})
            elif cmd == "btc_tf1_ema13": msg = json.dumps({"BTC TF1 EMA13": toggle_exit("BTC","EMA13","TF1")})
            elif cmd == "btc_tf1_ema26": msg = json.dumps({"BTC TF1 EMA26": toggle_exit("BTC","EMA26","TF1")})
            elif cmd == "btc_tf1_sh": msg = json.dumps({"BTC TF1 SwingHigh": toggle_exit("BTC","SwingHigh","TF1")})
            elif cmd == "btc_tf1_sl": msg = json.dumps({"BTC TF1 SwingLow": toggle_exit("BTC","SwingLow","TF1")})
            elif cmd == "btc_tf3_atr": msg = json.dumps({"BTC TF3 ATR": toggle_exit("BTC","ATR","TF3")})
            elif cmd == "btc_tf3_ce": msg = json.dumps({"BTC TF3 CE": toggle_exit("BTC","CE","TF3")})
            elif cmd == "btc_tf3_ema26": msg = json.dumps({"BTC TF3 EMA26": toggle_exit("BTC","EMA26","TF3")})

            elif cmd == "eth_tf1_atr": msg = json.dumps({"ETH TF1 ATR": toggle_exit("ETH","ATR","TF1")})
            elif cmd == "eth_tf1_ce": msg = json.dumps({"ETH TF1 CE": toggle_exit("ETH","CE","TF1")})
            elif cmd == "eth_tf1_ema13": msg = json.dumps({"ETH TF1 EMA13": toggle_exit("ETH","EMA13","TF1")})
            elif cmd == "eth_tf1_ema26": msg = json.dumps({"ETH TF1 EMA26": toggle_exit("ETH","EMA26","TF1")})
            elif cmd == "eth_tf1_sh": msg = json.dumps({"ETH TF1 SwingHigh": toggle_exit("ETH","SwingHigh","TF1")})
            elif cmd == "eth_tf1_sl": msg = json.dumps({"ETH TF1 SwingLow": toggle_exit("ETH","SwingLow","TF1")})
            elif cmd == "eth_tf3_atr": msg = json.dumps({"ETH TF3 ATR": toggle_exit("ETH","ATR","TF3")})
            elif cmd == "eth_tf3_ce": msg = json.dumps({"ETH TF3 CE": toggle_exit("ETH","CE","TF3")})
            elif cmd == "eth_tf3_ema26": msg = json.dumps({"ETH TF3 EMA26": toggle_exit("ETH","EMA26","TF3")})

            else:
                msg = json.dumps({"error":"unknown command"})
        except Exception as e:
            msg = json.dumps({"error": str(e)})

        # redirect back to page with small JSON message
        body = f"<pre>{msg}</pre><p><a href='/'>Back</a></p>"
        self._ok(body, "text/html; charset=utf-8")

    def _ok(self, body, ctype):
        body = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _notfound(self):
        self.send_response(404); self.end_headers()

    def log_message(self, *args, **kwargs):
        # quieter logs
        return

    def _reset_round_if_none(self):
        # if both empty => reset first order size for next round
        btc = get_position_qty(SYMBOLS["BTC"])
        eth = get_position_qty(SYMBOLS["ETH"])
        if btc == 0 and eth == 0:
            with state_lock:
                state["round_first_usdt"] = None

def run():
    port = int(os.environ.get("PORT", "8080"))
    server = HTTPServer(("0.0.0.0", port), App)
    ensure_bg()
    print(f"Listening on 0.0.0.0:{port} ...")
    server.serve_forever()

if __name__ == "__main__":
    run()

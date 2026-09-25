import os
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import Flask, jsonify, request, send_from_directory, make_response
import requests

app = Flask(__name__, static_folder=None)

BINANCE_BASES = [
    "https://data-api.binance.vision/api/v3",
    "https://api.binance.com/api/v3",
    "https://api-gcp.binance.com/api/v3",
]
MEXC_BASE = "https://api.mexc.com/api/v3"
SESSION = requests.Session()
CACHE = {}
LOCK = threading.Lock()

REFRESH_SECONDS = 60
MAX_LIMIT = 400
DEFAULT_LIMIT = 200
DEFAULT_MIN_VOL = 500_000.0

STABLES = {
    "USDC","FDUSD","TUSD","USDP","DAI","BUSD","USD1","USDE","BFUSD",
    "EUR","TRY","BRL","GBP","AUD","UAH","RUB","BIDR","IDRT","NGN",
    "ZAR","PLN","RON","ARS","MXN","CZK","JPY","AEUR","EURI"
}
LEV_SUFFIX = ("UP", "DOWN", "BULL", "BEAR")


def cached(key, ttl, fn):
    now = time.time()
    with LOCK:
        item = CACHE.get(key)
        if item and now - item[0] < ttl:
            return item[1]
    data = fn()
    with LOCK:
        CACHE[key] = (now, data)
    return data


def get_json(url, params=None, timeout=15):
    r = SESSION.get(url, params=params, timeout=timeout, headers={"User-Agent":"DualSpotScanner/1.0"})
    r.raise_for_status()
    return r.json()


def binance_get(path, params=None):
    last = None
    for base in BINANCE_BASES:
        try:
            return get_json(base + path, params)
        except Exception as e:
            last = e
    raise RuntimeError(f"Binance unavailable: {last}")


def mexc_get(path, params=None):
    return get_json(MEXC_BASE + path, params)


def safe_float(v, default=0.0):
    try:
        return float(v)
    except Exception:
        return default


def status_ok(symbol, exchange):
    status = str(symbol.get("status", "")).upper()
    if exchange == "BINANCE":
        return status == "TRADING"
    if status and status not in {"TRADING", "ENABLED", "1", "TRUE"}:
        return False
    return True


def spot_allowed(symbol):
    v = symbol.get("isSpotTradingAllowed")
    return v is not False


def ticker_map(exchange):
    key = f"tickers:{exchange}"
    def load():
        data = binance_get("/ticker/24hr") if exchange == "BINANCE" else mexc_get("/ticker/24hr")
        if isinstance(data, dict):
            data = data.get("data") or data.get("symbols") or []
        return {x.get("symbol"): x for x in data if isinstance(x, dict) and x.get("symbol")}
    return cached(key, REFRESH_SECONDS, load)


def exchange_info(exchange):
    key = f"exchange-info:{exchange}"
    def load():
        data = binance_get("/exchangeInfo") if exchange == "BINANCE" else mexc_get("/exchangeInfo")
        return data.get("symbols", []) if isinstance(data, dict) else []
    return cached(key, 1800, load)


def exchange_rows(exchange):
    tm = ticker_map(exchange)
    rows = []
    for s in exchange_info(exchange):
        if not isinstance(s, dict) or not status_ok(s, exchange) or not spot_allowed(s):
            continue
        base = str(s.get("baseAsset") or "").upper()
        quote = str(s.get("quoteAsset") or "").upper()
        symbol = str(s.get("symbol") or "").upper()
        if not base or quote != "USDT" or not symbol:
            continue
        if base in STABLES or base.endswith(LEV_SUFFIX):
            continue
        t = tm.get(symbol, {})
        price = safe_float(t.get("lastPrice", t.get("price", t.get("last", 0))))
        qv = safe_float(t.get("quoteVolume", t.get("amount", t.get("turnover", 0))))
        chg = safe_float(t.get("priceChangePercent", 0))
        if not chg:
            op = safe_float(t.get("openPrice", 0))
            if op and price:
                chg = (price / op - 1) * 100
        rows.append({
            "base": base,
            "symbol": symbol,
            "exchange": exchange,
            "price": price,
            "quoteVolume": qv,
            "change24h": chg,
        })
    return rows


def combined_universe():
    def build():
        all_rows = []
        errors = []
        for ex in ("BINANCE", "MEXC"):
            try:
                all_rows.extend(exchange_rows(ex))
            except Exception as e:
                errors.append(f"{ex}: {e}")
        by_base = {}
        for r in all_rows:
            cur = by_base.get(r["base"])
            if cur is None or r["quoteVolume"] > cur["quoteVolume"]:
                by_base[r["base"]] = r
        rows = list(by_base.values())
        rows.sort(key=lambda x: x["quoteVolume"], reverse=True)
        return rows, errors
    return cached("combined-universe", REFRESH_SECONDS, build)


def interval_name(exchange, tf):
    if exchange == "BINANCE":
        return tf
    return {"1h":"60m", "30m":"30m", "1d":"1d"}[tf]


def klines(exchange, symbol, tf, limit=90):
    key = f"k:{exchange}:{symbol}:{tf}:{limit}"
    def load():
        params = {"symbol": symbol, "interval": interval_name(exchange, tf), "limit": limit}
        data = binance_get("/klines", params) if exchange == "BINANCE" else mexc_get("/klines", params)
        if not isinstance(data, list):
            raise RuntimeError("bad kline response")
        return data
    return cached(key, REFRESH_SECONDS, load)


def ema(values, period):
    if len(values) < period:
        return None
    k = 2 / (period + 1)
    e = sum(values[:period]) / period
    for v in values[period:]:
        e = v * k + e * (1 - k)
    return e


def rsi(values, period=14):
    if len(values) < period + 1:
        return None
    gain = 0.0
    loss = 0.0
    for i in range(1, period + 1):
        d = values[i] - values[i-1]
        gain += max(d, 0)
        loss += max(-d, 0)
    avg_gain = gain / period
    avg_loss = loss / period
    for i in range(period + 1, len(values)):
        d = values[i] - values[i-1]
        g = max(d, 0)
        l = max(-d, 0)
        avg_gain = (avg_gain * (period - 1) + g) / period
        avg_loss = (avg_loss * (period - 1) + l) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - 100 / (1 + rs)


def closes_from(k):
    out = []
    for row in k:
        try:
            out.append(float(row[4]))
        except Exception:
            pass
    return out


def tf_stats(exchange, symbol, tf, live_price=None):
    c = closes_from(klines(exchange, symbol, tf, 90))
    if live_price and c:
        c[-1] = live_price
    e20 = ema(c, 20)
    e50 = ema(c, 50)
    last = c[-1] if c else None
    up = bool(e20 is not None and e50 is not None and last is not None and e20 > e50 and last > e20)
    return {
        "up": up,
        "rsi": rsi(c, 14),
        "ema20": e20,
        "ema50": e50,
    }


def analyze(row):
    ex = row["exchange"]
    sym = row["symbol"]
    px = row["price"]
    s1 = tf_stats(ex, sym, "1h", px)
    if not s1["up"]:
        return None
    sd = tf_stats(ex, sym, "1d", px)
    if not sd["up"]:
        return None
    s30 = tf_stats(ex, sym, "30m", px)
    return {
        "coin": row["base"],
        "symbol": sym,
        "exchange": ex,
        "price": px,
        "change24h": row["change24h"],
        "quoteVolume": row["quoteVolume"],
        "rsi1h": s1["rsi"],
        "rsi30m": s30["rsi"],
        "trend1h": "UP",
        "trend1d": "UP",
    }


def build_scan(limit, min_vol):
    universe, errors = combined_universe()
    eligible = [r for r in universe if r["quoteVolume"] >= min_vol]
    selected = eligible[:limit]
    out = []
    with ThreadPoolExecutor(max_workers=20) as ex:
        futures = [ex.submit(analyze, r) for r in selected]
        for f in as_completed(futures):
            try:
                row = f.result()
                if row:
                    out.append(row)
            except Exception:
                pass
    out.sort(key=lambda x: x["quoteVolume"], reverse=True)
    return {
        "updated": int(time.time()),
        "refreshSeconds": REFRESH_SECONDS,
        "uniqueUniverse": len(universe),
        "eligibleUniverse": len(eligible),
        "scanned": len(selected),
        "matches": len(out),
        "rows": out,
        "errors": errors,
    }


@app.get("/")
def home():
    resp = make_response(send_from_directory(os.path.dirname(__file__), "index.html"))
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return resp


@app.get("/api/health")
def health():
    return jsonify({"ok": True, "time": int(time.time()), "refreshSeconds": REFRESH_SECONDS})


@app.get("/api/scan")
def api_scan():
    try:
        limit = int(request.args.get("limit", DEFAULT_LIMIT))
    except Exception:
        limit = DEFAULT_LIMIT
    limit = max(20, min(limit, MAX_LIMIT))
    try:
        min_vol = float(request.args.get("minVol", DEFAULT_MIN_VOL))
    except Exception:
        min_vol = DEFAULT_MIN_VOL
    min_vol = max(0.0, min_vol)
    key = f"scan:{limit}:{min_vol}"
    data = cached(key, REFRESH_SECONDS, lambda: build_scan(limit, min_vol))
    resp = jsonify(data)
    resp.headers["Cache-Control"] = f"public, max-age={REFRESH_SECONDS}"
    return resp


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8080")), threaded=True)

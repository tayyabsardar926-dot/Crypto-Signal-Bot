"""Spot Finder Pro v3.

Broad Binance USDT spot scanner optimized for the user's primary finder rule:
1D uptrend + 1H uptrend + 1H RSI 50-55 + 30m RSI >= 58.
Read-only. No orders. No profit guarantee.
"""
from __future__ import annotations

import json
import math
import os
import statistics
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import requests
import yaml
from flask import Flask, jsonify, render_template

from signal_bot_v2 import ema, rsi

BASE_URL = "https://data-api.binance.vision"
REFRESH_INTERVAL = max(180, int(os.getenv("DASHBOARD_REFRESH_SECONDS", "300")))
MAX_WORKERS = max(8, min(24, int(os.getenv("DASHBOARD_WORKERS", "18"))))
MIN_QUOTE_VOLUME = max(0.0, float(os.getenv("DASHBOARD_MIN_QUOTE_VOLUME", "100000")))
MAX_PAIRS = max(100, min(700, int(os.getenv("DASHBOARD_MAX_PAIRS", "500"))))
REQUEST_TIMEOUT = (6, 20)

EXCLUDED_BASES = {
    "USDC", "FDUSD", "TUSD", "USDP", "DAI", "BUSD", "EUR", "EURI", "TRY",
    "AEUR", "BRL", "GBP", "UAH", "AUD", "BIDR", "IDRT", "NGN", "RUB", "ZAR",
}
LEVERAGED_SUFFIXES = ("UP", "DOWN", "BULL", "BEAR")

app = Flask(__name__)
_cfg = yaml.safe_load(Path("config.yaml").read_text()) or {}
R1_MIN = float(_cfg.get("finder_rsi_1h_min", 50))
R1_MAX = float(_cfg.get("finder_rsi_1h_max", 55))
R30_MIN = float(_cfg.get("finder_rsi_30m_min", 58))
REQ_D1 = bool(_cfg.get("finder_require_1d_uptrend", True))
REQ_H1 = bool(_cfg.get("finder_require_1h_uptrend", True))

_cache_lock = threading.Lock()
_refresh_lock = threading.Lock()
_started = False
_thread_local = threading.local()
_snapshot = {
    "updated_at": None,
    "scan_seconds": None,
    "coins": [],
    "errors": [],
    "refreshing": False,
    "coverage": 0,
    "universe_total": 0,
    "liquid_total": 0,
    "pre_candidates": 0,
    "matches": 0,
}


def _session() -> requests.Session:
    s = getattr(_thread_local, "session", None)
    if s is None:
        s = requests.Session()
        s.headers.update({"User-Agent": "SpotFinderPro/3.0"})
        _thread_local.session = s
    return s


def _finite(x, default=None):
    try:
        v = float(x)
        return v if math.isfinite(v) else default
    except (TypeError, ValueError):
        return default


def _get(endpoint: str, **params):
    s = _session()
    last = None
    for attempt in range(4):
        try:
            r = s.get(BASE_URL + endpoint, params=params, timeout=REQUEST_TIMEOUT)
            if r.status_code in (418, 451):
                raise RuntimeError("Binance unavailable in region")
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(min(2 ** attempt, 8))
                continue
            r.raise_for_status()
            return r.json()
        except requests.RequestException as exc:
            last = exc
            time.sleep(min(2 ** attempt, 8))
    raise RuntimeError(f"market request failed: {type(last).__name__ if last else 'unknown'}")


def _bars(symbol: str, interval: str, now_ms: int, limit: int = 120):
    rows = _get("/api/v3/klines", symbol=symbol, interval=interval, limit=limit)
    rows = [x for x in rows if int(x[6]) < now_ms]
    if len(rows) < 60:
        raise ValueError(f"{symbol}:{interval}:insufficient")
    out = [
        {"o": float(x[1]), "h": float(x[2]), "l": float(x[3]), "c": float(x[4]), "v": float(x[5])}
        for x in rows
    ]
    if any(not all(math.isfinite(v) for v in b.values()) for b in out):
        raise ValueError(f"{symbol}:{interval}:invalid")
    return out


def _trend_label(bars):
    c = [x["c"] for x in bars]
    e20, e50 = ema(c, 20), ema(c, 50)
    if c[-1] > e20[-1] > e50[-1] and e20[-1] > e20[-4] and e50[-1] >= e50[-4]:
        return "UP"
    if c[-1] < e20[-1] < e50[-1] and e20[-1] < e20[-4] and e50[-1] <= e50[-4]:
        return "DOWN"
    return "SIDEWAYS"


def _allowed_base(base):
    if not base or base in EXCLUDED_BASES:
        return False
    u = base.upper()
    if any(u.endswith(s) and len(u) > len(s) + 1 for s in LEVERAGED_SUFFIXES):
        return False
    return True


def _screening_map():
    raw = yaml.safe_load(Path("assets.yaml").read_text()) or {}
    return {k: v for k, v in raw.items() if isinstance(v, dict)}


def _screen_label(base, screening):
    meta = screening.get(base)
    if not meta:
        return False, "unreviewed"
    status = str(meta.get("status", "unreviewed"))
    return status == "approved", status


def _score(item):
    if not item.get("exact_match"):
        # Ranking for broad discovery only.
        score = 0
        score += 24 if item["trend_1d"] == "UP" else 8 if item["trend_1d"] == "SIDEWAYS" else 0
        score += 24 if item["trend_1h"] == "UP" else 8 if item["trend_1h"] == "SIDEWAYS" else 0
        d = abs(item["rsi_1h"] - ((R1_MIN + R1_MAX) / 2))
        score += max(0, 22 - d * 3)
        if item["rsi_30m"] is not None:
            score += min(20, max(0, item["rsi_30m"] - 45))
        score += 10 if item["spread_pct"] <= .05 else 6 if item["spread_pct"] <= .10 else 2
        return int(max(0, min(99, round(score))))

    # Exact matches are ranked by clean RSI positioning, volume and execution quality.
    center = (R1_MIN + R1_MAX) / 2
    r1_quality = max(0, 1 - abs(item["rsi_1h"] - center) / max((R1_MAX - R1_MIN) / 2, 1))
    r30_quality = min(1, max(0, (item["rsi_30m"] - R30_MIN) / 12 + .55))
    vol_quality = min(1, item["volume_ratio"] / 1.5) if item["volume_ratio"] is not None else 0
    spread_quality = 1 if item["spread_pct"] <= .05 else .75 if item["spread_pct"] <= .10 else .4
    score = 55 + 15 * r1_quality + 12 * r30_quality + 10 * vol_quality + 8 * spread_quality
    return int(max(0, min(100, round(score))))


def _stage1(symbol, base, now_ms, tick, book, screening):
    try:
        d1 = _bars(symbol, "1d", now_ms)
        h1 = _bars(symbol, "1h", now_ms)
        c1d = [x["c"] for x in d1]
        c1h = [x["c"] for x in h1]

        bid = _finite(book.get("bidPrice"), 0) or 0
        ask = _finite(book.get("askPrice"), 0) or 0
        if not (0 < bid <= ask):
            raise ValueError("bad book")
        price = (bid + ask) / 2
        spread = (ask - bid) / price * 100
        trend_1d = _trend_label(d1)
        trend_1h = _trend_label(h1)
        r1 = rsi(c1h)
        screened, screen_status = _screen_label(base, screening)

        pre = ((not REQ_D1 or trend_1d == "UP")
               and (not REQ_H1 or trend_1h == "UP")
               and R1_MIN <= r1 <= R1_MAX)

        item = {
            "symbol": symbol,
            "base": base,
            "price": price,
            "change_24h_pct": _finite(tick.get("priceChangePercent"), 0) or 0,
            "quote_volume_24h": _finite(tick.get("quoteVolume"), 0) or 0,
            "spread_pct": spread,
            "rsi_1d": rsi(c1d),
            "rsi_1h": r1,
            "rsi_30m": None,
            "trend_1d": trend_1d,
            "trend_1h": trend_1h,
            "volume_ratio": None,
            "pre_candidate": pre,
            "exact_match": False,
            "screened": screened,
            "screen_status": screen_status,
            "score": 0,
        }
        return item, None
    except Exception as exc:
        return None, f"{symbol}:stage1:{type(exc).__name__}"


def _stage2(item, now_ms):
    try:
        b30 = _bars(item["symbol"], "30m", now_ms)
        c30 = [x["c"] for x in b30]
        r30 = rsi(c30)
        avg = statistics.mean(x["v"] for x in b30[-21:-1]) or 1e-12
        vr = b30[-1]["v"] / avg
        item["rsi_30m"] = r30
        item["volume_ratio"] = vr
        item["exact_match"] = bool(item["pre_candidate"] and r30 >= R30_MIN)
        return item, None
    except Exception as exc:
        return item, f"{item['symbol']}:stage2:{type(exc).__name__}"


def build_snapshot():
    started = time.time()
    now_ms = int(_get("/api/v3/time")["serverTime"])
    infos = _get("/api/v3/exchangeInfo")["symbols"]
    tickers = {x["symbol"]: x for x in _get("/api/v3/ticker/24hr")}
    books = {x["symbol"]: x for x in _get("/api/v3/ticker/bookTicker")}
    screening = _screening_map()

    universe = []
    for info in infos:
        if info.get("quoteAsset") != "USDT" or info.get("status") != "TRADING":
            continue
        if not info.get("isSpotTradingAllowed", False):
            continue
        base = info.get("baseAsset")
        if not _allowed_base(base):
            continue
        universe.append((info["symbol"], base))

    liquid = []
    for sym, base in universe:
        qv = _finite(tickers.get(sym, {}).get("quoteVolume"), 0) or 0
        b = books.get(sym, {})
        bid = _finite(b.get("bidPrice"), 0) or 0
        ask = _finite(b.get("askPrice"), 0) or 0
        if qv < MIN_QUOTE_VOLUME or not (0 < bid <= ask):
            continue
        liquid.append((sym, base, qv))
    liquid.sort(key=lambda x: x[2], reverse=True)
    selected = liquid[:MAX_PAIRS]

    coins, errors = [], []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        fut = {
            pool.submit(_stage1, sym, base, now_ms, tickers.get(sym, {}), books.get(sym, {}), screening): sym
            for sym, base, _ in selected
        }
        for f in as_completed(fut):
            item, err = f.result()
            if item:
                coins.append(item)
            if err:
                errors.append(err)

    pre = [x for x in coins if x["pre_candidate"]]
    if pre:
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            fut2 = {pool.submit(_stage2, x, now_ms): x["symbol"] for x in pre}
            for f in as_completed(fut2):
                _, err = f.result()
                if err:
                    errors.append(err)

    for x in coins:
        x["score"] = _score(x)
    coins.sort(key=lambda x: (x["exact_match"], x["score"], x["quote_volume_24h"]), reverse=True)

    return {
        "updated_at": datetime.fromtimestamp(now_ms / 1000, timezone.utc).isoformat(),
        "scan_seconds": round(time.time() - started, 2),
        "coins": coins,
        "errors": errors[:40],
        "refreshing": False,
        "coverage": len(selected),
        "universe_total": len(universe),
        "liquid_total": len(liquid),
        "pre_candidates": sum(1 for x in coins if x["pre_candidate"]),
        "matches": sum(1 for x in coins if x["exact_match"]),
        "screened_count": sum(1 for x in coins if x["screened"]),
        "criteria": {
            "require_1d_uptrend": REQ_D1,
            "require_1h_uptrend": REQ_H1,
            "rsi_1h_min": R1_MIN,
            "rsi_1h_max": R1_MAX,
            "rsi_30m_min": R30_MIN,
        },
        "min_quote_volume": MIN_QUOTE_VOLUME,
        "max_pairs": MAX_PAIRS,
    }


def refresh_snapshot():
    if not _refresh_lock.acquire(blocking=False):
        return False
    try:
        with _cache_lock:
            _snapshot["refreshing"] = True
        fresh = build_snapshot()
        with _cache_lock:
            _snapshot.clear()
            _snapshot.update(fresh)
        return True
    except Exception as exc:
        with _cache_lock:
            _snapshot["refreshing"] = False
            _snapshot["errors"] = [f"refresh:{type(exc).__name__}"] + _snapshot.get("errors", [])[:39]
        return False
    finally:
        _refresh_lock.release()


def _loop():
    refresh_snapshot()
    while True:
        time.sleep(REFRESH_INTERVAL)
        refresh_snapshot()


def _ensure_started():
    global _started
    if _started:
        return
    with _cache_lock:
        if _started:
            return
        _started = True
    threading.Thread(target=_loop, daemon=True, name="spot-finder-refresh").start()


@app.before_request
def _warm():
    _ensure_started()


@app.get("/")
def index():
    return render_template("dashboard_v3.html")


@app.get("/api/coins")
def api_coins():
    with _cache_lock:
        snap = json.loads(json.dumps(_snapshot))
    if not snap.get("coins") and not snap.get("refreshing"):
        threading.Thread(target=refresh_snapshot, daemon=True).start()
    snap["rule_note"] = "Primary match = 1D uptrend + 1H uptrend + 1H RSI 50-55 + 30m RSI >= 58."
    snap["risk_note"] = "Read-only finder. A technical match is not a guaranteed profitable trade."
    return jsonify(snap)


@app.post("/api/refresh")
def api_refresh():
    threading.Thread(target=refresh_snapshot, daemon=True).start()
    return jsonify({"accepted": True}), 202


@app.get("/healthz")
def healthz():
    with _cache_lock:
        count = len(_snapshot.get("coins", []))
        updated = _snapshot.get("updated_at")
        refreshing = bool(_snapshot.get("refreshing"))
    return jsonify({"status": "ok" if count else "warming", "coins": count, "updated_at": updated, "refreshing": refreshing})


if __name__ == "__main__":
    _ensure_started()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8080")), threaded=True)

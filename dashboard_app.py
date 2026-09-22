"""Private-use Binance spot coin finder dashboard.

Read-only market scanner. No exchange keys, no order execution, no profit guarantee.
Uses the same purpose-screened asset list as the Telegram signal bot.
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

from signal_bot_v2 import atr, ema, pivots, rsi, setup_names, structure, trend_strength, uptrend

BASE_URL = "https://data-api.binance.vision"
CACHE_TTL = int(os.getenv("DASHBOARD_CACHE_SECONDS", "120"))
REFRESH_INTERVAL = int(os.getenv("DASHBOARD_REFRESH_SECONDS", "120"))
MAX_WORKERS = max(4, min(16, int(os.getenv("DASHBOARD_WORKERS", "10"))))
REQUEST_TIMEOUT = (6, 18)

app = Flask(__name__)

_cache_lock = threading.Lock()
_refresh_lock = threading.Lock()
_snapshot = {
    "updated_at": None,
    "scan_seconds": None,
    "coins": [],
    "errors": [],
    "refreshing": False,
}
_started = False


def _finite(x, default=None):
    try:
        x = float(x)
        return x if math.isfinite(x) else default
    except (TypeError, ValueError):
        return default


def _request_json(session: requests.Session, endpoint: str, **params):
    last = None
    for attempt in range(4):
        try:
            r = session.get(BASE_URL + endpoint, params=params, timeout=REQUEST_TIMEOUT)
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(min(2 ** attempt, 8))
                continue
            r.raise_for_status()
            return r.json()
        except requests.RequestException as exc:
            last = exc
            time.sleep(min(2 ** attempt, 8))
    raise RuntimeError(f"market request failed: {type(last).__name__ if last else 'unknown'}")


def _bars(session: requests.Session, symbol: str, interval: str, now_ms: int, limit: int = 140):
    rows = _request_json(session, "/api/v3/klines", symbol=symbol, interval=interval, limit=limit)
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


def _room_to_resistance(price, series_list):
    levels = sorted({x for s in series_list for x in pivots(s[-100:], "h") if x > price})
    if not levels:
        return None
    return (levels[0] / price - 1) * 100


def _rsi_sweet(value):
    # Best continuation zone for this dashboard: constructive but not overheated.
    if 52 <= value <= 66:
        return 1.0
    if 48 <= value <= 72:
        return 0.70
    if 40 <= value <= 76:
        return 0.40
    return 0.10


def _score(data):
    score = 0.0
    score += 18 if data["trend_4h"] == "UP" else 0
    score += 18 if data["trend_1h"] == "UP" else 0
    score += 12 * min(1.0, max(0.0, data["trend_strength"]))
    score += 12 * _rsi_sweet(data["rsi_1h"])
    score += 8 * _rsi_sweet(data["rsi_15m"])
    score += 12 * min(data["volume_ratio"] / 1.5, 1.0)
    score += 8 if data["spread_pct"] <= 0.05 else 5 if data["spread_pct"] <= 0.10 else 2
    score += 8 if data["mtf_confirmations"] == 2 else 4 if data["mtf_confirmations"] == 1 else 0
    score += 4 if data["smc"] else 0
    if data["room_pct"] is not None and data["room_pct"] >= 1.0:
        score += 4
    if data["change_3h_pct"] > 7 or data["change_7d_pct"] > 35:
        score -= 12
    if data["rsi_1d"] >= 78:
        score -= 8
    return int(max(0, min(100, round(score))))


def _quality(data):
    if (
        data["trend_4h"] == "UP"
        and data["trend_1h"] == "UP"
        and data["score"] >= 76
        and data["mtf_confirmations"] == 2
        and data["volume_ratio"] >= 0.90
        and data["spread_pct"] <= 0.15
        and (data["room_pct"] is None or data["room_pct"] >= 1.0)
        and data["change_3h_pct"] <= 7
    ):
        return "READY"
    if data["score"] >= 60:
        return "WATCH"
    return "AVOID"


def _load_assets():
    raw = yaml.safe_load(Path("assets.yaml").read_text()) or {}
    approved = {
        symbol: meta
        for symbol, meta in raw.items()
        if isinstance(meta, dict) and meta.get("status") == "approved" and meta.get("source")
    }
    return approved


def _scan_symbol(symbol, base_asset, sector, now_ms, tick, book):
    session = requests.Session()
    try:
        one_d = _bars(session, symbol, "1d", now_ms)
        h4 = _bars(session, symbol, "4h", now_ms)
        h1 = _bars(session, symbol, "1h", now_ms)
        m30 = _bars(session, symbol, "30m", now_ms)
        m15 = _bars(session, symbol, "15m", now_ms)

        bid = _finite(book.get("bidPrice"), 0.0) or 0.0
        ask = _finite(book.get("askPrice"), 0.0) or 0.0
        if not (0 < bid <= ask):
            raise ValueError(f"{symbol}:bad book")
        price = (bid + ask) / 2
        spread = (ask - bid) / price * 100

        c1d = [x["c"] for x in one_d]
        c4h = [x["c"] for x in h4]
        c1h = [x["c"] for x in h1]
        c30 = [x["c"] for x in m30]
        c15 = [x["c"] for x in m15]

        set15 = setup_names(m15, yaml.safe_load(Path("config.yaml").read_text()))
        set30 = setup_names(m30, yaml.safe_load(Path("config.yaml").read_text()))
        mtf = int(bool(set15)) + int(bool(set30))
        combined = set15 + [x for x in set30 if x not in set15]
        smc = next((x for x in combined if x.startswith("SMC:")), None)
        setup = smc or (combined[0] if combined else None)

        avg_vol = statistics.mean(x["v"] for x in m15[-21:-1]) or 1e-12
        vol_ratio = m15[-1]["v"] / avg_vol
        room = _room_to_resistance(price, [m30, h1, h4])
        change_3h = (h1[-1]["c"] / h1[-4]["c"] - 1) * 100
        change_7d = (one_d[-1]["c"] / one_d[-8]["c"] - 1) * 100 if len(one_d) >= 8 else 0.0

        item = {
            "symbol": symbol,
            "base": base_asset,
            "sector": sector,
            "price": price,
            "change_24h_pct": _finite(tick.get("priceChangePercent"), 0.0) or 0.0,
            "quote_volume_24h": _finite(tick.get("quoteVolume"), 0.0) or 0.0,
            "spread_pct": spread,
            "rsi_1d": rsi(c1d),
            "rsi_4h": rsi(c4h),
            "rsi_1h": rsi(c1h),
            "rsi_30m": rsi(c30),
            "rsi_15m": rsi(c15),
            "trend_4h": _trend_label(h4),
            "trend_1h": _trend_label(h1),
            "trend_strength": (trend_strength(h4) + trend_strength(h1)) / 2,
            "volume_ratio": vol_ratio,
            "mtf_confirmations": mtf,
            "setup": setup,
            "smc": smc,
            "room_pct": room,
            "change_3h_pct": change_3h,
            "change_7d_pct": change_7d,
            "structure_15m": structure(m15),
            "screened": True,
        }
        item["score"] = _score(item)
        item["quality"] = _quality(item)
        return item, None
    except Exception as exc:
        return None, f"{symbol}: {type(exc).__name__}"
    finally:
        session.close()


def build_snapshot():
    started = time.time()
    session = requests.Session()
    try:
        now_ms = int(_request_json(session, "/api/v3/time")["serverTime"])
        infos = _request_json(session, "/api/v3/exchangeInfo")["symbols"]
        tickers = {x["symbol"]: x for x in _request_json(session, "/api/v3/ticker/24hr")}
        books = {x["symbol"]: x for x in _request_json(session, "/api/v3/ticker/bookTicker")}
        approved = _load_assets()

        pairs = []
        for info in infos:
            if info.get("quoteAsset") != "USDT" or info.get("status") != "TRADING":
                continue
            base = info.get("baseAsset")
            if base not in approved or not info.get("isSpotTradingAllowed", False):
                continue
            pairs.append((info["symbol"], base, approved[base].get("sector", "Other")))

        coins, errors = [], []
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            futures = {
                pool.submit(
                    _scan_symbol,
                    sym,
                    base,
                    sector,
                    now_ms,
                    tickers.get(sym, {}),
                    books.get(sym, {}),
                ): sym
                for sym, base, sector in pairs
            }
            for future in as_completed(futures):
                item, err = future.result()
                if item:
                    coins.append(item)
                elif err:
                    errors.append(err)

        coins.sort(key=lambda x: (x["score"], x["volume_ratio"]), reverse=True)
        return {
            "updated_at": datetime.fromtimestamp(now_ms / 1000, timezone.utc).isoformat(),
            "scan_seconds": round(time.time() - started, 2),
            "coins": coins,
            "errors": errors[:20],
            "refreshing": False,
            "coverage": len(pairs),
        }
    finally:
        session.close()


def refresh_snapshot(block=False):
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
            _snapshot["errors"] = [f"refresh: {type(exc).__name__}"] + _snapshot.get("errors", [])[:19]
        return False
    finally:
        _refresh_lock.release()


def _background_loop():
    # Warm cache immediately, then keep it fresh. Failures retain last good snapshot.
    refresh_snapshot()
    while True:
        time.sleep(max(60, REFRESH_INTERVAL))
        refresh_snapshot()


def ensure_background_started():
    global _started
    if _started:
        return
    with _cache_lock:
        if _started:
            return
        _started = True
    threading.Thread(target=_background_loop, daemon=True, name="market-refresh").start()


@app.before_request
def _start_worker_once():
    ensure_background_started()


@app.get("/")
def index():
    return render_template("dashboard.html")


@app.get("/api/coins")
def api_coins():
    with _cache_lock:
        snap = json.loads(json.dumps(_snapshot))
    if not snap.get("coins") and not snap.get("refreshing"):
        threading.Thread(target=refresh_snapshot, daemon=True).start()
    snap["cache_ttl_seconds"] = CACHE_TTL
    snap["purpose_note"] = "Purpose-screened list only; this is not a Shariah certification or fatwa."
    snap["risk_note"] = "Read-only market scanner. No order execution and no guaranteed profit."
    return jsonify(snap)


@app.post("/api/refresh")
def api_refresh():
    with _cache_lock:
        updated = _snapshot.get("updated_at")
    if updated:
        try:
            age = time.time() - datetime.fromisoformat(updated).timestamp()
            if age < 45:
                return jsonify({"accepted": False, "reason": "recently refreshed"}), 200
        except ValueError:
            pass
    threading.Thread(target=refresh_snapshot, daemon=True).start()
    return jsonify({"accepted": True}), 202


@app.get("/healthz")
def healthz():
    with _cache_lock:
        updated = _snapshot.get("updated_at")
        count = len(_snapshot.get("coins", []))
        refreshing = bool(_snapshot.get("refreshing"))
    state = "warming"
    if updated and count:
        state = "ok"
    return jsonify({"status": state, "coins": count, "updated_at": updated, "refreshing": refreshing})


if __name__ == "__main__":
    ensure_background_started()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8080")), threaded=True)

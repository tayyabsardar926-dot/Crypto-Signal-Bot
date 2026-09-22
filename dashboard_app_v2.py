"""Spot Finder Pro v2 - broad Binance USDT spot coin finder.

Read-only market scanner. No exchange keys, no order execution, no profit guarantee.
The dashboard scans the broad liquid Binance USDT spot universe. Purpose-screening
metadata is shown separately and never presented as a Shariah certification/fatwa.
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

from signal_bot_v2 import ema, pivots, rsi, setup_names, structure, trend_strength

BASE_URL = "https://data-api.binance.vision"
REFRESH_INTERVAL = max(180, int(os.getenv("DASHBOARD_REFRESH_SECONDS", "300")))
MAX_WORKERS = max(8, min(24, int(os.getenv("DASHBOARD_WORKERS", "16"))))
MIN_QUOTE_VOLUME = max(0.0, float(os.getenv("DASHBOARD_MIN_QUOTE_VOLUME", "250000")))
MAX_PAIRS = max(100, min(700, int(os.getenv("DASHBOARD_MAX_PAIRS", "500"))))
REQUEST_TIMEOUT = (6, 20)

# Stable/fiat-like bases and leveraged-token name patterns are not useful for this spot momentum finder.
EXCLUDED_BASES = {
    "USDC", "FDUSD", "TUSD", "USDP", "DAI", "BUSD", "EUR", "EURI", "TRY",
    "AEUR", "BRL", "GBP", "UAH", "AUD", "BIDR", "IDRT", "NGN", "RUB", "ZAR",
}
LEVERAGED_SUFFIXES = ("UP", "DOWN", "BULL", "BEAR")

app = Flask(__name__)

_cfg = yaml.safe_load(Path("config.yaml").read_text()) or {}
_cache_lock = threading.Lock()
_refresh_lock = threading.Lock()
_started = False
_snapshot = {
    "updated_at": None,
    "scan_seconds": None,
    "coins": [],
    "errors": [],
    "refreshing": False,
    "coverage": 0,
    "universe_total": 0,
    "liquid_total": 0,
}
_thread_local = threading.local()


def _session() -> requests.Session:
    s = getattr(_thread_local, "session", None)
    if s is None:
        s = requests.Session()
        s.headers.update({"User-Agent": "SpotFinderPro/2.0"})
        _thread_local.session = s
    return s


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
            if r.status_code in (418, 451):
                raise RuntimeError("Binance market data unavailable in region")
            if r.status_code == 429 or r.status_code >= 500:
                retry = _finite(r.headers.get("Retry-After"), 0) or min(2 ** attempt, 8)
                time.sleep(min(retry, 12))
                continue
            r.raise_for_status()
            return r.json()
        except requests.RequestException as exc:
            last = exc
            time.sleep(min(2 ** attempt, 8))
    raise RuntimeError(f"market request failed: {type(last).__name__ if last else 'unknown'}")


def _bars(symbol: str, interval: str, now_ms: int, limit: int = 130):
    rows = _request_json(_session(), "/api/v3/klines", symbol=symbol, interval=interval, limit=limit)
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
    if 52 <= value <= 66:
        return 1.0
    if 48 <= value <= 72:
        return 0.72
    if 40 <= value <= 76:
        return 0.42
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


def _screening_map():
    raw = yaml.safe_load(Path("assets.yaml").read_text()) or {}
    out = {}
    for symbol, meta in raw.items():
        if not isinstance(meta, dict):
            continue
        out[symbol] = {
            "status": str(meta.get("status", "unreviewed")),
            "sector": str(meta.get("sector", "Other")),
            "source": bool(meta.get("source")),
        }
    return out


def _screen_label(base, mapping):
    meta = mapping.get(base)
    if not meta:
        return False, "UNREVIEWED", "Other"
    status = meta["status"].lower()
    if status == "approved" and meta.get("source"):
        return True, "PURPOSE-SCREENED", meta.get("sector", "Other")
    if status == "rejected":
        return False, "EXCLUDED", meta.get("sector", "Other")
    return False, "REVIEW", meta.get("sector", "Other")


def _scan_symbol(symbol, base_asset, screening, now_ms, tick, book):
    try:
        one_d = _bars(symbol, "1d", now_ms)
        h4 = _bars(symbol, "4h", now_ms)
        h1 = _bars(symbol, "1h", now_ms)
        m30 = _bars(symbol, "30m", now_ms)
        m15 = _bars(symbol, "15m", now_ms)

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

        set15 = setup_names(m15, _cfg)
        set30 = setup_names(m30, _cfg)
        mtf = int(bool(set15)) + int(bool(set30))
        combined = set15 + [x for x in set30 if x not in set15]
        smc = next((x for x in combined if x.startswith("SMC:")), None)
        setup = smc or (combined[0] if combined else None)

        avg_vol = statistics.mean(x["v"] for x in m15[-21:-1]) or 1e-12
        vol_ratio = m15[-1]["v"] / avg_vol
        room = _room_to_resistance(price, [m30, h1, h4])
        change_3h = (h1[-1]["c"] / h1[-4]["c"] - 1) * 100
        change_7d = (one_d[-1]["c"] / one_d[-8]["c"] - 1) * 100 if len(one_d) >= 8 else 0.0
        screened, screen_status, sector = _screen_label(base_asset, screening)

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
            "screened": screened,
            "screen_status": screen_status,
        }
        item["score"] = _score(item)
        item["quality"] = _quality(item)
        return item, None
    except Exception as exc:
        return None, f"{symbol}: {type(exc).__name__}"


def _allowed_base(base):
    if not base or base in EXCLUDED_BASES:
        return False
    upper = base.upper()
    if any(upper.endswith(suffix) and len(upper) > len(suffix) + 1 for suffix in LEVERAGED_SUFFIXES):
        return False
    return True


def build_snapshot():
    started = time.time()
    session = requests.Session()
    try:
        now_ms = int(_request_json(session, "/api/v3/time")["serverTime"])
        infos = _request_json(session, "/api/v3/exchangeInfo")["symbols"]
        tickers = {x["symbol"]: x for x in _request_json(session, "/api/v3/ticker/24hr")}
        books = {x["symbol"]: x for x in _request_json(session, "/api/v3/ticker/bookTicker")}
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
            qv = _finite(tickers.get(sym, {}).get("quoteVolume"), 0.0) or 0.0
            book = books.get(sym, {})
            bid = _finite(book.get("bidPrice"), 0.0) or 0.0
            ask = _finite(book.get("askPrice"), 0.0) or 0.0
            if qv < MIN_QUOTE_VOLUME or not (0 < bid <= ask):
                continue
            liquid.append((sym, base, qv))

        liquid.sort(key=lambda x: x[2], reverse=True)
        selected = liquid[:MAX_PAIRS]

        coins, errors = [], []
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            futures = {
                pool.submit(
                    _scan_symbol,
                    sym,
                    base,
                    screening,
                    now_ms,
                    tickers.get(sym, {}),
                    books.get(sym, {}),
                ): sym
                for sym, base, _ in selected
            }
            for future in as_completed(futures):
                item, err = future.result()
                if item:
                    coins.append(item)
                elif err:
                    errors.append(err)

        coins.sort(key=lambda x: (x["score"], x["quote_volume_24h"]), reverse=True)
        return {
            "updated_at": datetime.fromtimestamp(now_ms / 1000, timezone.utc).isoformat(),
            "scan_seconds": round(time.time() - started, 2),
            "coins": coins,
            "errors": errors[:30],
            "refreshing": False,
            "coverage": len(selected),
            "universe_total": len(universe),
            "liquid_total": len(liquid),
            "screened_count": sum(1 for x in coins if x["screened"]),
            "min_quote_volume": MIN_QUOTE_VOLUME,
            "max_pairs": MAX_PAIRS,
        }
    finally:
        session.close()


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
            _snapshot["errors"] = [f"refresh: {type(exc).__name__}"] + _snapshot.get("errors", [])[:29]
        return False
    finally:
        _refresh_lock.release()


def _background_loop():
    refresh_snapshot()
    while True:
        time.sleep(REFRESH_INTERVAL)
        refresh_snapshot()


def ensure_background_started():
    global _started
    if _started:
        return
    with _cache_lock:
        if _started:
            return
        _started = True
    threading.Thread(target=_background_loop, daemon=True, name="broad-market-refresh").start()


@app.before_request
def _start_worker_once():
    ensure_background_started()


@app.get("/")
def index():
    return render_template("dashboard_v2.html")


@app.get("/api/coins")
def api_coins():
    with _cache_lock:
        snap = json.loads(json.dumps(_snapshot))
    if not snap.get("coins") and not snap.get("refreshing"):
        threading.Thread(target=refresh_snapshot, daemon=True).start()
    snap["refresh_interval_seconds"] = REFRESH_INTERVAL
    snap["purpose_note"] = (
        "Broad Binance USDT spot universe. PURPOSE-SCREENED means only the project's conservative purpose review; "
        "UNREVIEWED/REVIEW/EXCLUDED are not halal labels. This is not a Shariah certification or fatwa."
    )
    snap["risk_note"] = "Read-only scanner. No order execution and no guaranteed profit."
    return jsonify(snap)


@app.post("/api/refresh")
def api_refresh():
    with _cache_lock:
        refreshing = bool(_snapshot.get("refreshing"))
        updated = _snapshot.get("updated_at")
    if refreshing:
        return jsonify({"accepted": False, "reason": "scan already running"}), 200
    if updated:
        try:
            age = time.time() - datetime.fromisoformat(updated).timestamp()
            if age < 90:
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
    state = "ok" if updated and count else "warming"
    return jsonify({"status": state, "coins": count, "updated_at": updated, "refreshing": refreshing})


if __name__ == "__main__":
    ensure_background_started()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8080")), threaded=True)

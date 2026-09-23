"""High-quality crypto spot scalping signal scanner.
Public Binance market data only. No order execution and no guaranteed profit.
"""
import json, math, os, statistics, time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
import requests, yaml


def ema(v, n):
    if len(v) < n:
        raise ValueError("ema history")
    out = [sum(v[:n]) / n]
    k = 2 / (n + 1)
    for x in v[n:]:
        out.append(out[-1] + k * (x - out[-1]))
    return out


def rsi(v, n=14):
    if len(v) <= n:
        raise ValueError("rsi history")
    d = [b - a for a, b in zip(v, v[1:])]
    g = sum(max(x, 0) for x in d[:n]) / n
    l = sum(max(-x, 0) for x in d[:n]) / n
    for x in d[n:]:
        g = (g * (n - 1) + max(x, 0)) / n
        l = (l * (n - 1) + max(-x, 0)) / n
    return 50 if g == l == 0 else 100 if l == 0 else 100 - 100 / (1 + g / l)


def atr(b, n=14):
    tr = [max(y["h"] - y["l"], abs(y["h"] - x["c"]), abs(y["l"] - x["c"]))
          for x, y in zip(b, b[1:])]
    if len(tr) < n:
        raise ValueError("atr history")
    a = sum(tr[:n]) / n
    for x in tr[n:]:
        a = (a * (n - 1) + x) / n
    return a


def pivots(b, key):
    f = max if key == "h" else min
    return [b[i][key] for i in range(2, len(b) - 2)
            if b[i][key] == f(x[key] for x in b[i-2:i+3])]


def pivot_points(b, key):
    f = max if key == "h" else min
    return [(i, b[i][key]) for i in range(2, len(b) - 2)
            if b[i][key] == f(x[key] for x in b[i-2:i+3])]


def structure(b):
    h, l = pivots(b[-80:], "h"), pivots(b[-80:], "l")
    return len(h) >= 2 and len(l) >= 2 and h[-1] > h[-2] and l[-1] > l[-2]


def uptrend(b):
    c = [x["c"] for x in b]
    e20, e50 = ema(c, 20), ema(c, 50)
    return (c[-1] > e20[-1] > e50[-1]
            and e20[-1] > e20[-4]
            and e50[-1] >= e50[-4])


def trend_strength(b):
    c = [x["c"] for x in b]
    e20, e50 = ema(c, 20), ema(c, 50)
    price = c[-1]
    separation = max(0, (e20[-1] - e50[-1]) / price * 100)
    slope = max(0, (e20[-1] - e20[-5]) / price * 100)
    return min(1.0, separation / 1.2 + slope / 0.8)


def age_ok(first_ms, now, days):
    return 0 < first_ms <= (now - days * 86400) * 1000


def candle_quality(b):
    x = b[-1]
    rng = max(x["h"] - x["l"], 1e-12)
    body = abs(x["c"] - x["o"])
    close_pos = (x["c"] - x["l"]) / rng
    return max(0.0, min(1.0, 0.55 * (body / rng) + 0.45 * close_pos))


class Market:
    def __init__(self, cfg):
        self.cfg, self.s, self.cache = cfg, requests.Session(), {}

    def get(self, ep, **params):
        key = (ep, tuple(sorted(params.items())))
        if key in self.cache:
            return self.cache[key]
        for i in range(4):
            try:
                time.sleep(self.cfg.get("request_interval", .15))
                r = self.s.get(self.cfg["base_url"] + ep, params=params, timeout=(8, 22))
                if r.status_code in (418, 451):
                    raise RuntimeError("Binance unavailable in region")
                if r.status_code == 429 or r.status_code >= 500:
                    time.sleep(min(float(r.headers.get("Retry-After", 2 ** (i + 1))), 60))
                    continue
                r.raise_for_status()
                data = r.json()
                self.cache[key] = data
                return data
            except requests.RequestException:
                time.sleep(2 ** i)
        raise RuntimeError("market data unavailable")

    def bars(self, sym, tf, now):
        rows = self.get("/api/v3/klines", symbol=sym, interval=tf, limit=250)
        sec = {"15m": 900, "30m": 1800, "1h": 3600, "4h": 14400}[tf]
        rows = [x for x in rows if int(x[6]) < now * 1000]
        if len(rows) < 200:
            raise ValueError("candles:insufficient")
        if now * 1000 - int(rows[-1][6]) > sec * 1000 + 120000:
            raise ValueError("candles:stale")
        bars = [dict(o=float(x[1]), h=float(x[2]), l=float(x[3]), c=float(x[4]), v=float(x[5]))
                for x in rows]
        if any(not all(math.isfinite(v) for v in row.values()) for row in bars):
            raise ValueError("candles:invalid")
        return bars


class State:
    def __init__(self, path):
        self.p = Path(path)
        self.d = json.loads(self.p.read_text()) if self.p.exists() else {"version": 3, "alerts": [], "ages": {}}
        if not isinstance(self.d.get("alerts"), list) or not isinstance(self.d.get("ages"), dict):
            self.d = {"version": 3, "alerts": [], "ages": {}}

    def save(self):
        self.p.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.p.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.d, indent=2))
        tmp.replace(self.p)

    def blocked(self, sym, sector, now, cfg, cap):
        today = datetime.fromtimestamp(now, timezone.utc).date()
        daily = [x for x in self.d["alerts"]
                 if datetime.fromtimestamp(x["time"], timezone.utc).date() == today]
        if len(daily) >= cap:
            return "daily cap"
        if any(x["symbol"] == sym and now - x["time"] < cfg["cooldown_hours"] * 3600
               for x in self.d["alerts"]):
            return "cooldown"
        if sum(x["sector"] == sector for x in daily) >= cfg["max_sector_per_day"]:
            return "sector cap"

    def add(self, sig, now):
        self.d["alerts"] = [x for x in self.d["alerts"] if now - x["time"] < 30 * 86400]
        self.d["alerts"].append({"symbol": sig["symbol"], "sector": sig["sector"], "time": now})
        self.save()


def smc_setup(b, cfg):
    """Bullish SMC confirmation: BOS plus liquidity sweep/FVG/order-block retest."""
    if not cfg.get("smc_enabled", True) or len(b) < 80:
        return None
    a, price = atr(b), b[-1]["c"]
    highs, lows = pivot_points(b[-80:-2], "h"), pivot_points(b[-80:-2], "l")
    if not highs or not lows:
        return None
    swing_high, swing_low = highs[-1][1], lows[-1][1]
    bos = any(x["c"] > swing_high + a * .05 for x in b[-4:])
    sweep = any(x["l"] < swing_low - a * .03 and x["c"] > swing_low for x in b[-6:])
    min_gap = a * cfg.get("smc_min_gap_atr", .08)
    tol = a * cfg.get("smc_retest_atr_tolerance", .30)

    fvg = False
    for i in range(max(2, len(b) - 14), len(b)):
        gap_low, gap_high = b[i-2]["h"], b[i]["l"]
        if gap_high - gap_low >= min_gap and gap_low - tol <= price <= gap_high + tol:
            fvg = True
            break

    ob = False
    for i in range(max(2, len(b) - 16), len(b) - 2):
        candle = b[i]
        if candle["c"] >= candle["o"]:
            continue
        future = b[i+1:min(i+4, len(b))]
        displaced = any(x["c"] > candle["h"] + a * .35 and x["c"] > x["o"] for x in future)
        if displaced and candle["l"] - tol <= price <= candle["h"] + tol:
            ob = True
            break

    confirmations = []
    if sweep:
        confirmations.append("liquidity sweep")
    if fvg:
        confirmations.append("FVG retest")
    if ob:
        confirmations.append("order block retest")
    if bos and confirmations:
        return "SMC: BOS + " + " + ".join(confirmations[:2])
    return None


def setup_names(b, cfg):
    c = [x["c"] for x in b]
    last, prev = b[-1], b[-2]
    a, e20 = atr(b), ema(c, 20)[-1]
    res = max(x["h"] for x in b[-22:-2])
    vol = statistics.mean(x["v"] for x in b[-21:-1])
    sup = pivots(b[:-1], "l")
    found = []

    smc = smc_setup(b, cfg)
    if smc:
        found.append(smc)
    if ((last["c"] > res and last["v"] >= vol * 1.10)
            or (prev["c"] > res and last["l"] <= res + a * .45 and last["c"] > res)):
        found.append("breakout/retest")
    if last["l"] <= e20 + a * .5 and last["c"] >= e20 * .995 and last["c"] > last["o"]:
        found.append("pullback continuation")
    if last["c"] > max(x["h"] for x in b[-6:-1]) and last["v"] >= vol * 1.10:
        found.append("momentum continuation")
    if sup and abs(last["l"] - sup[-1]) <= a * .55 and last["c"] > last["o"]:
        found.append("support bounce")
    if c[-1] > e20 and (c[-1] > c[-2] > c[-3] or structure(b)):
        found.append("trend continuation")
    return found


def levels(b, higher, price, cfg):
    a = atr(b)
    c = [x["c"] for x in b]
    supports = [x for x in pivots(b[-80:], "l") if x < price]
    if not supports:
        fallback = min(x["l"] for x in b[-20:-1])
        supports = [fallback] if fallback < price else []
    if not supports:
        raise ValueError("levels:no support")

    support = max(supports)
    anchor = max(support, min(ema(c, 20)[-1], price))
    extension_pct = (price / anchor - 1) * 100 if anchor > 0 else 999
    extension_atr = (price - anchor) / max(a, 1e-12)
    if extension_pct > cfg["max_entry_extension_pct"] or extension_atr > cfg["max_entry_extension_atr"]:
        raise ValueError("entry:late/chasing")

    entry = price
    low = max(anchor - a * .10, price - a * .45)
    if low >= entry:
        low = entry - a * .20

    invalidation = support - a * .30

    resist = sorted(set(x for series in (b, *higher)
                        for x in pivots(series, "h") if x > entry))
    min_tp1 = entry * (1 + cfg["min_live_upside_pct"] / 100)
    valid = [x - a * .06 for x in resist if x - a * .06 >= min_tp1]
    if not valid:
        raise ValueError("levels:no 1pct room")

    tp1 = valid[0]
    live_upside = (tp1 / entry - 1) * 100
    if live_upside < cfg["min_live_upside_pct"]:
        raise ValueError("levels:live upside")

    desired_tp2 = entry * (1 + cfg["tp2_min_pct"] / 100)
    farther = [x - a * .04 for x in resist if x - a * .04 >= desired_tp2]
    if farther:
        tp2 = farther[0]
    else:
        projected = entry + max(a * 2.0, entry * cfg["tp2_min_pct"] / 100)
        max_reasonable = entry + atr(higher[0]) * cfg["max_tp2_h1_atr"]
        tp2 = min(projected, max_reasonable)
    if tp2 <= tp1:
        tp2 = tp1 + max(a * .8, entry * .004)

    runner = max(tp2 + a, entry * (1 + cfg["runner_min_pct"] / 100))
    runner_cap = entry * (1 + cfg["runner_max_pct"] / 100)
    runner = min(runner, runner_cap, entry + atr(higher[0]) * cfg["runner_max_h1_atr"])
    if runner <= tp2:
        runner = None

    return dict(low=low, entry=entry, invalidation=invalidation,
                tp1=tp1, tp2=tp2, runner=runner,
                live_upside=live_upside)


def evaluate(m, sym, cfg, now):
    h4, h1 = m.bars(sym, "4h", now), m.bars(sym, "1h", now)
    if not uptrend(h4) or not uptrend(h1):
        raise ValueError("trend:4H+1H")

    h1c = [x["c"] for x in h1]
    h1_rsi, h1_prev_rsi = rsi(h1c), rsi(h1c[:-1])
    if h1_rsi < cfg["min_h1_rsi"] or h1_rsi > cfg["max_h1_rsi"]:
        raise ValueError("momentum:1H RSI")
    if h1_rsi < h1_prev_rsi - cfg["max_h1_rsi_drop"]:
        raise ValueError("momentum:1H fading")

    if h1[-1]["c"] / h1[-4]["c"] - 1 > cfg["max_3h_pump_pct"] / 100:
        raise ValueError("overextension:pump")

    m30, m15 = m.bars(sym, "30m", now), m.bars(sym, "15m", now)
    c30 = [x["c"] for x in m30]
    e20_30, e50_30 = ema(c30, 20)[-1], ema(c30, 50)[-1]
    if c30[-1] < e50_30 or (c30[-1] < e20_30 * .997 and m30[-1]["c"] <= m30[-1]["o"]):
        raise ValueError("timing:30m")

    book = m.get("/api/v3/ticker/bookTicker", symbol=sym)
    bid, ask = float(book["bidPrice"]), float(book["askPrice"])
    if not 0 < bid <= ask:
        raise ValueError("liquidity:book")
    spread_pct = (ask - bid) / ((ask + bid) / 2) * 100
    if spread_pct > cfg["max_spread_pct"]:
        raise ValueError("liquidity:spread")
    price = (bid + ask) / 2

    setup_map = {
        "15m": setup_names(m15, cfg),
        "30m": setup_names(m30, cfg)
    }
    confluence = int(bool(setup_map["15m"])) + int(bool(setup_map["30m"]))
    opts = []

    for tf, b in (("15m", m15), ("30m", m30)):
        names = setup_map[tf]
        if not names:
            continue
        c = [x["c"] for x in b]
        a = atr(b)
        mom, prev_mom = rsi(c), rsi(c[:-1])
        vol = b[-1]["v"] / max(statistics.mean(x["v"] for x in b[-21:-1]), 1e-12)
        atr_pct = a / price * 100

        if not cfg["min_atr_pct"] <= atr_pct <= cfg["max_atr_pct"]:
            continue
        if not cfg["min_signal_rsi"] <= mom <= cfg["max_signal_rsi"]:
            continue
        if mom < prev_mom - cfg["max_signal_rsi_drop"]:
            continue
        if vol < cfg["min_volume_ratio"]:
            continue

        try:
            lv = levels(b, [h1, h4], price, cfg)
        except ValueError:
            continue

        struct = 1.0 if structure(b) else .55
        tq = (trend_strength(h1) + trend_strength(h4)) / 2
        cq = candle_quality(b)
        mom_score = max(0, 1 - abs(mom - 60) / 18)
        vol_score = min(vol / 1.5, 1)
        setup_score = min(len(names) / 2, 1)
        mtf = 1.0 if confluence == 2 else .55

        score = round(
            22 * tq +
            16 * struct +
            14 * vol_score +
            14 * mom_score +
            10 * cq +
            10 * mtf +
            8 * setup_score +
            6
        )
        if any(x.startswith("SMC:") for x in names):
            score = min(100, score + cfg.get("smc_score_bonus", 4))
        if score < cfg["min_score"]:
            continue

        primary = next((x for x in names if x.startswith("SMC:")), names[0])
        opts.append(dict(**lv, price=price, setup=primary, timing=tf, score=score,
                         rsi=mom, h1_rsi=h1_rsi, volume=vol, spread=spread_pct,
                         trend_strength=tq, confluence=confluence))

    if not opts:
        raise ValueError("setup:no qualified timing")
    return max(opts, key=lambda x: (x["score"], x["live_upside"]))


def telegram(msg):
    token, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat:
        raise RuntimeError("telegram secrets missing")
    r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      json={"chat_id": chat, "text": msg}, timeout=(8, 22))
    if r.status_code != 200 or not r.json().get("ok"):
        raise RuntimeError("telegram rejected")


def fmt(s):
    p = lambda x: f"{x:.8g}"
    runner = (f"\nRunner/Stretch: {p(s['runner'])} "
              f"(+{(s['runner']/s['entry']-1)*100:.2f}%)"
              if s.get("runner") else "")
    return (
        f"🟢 SPOT SCALP SIGNAL — {s['symbol']}\n"
        f"Current / Entry: {p(s['price'])} USDT\n"
        f"Buy Zone: {p(s['low'])} – {p(s['entry'])} USDT\n"
        f"TP1: {p(s['tp1'])} (+{(s['tp1']/s['entry']-1)*100:.2f}%)\n"
        f"TP2: {p(s['tp2'])} (+{(s['tp2']/s['entry']-1)*100:.2f}%)"
        f"{runner}\n"
        f"Setup: {s['setup']} | Score: {s['score']}/100\n"
        f"4H+1H: Uptrend | Timing: {s['timing']} | MTF confirmations: {s['confluence']}/2\n"
        f"RSI {s['timing']}: {s['rsi']:.1f} | RSI 1H: {s['h1_rsi']:.1f} | Volume: {s['volume']:.2f}x\n"
        f"Spread: {s['spread']:.3f}% | Live room to TP1: {s['live_upside']:.2f}%\n"
        f"Profit plan: TP1 par partial profit; trend strong rahe to TP2/runner. "
        f"Technical invalidation: {p(s['invalidation'])}.\n"
        f"UTC: {s['timestamp']}\n"
        f"No guaranteed outcome; fees/slippage excluded."
    )


def main():
    cfg = yaml.safe_load(Path("config.yaml").read_text())
    assets = yaml.safe_load(Path("assets.yaml").read_text())
    approved = {k: v for k, v in assets.items()
                if isinstance(v, dict) and v.get("status") == "approved" and v.get("source")}

    st, m = State(cfg["state_path"]), Market(cfg)
    now = m.get("/api/v3/time")["serverTime"] / 1000
    infos = m.get("/api/v3/exchangeInfo")["symbols"]
    tick = {x["symbol"]: x for x in m.get("/api/v3/ticker/24hr")}
    books = {x["symbol"]: x for x in m.get("/api/v3/ticker/bookTicker")}
    counts, candidates = Counter(), []
    eligible = [x for x in infos
                if x.get("quoteAsset") == "USDT" and x.get("baseAsset") in approved]

    for info in eligible:
        sym, base = info["symbol"], info["baseAsset"]
        try:
            t, book = tick.get(sym, {}), books.get(sym, {})
            bid, ask = float(book.get("bidPrice", 0)), float(book.get("askPrice", 0))
            if info.get("status") != "TRADING" or not info.get("isSpotTradingAllowed", False):
                raise ValueError("liquidity:status")
            if float(t.get("quoteVolume", 0)) < cfg["min_quote_volume"]:
                raise ValueError("liquidity:volume")
            if not 0 < bid <= ask or (ask - bid) / ((ask + bid) / 2) * 100 > cfg["max_spread_pct"]:
                raise ValueError("liquidity:spread")

            if sym not in st.d["ages"]:
                first = m.get("/api/v3/klines", symbol=sym, interval="1d", startTime=0, limit=1)
                if not first:
                    raise ValueError("age:missing")
                st.d["ages"][sym] = int(first[0][0])
            if not age_ok(st.d["ages"][sym], now, cfg["min_age_days"]):
                raise ValueError("age:young")

            s = evaluate(m, sym, cfg, now)
            s.update(symbol=sym, sector=approved[base]["sector"],
                     timestamp=datetime.fromtimestamp(now, timezone.utc).isoformat())
            candidates.append(s)
        except ValueError as e:
            counts[str(e).split(":")[0]] += 1

    cap, sent = cfg["daily_cap"], 0
    for s in sorted(candidates, key=lambda x: (x["score"], x["live_upside"]), reverse=True):
        if sent >= cap:
            break
        why = st.blocked(s["symbol"], s["sector"], now, cfg, cap)
        if why:
            counts[why] += 1
            continue
        telegram(fmt(s))
        st.add(s, now)
        sent += 1

    st.save()
    print("SUMMARY", json.dumps({
        "approved_assets": len(approved),
        "eligible_pairs": len(eligible),
        "qualified": len(candidates),
        "alerts": sent,
        "rejections": dict(counts)
    }, sort_keys=True))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print("SCAN FAILED", type(e).__name__)
        raise SystemExit(1)

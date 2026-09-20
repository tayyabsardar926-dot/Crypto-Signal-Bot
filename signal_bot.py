from __future__ import annotations

import json
import math
import os
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import requests
import yaml

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.yaml"
ASSETS_PATH = ROOT / "assets.yaml"
STATE_PATH = ROOT / "signal_state.json"


@dataclass
class Signal:
    symbol: str
    strategy: str
    score: float
    current_price: float
    buy_low: float
    buy_high: float
    tp1: float
    tp2: float
    stop: float
    tp1_pct: float
    tp2_pct: float
    risk_pct: float
    move_potential_pct: float
    rsi_1h: float
    rsi_4h: float
    trend_1h: int
    trend_4h: int
    quote_volume_24h: float
    spread_bps: float
    purpose_category: str
    market_regime: str
    note: str


class BinancePublicClient:
    """Public market-data only. No exchange API key or trading permission is used."""

    BASES = [
        "https://data-api.binance.vision",
        "https://api.binance.com",
        "https://api1.binance.com",
    ]

    def __init__(self, timeout: int = 15):
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "FreeCryptoSignalScanner/1.0"})

    def get(self, path: str, params: Optional[dict] = None):
        last_err = None
        for base in self.BASES:
            try:
                r = self.session.get(base + path, params=params, timeout=self.timeout)
                if r.status_code == 429:
                    time.sleep(2)
                    last_err = RuntimeError(f"Binance rate limit on {base}")
                    continue
                r.raise_for_status()
                return r.json()
            except Exception as exc:
                last_err = exc
        raise RuntimeError(f"Binance public data request failed: {last_err}")

    def exchange_info(self) -> dict:
        return self.get("/api/v3/exchangeInfo")

    def ticker_24h(self) -> list:
        return self.get("/api/v3/ticker/24hr")

    def book_ticker(self) -> list:
        return self.get("/api/v3/ticker/bookTicker")

    def klines(self, symbol: str, interval: str, limit: int = 300, start_time: Optional[int] = None) -> list:
        params = {"symbol": symbol, "interval": interval, "limit": limit}
        if start_time is not None:
            params["startTime"] = start_time
        return self.get("/api/v3/klines", params=params)


def load_yaml(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def klines_to_df(rows: list) -> pd.DataFrame:
    cols = [
        "open_time", "open", "high", "low", "close", "volume", "close_time",
        "quote_volume", "trades", "taker_buy_base", "taker_buy_quote", "ignore"
    ]
    df = pd.DataFrame(rows, columns=cols)
    numeric = ["open", "high", "low", "close", "volume", "quote_volume"]
    df[numeric] = df[numeric].astype(float)
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    return df


def ema(s: pd.Series, span: int) -> pd.Series:
    return s.ewm(span=span, adjust=False).mean()


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    out = 100 - (100 / (1 + rs))
    out = out.where(~((avg_loss == 0) & (avg_gain > 0)), 100.0)
    out = out.where(~((avg_gain == 0) & (avg_loss > 0)), 0.0)
    out = out.where(~((avg_gain == 0) & (avg_loss == 0)), 50.0)
    return out.fillna(50)


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    prev_close = df["close"].shift(1)
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev_close).abs(),
        (df["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    d = df.copy()
    d["ema20"] = ema(d["close"], 20)
    d["ema50"] = ema(d["close"], 50)
    d["ema200"] = ema(d["close"], 200)
    d["rsi"] = rsi(d["close"], 14)
    d["atr"] = atr(d, 14)
    d["vol_ma20"] = d["volume"].rolling(20).mean()
    d["vol_ratio"] = d["volume"] / d["vol_ma20"].replace(0, np.nan)
    d["macd"] = ema(d["close"], 12) - ema(d["close"], 26)
    d["macd_signal"] = ema(d["macd"], 9)
    d["macd_hist"] = d["macd"] - d["macd_signal"]

    # Internal smoothed Heikin-Ashi approximation. This is NOT claimed to be
    # identical to any proprietary TradingView "Smooth Ashi Candles v1" script.
    so = ema(d["open"], 5)
    sh = ema(d["high"], 5)
    sl = ema(d["low"], 5)
    sc = ema(d["close"], 5)
    ha_close = (so + sh + sl + sc) / 4
    ha_open = ha_close.copy()
    ha_open.iloc[0] = (so.iloc[0] + sc.iloc[0]) / 2
    for i in range(1, len(d)):
        ha_open.iloc[i] = (ha_open.iloc[i - 1] + ha_close.iloc[i - 1]) / 2
    d["sha_open"] = ema(ha_open, 3)
    d["sha_close"] = ema(ha_close, 3)
    d["sha_green"] = d["sha_close"] > d["sha_open"]
    return d


def structure_up(df: pd.DataFrame, window: int = 12) -> bool:
    if len(df) < window * 2 + 1:
        return False
    a = df.iloc[-2 * window:-window]
    b = df.iloc[-window:]
    return (b["high"].max() > a["high"].max()) and (b["low"].min() >= a["low"].min() * 0.995)


def trend_score(df: pd.DataFrame) -> int:
    x = df.iloc[-1]
    score = 0
    score += int(x["close"] > x["ema20"] > x["ema50"])
    score += int(x["ema20"] > df["ema20"].iloc[-5])
    score += int(x["rsi"] >= 50)
    score += int(structure_up(df))
    score += int(bool(x["sha_green"]))
    return score


def rolling_4h_move_potential(df_1h: pd.DataFrame) -> float:
    if len(df_1h) < 40:
        return 0.0
    highs = df_1h["high"].rolling(4).max()
    lows = df_1h["low"].rolling(4).min()
    ranges = (highs / lows - 1.0) * 100
    recent = ranges.dropna().iloc[-30:]
    return float(recent.median()) if not recent.empty else 0.0


def strategy_breakout(d: pd.DataFrame) -> Tuple[float, str]:
    x = d.iloc[-1]
    resistance = d["high"].iloc[-21:-1].max()
    cond = x["close"] > resistance and x["vol_ratio"] >= 1.15 and 54 <= x["rsi"] <= 74 and x["sha_green"]
    if not cond:
        return 0.0, ""
    score = 25 + min(8, (x["vol_ratio"] - 1) * 10)
    return score, "Breakout + volume confirmation"


def strategy_pullback(d: pd.DataFrame) -> Tuple[float, str]:
    x = d.iloc[-1]
    distance_atr = abs(x["close"] - x["ema20"]) / max(x["atr"], 1e-12)
    bullish_candle = x["close"] > x["open"]
    cond = distance_atr <= 0.65 and bullish_candle and 47 <= x["rsi"] <= 66 and x["sha_green"]
    if not cond:
        return 0.0, ""
    return 28 + max(0, 4 - distance_atr * 4), "EMA20 pullback continuation"


def strategy_momentum(d: pd.DataFrame) -> Tuple[float, str]:
    x, p = d.iloc[-1], d.iloc[-2]
    rsi_cross = p["rsi"] <= 55 < x["rsi"] or (x["rsi"] >= 55 and x["rsi"] > p["rsi"])
    macd_ok = x["macd_hist"] > 0 and x["macd_hist"] >= p["macd_hist"]
    cond = rsi_cross and macd_ok and x["sha_green"] and x["vol_ratio"] >= 0.95 and x["rsi"] <= 72
    if not cond:
        return 0.0, ""
    return 27 + min(5, max(0, x["rsi"] - 55) * 0.4), "Momentum continuation"


def strategy_support_bounce(d: pd.DataFrame) -> Tuple[float, str]:
    x, p = d.iloc[-1], d.iloc[-2]
    support = d["low"].iloc[-25:-1].min()
    near_support = (x["close"] - support) <= max(1.1 * x["atr"], x["close"] * 0.008)
    reversal = x["close"] > x["open"] and x["close"] > p["close"] and x["rsi"] > p["rsi"] and x["sha_green"]
    if not (near_support and reversal and x["rsi"] >= 45):
        return 0.0, ""
    return 25.0, "Support bounce + bullish reversal"


def choose_strategy(d1h: pd.DataFrame) -> Tuple[float, str]:
    candidates = [
        strategy_breakout(d1h),
        strategy_pullback(d1h),
        strategy_momentum(d1h),
        strategy_support_bounce(d1h),
    ]
    return max(candidates, key=lambda x: x[0])


def price_levels(d1h: pd.DataFrame, min_tp1_pct: float, min_tp2_pct: float) -> Optional[dict]:
    x = d1h.iloc[-1]
    entry = float(x["close"])
    a = float(x["atr"])
    if not math.isfinite(a) or a <= 0:
        return None

    buy_low = entry - 0.15 * a
    buy_high = entry + 0.08 * a
    recent_swing_low = float(d1h["low"].iloc[-12:].min())
    # Use the tighter of an ATR-based volatility stop and the latest swing area.
    # This is a risk stop, not a guarantee that price cannot move through it.
    stop = max(entry - 1.15 * a, recent_swing_low - 0.10 * a)
    risk_pct = (entry - stop) / entry * 100
    # Avoid stops that are unrealistically tight or too wide for a short-term setup.
    if risk_pct < 0.45:
        stop = entry * (1 - 0.0045)
        risk_pct = 0.45
    if risk_pct > 2.5:
        return None

    tp1 = entry * (1 + min_tp1_pct / 100)
    tp2 = entry * (1 + min_tp2_pct / 100)
    # Let ATR expand targets when volatility supports it.
    tp1 = max(tp1, entry + 1.05 * a)
    tp2 = max(tp2, entry + 1.90 * a)

    tp1_pct = (tp1 / entry - 1) * 100
    tp2_pct = (tp2 / entry - 1) * 100
    rr1 = (tp1 - entry) / max(entry - stop, 1e-12)
    if rr1 < 0.75:
        return None

    return {
        "current_price": entry,
        "buy_low": buy_low,
        "buy_high": buy_high,
        "tp1": tp1,
        "tp2": tp2,
        "stop": stop,
        "tp1_pct": tp1_pct,
        "tp2_pct": tp2_pct,
        "risk_pct": risk_pct,
    }


def fmt_price(x: float) -> str:
    if x >= 1000:
        return f"{x:,.2f}"
    if x >= 1:
        return f"{x:.4f}".rstrip("0").rstrip(".")
    if x >= 0.01:
        return f"{x:.6f}".rstrip("0").rstrip(".")
    return f"{x:.8f}".rstrip("0").rstrip(".")


def load_state() -> dict:
    if not STATE_PATH.exists():
        return {}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(state: dict):
    STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")


def can_alert(symbol: str, strategy: str, cooldown_hours: float, state: dict) -> bool:
    key = f"{symbol}:{strategy}"
    last = state.get(key)
    if not last:
        return True
    try:
        t = datetime.fromisoformat(last)
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) - t >= timedelta(hours=cooldown_hours)
    except Exception:
        return True


def mark_alert(symbol: str, strategy: str, state: dict):
    state[f"{symbol}:{strategy}"] = datetime.now(timezone.utc).isoformat()


def telegram_send(token: str, chat_id: str, text: str):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    r = requests.post(url, json={"chat_id": chat_id, "text": text}, timeout=20)
    r.raise_for_status()


def signal_message(s: Signal) -> str:
    return (
        "🟢 SPOT BUY SETUP\n"
        f"Coin: {s.symbol}\n"
        f"Current: {fmt_price(s.current_price)}\n"
        f"Buy zone: {fmt_price(s.buy_low)} – {fmt_price(s.buy_high)}\n"
        f"TP1: {fmt_price(s.tp1)}  (+{s.tp1_pct:.2f}%)\n"
        f"TP2: {fmt_price(s.tp2)}  (+{s.tp2_pct:.2f}%)\n"
        f"Invalidation/SL: {fmt_price(s.stop)}  (-{s.risk_pct:.2f}%)\n\n"
        f"Setup: {s.strategy}\n"
        f"Score: {s.score:.0f}/100\n"
        f"1H RSI: {s.rsi_1h:.1f} | 4H RSI: {s.rsi_4h:.1f}\n"
        f"1H trend: {s.trend_1h}/5 | 4H trend: {s.trend_4h}/5\n"
        f"Typical 4H move potential: {s.move_potential_pct:.2f}%\n"
        f"24H quote volume: ${s.quote_volume_24h:,.0f}\n"
        f"Spread: {s.spread_bps:.1f} bps\n"
        f"BTC regime: {s.market_regime}\n"
        f"Purpose screen: {s.purpose_category}\n\n"
        "⚠️ Signal, not guaranteed profit. Spot only; verify before entry."
    )


def btc_regime(client: BinancePublicClient) -> Tuple[str, int]:
    d1 = add_indicators(klines_to_df(client.klines("BTCUSDT", "1h", 250)))
    d4 = add_indicators(klines_to_df(client.klines("BTCUSDT", "4h", 250)))
    s1, s4 = trend_score(d1), trend_score(d4)
    if s1 >= 4 and s4 >= 4:
        return "Bullish", 10
    if s1 >= 3 and s4 >= 3:
        return "Neutral/Bullish", 6
    if s1 <= 1 and s4 <= 2:
        return "Bearish", -15
    return "Mixed", 0


def old_enough_on_binance(client: BinancePublicClient, symbol: str, min_age_days: int) -> bool:
    rows = client.klines(symbol, "1d", limit=1, start_time=0)
    if not rows:
        return False
    first_open = datetime.fromtimestamp(rows[0][0] / 1000, tz=timezone.utc)
    return first_open <= datetime.now(timezone.utc) - timedelta(days=min_age_days)


def build_universe(exchange_info: dict, assets_cfg: dict) -> Dict[str, dict]:
    approved = {a["symbol"].upper(): a for a in assets_cfg.get("assets", []) if a.get("enabled", True)}
    universe = {}
    for s in exchange_info.get("symbols", []):
        base = s.get("baseAsset", "").upper()
        if (
            base in approved
            and s.get("quoteAsset") == "USDT"
            and s.get("status") == "TRADING"
            and s.get("isSpotTradingAllowed", True)
        ):
            universe[s["symbol"]] = approved[base]
    return universe


def scan() -> List[Signal]:
    cfg = load_yaml(CONFIG_PATH)
    assets_cfg = load_yaml(ASSETS_PATH)
    c = BinancePublicClient(timeout=int(cfg["network"]["timeout_seconds"]))

    exchange = c.exchange_info()
    universe = build_universe(exchange, assets_cfg)
    ticker_rows = c.ticker_24h()
    book_rows = c.book_ticker()
    tickers = {x["symbol"]: x for x in ticker_rows if "symbol" in x}
    books = {x["symbol"]: x for x in book_rows if "symbol" in x}

    regime, btc_points = btc_regime(c)
    if cfg["filters"].get("reject_when_btc_bearish", True) and regime == "Bearish":
        print("BTC regime is bearish; no new long spot signals this run.")
        return []

    min_vol = float(cfg["filters"]["min_quote_volume_usdt_24h"])
    max_spread = float(cfg["filters"]["max_spread_bps"])
    min_age_days = int(float(cfg["filters"]["minimum_coin_age_years"]) * 365.25)
    min_move = float(cfg["filters"]["minimum_typical_4h_move_pct"])
    min_score = float(cfg["filters"]["minimum_signal_score"])
    max_scan = int(cfg["filters"].get("max_symbols_per_run", 40))

    # Liquidity-first ordering reduces requests and focuses on executable spot markets.
    candidates = []
    for symbol, meta in universe.items():
        t = tickers.get(symbol)
        b = books.get(symbol)
        if not t or not b:
            continue
        qv = float(t.get("quoteVolume", 0) or 0)
        bid = float(b.get("bidPrice", 0) or 0)
        ask = float(b.get("askPrice", 0) or 0)
        mid = (bid + ask) / 2 if bid > 0 and ask > 0 else 0
        spread_bps = ((ask - bid) / mid * 10000) if mid else 9999
        if qv >= min_vol and spread_bps <= max_spread:
            candidates.append((symbol, meta, qv, spread_bps))
    candidates.sort(key=lambda x: x[2], reverse=True)
    candidates = candidates[:max_scan]

    signals: List[Signal] = []
    for i, (symbol, meta, qv, spread_bps) in enumerate(candidates, start=1):
        try:
            if not old_enough_on_binance(c, symbol, min_age_days):
                print(f"[{i}/{len(candidates)}] {symbol}: excluded by >= {min_age_days}d Binance-history proxy")
                continue

            d1h = add_indicators(klines_to_df(c.klines(symbol, "1h", 260)))
            d4h = add_indicators(klines_to_df(c.klines(symbol, "4h", 260)))
            if len(d1h) < 210 or len(d4h) < 210:
                continue

            t1, t4 = trend_score(d1h), trend_score(d4h)
            if t1 < int(cfg["filters"]["minimum_trend_score_1h"]) or t4 < int(cfg["filters"]["minimum_trend_score_4h"]):
                continue

            move_potential = rolling_4h_move_potential(d1h)
            if move_potential < min_move:
                continue

            strategy_points, strategy_name = choose_strategy(d1h)
            if strategy_points <= 0:
                continue

            levels = price_levels(
                d1h,
                min_tp1_pct=float(cfg["targets"]["minimum_tp1_pct"]),
                min_tp2_pct=float(cfg["targets"]["minimum_tp2_pct"]),
            )
            if not levels:
                continue

            x1, x4 = d1h.iloc[-1], d4h.iloc[-1]
            trend_points = min(30, (t1 + t4) * 3)
            liquidity_points = min(10, 4 + math.log10(max(qv, 1) / min_vol + 1) * 4)
            volatility_points = min(10, 5 + max(0, move_potential - min_move) * 2)
            volume_points = min(10, max(0, float(x1["vol_ratio"]) * 6))
            score = min(100, trend_points + strategy_points + liquidity_points + volatility_points + volume_points + btc_points)

            if score < min_score:
                continue

            signals.append(Signal(
                symbol=symbol,
                strategy=strategy_name,
                score=score,
                current_price=levels["current_price"],
                buy_low=levels["buy_low"],
                buy_high=levels["buy_high"],
                tp1=levels["tp1"],
                tp2=levels["tp2"],
                stop=levels["stop"],
                tp1_pct=levels["tp1_pct"],
                tp2_pct=levels["tp2_pct"],
                risk_pct=levels["risk_pct"],
                move_potential_pct=move_potential,
                rsi_1h=float(x1["rsi"]),
                rsi_4h=float(x4["rsi"]),
                trend_1h=t1,
                trend_4h=t4,
                quote_volume_24h=qv,
                spread_bps=spread_bps,
                purpose_category=meta.get("purpose_category", "reviewed utility"),
                market_regime=regime,
                note=meta.get("note", ""),
            ))
            print(f"[{i}/{len(candidates)}] {symbol}: SIGNAL {score:.0f} {strategy_name}")
        except Exception as exc:
            print(f"[{i}/{len(candidates)}] {symbol}: error: {exc}")

    signals.sort(key=lambda s: s.score, reverse=True)
    return signals[: int(cfg["alerts"].get("max_signals_per_run", 3))]


def main():
    cfg = load_yaml(CONFIG_PATH)
    signals = scan()

    # Always write machine-readable output for audit/debugging.
    out = ROOT / "latest_signals.json"
    out.write_text(json.dumps([asdict(s) for s in signals], indent=2), encoding="utf-8")

    if not signals:
        print("No qualifying signal this run.")
        return

    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    state = load_state()
    cooldown = float(cfg["alerts"]["cooldown_hours_same_setup"])

    for s in signals:
        print("\n" + signal_message(s) + "\n")
        if not token or not chat_id:
            print("Telegram secrets missing; printed signal only.")
            continue
        if not can_alert(s.symbol, s.strategy, cooldown, state):
            print(f"Cooldown active: {s.symbol} / {s.strategy}")
            continue
        telegram_send(token, chat_id, signal_message(s))
        mark_alert(s.symbol, s.strategy, state)

    save_state(state)


if __name__ == "__main__":
    main()

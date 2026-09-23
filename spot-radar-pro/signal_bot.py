"""Conservative public-data spot scanner. No exchange credentials or order methods."""
import argparse
from collections import Counter
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import statistics
import subprocess
import time

import requests
import yaml


def ema(values, n):
    if len(values) < n:
        raise ValueError('Insufficient EMA history')
    out = [sum(values[:n]) / n]
    for v in values[n:]:
        out.append(out[-1] + 2 / (n + 1) * (v - out[-1]))
    return out


def rsi(values, n=14):
    if len(values) <= n:
        raise ValueError('Insufficient RSI history')
    changes = [b-a for a, b in zip(values, values[1:])]
    gain = sum(max(x, 0) for x in changes[:n]) / n
    loss = sum(max(-x, 0) for x in changes[:n]) / n
    for x in changes[n:]:
        gain = (gain*(n-1)+max(x, 0))/n
        loss = (loss*(n-1)+max(-x, 0))/n
    return 50.0 if gain == loss == 0 else 100.0 if loss == 0 else 100-100/(1+gain/loss)


def atr(bars, n=14):
    tr = [max(b['h']-b['l'], abs(b['h']-a['c']), abs(b['l']-a['c'])) for a, b in zip(bars, bars[1:])]
    if len(tr) < n:
        raise ValueError('Insufficient ATR history')
    value = sum(tr[:n])/n
    for x in tr[n:]:
        value = (value*(n-1)+x)/n
    return value


def pivots(bars, key):
    test = max if key == 'h' else min
    return [bars[i][key] for i in range(2, len(bars)-2)
            if bars[i][key] == test(b[key] for b in bars[i-2:i+3])
            and bars[i][key] != bars[i-1][key]]


def structure(bars):
    highs, lows = pivots(bars[-80:], 'h'), pivots(bars[-80:], 'l')
    return len(highs) >= 2 and len(lows) >= 2 and highs[-1] > highs[-2] and lows[-1] > lows[-2]


def trend(bars):
    c = [b['c'] for b in bars]
    e20, e50 = ema(c, 20), ema(c, 50)
    return c[-1] > e20[-1] > e50[-1] and e20[-1] > e20[-4] and e50[-1] > e50[-4] and structure(bars)


def age_ok(first_ms, now, minimum):
    return 0 < first_ms <= (now-minimum*86400)*1000


def liquid(info, ticker, book, cfg):
    if info.get('status') != 'TRADING' or info.get('quoteAsset') != 'USDT' or not info.get('isSpotTradingAllowed', False):
        return False
    bid, ask = float(book.get('bidPrice', 0)), float(book.get('askPrice', 0))
    return (0 < bid <= ask and float(ticker.get('quoteVolume', 0)) >= cfg['min_quote_volume']
            and (ask-bid)/((ask+bid)/2)*100 <= cfg['max_spread_pct'])


def setups(bars):
    c = [b['c'] for b in bars]
    a, last, prev = atr(bars), bars[-1], bars[-2]
    e = ema(c, 20)[-1]
    resistance = max(b['h'] for b in bars[-22:-2])
    supports = pivots(bars[:-1], 'l')
    found = []
    if prev['c'] > resistance and last['l'] <= resistance+a*.3 and last['c'] > resistance:
        found.append('breakout-retest')
    if last['l'] <= e+a*.3 and last['c'] > e and last['c'] > last['o']:
        found.append('pullback continuation')
    if last['c'] > max(b['h'] for b in bars[-6:-1]) and last['v'] > statistics.mean(b['v'] for b in bars[-21:-1])*1.3:
        found.append('momentum continuation')
    if supports and abs(last['l']-supports[-1]) <= a*.4 and last['c'] > last['o'] and last['c']-last['l'] > a*.5:
        found.append('support bounce')
    if structure(bars) and c[-1] > c[-2] > c[-3] and c[-1] > e:
        found.append('trend continuation')
    return found


def levels(bars, higher, price, cfg):
    a = atr(bars)
    supports = [x for x in pivots(bars[-80:], 'l') if x < price]
    if not supports:
        raise ValueError('levels:no confirmed support')
    support = max(supports)
    anchor = max(support, min(ema([b['c'] for b in bars], 20)[-1], price))
    low, entry = anchor-a*.1, min(price, anchor+a*.25)
    if not 0 < low <= entry or price-entry > a*.75:
        raise ValueError('levels:entry too far from market')
    stop = support-a*.25
    risk = entry-stop
    if risk <= 0 or not cfg['min_stop_pct'] <= risk/entry*100 <= cfg['max_stop_pct']:
        raise ValueError('levels:stop distance')
    resistance = sorted(set(x for series in (bars, *higher) for x in pivots(series, 'h') if x > entry))
    if len(resistance) < 2:
        raise ValueError('levels:two confirmed resistances unavailable')
    tp1, tp2 = resistance[0]-a*.1, resistance[1]-a*.1
    if tp1 <= price or tp2 <= tp1 or (tp1-entry)/entry*100 < cfg['min_target_pct']:
        raise ValueError('levels:insufficient upside before resistance')
    if (tp1-entry)/risk < cfg['min_rr']:
        raise ValueError('levels:R:R')
    # Four 1H ATRs is a feasibility ceiling, not a time or profit prediction.
    if tp1-entry > atr(higher[0])*4:
        raise ValueError('levels:target beyond near-term volatility budget')
    return dict(low=low, entry=entry, sl=stop, tp1=tp1, tp2=tp2, rr=(tp1-entry)/risk)


def score(features):
    weights = dict(trend=20, structure=10, volume=10, momentum=10, volatility=10,
                   liquidity=10, btc=10, extension=5, resistance=5, rr=10)
    return round(sum(weights[k]*max(0, min(1, features.get(k, 0))) for k in weights))


def correlation(a, b):
    n = min(len(a), len(b), 48)
    if n < 20:
        return 1.0  # Unknown correlation: conservative suppression.
    x, y = a[-n:], b[-n:]
    mx, my = statistics.mean(x), statistics.mean(y)
    denom = math.sqrt(sum((v-mx)**2 for v in x)*sum((v-my)**2 for v in y))
    return sum((u-mx)*(v-my) for u, v in zip(x, y))/denom if denom else 1.0


class State:
    def __init__(self, path):
        self.path = Path(path)
        self.data = json.loads(self.path.read_text()) if self.path.exists() else {'version': 1, 'alerts': [], 'ages': {}}
        if self.data.get('version') != 1 or not isinstance(self.data.get('alerts'), list) or not isinstance(self.data.get('ages'), dict):
            raise ValueError('Invalid state: restore valid state; refusing reset')
        for alert in self.data['alerts']:
            if not isinstance(alert.get('symbol'), str) or not isinstance(alert.get('sector'), str) or not isinstance(alert.get('time'), (int, float)) or not math.isfinite(alert['time']) or alert['time'] <= 0:
                raise ValueError('Invalid alert state')
        if any(not isinstance(v, int) or v <= 0 for v in self.data['ages'].values()):
            raise ValueError('Invalid age state')

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix('.tmp')
        tmp.write_text(json.dumps(self.data, indent=2), encoding='utf-8')
        tmp.replace(self.path)

    def allowed(self, symbol, sector, now, cfg, cap):
        today = datetime.fromtimestamp(now, timezone.utc).date()
        daily = [x for x in self.data['alerts'] if datetime.fromtimestamp(x['time'], timezone.utc).date() == today]
        if len(daily) >= cap:
            return 'daily cap'
        if any(x['symbol'] == symbol and now-x['time'] < cfg['cooldown_hours']*3600 for x in self.data['alerts']):
            return 'cooldown'
        if sum(x['sector'] == sector for x in daily) >= cfg['max_sector_per_day']:
            return 'sector daily cap'
        return None

    def reserve(self, signal, now):
        self.data['alerts'] = [x for x in self.data['alerts'] if now-x['time'] < 30*86400]
        self.data['alerts'].append(dict(symbol=signal['symbol'], sector=signal['sector'], time=now, status='reserved'))
        self.save()


class Market:
    def __init__(self, cfg):
        self.cfg, self.session, self.cache = cfg, requests.Session(), {}

    def get(self, endpoint, **params):
        key = (endpoint, tuple(sorted(params.items())))
        if key in self.cache:
            return self.cache[key]
        for attempt in range(4):
            time.sleep(self.cfg['request_interval'])
            try:
                r = self.session.get(self.cfg['base_url']+endpoint, params=params, timeout=(10, 25))
                if r.status_code in (418, 451):
                    raise RuntimeError('Binance access unavailable; no signals')
                if r.status_code == 429 or r.status_code >= 500:
                    delay = min(float(r.headers.get('Retry-After', 2**(attempt+1))), 120)
                    time.sleep(delay)
                    continue
                r.raise_for_status()
                result = r.json()
                self.cache[key] = result
                if int(r.headers.get('X-MBX-USED-WEIGHT-1M', 0)) > 4000:
                    time.sleep(60)
                return result
            except (requests.RequestException, ValueError):
                time.sleep(2**attempt)
        raise RuntimeError('Market data unavailable after retries')

    def bars(self, symbol, interval, now):
        rows = self.get('/api/v3/klines', symbol=symbol, interval=interval, limit=250)
        seconds = {'15m': 900, '30m': 1800, '1h': 3600, '4h': 14400}[interval]
        closed = [r for r in rows if int(r[6]) < now*1000]
        if len(closed) < 200 or now*1000-int(closed[-1][6]) > seconds*1000+120000:
            raise ValueError('stale/insufficient candles')
        if any(int(b[0])-int(a[0]) != seconds*1000 for a, b in zip(closed, closed[1:])):
            raise ValueError('gapped candles')
        bars = [dict(o=float(r[1]), h=float(r[2]), l=float(r[3]), c=float(r[4]), v=float(r[5])) for r in closed]
        if any(not all(math.isfinite(v) for v in b.values()) or not 0 < b['l'] <= min(b['o'], b['c']) <= max(b['o'], b['c']) <= b['h'] or b['v'] < 0 for b in bars):
            raise ValueError('invalid candles')
        return bars


def telegram(message):
    token, chat = os.environ.get('TELEGRAM_BOT_TOKEN'), os.environ.get('TELEGRAM_CHAT_ID')
    if not token or not chat:
        raise RuntimeError('Required Telegram secrets missing')
    # No blind POST retry: timeout may mean Telegram already accepted the alert.
    try:
        response = requests.post('https://api.telegram.org/bot'+token+'/sendMessage',
                                 json={'chat_id': chat, 'text': message}, timeout=(10, 25))
        if response.status_code != 200 or not response.json().get('ok'):
            raise RuntimeError('Telegram rejected message; reservation retained')
    except (requests.RequestException, ValueError):
        raise RuntimeError('Telegram delivery uncertain; reservation retained') from None


def format_signal(s):
    p = lambda x: f'{x:.8g}'
    e = s['entry']
    return (f"SPOT WATCH — {s['symbol']}\nCurrent Price: {p(s['price'])}\n"
            f"Buy Zone: {p(s['low'])} – {p(e)} USDT\n"
            f"TP1: {p(s['tp1'])} (+{(s['tp1']/e-1)*100:.2f}%)\n"
            f"TP2: {p(s['tp2'])} (+{(s['tp2']/e-1)*100:.2f}%)\n"
            f"SL / technical invalidation: {p(s['sl'])} (risk {(1-s['sl']/e)*100:.2f}%)\n"
            f"Setup: {s['setup']}\nScore: {s['score']}/100\nRisk: elevated (crypto spot)\n"
            f"4H: EMA20 > EMA50, rising, HH/HL\n1H: EMA20 > EMA50, rising, HH/HL\n"
            f"Timing: {s['timing']} setup; 30m EMA alignment\nRSI: {s['rsi']:.1f}\n"
            f"Volume: {s['volume']:.2f}x prior 20 bars\nBTC: {s['btc']}\n"
            f"Passed: trend, confirmed resistance room, liquidity, purpose proxy, R:R {s['rr']:.2f}\n"
            f"UTC: {s['timestamp']}\nPercentages use upper entry; fees/slippage excluded. "
            'Watch zone for next 60 minutes; skip if invalidated or price runs away. No guaranteed outcome.')


def persist_remote():
    if os.environ.get('PERSIST_STATE_COMMAND') == 'github':
        subprocess.run(['bash', 'scripts/persist_state.sh'], check=True)


def evaluate(market, symbol, cfg, btc, now):
    h4 = market.bars(symbol, '4h', now)
    h1 = market.bars(symbol, '1h', now)
    if not trend(h4) or not trend(h1):
        raise ValueError('trend:4H+1H mandatory')
    if h1[-1]['c']/h1[-4]['c']-1 > .06 or h1[-1]['h']-h1[-1]['l'] > 3*atr(h1):
        raise ValueError('overextension:recent pump')
    m30, m15 = market.bars(symbol, '30m', now), market.bars(symbol, '15m', now)
    if m30[-1]['c'] < ema([b['c'] for b in m30], 20)[-1]:
        raise ValueError('timing:30m alignment')
    book = market.get('/api/v3/ticker/bookTicker', symbol=symbol)
    bid, ask = float(book['bidPrice']), float(book['askPrice'])
    if not 0 < bid <= ask or (ask-bid)/((ask+bid)/2)*100 > cfg['max_spread_pct']:
        raise ValueError('liquidity:fresh spread')
    price = (bid+ask)/2
    options, reasons = [], []
    for interval, bars in [('15m', m15), ('30m', m30)]:
        try:
            names = setups(bars)
            if not names:
                raise ValueError('setup:none')
            c = [b['c'] for b in bars]
            a, momentum = atr(bars), rsi(c)
            volume = bars[-1]['v']/max(statistics.mean(b['v'] for b in bars[-21:-1]), 1e-12)
            extension = (price-ema(c, 20)[-1])/a
            if not cfg['min_atr_pct'] <= a/price*100 <= cfg['max_atr_pct']:
                raise ValueError('volatility')
            if not 50 <= momentum <= 72 or volume < 1.05 or extension > 2 or abs(price-c[-1]) > a*.75:
                raise ValueError('momentum/volume/overextension')
            lv = levels(bars, [h1, h4], price, cfg)
            value = score(dict(trend=1, structure=float(structure(bars)), volume=min(volume/1.5, 1),
                               momentum=1-abs(momentum-60)/25, volatility=1, liquidity=1,
                               btc=1 if btc == 'strong uptrend' else .6, extension=1-max(0, extension)/3,
                               resistance=1, rr=min(lv['rr']/2, 1)))
            if value < cfg['min_score']:
                raise ValueError('score')
            options.append(dict(**lv, price=price, setup=names[0], timing=interval, score=value,
                                rsi=momentum, volume=volume, btc=btc,
                                returns=[b['c']/a['c']-1 for a, b in zip(h1, h1[1:])]))
        except ValueError as exc:
            reasons.append(interval+':'+str(exc))
    if not options:
        raise ValueError('; '.join(reasons))
    return max(options, key=lambda x: x['score'])


def load_config(path):
    cfg = yaml.safe_load(Path(path).read_text())
    positive = ['min_age_days', 'min_quote_volume', 'max_spread_pct', 'min_score', 'min_rr', 'max_stop_pct',
                'min_stop_pct', 'min_target_pct', 'max_atr_pct', 'min_atr_pct', 'cooldown_hours',
                'max_sector_per_day', 'max_setup_per_run', 'request_interval']
    if any(not isinstance(cfg.get(k), (float, int)) or not math.isfinite(cfg[k]) or cfg[k] <= 0 for k in positive):
        raise ValueError('Invalid positive config setting')
    if not 1 <= cfg['daily_cap'] <= 5 or not cfg['daily_cap'] <= cfg['strong_market_cap'] <= 8:
        raise ValueError('Daily cap must be 1..5; strong market cap up to 8')
    if not 0 <= cfg['correlation_threshold'] <= 1 or cfg['min_score'] > 100 or cfg['min_stop_pct'] > cfg['max_stop_pct'] or cfg['min_atr_pct'] > cfg['max_atr_pct']:
        raise ValueError('Invalid config range')
    if cfg['base_url'] != 'https://data-api.binance.vision':
        raise ValueError('Only Binance public market-data endpoint supported')
    return cfg


def main():
    parser = argparse.ArgumentParser()
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument('--test-telegram', action='store_true')
    modes.add_argument('--dry-run', action='store_true')
    parser.add_argument('--symbol')
    parser.add_argument('--config', default='config.yaml')
    args = parser.parse_args()
    if args.test_telegram:
        telegram('Crypto Spot Signal Bot: connection test only. No trade signal.')
        print('Telegram test accepted')
        return
    cfg = load_config(args.config)
    assets = yaml.safe_load(Path('assets.yaml').read_text())
    state, market = State(cfg['state_path']), Market(cfg)
    now = market.get('/api/v3/time')['serverTime']/1000
    if abs(time.time()-now) > 120:
        raise RuntimeError('Local clock differs from exchange by over 120 seconds')
    infos = market.get('/api/v3/exchangeInfo')['symbols']
    tickers = {x['symbol']: x for x in market.get('/api/v3/ticker/24hr')}
    books = {x['symbol']: x for x in market.get('/api/v3/ticker/bookTicker')}
    btc = 'strong uptrend' if all(trend(market.bars('BTCUSDT', interval, now)) for interval in ['4h', '1h']) else 'bearish/choppy: long signals suppressed'
    candidates, counts = [], Counter()
    def reject(symbol, reason):
        counts[reason.split(':')[0]] += 1
        print(f'REJECT {symbol}: {reason}')
    for info in infos:
        symbol, base = info['symbol'], info['baseAsset']
        if info['quoteAsset'] != 'USDT' or args.symbol and symbol != args.symbol.upper():
            continue
        try:
            asset = assets.get(base, {})
            if asset.get('status') != 'approved' or not asset.get('notes') or not asset.get('source'):
                raise ValueError('purpose:unknown/rejected/review needed')
            if not liquid(info, tickers.get(symbol, {}), books.get(symbol, {}), cfg):
                raise ValueError('liquidity:status/volume/spread')
            if symbol not in state.data['ages']:
                first = market.get('/api/v3/klines', symbol=symbol, interval='1d', startTime=0, limit=1)
                if not first:
                    raise ValueError('age:missing history')
                state.data['ages'][symbol] = int(first[0][0])
            if not age_ok(state.data['ages'][symbol], now, cfg['min_age_days']):
                raise ValueError('age:under minimum history')
            if btc != 'strong uptrend':
                raise ValueError('BTC:bearish or choppy')
            result = evaluate(market, symbol, cfg, btc, now)
            result.update(symbol=symbol, sector=asset['sector'], timestamp=datetime.fromtimestamp(now, timezone.utc).isoformat())
            candidates.append(result)
        except ValueError as exc:
            reject(symbol, str(exc))
    chosen, setup_counts = [], Counter()
    cap = cfg['strong_market_cap'] if btc == 'strong uptrend' else cfg['daily_cap']
    for signal in sorted(candidates, key=lambda x: x['score'], reverse=True):
        reason = state.allowed(signal['symbol'], signal['sector'], now, cfg, cap)
        if reason or setup_counts[signal['setup']] >= cfg['max_setup_per_run'] or any(correlation(signal['returns'], x['returns']) >= cfg['correlation_threshold'] for x in chosen):
            reject(signal['symbol'], reason or 'correlation/setup overlap')
            continue
        # A scan takes time: avoid transmitting stale opportunities.
        if time.time()-now > 1200:
            raise RuntimeError('Scan exceeded 20-minute freshness budget')
        if not args.dry_run:
            if not os.environ.get('TELEGRAM_BOT_TOKEN') or not os.environ.get('TELEGRAM_CHAT_ID'):
                raise RuntimeError('Required Telegram secrets missing')
            market.cache.pop(('/api/v3/ticker/bookTicker', (('symbol', signal['symbol']),)), None)
            fresh = market.get('/api/v3/ticker/bookTicker', symbol=signal['symbol'])
            bid, ask = float(fresh['bidPrice']), float(fresh['askPrice'])
            current = (bid+ask)/2
            if not 0 < bid <= ask or (ask-bid)/current*100 > cfg['max_spread_pct'] or abs(current/signal['price']-1) > .002 or current <= signal['sl'] or current >= signal['tp1']:
                reject(signal['symbol'], 'freshness:price/spread changed')
                continue
            signal['price'] = current
            state.reserve(signal, now)
            persist_remote()  # Fail closed if durable reservation cannot be saved.
            telegram(format_signal(signal))
        else:
            state.data['alerts'].append(dict(symbol=signal['symbol'], sector=signal['sector'], time=now, status='dry-run'))
            print(format_signal(signal))
        chosen.append(signal)
        setup_counts[signal['setup']] += 1
    if not args.dry_run:
        state.save()
        persist_remote()
    if not chosen:
        print('NO TRADE')
    print('SUMMARY', json.dumps(dict(rejections=dict(counts), qualified=len(candidates), alerts=len(chosen), dry_run=args.dry_run)))


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        # Never expose request URLs, credentials, HTTP bodies or exception traces.
        print('SCAN FAILED:', type(exc).__name__, '— inspect configuration/state and service availability; no automatic retry of alerts')
        raise SystemExit(1)

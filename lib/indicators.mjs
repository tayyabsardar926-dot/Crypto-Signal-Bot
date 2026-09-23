import { avg, atr, clamp, ema, pct, rsi, round } from './math.mjs';

export function closedBars(bars, now = Date.now()) {
  const c = bars.filter(b => Number.isFinite(b.closeTime) ? b.closeTime < now : true);
  return c.length ? c : bars;
}

function latest(arr) { return arr[arr.length - 1]; }

export function marketRegime(hourly) {
  const bars = closedBars(hourly);
  if (bars.length < 60) return { state: 'UNKNOWN', score: 0, momentum6h: null };
  const closes = bars.map(b => b.close);
  const e20 = ema(closes, 20), e50 = ema(closes, 50);
  const i = closes.length - 1;
  const m6 = pct(closes[i - 6], closes[i]);
  const up = closes[i] > e20[i] && e20[i] > e50[i] && e20[i] > e20[i - 5];
  const down = closes[i] < e20[i] && e20[i] < e50[i] && e20[i] < e20[i - 5];
  return { state: up ? 'BULLISH' : down ? 'BEARISH' : 'NEUTRAL', score: up ? 1 : down ? -1 : 0, momentum6h: round(m6, 2) };
}

export function deriveFeatures(hourlyRaw, fifteenRaw, market, liquidity = {}) {
  const hourly = closedBars(hourlyRaw);
  const fifteen = closedBars(fifteenRaw);
  if (hourly.length < 70 || fifteen.length < 30) throw new Error('Not enough candles');

  const hc = hourly.map(b => b.close), hv = hourly.map(b => b.volume);
  const fc = fifteen.map(b => b.close);
  const e20 = ema(hc, 20), e50 = ema(hc, 50), rs = rsi(hc, 14), at = atr(hourly, 14);
  const i = hc.length - 1;
  const close = hc[i];
  const atrPct = at[i] / close * 100;
  const volumeBase = avg(hv.slice(i - 20, i));
  const volumeRatio = volumeBase > 0 ? hv[i] / volumeBase : 1;
  const high20 = Math.max(...hourly.slice(i - 20, i).map(b => b.high));
  const breakoutRatio = close / high20;
  const mom1h = pct(hc[i - 1], close);
  const mom3h = pct(hc[i - 3], close);
  const mom15_1h = pct(fc[fc.length - 5], latest(fc));
  const mom15_3h = pct(fc[fc.length - 13], latest(fc));
  const candle = hourly[i];
  const bodyPct = Math.abs(candle.close - candle.open) / Math.max(candle.high - candle.low, close * 0.000001);
  const trendUp = close > e20[i] && e20[i] > e50[i] && e20[i] > e20[i - 4];
  const distEma20Pct = (close / e20[i] - 1) * 100;

  let score = 0;
  if (trendUp) score += 25;
  else if (close > e20[i] && e20[i] >= e50[i] * 0.995) score += 16;
  else if (close > e20[i]) score += 8;
  const rv = rs[i] ?? 50;
  if (rv >= 54 && rv <= 68) score += 12;
  else if (rv >= 50 && rv < 54) score += 8;
  else if (rv > 68 && rv <= 74) score += 6;
  if (mom15_1h > 0.25 && mom15_1h < Math.max(4, atrPct * 1.8)) score += 5;
  if (mom1h > 0) score += 3;
  if (volumeRatio >= 1.8) score += 15;
  else if (volumeRatio >= 1.35) score += 12;
  else if (volumeRatio >= 1.10) score += 8;
  else if (volumeRatio >= 0.90) score += 4;
  if (breakoutRatio >= 1.002) score += 15;
  else if (breakoutRatio >= 0.995) score += 12;
  else if (breakoutRatio >= 0.985) score += 7;
  if (market?.state === 'BULLISH') score += 10;
  else if (market?.state === 'NEUTRAL') score += 6;
  else if (market?.state === 'BEARISH') score += 1;
  const spread = liquidity.spreadPct ?? 99;
  if (spread <= 0.08) score += 10;
  else if (spread <= 0.18) score += 8;
  else if (spread <= 0.35) score += 5;
  else if (spread <= 0.60) score += 2;
  score = clamp(score, 0, 100);

  const overextended = rv > 76 || mom3h > Math.max(7, atrPct * 3.1) || distEma20Pct > Math.max(7, atrPct * 2.4);
  let stage = 'BASE';
  if (overextended) stage = 'OVEREXTENDED';
  else if (trendUp && breakoutRatio >= 0.995 && volumeRatio >= 1.10 && mom1h > 0) stage = 'EARLY';
  else if (trendUp && mom1h > 0) stage = 'DEVELOPING';
  else if (trendUp) stage = 'TRENDING';
  else stage = 'WAIT';

  return {
    close, ema20: e20[i], ema50: e50[i], rsi: rv, atrPct,
    volumeRatio, breakoutRatio, mom1h, mom3h, mom15_1h, mom15_3h,
    bodyPct, trendUp, distEma20Pct, score, stage,
    spreadPct: spread,
    quoteVolume24h: liquidity.quoteVolume24h ?? null
  };
}

export function hourlyFeatureAt(hourly, index) {
  if (index < 70) return null;
  const slice = hourly.slice(index - 70, index + 1);
  const closes = slice.map(b => b.close), vols = slice.map(b => b.volume);
  const e20 = ema(closes, 20), e50 = ema(closes, 50), rs = rsi(closes, 14), at = atr(slice, 14);
  const i = slice.length - 1;
  const close = closes[i], atrPct = at[i] / close * 100;
  const vrBase = avg(vols.slice(i - 20, i));
  const volumeRatio = vrBase > 0 ? vols[i] / vrBase : 1;
  const high20 = Math.max(...slice.slice(i - 20, i).map(b => b.high));
  const breakoutRatio = close / high20;
  const mom1h = pct(closes[i - 1], close), mom3h = pct(closes[i - 3], close);
  const rv = rs[i] ?? 50;
  const trendUp = close > e20[i] && e20[i] > e50[i] && e20[i] > e20[i - 4];
  const distEma20Pct = (close / e20[i] - 1) * 100;
  let score = 0;
  if (trendUp) score += 30; else if (close > e20[i]) score += 12;
  if (rv >= 54 && rv <= 68) score += 18; else if (rv >= 50 && rv < 54) score += 10; else if (rv > 68 && rv <= 74) score += 7;
  if (mom1h > 0) score += 7;
  if (volumeRatio >= 1.8) score += 20; else if (volumeRatio >= 1.35) score += 15; else if (volumeRatio >= 1.10) score += 10; else if (volumeRatio >= .9) score += 4;
  if (breakoutRatio >= 1.002) score += 20; else if (breakoutRatio >= .995) score += 15; else if (breakoutRatio >= .985) score += 8;
  score = clamp(score, 0, 100);
  const overextended = rv > 76 || mom3h > Math.max(7, atrPct * 3.1) || distEma20Pct > Math.max(7, atrPct * 2.4);
  return { close, rsi: rv, atrPct, volumeRatio, breakoutRatio, mom1h, mom3h, score, overextended };
}

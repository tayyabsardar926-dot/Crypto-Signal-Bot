import { clamp, round } from './math.mjs';

export function buildPlan(price, f) {
  const atr = clamp(f.atrPct || 2, 0.5, 12);
  const stopPct = clamp(atr * 0.90, 1.0, 3.2);
  let tp1Pct = clamp(atr * 1.10, 1.8, 4.0);
  let tp2Pct = clamp(atr * 1.80, 3.0, 8.0);
  let tp3Pct = clamp(atr * 2.70, 5.0, 15.0);
  if (tp2Pct <= tp1Pct) tp2Pct = tp1Pct + 1.0;
  if (tp3Pct <= tp2Pct) tp3Pct = tp2Pct + 1.5;
  const entryBufferPct = clamp(atr * 0.15, 0.08, 0.45);
  const chaseBufferPct = clamp(atr * 0.08, 0.05, 0.25);
  const entryLow = price * (1 - entryBufferPct / 100);
  const entryHigh = price * (1 + chaseBufferPct / 100);
  const mid = (entryLow + entryHigh) / 2;
  const stop = mid * (1 - stopPct / 100);
  const tp1 = mid * (1 + tp1Pct / 100);
  const tp2 = mid * (1 + tp2Pct / 100);
  const tp3 = mid * (1 + tp3Pct / 100);
  return {
    entryLow: round(entryLow, 10), entryHigh: round(entryHigh, 10), entryMid: round(mid, 10),
    stop: round(stop, 10), stopPct: round(stopPct, 2),
    tp1: round(tp1, 10), tp1Pct: round(tp1Pct, 2),
    tp2: round(tp2, 10), tp2Pct: round(tp2Pct, 2),
    tp3: round(tp3, 10), tp3Pct: round(tp3Pct, 2),
    expectedLowPct: round(tp1Pct, 2), expectedHighPct: round(tp2Pct, 2),
    rr1: round(tp1Pct / stopPct, 2), rr2: round(tp2Pct / stopPct, 2),
    expiryMinutes: f.stage === 'EARLY' ? 60 : 120
  };
}

export function explainCandidate(f, market) {
  const reasons = [];
  if (f.trendUp) reasons.push('1H trend up');
  if (f.volumeRatio >= 1.35) reasons.push(`volume ${f.volumeRatio.toFixed(1)}× average`);
  else if (f.volumeRatio >= 1.10) reasons.push('volume improving');
  if (f.breakoutRatio >= .995) reasons.push('near/fresh breakout');
  if (f.rsi >= 54 && f.rsi <= 68) reasons.push('momentum healthy');
  if (market?.state === 'BULLISH') reasons.push('BTC regime supportive');
  if (f.spreadPct <= .18) reasons.push('tight spread');
  if (f.stage === 'OVEREXTENDED') reasons.push('move already stretched');
  if (!reasons.length) reasons.push('setup not confirmed');
  return reasons.slice(0, 5).join(' · ');
}

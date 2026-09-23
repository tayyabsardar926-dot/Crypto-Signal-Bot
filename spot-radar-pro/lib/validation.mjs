import { hourlyFeatureAt } from './indicators.mjs';
import { round, wilsonLower } from './math.mjs';

function outcome(bars, i, targetPct, stopPct, horizon = 6) {
  const entry = bars[i].close;
  const target = entry * (1 + targetPct / 100);
  const stop = entry * (1 - stopPct / 100);
  for (let j = i + 1; j <= Math.min(i + horizon, bars.length - 1); j++) {
    const b = bars[j];
    const hitT = b.high >= target;
    const hitS = b.low <= stop;
    if (hitT && hitS) return 'fail';
    if (hitS) return 'fail';
    if (hitT) return 'win';
  }
  return 'fail';
}

export function validateCurrent(history, current, plan, minEvents = 30) {
  const targets = [plan.tp1Pct, plan.tp2Pct, plan.tp3Pct];
  const stats = targets.map(t => ({ targetPct: t, n: 0, wins: 0 }));
  const scoreFloor = Math.max(62, current.score - 10);
  for (let i = 75; i < history.length - 7; i++) {
    const f = hourlyFeatureAt(history, i);
    if (!f || f.overextended) continue;
    if (f.score < scoreFloor) continue;
    if (Math.abs(f.rsi - current.rsi) > 10) continue;
    if (current.atrPct > 0 && (f.atrPct < current.atrPct * .45 || f.atrPct > current.atrPct * 1.8)) continue;
    for (const s of stats) {
      s.n++;
      if (outcome(history, i, s.targetPct, plan.stopPct, 6) === 'win') s.wins++;
    }
  }
  return stats.map(s => ({
    targetPct: round(s.targetPct, 2), n: s.n, wins: s.wins,
    frequency: s.n ? round(s.wins / s.n, 4) : null,
    wilsonLower: s.n ? round(wilsonLower(s.wins, s.n), 4) : null,
    validated: s.n >= minEvents
  }));
}

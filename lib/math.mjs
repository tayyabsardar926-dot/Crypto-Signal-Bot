export const clamp = (x, lo, hi) => Math.max(lo, Math.min(hi, x));
export const avg = (a) => a.length ? a.reduce((s, x) => s + x, 0) / a.length : NaN;
export const pct = (a, b) => (a && Number.isFinite(a) && Number.isFinite(b)) ? ((b / a) - 1) * 100 : NaN;
export const round = (x, d = 4) => Number.isFinite(x) ? Number(x.toFixed(d)) : null;

export function ema(values, period) {
  if (!values.length) return [];
  const k = 2 / (period + 1);
  const out = new Array(values.length).fill(null);
  let prev = values[0];
  out[0] = prev;
  for (let i = 1; i < values.length; i++) {
    prev = values[i] * k + prev * (1 - k);
    out[i] = prev;
  }
  return out;
}

export function rsi(values, period = 14) {
  const out = new Array(values.length).fill(null);
  if (values.length <= period) return out;
  let gains = 0, losses = 0;
  for (let i = 1; i <= period; i++) {
    const d = values[i] - values[i - 1];
    if (d >= 0) gains += d; else losses -= d;
  }
  let ag = gains / period, al = losses / period;
  out[period] = al === 0 ? 100 : 100 - 100 / (1 + ag / al);
  for (let i = period + 1; i < values.length; i++) {
    const d = values[i] - values[i - 1];
    const g = Math.max(d, 0), l = Math.max(-d, 0);
    ag = (ag * (period - 1) + g) / period;
    al = (al * (period - 1) + l) / period;
    out[i] = al === 0 ? 100 : 100 - 100 / (1 + ag / al);
  }
  return out;
}

export function atr(bars, period = 14) {
  const tr = bars.map((b, i) => {
    if (i === 0) return b.high - b.low;
    const pc = bars[i - 1].close;
    return Math.max(b.high - b.low, Math.abs(b.high - pc), Math.abs(b.low - pc));
  });
  const out = new Array(bars.length).fill(null);
  if (bars.length < period) return out;
  let a = avg(tr.slice(0, period));
  out[period - 1] = a;
  for (let i = period; i < tr.length; i++) {
    a = (a * (period - 1) + tr[i]) / period;
    out[i] = a;
  }
  return out;
}

export function wilsonLower(successes, n, z = 1.96) {
  if (!n) return 0;
  const phat = successes / n;
  const z2 = z * z;
  return (phat + z2/(2*n) - z * Math.sqrt((phat*(1-phat)+z2/(4*n))/n)) / (1 + z2/n);
}

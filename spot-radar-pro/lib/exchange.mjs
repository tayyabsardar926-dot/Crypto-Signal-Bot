const BASES = {
  binance: ['https://data-api.binance.vision','https://api.binance.com','https://api-gcp.binance.com','https://api1.binance.com'],
  mexc: ['https://api.mexc.com']
};

function timeoutSignal(ms = 10000) {
  const c = new AbortController();
  const id = setTimeout(() => c.abort(), ms);
  return { signal: c.signal, clear: () => clearTimeout(id) };
}

export async function getJson(url, ms = 12000) {
  const t = timeoutSignal(ms);
  try {
    const r = await fetch(url, { signal: t.signal, headers: { 'User-Agent': 'SpotRadarPro/1.0' } });
    if (!r.ok) throw new Error(`${r.status} ${r.statusText} for ${url}`);
    return await r.json();
  } finally { t.clear(); }
}

function klineInterval(exchange, interval) {
  if (exchange === 'mexc' && interval === '1h') return '60m';
  return interval;
}

export function normalizeKlines(rows) {
  return rows.map(r => ({
    openTime: Number(r[0]), open: Number(r[1]), high: Number(r[2]), low: Number(r[3]),
    close: Number(r[4]), volume: Number(r[5]), closeTime: Number(r[6]), quoteVolume: Number(r[7] || 0)
  })).filter(b => [b.open,b.high,b.low,b.close,b.volume].every(Number.isFinite));
}

async function marketGet(exchange, pathname) {
  let last;
  for (const base of BASES[exchange]) {
    try { return await getJson(`${base}${pathname}`); } catch (e) { last = e; }
  }
  throw last || new Error(`No endpoint available for ${exchange}`);
}

export async function exchangeInfo(exchange) {
  return marketGet(exchange, '/api/v3/exchangeInfo');
}

export async function ticker24(exchange) {
  return marketGet(exchange, '/api/v3/ticker/24hr');
}

export async function bookTicker(exchange) {
  return marketGet(exchange, '/api/v3/ticker/bookTicker');
}

export async function klines(exchange, symbol, interval = '1h', limit = 200, endTime = null) {
  const p = new URLSearchParams({ symbol, interval: klineInterval(exchange, interval), limit: String(limit) });
  if (endTime) p.set('endTime', String(endTime));
  const rows = await marketGet(exchange, `/api/v3/klines?${p.toString()}`);
  return normalizeKlines(rows);
}

export async function historyHours(exchange, symbol, days = 120) {
  const need = Math.ceil(days * 24) + 80;
  let endTime = null, all = [];
  for (let page = 0; page < 8 && all.length < need; page++) {
    const batch = await klines(exchange, symbol, '1h', Math.min(1000, need - all.length), endTime);
    if (!batch.length) break;
    all = batch.concat(all);
    const oldest = batch[0].openTime;
    endTime = oldest - 1;
    if (batch.length < 100) break;
    await new Promise(r => setTimeout(r, 90));
  }
  const map = new Map(all.map(b => [b.openTime, b]));
  return [...map.values()].sort((a,b) => a.openTime - b.openTime).slice(-need);
}

export function universeFromInfo(exchange, info) {
  const stable = new Set(['USDT','USDC','FDUSD','TUSD','USDP','DAI','BUSD','EUR','EURC','USD1','RLUSD','PYUSD']);
  const leveraged = /(UP|DOWN|BULL|BEAR|3L|3S|5L|5S)$/i;
  return (info.symbols || []).filter(s => {
    const status = String(s.status || '').toUpperCase();
    const online = exchange === 'binance' ? status === 'TRADING' : ['ENABLED','1','TRADING'].includes(status);
    const spot = s.isSpotTradingAllowed !== false && (!s.permissions || s.permissions.includes('SPOT') || exchange === 'mexc');
    return online && spot && s.quoteAsset === 'USDT' && !stable.has(s.baseAsset) && !leveraged.test(s.baseAsset || '');
  }).map(s => ({ symbol: s.symbol, baseAsset: s.baseAsset, quoteAsset: s.quoteAsset }));
}

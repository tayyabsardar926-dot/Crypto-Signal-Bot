import config from '../config.json' with { type: 'json' };
import { bookTicker, exchangeInfo, historyHours, klines, ticker24, universeFromInfo } from './exchange.mjs';
import { deriveFeatures, marketRegime } from './indicators.mjs';
import { buildPlan, explainCandidate } from './plans.mjs';
import { validateCurrent } from './validation.mjs';
import { round } from './math.mjs';

const quoteVol = t => Number(t.quoteVolume ?? t.quoteVolume24h ?? 0);
const lastPrice = t => Number(t.lastPrice ?? t.price ?? 0);

function spreadPct(b) {
  const bid = Number(b?.bidPrice || 0), ask = Number(b?.askPrice || 0);
  if (!(bid > 0 && ask > 0)) return 99;
  return (ask - bid) / ((ask + bid) / 2) * 100;
}

async function mapLimit(items, limit, fn) {
  const out = new Array(items.length); let next = 0;
  async function worker() { while (true) { const i = next++; if (i >= items.length) return; try { out[i] = await fn(items[i], i); } catch (e) { out[i] = { error: e.message, ...items[i] }; } } }
  await Promise.all(Array.from({ length: Math.min(limit, items.length) }, worker));
  return out;
}

async function exchangeCandidates(exchange, market) {
  const [info, t24, books] = await Promise.all([exchangeInfo(exchange), ticker24(exchange), bookTicker(exchange)]);
  const u = universeFromInfo(exchange, info);
  const tMap = new Map((Array.isArray(t24) ? t24 : [t24]).map(x => [x.symbol, x]));
  const bMap = new Map((Array.isArray(books) ? books : [books]).map(x => [x.symbol, x]));
  const minVol = exchange === 'binance' ? config.filters.binanceMin24hQuoteVolumeUsd : config.filters.mexcMin24hQuoteVolumeUsd;
  const cheap = u.map(x => {
    const t = tMap.get(x.symbol), b = bMap.get(x.symbol);
    return { ...x, exchange, price: lastPrice(t), quoteVolume24h: quoteVol(t), spreadPct: spreadPct(b) };
  }).filter(x => x.price > 0 && x.quoteVolume24h >= minVol && x.spreadPct <= config.filters.maxSpreadPct)
    .sort((a,b) => b.quoteVolume24h - a.quoteVolume24h)
    .slice(0, config.topMarketsPerExchange);

  const enriched = await mapLimit(cheap, 6, async x => {
    const [h, f] = await Promise.all([klines(exchange, x.symbol, '1h', 120), klines(exchange, x.symbol, '15m', 120)]);
    const features = deriveFeatures(h, f, market, x);
    const plan = buildPlan(x.price, features);
    return { ...x, features, plan, score: features.score, stage: features.stage, reason: explainCandidate(features, market) };
  });
  return enriched.filter(x => !x.error).sort((a,b) => b.score - a.score);
}

function decideStatus(c, market) {
  const v = c.validation?.[0];
  const b = config.buyRules;
  const buy = c.score >= b.minScore && ['EARLY','DEVELOPING'].includes(c.stage) && market.state !== 'BEARISH' &&
    c.spreadPct <= b.maxSpreadPct && v?.validated && v.frequency >= b.minTp1Frequency && v.wilsonLower >= b.minWilsonLowerBound;
  if (buy) return 'BUY NOW';
  if (c.stage === 'OVEREXTENDED') return 'AVOID';
  if (c.score >= 65) return 'WATCH';
  return 'AVOID';
}

export async function scanAll() {
  let btc = null, marketSource = 'binance';
  try { btc = await klines('binance', 'BTCUSDT', '1h', 120); }
  catch { marketSource = 'mexc'; btc = await klines('mexc', 'BTCUSDT', '1h', 120); }
  const market = { ...marketRegime(btc), source: marketSource };
  const results = await Promise.allSettled([exchangeCandidates('binance', market), exchangeCandidates('mexc', market)]);
  let candidates = [];
  const errors = [];
  for (let i = 0; i < results.length; i++) {
    const ex = i === 0 ? 'binance' : 'mexc';
    if (results[i].status === 'fulfilled') candidates.push(...results[i].value);
    else errors.push(`${ex}: ${results[i].reason?.message || results[i].reason}`);
  }
  candidates.sort((a,b) => b.score - a.score);
  const toValidate = candidates.filter(c => c.stage !== 'OVEREXTENDED' && c.score >= 65).slice(0, config.deepValidateTop);
  await mapLimit(toValidate, 2, async c => {
    try {
      const hist = await historyHours(c.exchange, c.symbol, config.historyDays);
      c.validation = validateCurrent(hist, c.features, c.plan, config.minHistoryEvents);
      c.historyBars = hist.length;
    } catch (e) { c.validationError = e.message; }
    return c;
  });
  for (const c of candidates) {
    c.status = decideStatus(c, market);
    c.price = round(c.price, 12);
    c.spreadPct = round(c.spreadPct, 3);
    c.quoteVolume24h = round(c.quoteVolume24h, 0);
    c.features = {
      rsi: round(c.features.rsi, 1), atrPct: round(c.features.atrPct, 2), volumeRatio: round(c.features.volumeRatio, 2),
      mom1h: round(c.features.mom1h, 2), mom3h: round(c.features.mom3h, 2), breakoutRatio: round(c.features.breakoutRatio, 4), trendUp: c.features.trendUp
    };
  }
  const buyNow = candidates.filter(c => c.status === 'BUY NOW');
  const watch = candidates.filter(c => c.status === 'WATCH');
  return {
    generatedAt: new Date().toISOString(), market, errors,
    summary: { scanned: candidates.length, buyNow: buyNow.length, watch: watch.length },
    bestTrade: buyNow[0] || null,
    top3: buyNow.slice(0,3),
    watch: watch.slice(0,12),
    candidates: candidates.slice(0,60),
    notes: [
      'BUY NOW is withheld unless the top target has enough same-asset historical examples and passes conservative validation.',
      'Historical frequency is not a guaranteed future probability.',
      'No API keys or order placement are used.'
    ]
  };
}

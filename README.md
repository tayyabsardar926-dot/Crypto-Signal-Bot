# Crypto Spot Signal Bot

Conservative Python scanner for Binance public spot USDT markets. Produces watchlist signals and Telegram alerts; never submits orders. No exchange API key, paid data feed, TradingView dependency, or AI subscription/API is used at runtime.

## Quick start

Use Python 3.12. From the extracted project root:

```text
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
python signal_bot.py --dry-run
python signal_bot.py --dry-run --symbol BTCUSDT
python signal_bot.py --test-telegram
python signal_bot.py
```

Normal and Telegram-test modes use environment variables `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`. Never put their values in files or commands committed to GitHub. A Telegram test sends one connection message only. Dry run uses public network data, prints qualifying signals, and does not send messages or write state. Unit tests are offline.

## Decision rules

All available USDT markets are screened. Only trading, spot-enabled pairs with at least 15m USDT quote volume/day and spread <=0.15% proceed. Purpose metadata is an allowlist: unknown, review, lending, gambling, adult, leveraged or unclear-purpose assets do not pass by default. Initial approved coverage is deliberately small: payment/storage use cases; expand only after independent review. This is broad market screening, not a claim that hundreds of tokens are approved.

Earliest available Binance daily kline is cached per pair and must be at least 548 days old. This is a practical *listing-history proxy*, not proof of project age. Renames, migrations, missing older listings and delistings can distort it. It may exclude older projects listed recently. Review metadata and clear only the affected cached age after a migration.

4H and 1H each require price > EMA20 > EMA50, rising EMA20/50, and two confirmed higher swing highs and lows. Swings use two closed candles on each side. BTC must satisfy both trends too; otherwise all longs are suppressed. This strict binary regime gate treats weak/choppy conditions as NO TRADE.

15m and 30m independently evaluate breakout-retest, EMA pullback continuation, momentum continuation, confirmed-support bounce and HH/HL trend continuation. 30m EMA alignment is also mandatory. If setups overlap, the first applicable descriptive label is used; the highest scoring eligible timeframe wins. RSI uses Wilder smoothing (flat=50), ATR uses Wilder true-range smoothing, EMA starts from an SMA seed. No proprietary indicator equivalence is claimed.

Score weights: trend 20; structure, volume, momentum, volatility, liquidity, BTC and R:R 10 each; extension and resistance 5 each. Minimum 80. Hard gates reject weak trend, RSI outside 50–72, volume below 1.05x prior 20 candles, extreme ATR, >2 ATR extension, recent >6% three-hour pump, and abnormal candle ranges. Scores are heuristic, not calibrated probabilities or accuracy estimates.

Entry zone uses confirmed support, EMA20 and ATR. SL is below confirmed support by 0.25 ATR. Risk is measured from the upper entry (worst entry in the zone). Default stop distance 0.3–2%, TP1 reward:risk >=1.5 and TP1 upside >=1% before the nearest confirmed resistance. TP2 uses the next resistance. Targets are set slightly below resistance; no resistance means no invented target. A four-1H-ATR feasibility ceiling bounds TP1 distance. This does not predict a move or arrival time. All percentages exclude fees and slippage. Inspect the zone within 60 minutes; skip an invalidated or escaped setup.

Sort by score; allow up to five reservations/day UTC. `strong_market_cap` may be raised to eight (never above eight). Current strict BTC gate means every eligible run is a strong-market run. One per sector/day, two per setup/run, and hourly-return Pearson correlation >=0.85 suppression within the current batch reduce spam. Cross-run correlation is not stored; sector/day and symbol cooldown provide cross-run controls. The small default list may produce fewer than five even in a strong market. NO TRADE is expected, sometimes for long periods.

## Reliability and state

GET requests use bounded retries, pacing, request timeouts, 429 Retry-After backoff, and a high-used-weight pause. Market-wide ticker/book snapshots are batched, candles cached per run and age dates cached across runs. Only closed, contiguous, recent, validated candles are accepted. Bans/restricted-region responses stop the scan without trying to bypass access restrictions. Unexpected service failures fail the run rather than masquerading as NO TRADE. Fresh quotes are checked again before delivery.

Local state is `state/state.json`; absent file starts clean and saves atomically. Malformed state fails closed. Do not run overlapping local processes or a local live sender alongside Actions. There is no distributed lock across independent clones.

Actions stores state on dedicated `signal-bot-state` branch using its automatic GitHub token and `contents: write`. No additional secret is needed. Concurrency serializes this workflow's runs. State is committed **before** each Telegram request. If persistence fails, no alert is sent. If Telegram times out or rejects delivery, the reservation still consumes cooldown/cap. This at-most-once preference may miss an alert but avoids blind duplicate POST retries. Delivery is not transactionally exactly-once. GitHub state has no Telegram secrets, but symbols and alert timestamps are public in a public repository.

See `STATE.md` for recovery. Do not delete the state branch routinely. Keep branch rules compatible with workflow writes. Manual `test-telegram` bypasses state and does not consume caps. Dry-run restores existing state but does not persist changes.

## GitHub schedule and costs

The included workflow runs at minutes 7 and 37 each hour, on the default branch, and supports manual `scan`, `dry-run` and `test-telegram` with optional symbol. GitHub cron is best-effort and can be delayed or dropped. Inactive public repositories may have schedules disabled; re-enable them in Actions if needed. Standard public-repository hosted runner usage is free under GitHub's current policy; private repositories have plan quotas. No paid service is required by this code; keep a public repo for the intended free hosted setup and check account usage settings. Pip packages are cached; state does not depend on cache/artifact retention. Repository policies can prevent writes and must be resolved before live operation.

## Purpose screening and limitations

`assets.yaml` is a provisional, editable conservative-purpose screen, **not Shariah certification or a fatwa**. Final religious judgment belongs to a qualified scholar, including token economics, staking, trading method and your circumstances. Project purpose does not certify every ecosystem activity or token. Every approved entry has a project source and a note; revisit these as projects evolve. Adding a ticker to this file alone is not a religious assessment.

No guaranteed profit, accuracy percentage, or promise of 1% within a few hours. Offline tests validate code behavior, not trading performance. Live exchange access, Telegram permissions, GitHub execution, slippage and strategy profitability require operational observation; no live delivery is claimed by this package.

## Sources checked for implementation

- [Binance public market-data-only API](https://github.com/binance/binance-spot-api-docs/blob/master/faqs/market_data_only.md)
- [Binance spot REST specification](https://github.com/binance/binance-spot-api-docs/blob/master/rest-api.md)
- [GitHub dependency caching](https://docs.github.com/en/actions/reference/workflows-and-actions/dependency-caching)
- [GitHub scheduled workflows](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)
- Project-purpose references are linked individually in `assets.yaml`; classification remains a conservative editorial judgment.

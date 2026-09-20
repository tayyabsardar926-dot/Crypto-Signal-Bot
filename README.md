# Free Multi-Strategy Crypto Spot Signal Bot

This project scans a conservative, purpose-reviewed list of Binance USDT **spot** markets and sends Telegram alerts only when a strong long setup passes all hard filters.

## What it checks

- Coin has at least **1.5 years of Binance trading history** (conservative age proxy).
- Asset is present in `assets.yaml` purpose-screened candidate list.
- 24h quote volume and bid/ask spread are acceptable.
- **1H and 4H must both be in strong uptrends.**
- Typical rolling 4-hour volatility must make a ~1% move plausible.
- The scanner chooses the best valid method among:
  - Breakout + volume
  - EMA20 pullback continuation
  - Momentum continuation
  - Support bounce/reversal
- BTC market regime must not be strongly bearish (configurable).
- Signal score must exceed the configured threshold.

Each alert includes **Current Price, Buy Zone, TP1, TP2, Invalidation/SL, expected percentages, trend scores, RSI, setup, liquidity, spread and BTC regime**.

## Important limitations

- This is a **signal scanner, not an auto-trader**. It never places exchange orders.
- A 1% gain in 3–4 hours is **not guaranteed**. The 4-hour range filter only rejects markets where that move is historically less plausible.
- `assets.yaml` is a **purpose-screening list, not a Shariah certification or fatwa**. General-purpose blockchains can host both permissible and impermissible applications. Review the list with a qualified scholar if you need a religious ruling.
- Coin age is checked via Binance's earliest available daily candle. This is conservative: an old project listed recently on Binance may be excluded.
- The internal smoothed Heikin-Ashi calculation is not claimed to match a proprietary TradingView indicator exactly.

## Free setup

### 1. Create a Telegram bot

1. Open Telegram and message `@BotFather`.
2. Send `/newbot` and follow the prompts.
3. Copy the bot token.
4. Open your new bot and send it any message, e.g. `hi`.
5. In a browser open:
   `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates`
6. In the JSON response find `chat.id`. That number is your `TELEGRAM_CHAT_ID`.

Never put the token directly in this repository.

### 2. Test locally (optional)

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt
python signal_bot.py
```

Without Telegram environment variables, qualifying signals are printed only.

### 3. Put it on GitHub

Create a repository and upload this project's files, including `.github/workflows/signal-bot.yml`.

In GitHub go to **Settings → Secrets and variables → Actions → New repository secret** and add:

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

Then open **Actions → Crypto Signal Bot → Run workflow** once to test.

The included workflow runs at minutes **07 and 37 of every hour** (about every 30 minutes). GitHub schedules are not real-time guarantees and can be delayed during load.

### 4. Adjust strictness

Edit `config.yaml`:

- Raise `minimum_signal_score` for fewer, stricter signals.
- Raise `min_quote_volume_usdt_24h` for more liquid markets.
- Keep `minimum_trend_score_1h` and `minimum_trend_score_4h` at 4 for strong-uptrend-only scanning.
- Raise `minimum_typical_4h_move_pct` if you only want more volatile assets.

## Files

- `signal_bot.py` — scanner and Telegram alert logic
- `config.yaml` — thresholds/risk filters
- `assets.yaml` — reviewed-purpose candidate whitelist
- `.github/workflows/signal-bot.yml` — free scheduled GitHub runner
- `tests/test_logic.py` — offline indicator/level sanity tests

## Security

This version uses only Binance public market data. It does **not** require your Binance/MEXC account password, API key, secret key, or withdrawal permission.

## GitHub Free note

For a **public repository**, standard GitHub-hosted Actions runners are free. For a **private repository**, GitHub Free currently includes a monthly Actions-minute allowance, so a very frequent schedule can eventually hit the free quota. If you want the simplest zero-cost long-term setup, use a public repository and keep all Telegram credentials only in GitHub Actions Secrets. Public scheduled workflows can be disabled after long repository inactivity, so check the Actions page if alerts unexpectedly stop.

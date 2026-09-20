# Setup — Roman Urdu

## Aap ko kya chahiye

- Free GitHub account
- Telegram account
- Koi paid VPS nahi
- Koi Binance API key nahi
- Koi OpenAI API key nahi

## Step 1 — Telegram bot banao

1. Telegram mein `@BotFather` open karo.
2. `/newbot` bhejo.
3. Bot ka naam aur username set karo.
4. BotFather jo **token** de usay private rakho.
5. Apne naye bot ko open karke `hi` bhejo.
6. Browser mein ye address kholo, apna token replace karke:
   `https://api.telegram.org/botYOUR_TOKEN/getUpdates`
7. Result mein `chat` ke andar `id` number milega. Ye aap ka `TELEGRAM_CHAT_ID` hai.

## Step 2 — GitHub repository

1. GitHub par free account/login karo.
2. New repository banao.
3. Is project ki sari files upload karo. `.github` folder bhi lazmi upload hona chahiye.
4. Zero-cost long-term usage ke liye public repository simplest hai. Telegram token code mein nazar nahi aayega kyun ke hum Secrets use karte hain.

## Step 3 — Telegram secrets GitHub mein lagao

Repository mein:

`Settings → Secrets and variables → Actions → New repository secret`

Do secrets banao:

- Name: `TELEGRAM_BOT_TOKEN` — Value: BotFather wala token
- Name: `TELEGRAM_CHAT_ID` — Value: aap ka chat id

Token mujhe chat mein send karna zaroori nahi.

## Step 4 — First test

GitHub repository mein:

`Actions → Crypto Signal Bot → Run workflow`

Run open karo. Agar scanner ko valid setup milta hai to Telegram alert aayega. Agar strong setup nahi milta to log mein `No qualifying signal this run.` aa sakta hai — ye error nahi hai.

## Signal format

Bot roughly ye message dega:

```text
🟢 SPOT BUY SETUP
Coin: XYZUSDT
Current: ...
Buy zone: ... – ...
TP1: ... (+1.xx%)
TP2: ... (+x.xx%)
Invalidation/SL: ... (-x.xx%)

Setup: Momentum / Breakout / Pullback / Support Bounce
Score: xx/100
1H RSI: ... | 4H RSI: ...
1H trend: x/5 | 4H trend: x/5
Typical 4H move potential: ...%
BTC regime: ...
```

## Important

Bot auto-buy/sell nahi karega. Sirf signal dega. 1% profit 3–4 hours mein guarantee nahi hota; bot sirf aise setups filter karta hai jahan historical short-term volatility aur trend us move ko comparatively plausible banate hain.

`assets.yaml` purpose-screening list hai, fatwa nahi. Kisi project ka use-case badal sakta hai, is liye list ko time to time review karna hoga.

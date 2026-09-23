# Ek dafa setup

Aap ka repo aur Telegram secrets pehle se hain. Unko delete ya rename nahi karna.

1. Pehle repo ka backup: Code > Download ZIP. Purane bot ka Actions workflow khol kar menu se Disable workflow karein, taa-ke do bots saath na chalein.
2. Is final ZIP ko extract karein. ZIP khud repo mein upload karna kaafi nahi. Andar `signal_bot.py`, `config.yaml`, `assets.yaml`, `requirements.txt`, `tests`, `scripts`, `.github` aur guides hain.
3. Repo ki default branch ke root mein ye files rakhein. `signal_bot.py` seedha root mein hona chahiye, extra folder ke andar nahi. Purane bot ki files replace karein; purane scheduled workflow ki .yml file delete karein. Apni unrelated files ya `.git` delete na karein.
4. Asaan reliable tareeqa GitHub Desktop hai: existing repo clone/open karein, extracted contents us local repo folder mein copy/replace karein. Hidden `.github` aur `.gitignore` bhi zaroor copy hon. Changes review karein, Commit to default branch, phir Push origin. Sirf browser se kar rahe hain to Add file > Upload files se contents upload karein; `.github/workflows/signal-bot.yml` na aaye to Add file > Create new file mein yahi poora path likh kar supplied file ka text paste karein. Purana workflow alag delete karke commit karein.
5. Settings > Secrets and variables > Actions mein EXACT names check karein: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`. Values dobara banane ki zaroorat nahi. Public files mein kabhi paste na karein.
6. Settings > Actions > General > Workflow permissions mein Read and write permissions allow karein agar repo policy zaroori kare. State branch par bot ko push ki ijazat honi chahiye. Koi naya token secret nahi chahiye.
7. Actions > Crypto Spot Signal Bot > Run workflow > mode `test-telegram` > Run workflow. Telegram par sirf connection-test message aayega. Bot ko pehle /start bheja hona chahiye; group use karte hain to bot member aur message permission wala ho.
8. Phir mode `dry-run` chalayein. Logs mein reject reasons, summary ya NO TRADE dekhein. Ye Telegram signal nahi bhejta. Single coin debug ke liye symbol `BTCUSDT` de sakte hain; filters bypass nahi hote.
9. Phir mode `scan` chalayein. Ab qualifying signal ho to Telegram par aayega. Scheduled scan har aadhe ghante ke qareeb khud chalega; exact timing guaranteed nahi. File default branch par honi chahiye.
10. Pehle scan ke baad `signal-bot-state` branch banegi. Isay delete/replace na karein. Purane bot ka doosra schedule band rehna chahiye.

Local Python 3.12 se bhi chala sakte hain:

```text
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
python signal_bot.py --dry-run
python signal_bot.py --dry-run --symbol BTCUSDT
python signal_bot.py --test-telegram
```

Local Telegram mode ke liye dono secrets apne system environment mein set karein; code mein nahi. GitHub secrets sirf Actions ko milte hain. Local live scanner aur Actions ko ek saath na chalayein.

NO TRADE error nahi: quality filters jaan-boojh kar strict hain. Default purpose list chhoti hai; uncertain coins khud-ba-khud reject hote hain. `assets.yaml` mein source/review ke baad hi coins approve karein. Ye halal certification nahi; final deeni faisla qualified scholar ka hai. Target 1% opportunity filter hai, profit ya time ki guarantee nahi.

Red run ho to failed step dekhein: dependency/network issue, state push permissions, Binance location restriction ya Telegram access ho sakta hai. Secrets log mein paste na karein. Public repo intended free setup hai; private repo Actions quota consume karta hai. Bot ko ChatGPT ya paid API ki zaroorat nahi.

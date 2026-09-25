import os, time, threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import Flask, jsonify, request, send_file
import requests

app = Flask(__name__)
BASES = ["https://data-api.binance.vision/api/v3", "https://api.binance.com/api/v3", "https://api-gcp.binance.com/api/v3"]
SESSION = requests.Session()
CACHE = {}
LOCK = threading.Lock()
STABLES = {"USDC","FDUSD","TUSD","USDP","DAI","BUSD","EUR","TRY","BRL","GBP","AUD","UAH","RUB","BIDR","IDRT","NGN","ZAR","PLN","RON","ARS","MXN","CZK","JPY"}
LEV_SUFFIX = ("UP","DOWN","BULL","BEAR")
DEFAULT_MIN_QV = 5_000_000.0
MAX_DEEP = 80


def bget(path, params=None, timeout=12):
    last = None
    for base in BASES:
        try:
            r = SESSION.get(base + path, params=params, timeout=timeout)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last = e
    raise RuntimeError(f"Binance data unavailable: {last}")


def cached(key, ttl, fn):
    now = time.time()
    with LOCK:
        v = CACHE.get(key)
        if v and now - v[0] < ttl:
            return v[1]
    data = fn()
    with LOCK:
        CACHE[key] = (now, data)
    return data


def exchange_info():
    return cached("exchange", 1800, lambda: bget("/exchangeInfo"))


def tickers():
    return cached("tickers", 20, lambda: bget("/ticker/24hr"))


def universe():
    info = exchange_info()
    tmap = {x["symbol"]: x for x in tickers() if isinstance(x, dict) and "symbol" in x}
    rows = []
    for s in info.get("symbols", []):
        if s.get("status") != "TRADING" or s.get("quoteAsset") != "USDT":
            continue
        if s.get("isSpotTradingAllowed") is False:
            continue
        base = s.get("baseAsset", "")
        if base in STABLES or base.endswith(LEV_SUFFIX):
            continue
        t = tmap.get(s["symbol"], {})
        try:
            qv = float(t.get("quoteVolume") or 0)
            last = float(t.get("lastPrice") or 0)
            chg = float(t.get("priceChangePercent") or 0)
        except Exception:
            qv, last, chg = 0, 0, 0
        rows.append({"symbol": s["symbol"], "base": base, "quoteVolume": qv, "price": last, "change24h": chg})
    rows.sort(key=lambda x: x["quoteVolume"], reverse=True)
    return rows


def klines(symbol, interval, limit=120):
    key = f"k:{symbol}:{interval}:{limit}"
    return cached(key, 45, lambda: bget("/klines", {"symbol": symbol, "interval": interval, "limit": limit}))


def ema(vals, n):
    if not vals: return []
    k = 2/(n+1)
    out=[]; p=vals[0]
    for i,v in enumerate(vals):
        p = v if i == 0 else v*k + p*(1-k)
        out.append(p)
    return out


def rsi(vals, n=14):
    if len(vals) < n+1: return None
    gains=[]; losses=[]
    for i in range(1, len(vals)):
        d=vals[i]-vals[i-1]
        gains.append(max(d,0)); losses.append(max(-d,0))
    ag=sum(gains[:n])/n; al=sum(losses[:n])/n
    for g,l in zip(gains[n:], losses[n:]):
        ag=(ag*(n-1)+g)/n; al=(al*(n-1)+l)/n
    return 100.0 if al == 0 else 100 - 100/(1+ag/al)


def atr(k, n=14):
    if len(k) < n+1: return None
    tr=[]
    for i in range(1,len(k)):
        h=float(k[i][2]); lo=float(k[i][3]); pc=float(k[i-1][4])
        tr.append(max(h-lo, abs(h-pc), abs(lo-pc)))
    return sum(tr[-n:])/n


def vr(k, n=20):
    if len(k) < n+1: return None
    vols=[float(x[5]) for x in k]
    avg=sum(vols[-n-1:-1])/n
    return vols[-1]/avg if avg else None


def tf(k):
    c=[float(x[4]) for x in k]
    e20=ema(c,20); e50=ema(c,50)
    a,b=e20[-1],e50[-1]
    trend="BULL" if a>b*1.001 else "BEAR" if a<b*0.999 else "FLAT"
    slope=(e20[-1]/e20[-4]-1)*100 if len(e20)>=4 and e20[-4] else 0
    return {"price":c[-1],"ema20":a,"ema50":b,"rsi":rsi(c),"vr":vr(k),"trend":trend,"slope":slope}


def analyze(row, detail=False):
    sym=row["symbol"]
    k1=klines(sym,"1h",140); k4=klines(sym,"4h",140)
    s1=tf(k1); s4=tf(k4)
    score=0; tags=[]
    if s4["trend"]=="BULL": score+=30; tags.append("4H uptrend")
    if s1["trend"]=="BULL": score+=25; tags.append("1H uptrend")
    if 50 <= (s1["rsi"] or 0) <= 68: score+=15; tags.append("RSI healthy")
    elif 45 <= (s1["rsi"] or 0) <= 72: score+=8
    if (s1["vr"] or 0) >= 1.0: score+=12; tags.append("volume >= avg")
    if s1["price"] > s1["ema20"]: score+=8
    if s1["slope"] > 0: score+=5
    if row.get("change24h",0) > 0: score+=5
    score=min(100, round(score))
    signal="WAIT"
    if s4["trend"]=="BULL" and s1["trend"]=="BULL" and score>=70 and (s1["rsi"] or 99)<=72:
        signal="BUY"
    elif s4["trend"]=="BEAR" or (s1["trend"]=="BEAR" and (s1["rsi"] or 50)<45):
        signal="EXIT"
    a1=atr(k1)
    plan=None
    if signal=="BUY" and a1:
        mid=s1["price"]; stop=mid-1.5*a1; R=mid-stop
        plan={"entryLow":mid-0.15*a1,"entryHigh":mid+0.15*a1,"stop":stop,"tp1":mid+R,"tp2":mid+1.5*R,"tp3":mid+2*R}
    out={**row,"signal":signal,"score":score,"tags":tags,"tf1h":s1,"tf4h":s4,"atr1h":a1,"plan":plan}
    if detail:
        k15=klines(sym,"15m",180); s15=tf(k15)
        out["tf15m"]=s15
        out["candles1h"]=[[int(x[0]),float(x[1]),float(x[2]),float(x[3]),float(x[4]),float(x[5])] for x in k1[-100:]]
    return out


@app.get("/")
def home():
    return send_file(os.path.join(os.path.dirname(__file__), "dashboard_v2.html"))

@app.get("/api/health")
def health():
    return jsonify({"ok":True,"time":int(time.time())})

@app.get("/api/universe")
def api_universe():
    rows=universe()
    return jsonify({"count":len(rows),"symbols":[r["symbol"] for r in rows]})

@app.get("/api/scan")
def api_scan():
    minq=float(request.args.get("minq", DEFAULT_MIN_QV))
    limit=min(int(request.args.get("limit", 60)), MAX_DEEP)
    rows=[r for r in universe() if r["quoteVolume"]>=minq][:limit]
    out=[]
    with ThreadPoolExecutor(max_workers=12) as ex:
        fut={ex.submit(analyze,r,False):r for r in rows}
        for f in as_completed(fut):
            try: out.append(f.result())
            except Exception: pass
    out.sort(key=lambda x:(x["signal"]=="BUY",x["score"],x["quoteVolume"]), reverse=True)
    buys=sum(1 for x in out if x["signal"]=="BUY")
    bulls=sum(1 for x in out if x["tf4h"]["trend"]=="BULL")
    return jsonify({"updated":int(time.time()),"universeCount":len(universe()),"scanned":len(out),"buys":buys,"breadthPct":round(100*bulls/len(out),1) if out else 0,"rows":out})

@app.get("/api/detail/<symbol>")
def api_detail(symbol):
    symbol=symbol.upper()
    match=next((r for r in universe() if r["symbol"]==symbol),None)
    if not match: return jsonify({"error":"Unknown active Binance Spot USDT pair"}),404
    try: return jsonify(analyze(match,True))
    except Exception as e: return jsonify({"error":str(e)}),502

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT","8080")))

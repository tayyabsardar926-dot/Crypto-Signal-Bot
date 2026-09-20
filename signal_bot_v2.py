"""Crypto spot signal scanner v2. Public Binance market data only; no order execution."""
import json, math, os, statistics, time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
import requests, yaml

def ema(v,n):
    if len(v)<n: raise ValueError("ema history")
    out=[sum(v[:n])/n]; k=2/(n+1)
    for x in v[n:]: out.append(out[-1]+k*(x-out[-1]))
    return out

def rsi(v,n=14):
    if len(v)<=n: raise ValueError("rsi history")
    d=[b-a for a,b in zip(v,v[1:])]
    g=sum(max(x,0) for x in d[:n])/n; l=sum(max(-x,0) for x in d[:n])/n
    for x in d[n:]:
        g=(g*(n-1)+max(x,0))/n; l=(l*(n-1)+max(-x,0))/n
    return 50 if g==l==0 else 100 if l==0 else 100-100/(1+g/l)

def atr(b,n=14):
    tr=[max(y["h"]-y["l"],abs(y["h"]-x["c"]),abs(y["l"]-x["c"])) for x,y in zip(b,b[1:])]
    if len(tr)<n: raise ValueError("atr history")
    a=sum(tr[:n])/n
    for x in tr[n:]: a=(a*(n-1)+x)/n
    return a

def pivots(b,key):
    f=max if key=="h" else min
    return [b[i][key] for i in range(2,len(b)-2) if b[i][key]==f(x[key] for x in b[i-2:i+3])]

def structure(b):
    h,l=pivots(b[-80:],"h"),pivots(b[-80:],"l")
    return len(h)>=2 and len(l)>=2 and h[-1]>h[-2] and l[-1]>l[-2]

def uptrend(b):
    c=[x["c"] for x in b]; e20,e50=ema(c,20),ema(c,50)
    return c[-1]>e20[-1]>e50[-1] and e20[-1]>e20[-4] and e50[-1]>=e50[-4]

def downtrend(b):
    c=[x["c"] for x in b]; e20,e50=ema(c,20),ema(c,50)
    return c[-1]<e20[-1]<e50[-1] and e20[-1]<e20[-4] and e50[-1]<=e50[-4]

def btc_regime(h4,h1):
    if uptrend(h4) and uptrend(h1): return "strong uptrend"
    if downtrend(h4) and downtrend(h1): return "strongly bearish"
    return "neutral/choppy"

def age_ok(first_ms,now,days): return 0<first_ms<=(now-days*86400)*1000

class Market:
    def __init__(self,cfg): self.cfg=cfg; self.s=requests.Session(); self.cache={}
    def get(self,ep,**params):
        key=(ep,tuple(sorted(params.items())))
        if key in self.cache:return self.cache[key]
        for i in range(4):
            try:
                time.sleep(self.cfg.get("request_interval",.15))
                r=self.s.get(self.cfg["base_url"]+ep,params=params,timeout=(8,22))
                if r.status_code in (418,451): raise RuntimeError("Binance unavailable in region")
                if r.status_code==429 or r.status_code>=500:
                    time.sleep(min(float(r.headers.get("Retry-After",2**(i+1))),60)); continue
                r.raise_for_status(); data=r.json(); self.cache[key]=data; return data
            except requests.RequestException: time.sleep(2**i)
        raise RuntimeError("market data unavailable")
    def bars(self,sym,tf,now):
        rows=self.get("/api/v3/klines",symbol=sym,interval=tf,limit=250)
        sec={"15m":900,"30m":1800,"1h":3600,"4h":14400}[tf]
        rows=[x for x in rows if int(x[6])<now*1000]
        if len(rows)<200: raise ValueError("candles:insufficient")
        if now*1000-int(rows[-1][6])>sec*1000+120000: raise ValueError("candles:stale")
        return [dict(o=float(x[1]),h=float(x[2]),l=float(x[3]),c=float(x[4]),v=float(x[5])) for x in rows]

class State:
    def __init__(self,path):
        self.p=Path(path); self.d=json.loads(self.p.read_text()) if self.p.exists() else {"version":2,"alerts":[],"ages":{}}
        if not isinstance(self.d.get("alerts"),list) or not isinstance(self.d.get("ages"),dict): self.d={"version":2,"alerts":[],"ages":{}}
    def save(self): self.p.parent.mkdir(parents=True,exist_ok=True); self.p.write_text(json.dumps(self.d,indent=2))
    def blocked(self,sym,sector,now,cfg,cap):
        today=datetime.fromtimestamp(now,timezone.utc).date(); daily=[x for x in self.d["alerts"] if datetime.fromtimestamp(x["time"],timezone.utc).date()==today]
        if len(daily)>=cap:return "daily cap"
        if any(x["symbol"]==sym and now-x["time"]<cfg["cooldown_hours"]*3600 for x in self.d["alerts"]):return "cooldown"
        if sum(x["sector"]==sector for x in daily)>=cfg["max_sector_per_day"]:return "sector cap"
    def add(self,sig,now): self.d["alerts"]=[x for x in self.d["alerts"] if now-x["time"]<30*86400]; self.d["alerts"].append({"symbol":sig["symbol"],"sector":sig["sector"],"time":now}); self.save()

def setup_names(b):
    c=[x["c"] for x in b]; last,prev=b[-1],b[-2]; a=atr(b); e20=ema(c,20)[-1]; res=max(x["h"] for x in b[-22:-2]); vol=statistics.mean(x["v"] for x in b[-21:-1]); sup=pivots(b[:-1],"l"); f=[]
    if (last["c"]>res and last["v"]>=vol*1.10) or (prev["c"]>res and last["l"]<=res+a*.45 and last["c"]>res):f.append("breakout/retest")
    if last["l"]<=e20+a*.5 and last["c"]>=e20*.995 and last["c"]>last["o"]:f.append("pullback continuation")
    if last["c"]>max(x["h"] for x in b[-6:-1]) and last["v"]>=vol*1.10:f.append("momentum continuation")
    if sup and abs(last["l"]-sup[-1])<=a*.55 and last["c"]>last["o"]:f.append("support bounce")
    if c[-1]>e20 and (c[-1]>c[-2]>c[-3] or structure(b)):f.append("trend continuation")
    return f

def levels(b,higher,price,cfg):
    a=atr(b); c=[x["c"] for x in b]; supports=[x for x in pivots(b[-80:],"l") if x<price]
    if not supports:
        low=min(x["l"] for x in b[-20:-1]); supports=[low] if low<price else []
    if not supports:raise ValueError("levels:no support")
    sup=max(supports); anchor=max(sup,min(ema(c,20)[-1],price)); low=anchor-a*.12; entry=min(price,anchor+a*.30); sl=sup-a*.30; risk=entry-sl; rp=risk/entry*100
    if risk<=0 or not cfg["min_stop_pct"]<=rp<=cfg["max_stop_pct"]:raise ValueError("levels:stop")
    resist=sorted(set(x for s in (b,*higher) for x in pivots(s,"h") if x>entry)); min_tp=entry*(1+cfg["min_target_pct"]/100); valid=[x-a*.08 for x in resist if x-a*.08>=min_tp]
    if not valid:raise ValueError("levels:target room")
    tp1=valid[0]; rr=(tp1-entry)/risk
    if rr<cfg["min_rr"]:raise ValueError("levels:R:R")
    if tp1-entry>atr(higher[0])*5:raise ValueError("levels:too far")
    tp2=valid[1] if len(valid)>1 else max(tp1+a*1.2,entry+risk*2.0)
    return dict(low=low,entry=entry,sl=sl,tp1=tp1,tp2=tp2,rr=rr)

def evaluate(m,sym,cfg,btc,now):
    h4,h1=m.bars(sym,"4h",now),m.bars(sym,"1h",now)
    if not uptrend(h4) or not uptrend(h1):raise ValueError("trend:4H+1H")
    if h1[-1]["c"]/h1[-4]["c"]-1>cfg["max_3h_pump_pct"]/100:raise ValueError("overextension:pump")
    m30,m15=m.bars(sym,"30m",now),m.bars(sym,"15m",now); c30=[x["c"] for x in m30]; e20,e50=ema(c30,20)[-1],ema(c30,50)[-1]
    if c30[-1]<e50 or (c30[-1]<e20*.997 and m30[-1]["c"]<=m30[-1]["o"]):raise ValueError("timing:30m")
    book=m.get("/api/v3/ticker/bookTicker",symbol=sym); bid,ask=float(book["bidPrice"]),float(book["askPrice"]); price=(bid+ask)/2; opts=[]
    for tf,b in (("15m",m15),("30m",m30)):
        names=setup_names(b)
        if not names:continue
        c=[x["c"] for x in b]; a=atr(b); mom=rsi(c); vol=b[-1]["v"]/max(statistics.mean(x["v"] for x in b[-21:-1]),1e-12)
        if not cfg["min_atr_pct"]<=a/price*100<=cfg["max_atr_pct"] or not 48<=mom<=76 or vol<cfg["min_volume_ratio"]:continue
        try: lv=levels(b,[h1,h4],price,cfg)
        except ValueError: continue
        struct=1.0 if structure(b) else .55; btcw={"strong uptrend":1.0,"neutral/choppy":.65,"strongly bearish":.2}[btc]
        score=round(20+10*struct+10*min(vol/1.35,1)+10*max(0,1-abs(mom-60)/24)+10+10+10*btcw+5+5+10*min(lv["rr"]/2,1)); need=cfg["min_score"]+(cfg["bearish_btc_extra_score"] if btc=="strongly bearish" and sym!="BTCUSDT" else 0)
        if score<need or (btc=="strongly bearish" and sym!="BTCUSDT" and vol<1.0):continue
        opts.append(dict(**lv,price=price,setup=names[0],timing=tf,score=score,rsi=mom,volume=vol,btc=btc))
    if not opts:raise ValueError("setup:no qualified timing")
    return max(opts,key=lambda x:x["score"])

def telegram(msg):
    token,chat=os.getenv("TELEGRAM_BOT_TOKEN"),os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat:raise RuntimeError("telegram secrets missing")
    r=requests.post(f"https://api.telegram.org/bot{token}/sendMessage",json={"chat_id":chat,"text":msg},timeout=(8,22))
    if r.status_code!=200 or not r.json().get("ok"):raise RuntimeError("telegram rejected")

def fmt(s):
    e=s["entry"]; p=lambda x:f"{x:.8g}"
    return (f"🟢 SPOT SIGNAL — {s['symbol']}\nCurrent: {p(s['price'])}\nBuy Zone: {p(s['low'])} – {p(e)} USDT\nTP1: {p(s['tp1'])} (+{(s['tp1']/e-1)*100:.2f}%)\nTP2: {p(s['tp2'])} (+{(s['tp2']/e-1)*100:.2f}%)\nSL/Invalidation: {p(s['sl'])} ({(1-s['sl']/e)*100:.2f}% risk)\nSetup: {s['setup']} | Score: {s['score']}/100\n4H+1H: Uptrend | Timing: {s['timing']} | RSI: {s['rsi']:.1f} | Volume: {s['volume']:.2f}x\nBTC regime: {s['btc']} (score modifier, not a hard gate)\nR:R to TP1: {s['rr']:.2f}\nUTC: {s['timestamp']}\nNo guaranteed outcome; fees/slippage excluded.")

def main():
    cfg=yaml.safe_load(Path("config.yaml").read_text()); assets=yaml.safe_load(Path("assets.yaml").read_text()); approved={k:v for k,v in assets.items() if isinstance(v,dict) and v.get("status")=="approved" and v.get("source")}; st=State(cfg["state_path"]); m=Market(cfg); now=m.get("/api/v3/time")["serverTime"]/1000
    infos=m.get("/api/v3/exchangeInfo")["symbols"]; tick={x["symbol"]:x for x in m.get("/api/v3/ticker/24hr")}; books={x["symbol"]:x for x in m.get("/api/v3/ticker/bookTicker")}; btc=btc_regime(m.bars("BTCUSDT","4h",now),m.bars("BTCUSDT","1h",now)); counts=Counter(); candidates=[]; eligible=[x for x in infos if x.get("quoteAsset")=="USDT" and x.get("baseAsset") in approved]
    for info in eligible:
        sym,base=info["symbol"],info["baseAsset"]
        try:
            t=tick.get(sym,{}); b=books.get(sym,{}); bid,ask=float(b.get("bidPrice",0)),float(b.get("askPrice",0))
            if info.get("status")!="TRADING" or not info.get("isSpotTradingAllowed",False):raise ValueError("liquidity:status")
            if float(t.get("quoteVolume",0))<cfg["min_quote_volume"]:raise ValueError("liquidity:volume")
            if not 0<bid<=ask or (ask-bid)/((ask+bid)/2)*100>cfg["max_spread_pct"]:raise ValueError("liquidity:spread")
            if sym not in st.d["ages"]:
                first=m.get("/api/v3/klines",symbol=sym,interval="1d",startTime=0,limit=1)
                if not first:raise ValueError("age:missing")
                st.d["ages"][sym]=int(first[0][0])
            if not age_ok(st.d["ages"][sym],now,cfg["min_age_days"]):raise ValueError("age:young")
            s=evaluate(m,sym,cfg,btc,now); s.update(symbol=sym,sector=approved[base]["sector"],timestamp=datetime.fromtimestamp(now,timezone.utc).isoformat()); candidates.append(s)
        except ValueError as e: counts[str(e).split(":")[0]]+=1
    cap=cfg["strong_market_cap"] if btc=="strong uptrend" else cfg["daily_cap"]; sent=0
    for s in sorted(candidates,key=lambda x:x["score"],reverse=True):
        if sent>=cap:break
        why=st.blocked(s["symbol"],s["sector"],now,cfg,cap)
        if why: counts[why]+=1; continue
        telegram(fmt(s)); st.add(s,now); sent+=1
    st.save(); print("SUMMARY",json.dumps({"btc":btc,"approved_assets":len(approved),"eligible_pairs":len(eligible),"qualified":len(candidates),"alerts":sent,"rejections":dict(counts)},sort_keys=True))

if __name__=="__main__":
    try: main()
    except Exception as e:
        print("SCAN FAILED",type(e).__name__); raise SystemExit(1)

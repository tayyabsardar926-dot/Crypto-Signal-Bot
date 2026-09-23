import json, math, os, time, urllib.parse, urllib.request, threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get('PORT', '8080'))
CACHE_SECONDS = 180
TOP_PER_EXCHANGE = 22
DEEP_VALIDATE_TOP = 3
MIN_EVENTS = 24
BASES = {
    'binance': ['https://data-api.binance.vision','https://api.binance.com','https://api1.binance.com'],
    'mexc': ['https://api.mexc.com']
}
STABLE = {'USDT','USDC','FDUSD','TUSD','USDP','DAI','BUSD','EUR','EURC','USD1','RLUSD','PYUSD'}
_cache = {'at':0,'data':None}
_lock = threading.Lock()

def get_json(url, timeout=12):
    req = urllib.request.Request(url, headers={'User-Agent':'SpotRadarPro/1.0'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode('utf-8'))

def market_get(ex, path):
    last = None
    for base in BASES[ex]:
        try: return get_json(base + path)
        except Exception as e: last = e
    raise last or RuntimeError('No endpoint')

def klines(ex, symbol, interval='1h', limit=120, end_time=None):
    if ex == 'mexc' and interval == '1h': interval = '60m'
    q = {'symbol':symbol,'interval':interval,'limit':str(limit)}
    if end_time: q['endTime'] = str(end_time)
    rows = market_get(ex, '/api/v3/klines?' + urllib.parse.urlencode(q))
    out=[]
    for r in rows:
        try: out.append({'t':int(r[0]),'o':float(r[1]),'h':float(r[2]),'l':float(r[3]),'c':float(r[4]),'v':float(r[5]),'ct':int(r[6])})
        except Exception: pass
    return out

def ema(vals,p):
    if not vals:return []
    k=2/(p+1); out=[vals[0]]; prev=vals[0]
    for x in vals[1:]: prev=x*k+prev*(1-k); out.append(prev)
    return out

def rsi(vals,p=14):
    out=[None]*len(vals)
    if len(vals)<=p:return out
    g=l=0
    for i in range(1,p+1):
        d=vals[i]-vals[i-1]; g+=max(d,0); l+=max(-d,0)
    ag,al=g/p,l/p; out[p]=100 if al==0 else 100-100/(1+ag/al)
    for i in range(p+1,len(vals)):
        d=vals[i]-vals[i-1]; ag=(ag*(p-1)+max(d,0))/p; al=(al*(p-1)+max(-d,0))/p
        out[i]=100 if al==0 else 100-100/(1+ag/al)
    return out

def atr(bars,p=14):
    tr=[]
    for i,b in enumerate(bars):
        if i==0: tr.append(b['h']-b['l'])
        else:
            pc=bars[i-1]['c']; tr.append(max(b['h']-b['l'],abs(b['h']-pc),abs(b['l']-pc)))
    out=[None]*len(bars)
    if len(bars)<p:return out
    a=sum(tr[:p])/p; out[p-1]=a
    for i in range(p,len(tr)): a=(a*(p-1)+tr[i])/p; out[i]=a
    return out

def pct(a,b): return ((b/a)-1)*100 if a else 0
def clamp(x,a,b): return max(a,min(b,x))
def wilson(w,n,z=1.96):
    if not n:return 0
    ph=w/n; z2=z*z
    return (ph+z2/(2*n)-z*math.sqrt((ph*(1-ph)+z2/(4*n))/n))/(1+z2/n)

def regime(bars):
    bars=[b for b in bars if b['ct']<int(time.time()*1000)]
    if len(bars)<60:return {'state':'UNKNOWN','momentum6h':None}
    c=[b['c'] for b in bars]; e20=ema(c,20); e50=ema(c,50); i=len(c)-1
    up=c[i]>e20[i]>e50[i] and e20[i]>e20[i-5]
    dn=c[i]<e20[i]<e50[i] and e20[i]<e20[i-5]
    return {'state':'BULLISH' if up else 'BEARISH' if dn else 'NEUTRAL','momentum6h':round(pct(c[i-6],c[i]),2)}

def features(hourly, fifteen, market, spread, qvol):
    hourly=[b for b in hourly if b['ct']<int(time.time()*1000)]; fifteen=[b for b in fifteen if b['ct']<int(time.time()*1000)]
    if len(hourly)<70 or len(fifteen)<30: raise ValueError('not enough candles')
    c=[b['c'] for b in hourly]; v=[b['v'] for b in hourly]; fc=[b['c'] for b in fifteen]
    e20,e50,rs,at=ema(c,20),ema(c,50),rsi(c),atr(hourly); i=len(c)-1; close=c[i]
    ap=(at[i]/close*100) if at[i] else 2
    vb=sum(v[i-20:i])/20; vr=v[i]/vb if vb>0 else 1
    h20=max(b['h'] for b in hourly[i-20:i]); br=close/h20
    m1=pct(c[i-1],close); m3=pct(c[i-3],close); m15=pct(fc[-5],fc[-1])
    rv=rs[i] or 50; trend=close>e20[i]>e50[i] and e20[i]>e20[i-4]; dist=(close/e20[i]-1)*100
    score=0
    score += 25 if trend else 16 if close>e20[i] and e20[i]>=e50[i]*.995 else 8 if close>e20[i] else 0
    score += 12 if 54<=rv<=68 else 8 if 50<=rv<54 else 6 if 68<rv<=74 else 0
    score += 5 if m15>.25 and m15<max(4,ap*1.8) else 0; score += 3 if m1>0 else 0
    score += 15 if vr>=1.8 else 12 if vr>=1.35 else 8 if vr>=1.10 else 4 if vr>=.9 else 0
    score += 15 if br>=1.002 else 12 if br>=.995 else 7 if br>=.985 else 0
    score += 10 if market['state']=='BULLISH' else 6 if market['state']=='NEUTRAL' else 1
    score += 10 if spread<=.08 else 8 if spread<=.18 else 5 if spread<=.35 else 2 if spread<=.60 else 0
    score=int(clamp(score,0,100))
    over=rv>76 or m3>max(7,ap*3.1) or dist>max(7,ap*2.4)
    stage='OVEREXTENDED' if over else 'EARLY' if trend and br>=.995 and vr>=1.10 and m1>0 else 'DEVELOPING' if trend and m1>0 else 'TRENDING' if trend else 'WAIT'
    return {'price':close,'rsi':round(rv,1),'atrPct':round(ap,2),'volumeRatio':round(vr,2),'breakoutRatio':round(br,4),'mom1h':round(m1,2),'mom3h':round(m3,2),'trendUp':trend,'score':score,'stage':stage,'spreadPct':round(spread,3),'quoteVolume24h':round(qvol)}

def plan(price,f):
    ap=clamp(f['atrPct'] or 2,.5,12); stop=clamp(ap*.9,1,3.2); t1=clamp(ap*1.1,1.8,4); t2=clamp(ap*1.8,3,8); t3=clamp(ap*2.7,5,15)
    if t2<=t1:t2=t1+1
    if t3<=t2:t3=t2+1.5
    low=price*(1-clamp(ap*.15,.08,.45)/100); high=price*(1+clamp(ap*.08,.05,.25)/100); mid=(low+high)/2
    return {'entryLow':low,'entryHigh':high,'stop':mid*(1-stop/100),'stopPct':round(stop,2),'tp1':mid*(1+t1/100),'tp1Pct':round(t1,2),'tp2':mid*(1+t2/100),'tp2Pct':round(t2,2),'tp3':mid*(1+t3/100),'tp3Pct':round(t3,2),'expectedLowPct':round(t1,2),'expectedHighPct':round(t2,2),'expiryMinutes':60 if f['stage']=='EARLY' else 120}

def history(ex,symbol,days=70):
    need=days*24+80; allb=[]; end=None
    for _ in range(3):
        batch=klines(ex,symbol,'1h',min(1000,need-len(allb)),end)
        if not batch: break
        allb=batch+allb; end=batch[0]['t']-1
        if len(allb)>=need:break
        time.sleep(.08)
    d={b['t']:b for b in allb}
    return sorted(d.values(),key=lambda x:x['t'])[-need:]

def hist_feature(bars,i):
    if i<70:return None
    s=bars[i-70:i+1]; c=[b['c'] for b in s]; v=[b['v'] for b in s]; e20,e50,rs,at=ema(c,20),ema(c,50),rsi(c),atr(s); k=len(c)-1
    ap=(at[k]/c[k]*100) if at[k] else 2; vb=sum(v[k-20:k])/20; vr=v[k]/vb if vb else 1; br=c[k]/max(b['h'] for b in s[k-20:k]); rv=rs[k] or 50; m1=pct(c[k-1],c[k]); m3=pct(c[k-3],c[k]); trend=c[k]>e20[k]>e50[k] and e20[k]>e20[k-4]; dist=(c[k]/e20[k]-1)*100
    sc=(30 if trend else 12 if c[k]>e20[k] else 0)+(18 if 54<=rv<=68 else 10 if 50<=rv<54 else 7 if 68<rv<=74 else 0)+(7 if m1>0 else 0)+(20 if vr>=1.8 else 15 if vr>=1.35 else 10 if vr>=1.1 else 4 if vr>=.9 else 0)+(20 if br>=1.002 else 15 if br>=.995 else 8 if br>=.985 else 0)
    return {'score':min(100,sc),'rsi':rv,'atrPct':ap,'over':rv>76 or m3>max(7,ap*3.1) or dist>max(7,ap*2.4)}

def validate(bars,cur,p):
    out=[]
    for tp in [p['tp1Pct'],p['tp2Pct'],p['tp3Pct']]:
        n=w=0; floor=max(62,cur['score']-10)
        for i in range(75,len(bars)-7):
            f=hist_feature(bars,i)
            if not f or f['over'] or f['score']<floor or abs(f['rsi']-cur['rsi'])>10:continue
            if cur['atrPct'] and (f['atrPct']<cur['atrPct']*.45 or f['atrPct']>cur['atrPct']*1.8):continue
            n+=1; entry=bars[i]['c']; target=entry*(1+tp/100); stop=entry*(1-p['stopPct']/100); result=False
            for j in range(i+1,min(i+7,len(bars))):
                b=bars[j]; ht=b['h']>=target; hs=b['l']<=stop
                if ht and hs: result=False; break
                if hs: result=False; break
                if ht: result=True; break
            if result:w+=1
        out.append({'targetPct':tp,'n':n,'wins':w,'frequency':round(w/n,4) if n else None,'wilsonLower':round(wilson(w,n),4) if n else None,'validated':n>=MIN_EVENTS})
    return out

def explain(f,market):
    r=[]
    if f['trendUp']:r.append('1H trend up')
    if f['volumeRatio']>=1.35:r.append(f"volume {f['volumeRatio']:.1f}× average")
    elif f['volumeRatio']>=1.1:r.append('volume improving')
    if f['breakoutRatio']>=.995:r.append('near/fresh breakout')
    if 54<=f['rsi']<=68:r.append('momentum healthy')
    if market['state']=='BULLISH':r.append('BTC regime supportive')
    if f['spreadPct']<=.18:r.append('tight spread')
    return ' · '.join(r[:5]) or 'setup not confirmed'

def exchange_candidates(ex,market):
    info,t24,books=market_get(ex,'/api/v3/exchangeInfo'),market_get(ex,'/api/v3/ticker/24hr'),market_get(ex,'/api/v3/ticker/bookTicker')
    tm={x.get('symbol'):x for x in (t24 if isinstance(t24,list) else [t24])}; bm={x.get('symbol'):x for x in (books if isinstance(books,list) else [books])}
    uni=[]
    for s in info.get('symbols',[]):
        status=str(s.get('status','')).upper(); online=(status=='TRADING') if ex=='binance' else status in ('ENABLED','1','TRADING')
        base=s.get('baseAsset',''); quote=s.get('quoteAsset','')
        if not online or quote!='USDT' or base in STABLE or base.endswith(('UP','DOWN','BULL','BEAR','3L','3S','5L','5S')):continue
        t=tm.get(s.get('symbol'),{}); b=bm.get(s.get('symbol'),{}); price=float(t.get('lastPrice') or t.get('price') or 0); qv=float(t.get('quoteVolume') or 0); bid=float(b.get('bidPrice') or 0); ask=float(b.get('askPrice') or 0); sp=((ask-bid)/((ask+bid)/2)*100) if bid>0 and ask>0 else 99
        minv=2_000_000 if ex=='binance' else 1_000_000
        if price>0 and qv>=minv and sp<=.60:uni.append({'symbol':s['symbol'],'exchange':ex,'price':price,'quoteVolume24h':qv,'spreadPct':sp})
    uni=sorted(uni,key=lambda x:x['quoteVolume24h'],reverse=True)[:TOP_PER_EXCHANGE]
    out=[]
    def one(x):
        h,f=klines(ex,x['symbol'],'1h',120),klines(ex,x['symbol'],'15m',120); ft=features(h,f,market,x['spreadPct'],x['quoteVolume24h']); p=plan(x['price'],ft)
        return {**x,'features':ft,'plan':p,'score':ft['score'],'stage':ft['stage'],'reason':explain(ft,market)}
    with ThreadPoolExecutor(max_workers=6) as pool:
        futs=[pool.submit(one,x) for x in uni]
        for fu in as_completed(futs):
            try: out.append(fu.result())
            except Exception: pass
    return sorted(out,key=lambda x:x['score'],reverse=True)

def scan_all():
    try: btc=klines('binance','BTCUSDT','1h',120)
    except Exception: btc=klines('mexc','BTCUSDT','1h',120)
    market=regime(btc)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futs={pool.submit(exchange_candidates,ex,market):ex for ex in ('binance','mexc')}; candidates=[]; errors=[]
        for fu,ex in [(f,e) for f,e in futs.items()]:
            try:candidates.extend(fu.result())
            except Exception as er:errors.append(f'{ex}: {er}')
    candidates.sort(key=lambda x:x['score'],reverse=True)
    for c in [x for x in candidates if x['stage']!='OVEREXTENDED' and x['score']>=65][:DEEP_VALIDATE_TOP]:
        try:c['validation']=validate(history(c['exchange'],c['symbol']),c['features'],c['plan'])
        except Exception as e:c['validationError']=str(e)
    for c in candidates:
        v=(c.get('validation') or [{}])[0]; buy=c['score']>=80 and c['stage'] in ('EARLY','DEVELOPING') and market['state']!='BEARISH' and c['spreadPct']<=.35 and v.get('validated') and (v.get('frequency') or 0)>=.60 and (v.get('wilsonLower') or 0)>=.50
        c['status']='BUY NOW' if buy else 'AVOID' if c['stage']=='OVEREXTENDED' else 'WATCH' if c['score']>=65 else 'AVOID'
    buys=[c for c in candidates if c['status']=='BUY NOW']; watches=[c for c in candidates if c['status']=='WATCH']
    return {'generatedAt':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),'market':market,'errors':errors,'summary':{'scanned':len(candidates),'buyNow':len(buys),'watch':len(watches)},'bestTrade':buys[0] if buys else None,'top3':buys[:3],'watch':watches[:12],'candidates':candidates[:50]}

HTML='''<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Spot Radar Pro</title><style>body{margin:0;background:#071019;color:#eef6ff;font:14px Arial,sans-serif}header{display:flex;justify-content:space-between;align-items:center;padding:18px 28px;background:#0b1722;position:sticky;top:0}.brand{font-size:22px;font-weight:800}.brand b{color:#24d18f}.sub{color:#8fa4b8;font-size:12px;margin-top:4px}button{background:#17314a;color:white;border:1px solid #2d5273;border-radius:9px;padding:10px 14px}main{max-width:1300px;margin:auto;padding:24px}.bar{display:flex;gap:10px;flex-wrap:wrap}.pill,.card,.best{background:#0d1824;border:1px solid #1e3244;border-radius:14px;padding:14px}.best{margin-top:12px;border-color:#285d4d}.grid{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}.levels{display:grid;grid-template-columns:repeat(5,1fr);gap:8px;margin-top:12px}.m{background:#09131d;border-radius:9px;padding:10px}.m span{font-size:10px;color:#8fa4b8}.status{padding:5px 9px;border-radius:999px;font-weight:800}.buy{background:#24d18f;color:#07130f}.watch{background:#f6b94a;color:#1a1305}.avoid{background:#6b3636}.bubbles{display:flex;gap:10px;flex-wrap:wrap;justify-content:center;background:#08131e;border:1px solid #1e3244;border-radius:16px;padding:16px}.bubble{border-radius:50%;display:flex;flex-direction:column;justify-content:center;align-items:center;background:#17344a;border:1px solid #2e5875}.bubble.watch{background:#5a4417}.bubble.buy{background:#174d39}.small{font-size:11px;color:#8fa4b8}.reason{color:#bfd0df;margin-top:9px}@media(max-width:850px){.grid{grid-template-columns:1fr}.levels{grid-template-columns:repeat(2,1fr)}}</style></head><body><header><div><div class="brand">SPOT RADAR <b>PRO</b></div><div class="sub">Binance + MEXC Spot · USDT · 1–6h scanner</div></div><button onclick="scan(true)">↻ Scan now</button></header><main><div id="market" class="bar"></div><h2>Best trade now</h2><div id="best" class="best">Scanning live markets…</div><h2>Top trade-ready setups</h2><div id="top" class="grid"></div><h2>Watchlist</h2><div id="watch" class="grid"></div><h2>Opportunity bubbles</h2><div id="bubbles" class="bubbles"></div><p class="small">Score is not profit probability. Historical target-hit frequency is evidence, not a guarantee. No automatic order placement.</p></main><script>
const f=x=>{if(x==null)return'—';if(x>=1)return'$'+Number(x).toFixed(4);return'$'+Number(x).toPrecision(5)};const v=(c,i=0)=>{let x=c.validation&&c.validation[i];return x&&x.validated?Math.round(x.frequency*100)+'% / '+x.n+' cases':'Not validated'};const cls=s=>s==='BUY NOW'?'buy':s==='WATCH'?'watch':'avoid';
function card(c){let p=c.plan;return `<div class="card"><b>${c.symbol.replace('USDT','')}/USDT</b> <span class="status ${cls(c.status)}">${c.status}</span><div class="small">${c.exchange.toUpperCase()} · ${c.stage} · Score ${c.score}/100</div><div class="levels"><div class="m"><span>ENTRY</span><br><b>${f(p.entryLow)}–${f(p.entryHigh)}</b></div><div class="m"><span>STOP</span><br><b>-${p.stopPct}%</b></div><div class="m"><span>TP1</span><br><b>+${p.tp1Pct}%</b></div><div class="m"><span>TP2</span><br><b>+${p.tp2Pct}%</b></div><div class="m"><span>TP1 EVIDENCE</span><br><b>${v(c)}</b></div></div><div class="reason">${c.reason}</div></div>`}
function render(d){document.querySelector('#market').innerHTML=`<div class="pill">BTC <b>${d.market.state}</b></div><div class="pill">BTC 6h <b>${d.market.momentum6h??'—'}%</b></div><div class="pill">Scanned <b>${d.summary.scanned}</b></div><div class="pill">BUY <b>${d.summary.buyNow}</b></div><div class="pill">WATCH <b>${d.summary.watch}</b></div>`;document.querySelector('#best').innerHTML=d.bestTrade?card(d.bestTrade):'<b>NO HIGH-QUALITY TRADE RIGHT NOW</b><div class="reason">No setup passed strict BUY rules. WATCH does not mean automatic entry.</div>';document.querySelector('#top').innerHTML=d.top3.length?d.top3.map(card).join(''):'<div class="card">No validated BUY setup right now.</div>';document.querySelector('#watch').innerHTML=d.watch.map(card).join('')||'<div class="card">No watch candidate.</div>';document.querySelector('#bubbles').innerHTML=d.candidates.map(c=>{let s=55+Math.max(0,c.score-40)*1.3;return `<div class="bubble ${cls(c.status)}" style="width:${s}px;height:${s}px"><b>${c.symbol.replace('USDT','')}</b><span>${c.score}</span></div>`}).join('')}
async function scan(force=false){document.querySelector('#best').innerHTML='Scanning live markets…';try{let r=await fetch('/api/scan'+(force?'?force=1':''));let d=await r.json();if(!r.ok)throw Error(d.error||'scan failed');render(d)}catch(e){document.querySelector('#best').innerHTML='<b>Scan error</b><div class="reason">'+e.message+'</div>'}}scan();setInterval(()=>scan(false),300000);
</script></body></html>'''

class Handler(BaseHTTPRequestHandler):
    def send_bytes(self,code,data,ctype='application/json; charset=utf-8'):
        self.send_response(code); self.send_header('Content-Type',ctype); self.send_header('Cache-Control','no-store'); self.end_headers(); self.wfile.write(data)
    def do_GET(self):
        u=urllib.parse.urlparse(self.path)
        if u.path=='/api/health': return self.send_bytes(200,json.dumps({'ok':True}).encode())
        if u.path=='/api/scan':
            try:
                force=urllib.parse.parse_qs(u.query).get('force',['0'])[0]=='1'
                with _lock: fresh=_cache['data'] is not None and time.time()-_cache['at']<CACHE_SECONDS
                if fresh and not force: data=_cache['data']
                else:
                    data=scan_all()
                    with _lock: _cache['data']=data; _cache['at']=time.time()
                return self.send_bytes(200,json.dumps(data,separators=(',',':')).encode())
            except Exception as e: return self.send_bytes(500,json.dumps({'error':str(e)}).encode())
        if u.path in ('/','/index.html'): return self.send_bytes(200,HTML.encode(),'text/html; charset=utf-8')
        return self.send_bytes(404,b'Not found','text/plain')
    def log_message(self,fmt,*args): print(fmt%args)

if __name__=='__main__':
    print('Spot Radar Pro listening on',PORT)
    ThreadingHTTPServer(('0.0.0.0',PORT),Handler).serve_forever()

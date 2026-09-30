import os,time,threading,json
from concurrent.futures import ThreadPoolExecutor,as_completed
from flask import Flask,jsonify,send_file
import requests

app=Flask(__name__)
S=requests.Session()
B=['https://data-api.binance.vision/api/v3','https://api.binance.com/api/v3','https://api-gcp.binance.com/api/v3']
M='https://api.mexc.com/api/v3'
STABLE={'USDC','FDUSD','TUSD','USDP','DAI','BUSD','USD1','USDE','BFUSD','EUR','TRY','BRL','GBP','AUD','UAH','RUB','BIDR','IDRT','NGN','ZAR','PLN','RON','ARS','MXN','CZK','JPY','AEUR','EURI'}
LOCK=threading.Lock(); STATE={'rows':[],'updated':0,'errors':[],'analytics_done':0,'analytics_total':0,'binance_count':0,'mexc_count':0,'unique_count':0,'analytics_running':False,'criteria_matches':[],'criteria_updated':0,'criteria_running':False,'criteria_checked':0}
HIST={}; HIST_T={}; STARTED=False

def getj(url,timeout=12):
 r=S.get(url,timeout=timeout,headers={'User-Agent':'Mozilla/5.0'});r.raise_for_status();return r.json()

def bget(path):
 last=None
 for x in B:
  try:return getj(x+path)
  except Exception as e:last=e
 raise last

def mget(path): return getj(M+path)

def tickrow(t):
 def f(x):
  try:return float(x)
  except:return 0.0
 p=f(t.get('lastPrice',t.get('price',0))); q=f(t.get('quoteVolume',t.get('amount',t.get('turnover',0)))); return p,q

def exrows(ex):
 if ex=='BINANCE': info=bget('/exchangeInfo');ticks=bget('/ticker/24hr')
 else: info=mget('/exchangeInfo');ticks=mget('/ticker/24hr')
 syms=info.get('symbols',[]) if isinstance(info,dict) else []
 tm={str(x.get('symbol','')).upper():x for x in ticks if isinstance(x,dict) and x.get('symbol')}
 out=[]
 for s in syms:
  base=str(s.get('baseAsset','')).upper(); quote=str(s.get('quoteAsset','')).upper(); sym=str(s.get('symbol','')).upper(); st=str(s.get('status','')).upper()
  if not base or quote!='USDT' or not sym or base in STABLE or base.endswith(('UP','DOWN','BULL','BEAR')): continue
  if ex=='BINANCE' and st!='TRADING': continue
  if s.get('isSpotTradingAllowed') is False: continue
  p,q=tickrow(tm.get(sym,{}))
  if p<=0: continue
  out.append({'coin':base,'symbol':sym,'exchange':ex,'price':p,'quoteVolume':q})
 return out

def universe():
 errors=[]; a=[]; b=[]
 try:a=exrows('BINANCE')
 except Exception as e:errors.append('Binance: '+str(e))
 try:b=exrows('MEXC')
 except Exception as e:errors.append('MEXC: '+str(e))
 d={}
 for r in a+b:
  old=d.get(r['coin'])
  if old is None or r['quoteVolume']>old['quoteVolume']: d[r['coin']]=r
 rows=sorted(d.values(),key=lambda x:x['quoteVolume'],reverse=True)
 with LOCK:
  STATE['binance_count']=len(a);STATE['mexc_count']=len(b);STATE['unique_count']=len(rows)
 print(f"UNIVERSE binance={len(a)} mexc={len(b)} unique={len(rows)} errors={errors}",flush=True)
 return rows,errors

def ema(v,n):
 if len(v)<n:return None
 e=sum(v[:n])/n;k=2/(n+1)
 for x in v[n:]:e=x*k+e*(1-k)
 return e

def rsi(v,n=14):
 if len(v)<n+1:return None
 g=l=0
 for i in range(1,n+1):
  d=v[i]-v[i-1];g+=max(d,0);l+=max(-d,0)
 ag=g/n;al=l/n
 for i in range(n+1,len(v)):
  d=v[i]-v[i-1];ag=(ag*(n-1)+max(d,0))/n;al=(al*(n-1)+max(-d,0))/n
 return 100.0 if al==0 else 100-100/(1+ag/al)

def rsi_series(v,n=14):
 if len(v)<n+1:return []
 g=l=0
 for i in range(1,n+1):
  d=v[i]-v[i-1];g+=max(d,0);l+=max(-d,0)
 ag=g/n;al=l/n
 out=[100.0 if al==0 else 100-100/(1+ag/al)]
 for i in range(n+1,len(v)):
  d=v[i]-v[i-1];ag=(ag*(n-1)+max(d,0))/n;al=(al*(n-1)+max(-d,0))/n
  out.append(100.0 if al==0 else 100-100/(1+ag/al))
 return out

def ik(ex,tf): return tf if ex=='BINANCE' else ('60m' if tf=='1h' else tf)

def klines(r,tf):
 key=(r['exchange'],r['symbol'],tf);now=time.time()
 if key in HIST and now-HIST_T.get(key,0)<1800:return HIST[key]
 q=f"/klines?symbol={r['symbol']}&interval={ik(r['exchange'],tf)}&limit=90"
 d=bget(q) if r['exchange']=='BINANCE' else mget(q)
 c=[]
 for x in d:
  try:c.append(float(x[4]))
  except:pass
 HIST[key]=c;HIST_T[key]=now;return c

def metric(r,tf):
 c=klines(r,tf)[:]
 if c:c[-1]=r['price']
 e20=ema(c,20);e50=ema(c,50);p=c[-1] if c else None
 trend='—'
 if e20 is not None and e50 is not None and p is not None:
  trend='UP' if p>e20 and e20>e50 else ('DOWN' if p<e20 and e20<e50 else 'FLAT')
 return trend,rsi(c)

def analyze(r):
 t1,r1=metric(r,'1h');td,_=metric(r,'1d');_,r30=metric(r,'30m')
 return {'rsi1h':r1,'rsi30m':r30,'trend1h':t1,'trend1d':td}

def closed_stats(r,tf):
 c=klines(r,tf)[:]
 if len(c)>1:c=c[:-1]
 e20=ema(c,20);e50=ema(c,50);p=c[-1] if c else None
 up=bool(e20 is not None and e50 is not None and p is not None and p>e20>e50)
 rs=rsi_series(c)
 return c,up,rs

def criteria_eval(r):
 # Signals are based on fully closed candles only.
 c1,up1,rs1=closed_stats(r,'1h')
 if not up1 or len(rs1)<4:return None
 cur=rs1[-1]
 if cur<56 or cur>58:return None
 _,upd,_=closed_stats(r,'1d')
 if not upd:return None
 # Smooth-recovery proxy: a 20-40 RSI print within the prior 48 closed 1H bars,
 # positive recovery slope, at least 60% positive one-bar RSI steps, no drop >6 RSI,
 # and the final 3 RSI readings strictly rising.
 look=rs1[-49:-1] if len(rs1)>=50 else rs1[:-1]
 zone=[i for i,x in enumerate(look) if 20<=x<=40]
 if not zone:return None
 zi=zone[-1]
 start=len(rs1)-1-len(look)+zi
 path=rs1[start:]
 if len(path)<4:return None
 if not (path[-3]<path[-2]<path[-1]):return None
 steps=[path[i]-path[i-1] for i in range(1,len(path))]
 pos=sum(1 for x in steps if x>0)/len(steps)
 maxdrop=max([0]+[-x for x in steps if x<0])
 if path[-1]<=path[0] or pos<0.60 or maxdrop>6:return None
 c30,_,rs30=closed_stats(r,'30m')
 c15,_,rs15=closed_stats(r,'15m')
 if len(rs30)<2 or len(rs15)<2:return None
 if not (rs30[-1]>rs30[-2] and rs15[-1]>rs15[-2]):return None
 return {
  'coin':r['coin'],'symbol':r['symbol'],'exchange':r['exchange'],'price':r['price'],'quoteVolume':r['quoteVolume'],
  'rsi1h':round(cur,2),'rsi1h_prev':round(rs1[-2],2),'rsi1h_prev2':round(rs1[-3],2),
  'recovery_from_rsi':round(path[0],2),'recovery_bars':len(path)-1,'positive_step_pct':round(pos*100,1),'max_rsi_drop':round(maxdrop,2),
  'rsi30m':round(rs30[-1],2),'rsi30m_prev':round(rs30[-2],2),'rsi15m':round(rs15[-1],2),'rsi15m_prev':round(rs15[-2],2),
  'trend1h':'UP','trend1d':'UP'
 }

def criteria_job(rows):
 with LOCK:
  if STATE['criteria_running']:return
  STATE['criteria_running']=True;STATE['criteria_checked']=0
 try:
  matches=[]
  # Fast prefilter from already-computed dashboard analytics; exact check then uses closed candles.
  with LOCK: snapshot={x['coin']:dict(x) for x in STATE['rows']}
  pre=[]
  for r in rows:
   x=snapshot.get(r['coin'],{})
   rv=x.get('rsi1h')
   if x.get('trend1h')=='UP' and x.get('trend1d')=='UP' and isinstance(rv,(int,float)) and 53<=rv<=61: pre.append(r)
  print(f"CRITERIA prefilter={len(pre)}",flush=True)
  def one(r):
   try:return criteria_eval(r)
   except Exception as e:return None
  with ThreadPoolExecutor(max_workers=10) as ex:
   fut=[ex.submit(one,r) for r in pre]
   for f in as_completed(fut):
    v=f.result()
    if v:matches.append(v)
    with LOCK:STATE['criteria_checked']+=1
  matches.sort(key=lambda x:(abs(x['rsi1h']-57),-x['quoteVolume']))
  with LOCK:
   STATE['criteria_matches']=matches;STATE['criteria_updated']=int(time.time())
  print('CRITERIA_RESULT '+json.dumps(matches,separators=(',',':')),flush=True)
 finally:
  with LOCK:STATE['criteria_running']=False

def analytics_job(rows):
 with LOCK:
  if STATE['analytics_running']: return
  STATE['analytics_running']=True;STATE['analytics_total']=len(rows);STATE['analytics_done']=0
 try:
  def one(r):
   try:return r['coin'],analyze(r)
   except Exception:return r['coin'],None
  with ThreadPoolExecutor(max_workers=12) as ex:
   fut=[ex.submit(one,r) for r in rows]
   for f in as_completed(fut):
    coin,a=f.result()
    with LOCK:
     for x in STATE['rows']:
      if x['coin']==coin:
       if a:x.update(a)
       break
     STATE['analytics_done']+=1
 finally:
  with LOCK: STATE['analytics_running']=False
  # Run exact criteria scan after the full-market analytics snapshot is ready.
  threading.Thread(target=criteria_job,args=(rows,),daemon=True).start()

def refresh_all():
 while True:
  try:
   rows,errs=universe()
   with LOCK:
    old={x['coin']:x for x in STATE['rows']};merged=[]
    for r in rows:
     if r['coin'] in old:
      for k in ('rsi1h','rsi30m','trend1h','trend1d'):
       if k in old[r['coin']]:r[k]=old[r['coin']][k]
     merged.append(r)
    STATE['rows']=merged;STATE['errors']=errs;STATE['updated']=int(time.time());running=STATE['analytics_running']
   if not running: threading.Thread(target=analytics_job,args=(rows,),daemon=True).start()
  except Exception as e:
   with LOCK:STATE['errors']=[str(e)]
  time.sleep(60)

def start_bg():
 global STARTED
 if STARTED:return
 STARTED=True
 threading.Thread(target=refresh_all,daemon=True).start()

@app.before_request
def _s(): start_bg()

@app.get('/')
def home(): return send_file(os.path.join(os.path.dirname(__file__),'index.html'))
@app.get('/api/health')
def health(): return jsonify({'ok':True})
@app.get('/api/debug')
def debug():
 with LOCK:return jsonify({k:STATE[k] for k in ('binance_count','mexc_count','unique_count','errors','analytics_done','analytics_total','analytics_running','criteria_running','criteria_checked','criteria_updated','updated')})
@app.get('/api/scan')
def scan():
 with LOCK:
  rows=[dict(x) for x in STATE['rows']]
  return jsonify({'rows':rows,'count':len(rows),'updated':STATE['updated'],'errors':STATE['errors'],'analyticsDone':STATE['analytics_done'],'analyticsTotal':STATE['analytics_total'],'analyticsRunning':STATE['analytics_running'],'binanceCount':STATE['binance_count'],'mexcCount':STATE['mexc_count']})
@app.get('/api/criteria')
def criteria():
 with LOCK:
  return jsonify({'matches':[dict(x) for x in STATE['criteria_matches']],'count':len(STATE['criteria_matches']),'updated':STATE['criteria_updated'],'running':STATE['criteria_running'],'checked':STATE['criteria_checked'],'definition':{'trend':'closed-candle price > EMA20 > EMA50 on 1H and 1D','rsi1h':'56-58 on latest closed 1H candle','recovery':'20-40 RSI seen within prior 48 closed 1H bars; >=60% positive RSI steps; max pullback <=6 RSI; last 3 RSI readings strictly rising','momentum':'latest closed RSI > previous closed RSI on both 30m and 15m'}})

if __name__=='__main__': app.run(host='0.0.0.0',port=int(os.getenv('PORT','8080')))

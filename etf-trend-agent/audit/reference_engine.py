"""Offline, causal candidate research. No broker imports or execution endpoints."""
from pathlib import Path
from decimal import Decimal as D, ROUND_DOWN
import csv, json, hashlib, sys
import pandas as pd
W=Path(__file__).resolve().parent
import inputs as prep
SYMS=('SMH','QQQ','TQQQ')

def down(v): return max(D(0),v).quantize(D('.01'),rounding=ROUND_DOWN)
def digest(p): return hashlib.sha256(p.read_bytes()).hexdigest()

def features(dates,data):
 q=pd.Series([data['QQQ'][d]['Adj Close'] for d in dates]); t=pd.Series([data['TQQQ'][d]['Adj Close'] for d in dates])
 smh=pd.Series([data['SMH'][d]['Adj Close'] for d in dates])
 diff=q.diff(); g=diff.clip(lower=0).ewm(alpha=.5,adjust=False).mean(); l=(-diff.clip(upper=0)).ewm(alpha=.5,adjust=False).mean()
 f=pd.DataFrame({'q':q,'t':t,'rsi2':100-100/(1+g/l),'dd':1-q/q.rolling(252,min_periods=1).max()})
 for n in (20,40,50,60,150,180,200,220,250):
  f[f'ma{n}']=q.rolling(n).mean()
  f[f'ema{n}']=q.ewm(span=n,adjust=False).mean()
 f['ma20old']=f.ma20.shift(5); f['mom']=q/q.shift(126)-1
 for n in (63,126):f['relative'+str(n)]=t/t.shift(n)-smh/smh.shift(n)
 return f.to_dict('records')

# Freeze this finite hypothesis set before examining results. No optimization loop.
CANDIDATES=[dict(name='dca',kind='dca'),dict(name='monday-fixed8020',kind='monday'),dict(name='monday-account8020',kind='monday',allocation='gap')]
for reserve in (0.10,0.20):
 CANDIDATES.append(dict(name=f'core-dip-reserve{int(reserve*100)}',kind='dip',reserve=reserve))
CANDIDATES.append(dict(name='core-trend200',kind='coretrend',ma=200))
for monthly in (False,True):
 for reserve in (0,.05,.10):
  CANDIDATES.append(dict(name=f'trend200-{ "monthly" if monthly else "daily"}-cash{int(reserve*100)}',kind='tactical',ma=200,monthly=monthly,reserve=reserve,allocation='gap'))
for n in (150,250): CANDIDATES.append(dict(name=f'trend{n}-daily-cash5',kind='tactical',ma=n,reserve=.05,allocation='gap'))
CANDIDATES += [dict(name='momentum126-cash5',kind='tactical',signal='momentum',reserve=.05,allocation='gap'),dict(name='trend200-fixednewmoney-cash5',kind='tactical',ma=200,reserve=.05,allocation='fixed'),dict(name='tqqq-rsi2-cash5',kind='tactical',signal='rsi',ma=200,reserve=.05,allocation='gap')]

def replay(dates,data,fs,cfg,start='2021-01-01',end='2026-09-30',field='Open',bps=5,divmode='legacy',lag=0,save=None):
 funding,planned,events=prep.schedule(dates,start,end)
 qty={s:D(0) for s in SYMS}; cash=contrib=spent=sales=paid=due_total=cost=fees=D(0)
 receiv=[]; pending=[]; orders=[]; curve=[]; ledger=[]; decisions=[]
 cashdiv=pending_dca=rotation=D(0); on=False; armed=False; lastnav=0.; wealth=peak=1.; maxdd=0.; slip=D(bps)/10000
 def buy(s,amount,why,day,signal,px):
  nonlocal cash,spent,cost,rotation
  amount=down(min(cash,amount))
  if amount<1:return D(0)
  price=px[s]*(1+slip); quantity=amount/price
  qty[s]+=quantity; cash-=amount; spent+=amount; cost+=quantity*(price-px[s])
  orders.append(dict(date=day,signal_date=signal,symbol=s,side='BUY',amount=str(amount),quantity=str(quantity),price=str(price),reason=why))
  return amount
 def core(budget,targets,mode,why,day,signal,px,nav):
  budget=min(cash,budget); total=D(0)
  if mode=='fixed': amounts={s:budget*w for s,w in [('SMH',D('.8')),('QQQ',D('.2'))]}
  else:
   gaps={s:max(D(0),nav*D(str(targets[s]))-qty[s]*px[s]) for s in ('SMH','QQQ')}; denom=sum(gaps.values())
   amounts={s:min(gap,budget*gap/denom) if denom else D(0) for s,gap in gaps.items()}
  for s,a in amounts.items():total+=buy(s,a,why,day,signal,px)
  return total
 for idx,day in enumerate(dates):
  if day<start or day>end:continue
  i=idx-1-lag; f=fs[i]; signal=dates[i]
  assert i>=250 and signal<day
  px={s:D(str(data[s][day][field])) for s in SYMS}; close={s:D(str(data[s][day]['Close'])) for s in SYMS}
  for s in SYMS:
   div=D(str(data[s][day].get('Dividends',0)))
   if div and qty[s]:
    amount=div*qty[s]; due_total+=amount
    pay=prep.PAY_SMH.get(day) if s=='SMH' else None
    if pay is None:pay=dates[idx+10] if idx+10<len(dates) else '9999-12-31'
    if divmode=='quarantine':pay='9999-12-31'
    receiv.append(dict(symbol=s,amount=amount,pay_date=pay))
    ledger.append(dict(date=day,kind='DIVIDEND_ENTITLEMENT',symbol=s,amount=str(amount),pay_date=pay))
  pp=sum((r['amount'] for r in receiv if r['pay_date']<=day),D(0)); receiv=[r for r in receiv if r['pay_date']>day]
  if pp:ledger.append(dict(date=day,kind='DIVIDEND_PAID',amount=str(pp)))
  paid+=pp; cash+=pp; cashdiv+=pp
  released=sum((r['amount'] for r in pending if r['pay_date']<=day),D(0)); pending=[r for r in pending if r['pay_date']>day]
  if released:ledger.append(dict(date=day,kind='SALE_SETTLED',amount=str(released)))
  cash+=released;rotation+=released
  unsettled=sum((r['amount'] for r in pending),D(0)); ent=sum((r['amount'] for r in receiv),D(0))
  before=sum(qty[s]*px[s] for s in SYMS)+cash+unsettled+ent
  incoming=funding.get(day,D(0));cash+=incoming;contrib+=incoming
  if incoming:ledger.append(dict(date=day,kind='DEPOSIT',amount=str(incoming)))
  nav=before+incoming
  if f['dd']>=.10:armed=True
  kind=cfg['kind']; previous=on
  if kind=='tactical':
   eligible=not cfg.get('monthly') or (dates[idx-1][:7]!=day[:7])
   if eligible:
    if cfg.get('signal')=='rsi':
     if not on and f['q']>f['ma200'] and f['rsi2']<10:on=True
     elif on and (f['rsi2']>70 or f['q']<f['ma200']):on=False
    elif cfg.get('signal')=='momentum': on=f['mom']>0
    elif cfg.get('signal')=='goldencross': on=f['ma'+str(cfg.get('fast',50))]>f['ma'+str(cfg.get('slow',200))]
    elif cfg.get('signal')=='emacross': on=f['ema'+str(cfg.get('fast',50))]>f['ema'+str(cfg.get('slow',200))]
    elif cfg.get('signal')=='vote': on=sum(f['q']>f['ma'+str(n)] for n in (150,200,250))>=2
    else:
     ma=f['ma'+str(cfg['ma'])]
     if f['q']>ma*1.01:on=True
     elif f['q']<ma*.99:on=False
    if cfg.get('relative'):on=on and f['relative'+str(cfg['relative'])]>0
   if on!=previous:decisions.append(dict(date=day,signal_date=signal,on=on,qqq=f['q'],ma=f.get('ma'+str(cfg.get('ma',200))),mom=f['mom'],rsi2=f['rsi2']))
   trim=cfg.get('trim') and incoming and qty['TQQQ']*px['TQQQ']>nav*D('.35')
   if qty['TQQQ'] and (not on or trim):
    q=qty['TQQQ'] if not on else max(D(0),qty['TQQQ']-nav*D('.30')/px['TQQQ'])
    price=px['TQQQ']*(1-slip);gross=q*price;fee=min(D('.01'),gross);net=gross-fee
    settle=3 if day<'2017-09-05' else (2 if day<'2024-05-28' else 1)
    pay=dates[idx+settle] if idx+settle<len(dates) else '9999-12-31'
    orders.append(dict(date=day,signal_date=signal,symbol='TQQQ',side='SELL',amount=str(gross),net=str(net),quantity=str(q),price=str(price),fee=str(fee),pay_date=pay,reason='TREND_EXIT' if not on else 'HARVEST_ABOVE35_TO30'))
    pending.append(dict(amount=net,pay_date=pay));sales+=net;fees+=fee;cost+=q*(px['TQQQ']-price);qty['TQQQ']-=q
   if rotation>=1:
    used=core(rotation,{'SMH':.8,'QQQ':.2},'gap','FULL_TQQQ_PROCEEDS_TO_CORE',day,signal,px,nav)
    rotation-=used
   if on:
    reserve=min(nav*D(str(cfg.get('reserve',0))),D(str(cfg.get('reserve_cap','Infinity'))))
    available=max(D(0),cash-rotation-reserve)
    gap=max(D(0),nav*D('.3')-qty['TQQQ']*px['TQQQ'])
    buy('TQQQ',min(gap,available),'ATTACK_TARGET30',day,signal,px)
    core(max(D(0),cash-rotation-reserve),{'SMH':.6,'QQQ':.1},cfg.get('allocation','gap'),'ATTACK_CORE',day,signal,px,nav)
   elif cfg.get('relative') and f['q']>f['ma200']:
    reserve=min(nav*D(str(cfg.get('reserve',0))),D(str(cfg.get('reserve_cap','Infinity'))))
    core(max(D(0),cash-rotation-reserve),{'SMH':.8,'QQQ':.2},'gap','BULL_CORE_WITHOUT_LEVERAGE',day,signal,px,nav)
  elif kind=='dca' and day in planned:
   pending_dca+=planned[day];extra=min(cash,cashdiv);budget=min(cash-extra,pending_dca)+extra
   total=core(budget,{},'fixed','TUE_THU_DCA',day,signal,px,nav)
   used=min(extra,total);cashdiv-=used;pending_dca-=total-used
  elif kind=='monday' and incoming:
   core(cash,{'SMH':.8,'QQQ':.2},cfg.get('allocation','fixed'),'MONDAY',day,signal,px,nav)
  elif kind=='coretrend' and f['q']>f['ma200']:
   core(cash,{},'fixed','CORE_TREND_RECOVERY',day,signal,px,nav)
  elif kind=='dip':
   if incoming:core(incoming*(1-D(str(cfg['reserve']))),{},'fixed','BASE_PARTICIPATION',day,signal,px,nav)
   if armed and f['q']>f['ma20'] and f['ma20']>f['ma20old']:
    core(cash,{},'fixed','DIP_STABILIZED',day,signal,px,nav);armed=False
  unsettled=sum((r['amount'] for r in pending),D(0)); values={s:qty[s]*close[s] for s in SYMS}
  nav=sum(values.values())+cash+unsettled+ent
  assert cash>=0 and all(q>=0 for q in qty.values())
  assert abs(contrib+paid+sales-spent-cash-unsettled)<D('.0000001')
  growth=(float(before)/lastnav if lastnav else 1.)*(float(nav/(before+incoming)) if before+incoming else 1.)
  wealth*=growth;peak=max(peak,wealth);dd=1-wealth/peak;maxdd=max(maxdd,dd);lastnav=float(nav)
  curve.append(dict(date=day,nav=float(nav),cash=float(cash),unsettled=float(unsettled),dividend_entitlement=float(ent),contributed=float(contrib),contribution=float(incoming),unit_nav=wealth,drawdown=dd,holdings={s:float(x) for s,x in values.items()},quantity={s:str(qty[s]) for s in SYMS}))
 summary=dict(name=cfg['name'],start=curve[0]['date'],end=curve[-1]['date'],contributed=float(contrib),final_nav=float(nav),cash=float(cash),unsettled=float(unsettled),dividend_entitlement=float(ent),max_drawdown=maxdd,average_cash_weight=sum(c['cash']/c['nav'] if c['nav'] else 0 for c in curve)/len(curve),spent=float(spent),sales=float(sales),dividends_paid=float(paid),slippage=float(cost),fees=float(fees),orders=len(orders),field=field,bps=bps,divmode=divmode,lag=lag,holdings=curve[-1]['holdings'],config=cfg)
 if save:
  save.mkdir(parents=True,exist_ok=True)
  for n,x in [('summary',summary),('orders',orders),('curve',curve),('ledger',ledger),('transitions',decisions),('funding',events)]: (save/(n+'.json')).write_text(json.dumps(x,indent=2))
 return summary

def main():
 prep.verify();dates,data=prep.load();fs=features(dates,data)
 protocol=dict(candidates=CANDIDATES,primary_period=['2021-01-01','2026-09-30'],funding='Monday100, DCA Tuesday50 Thursday50; holidays next actual trading day',constraints='core BUY-only, TQQQ only sellable, no borrowing, no SGOV, no live changes',signal='completed prior-day prices, 1% hysteresis around MA; monthly only first trading day using prior close; momentum126 return; RSI2 Wilder entries<10 exits>70 with200MAfilter',allocation='tactical whole-account60/10/30 best effort, full net TQQQ proceeds to80/20 core gaps afterT+1; cash floor only ordinary buys; fixednewmoney explicitly separate alternative',dividends='same prior model: SMH known paydates, other10sessions assumed; quarantine sensitivity never reinvests dividends, includes receivables in NAV but NOT cash',execution='next-session Open+5bps buy / Open-5bps sell, 1cent sellfee; fractional split-normalized shares, prices already split-adjusted, no extra split multipliers; no cash interest; pretax',selection='exploratory all17 predefined candidates retained, history already examined, not blind OOS; no guarantee',code_sha256=digest(Path(__file__)))
 (W/'protocol.json').write_text(json.dumps(protocol,indent=2))
 rows=[]
 for cfg in CANDIDATES:
  r=replay(dates,data,fs,cfg,save=W/'results'/cfg['name']);rows.append(r)
  print(cfg['name'],round(r['final_nav'],2),round(r['cash'],2),round(r['max_drawdown']*100,2),flush=True)
 (W/'comparison.json').write_text(json.dumps(rows,indent=2));prep.verify()
if __name__=='__main__':main()

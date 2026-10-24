"""Public frozen inputs and a reproducible weekly funding calendar."""
from pathlib import Path
from decimal import Decimal as D
from collections import defaultdict
from datetime import date,timedelta
from bisect import bisect_left
import csv,json,hashlib,math
W=Path(__file__).resolve().parent
PAY_SMH=dict(line.split() for line in '''2021-12-20 2021-12-27
2022-12-19 2022-12-23
2023-12-18 2023-12-22
2024-12-23 2024-12-24
2025-12-22 2025-12-26'''.splitlines())
def verify():
 for s,m in json.loads((W/'data/manifest.json').read_text()).items():assert hashlib.sha256((W/'data'/f'{s}.csv').read_bytes()).hexdigest()==m['sha256'],s

def load():
 verify();data={}
 for s in ['SMH','QQQ','TQQQ']:
  with (W/'data'/f'{s}.csv').open() as f:rows=list(csv.DictReader(f))
  assert [r['Date'] for r in rows]==sorted(set(r['Date'] for r in rows))
  data[s]={r['Date']:{k:float(x) for k,x in r.items() if k!='Date' and x!=''} for r in rows}
 dates=[d for d in data['QQQ'] if d<='2026-09-30']
 for s in data:
  assert [d for d in data[s] if d<=dates[-1]]==dates
  for day in dates:
   r=data[s][day];assert all(math.isfinite(r[k]) and r[k]>0 for k in ['Open','High','Low','Close','Adj Close'])
   assert r['Low']<=min(r['Open'],r['Close'])<=max(r['Open'],r['Close'])<=r['High']
 return dates,data

def schedule(dates,start,end):
 funding=defaultdict(lambda:D(0));planned=defaultdict(lambda:D(0));events=[]
 monday=date.fromisoformat(start);monday+=timedelta(days=(-monday.weekday())%7)
 while monday.isoformat()<=end:
  idx=bisect_left(dates,monday.isoformat())
  if idx<len(dates) and dates[idx]<=end:
   received=dates[idx];funding[received]+=D(100);events.append(dict(kind='DEPOSIT',nominal=monday.isoformat(),actual=received,amount='100.00'))
   for offset in [1,3]:
    nominal=(monday+timedelta(days=offset)).isoformat();idx=bisect_left(dates,nominal)
    actual=dates[idx] if idx<len(dates) else None
    if nominal<=end and actual and actual<=end:
     assert actual>=received;planned[actual]+=D(50);events.append(dict(kind='DCA_BUY',nominal=nominal,actual=actual,amount='50.00'))
  monday+=timedelta(days=7)
 return dict(funding),dict(planned),events

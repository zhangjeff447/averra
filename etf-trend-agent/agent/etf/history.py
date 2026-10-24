"""Download audited public bars; signal calculations use completed sessions only."""
import hashlib
import json
from pathlib import Path
from datetime import datetime,timezone
import math


def download(destination,start='2015-01-01',end=None):
    import yfinance as yf
    root=Path(destination);root.mkdir(parents=True,exist_ok=True)
    yf.set_tz_cache_location(str(root/'timezone-cache'))
    end=end or datetime.now(timezone.utc).date().isoformat()
    manifest={}
    for symbol in ('SMH','QQQ','TQQQ'):
        frame=yf.download(symbol,start=start,end=end,interval='1d',auto_adjust=False,
                          actions=True,progress=False,threads=False,timeout=25,multi_level_index=False)
        if len(frame)<300:raise ValueError('history_insufficient:'+symbol)
        dates=[d.date().isoformat() for d in frame.index]
        if dates!=sorted(set(dates)) or any(d>=end for d in dates):raise ValueError('history_dates_invalid')
        for field in ('Open','High','Low','Close'):
            if any(not math.isfinite(float(v)) or v<=0 for v in frame[field]):raise ValueError('history_price_invalid')
        path=root/(symbol.replace('^','')+'.csv');frame.to_csv(path,index_label='Date')
        manifest[symbol]=dict(first=dates[0],last=dates[-1],rows=len(frame),
                              sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                              source='Yahoo Finance via yfinance',auto_adjust=False,
                              dividends='ex-date only; not authoritative broker payable-date cash',
                              retrieved_at=datetime.now(timezone.utc).isoformat())
    (root/'manifest.json').write_text(json.dumps(manifest,indent=2))
    return manifest


def load(destination):
    import pandas as pd
    root=Path(destination);manifest=json.loads((root/'manifest.json').read_text());frames={}
    for symbol,meta in manifest.items():
        p=root/(symbol.replace('^','')+'.csv')
        if hashlib.sha256(p.read_bytes()).hexdigest()!=meta['sha256']:raise ValueError('history_hash_changed')
        frames[symbol]=pd.read_csv(p,index_col='Date')
    if set(frames)!={'SMH','QQQ','TQQQ'}:raise ValueError('history_symbol_set_invalid')
    dates=sorted(set.intersection(*(set(f.index) for f in frames.values())))
    if len(dates)<300:raise ValueError('aligned_history_insufficient')
    from datetime import date,timedelta
    from .calendar import is_session
    from zoneinfo import ZoneInfo
    available=set(dates);cursor=date.fromisoformat(dates[0]);last=date.fromisoformat(dates[-1])
    while cursor<=last:
        instant=datetime.combine(cursor,datetime.min.time(),tzinfo=ZoneInfo('America/New_York'))
        if is_session(cursor.isoformat()) and cursor.isoformat() not in available:
            raise ValueError('aligned_history_missing_session:'+cursor.isoformat())
        cursor+=timedelta(days=1)
    return dates,{s:f.loc[dates] for s,f in frames.items()}


def signal(destination,*,now):
    from .calendar import previous_session,ET
    dates,frames=load(destination)
    expected=previous_session(now.astimezone(ET).date().isoformat())
    if dates[-1]!=expected:raise ValueError('completed_history_stale')
    from .cycle import POLICY
    period=POLICY.get('slow_ma_period',100)
    values=frames['QQQ']['Adj Close']
    if len(values)<period or any(not math.isfinite(float(x)) or x<=0 for x in values):raise ValueError('adjusted_history_invalid')
    return dict(date=dates[-1],close=str(float(values.iloc[-1])),sma50=str(float(values.rolling(50).mean().iloc[-1])),**{'sma'+str(period):str(float(values.rolling(period).mean().iloc[-1]))},history_sha256=hashlib.sha256((Path(destination)/'QQQ.csv').read_bytes()).hexdigest())

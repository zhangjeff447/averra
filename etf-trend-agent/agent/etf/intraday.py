"""Completed regular-session bars only; volume-weighted typical price is an estimate."""
from datetime import datetime,timedelta,timezone
from statistics import median
from .calendar import ET,exchange,is_session
from .cycle import money,POLICY,SYMBOLS
from .funds import timestamp


def fetch(call,now):
    day=now.astimezone(ET).date().isoformat()
    if not is_session(day):raise ValueError('intraday_not_session')
    opened=exchange().session_open(day).to_pydatetime()
    closed=exchange().session_close(day).to_pydatetime()
    if not opened<=now<closed:raise ValueError('intraday_outside_session')
    cutoff=now.replace(second=0,microsecond=0)
    params=dict(symbols=list(SYMBOLS),interval='minute',bounds='regular',adjustment_type='none',start_time=opened.isoformat(),end_time=cutoff.isoformat())
    data=call('get_equity_historicals',params)['data']
    results=data.get('results')
    if not isinstance(results,list):raise ValueError('intraday_response_invalid')
    package=dict(source='broker_mcp',tool='get_equity_historicals',retrieved_at=datetime.now(timezone.utc).isoformat(),as_of=now.isoformat(),symbols={})
    for row in results:
        symbol=row.get('symbol')
        if symbol not in SYMBOLS or symbol in package['symbols']:raise ValueError('intraday_symbol_conflict')
        package['symbols'][symbol]=row
    return package


def evaluate(package,symbol,quote,now):
    rules=POLICY['intraday']
    try:
        if package.get('source')!='broker_mcp' or package.get('tool')!='get_equity_historicals':raise ValueError('intraday_source_unverified')
        age=(now-timestamp(package['retrieved_at'])).total_seconds()
        if not 0<=age<=rules['package_max_age_seconds']:raise ValueError('intraday_package_stale')
        row=package['symbols'].get(symbol)
        if not row or row.get('interval')!='minute' or row.get('bounds')!='regular':raise ValueError('intraday_symbol_unavailable')
        day=now.astimezone(ET).date().isoformat();opened=exchange().session_open(day).to_pydatetime();closed=exchange().session_close(day).to_pydatetime()
        cutoff=now.replace(second=0,microsecond=0);bars=[];seen=set()
        for raw in row['bars']:
            at=timestamp(raw['begins_at'])
            if at in seen or at.second or at.microsecond:raise ValueError('intraday_timestamp_invalid')
            seen.add(at)
            if at==cutoff:continue  # never use the forming minute, even if returned inclusively
            if not opened<=at<min(cutoff,closed):raise ValueError('intraday_bar_outside_decision')
            flag=raw.get('interpolated')
            if raw.get('session')!='reg' or (flag is not None and flag is not False):raise ValueError('intraday_synthetic_or_session_unknown')
            # Native schema omits optional false flags; positive trade volume
            # below is still mandatory (documented synthetic bars have zero).
            o,h,l,c=(money(raw[k]) for k in ('open_price','high_price','low_price','close_price'))
            v=money(raw['volume'])
            if min(o,h,l,c)<=0 or not l<=min(o,c)<=max(o,c)<=h or v<=0 or v!=int(v):raise ValueError('intraday_ohlcv_invalid')
            bars.append(dict(at=at,o=o,h=h,l=l,c=c,v=v))
        bars.sort(key=lambda x:x['at'])
        if len(bars)<30:raise ValueError('intraday_insufficient_minutes')
        if bars[0]['at']!=opened or any(b['at']!=opened+timedelta(minutes=i) for i,b in enumerate(bars)):raise ValueError('intraday_minute_gap')
        last_end=bars[-1]['at']+timedelta(minutes=1)
        if not 0<=(now-last_end).total_seconds()<=rules['minute_max_age_seconds']:raise ValueError('intraday_minutes_stale')
        def aggregate(n):
            result=[]
            for i in range(0,len(bars)-n+1,n):
                part=bars[i:i+n]
                result.append(dict(o=part[0]['o'],c=part[-1]['c'],h=max(b['h'] for b in part),l=min(b['l'] for b in part),v=sum(b['v'] for b in part),end=part[-1]['at']+timedelta(minutes=1)))
            return result
        five=aggregate(5);fifteen=aggregate(15)
        if len(five)<3 or not fifteen:raise ValueError('intraday_aggregate_insufficient')
        last=five[-1];prior=five[-2];long=fifteen[-1]
        vwap=sum((b['h']+b['l']+b['c'])/3*b['v'] for b in bars)/sum(b['v'] for b in bars)
        ask=money(quote['ask']);spread=(ask-money(quote['bid']))/money(quote['last'])*100
        ratio=last['v']/median([b['v'] for b in five[max(0,len(five)-7):-1]])
        reasons=[]
        if ask>vwap*(1+money(rules['max_vwap_extension_pct'])/100):reasons.append('INTRADAY_ABOVE_VWAP_BAND')
        if ask>last['c']*(1+money(rules['max_quote_extension_pct'])/100) or ask<last['l']:reasons.append('INTRADAY_QUOTE_MOVED')
        if last['c']<last['o'] or last['c']<prior['c']:reasons.append('INTRADAY_NOT_STABILIZED')
        if (long['c']/long['o']-1)*100 < -money(rules['max_fifteen_minute_drop_pct']):reasons.append('INTRADAY_FALLING_15M')
        if ratio<money(rules['min_volume_ratio']):reasons.append('INTRADAY_VOLUME_THIN')
        if spread>money(rules['max_spread_pct']):reasons.append('INTRADAY_SPREAD_WIDE')
        return dict(decision='WAIT' if reasons else 'BUY',reasons=reasons or ['INTRADAY_STABILIZED'],minute_end=last_end.isoformat(),five_minute_end=last['end'].isoformat(),fifteen_minute_end=long['end'].isoformat(),vwap_estimate=str(vwap),vwap_method='VOLUME_WEIGHTED_HLC3_ESTIMATE_NOT_TRADE_VWAP',volume_ratio=str(ratio),spread_pct=str(spread))
    except (KeyError,ValueError,TypeError,ArithmeticError) as exc:
        return dict(decision='WAIT',reasons=[str(exc) if isinstance(exc,ValueError) else 'INTRADAY_DATA_INVALID'])

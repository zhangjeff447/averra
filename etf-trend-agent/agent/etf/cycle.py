"""Return-first SMA50/200 restored policy; live execution is a separate guarded layer."""
from decimal import Decimal,ROUND_DOWN
from pathlib import Path
import json
SYMBOLS=('SMH','QQQ','TQQQ');CORE=('SMH','QQQ')
POLICY=json.loads(Path(__file__).with_name('policy.json').read_text())
def money(value):
 if value is None or isinstance(value,bool):raise ValueError('money_missing')
 d=Decimal(str(value))
 if not d.is_finite() or d<0:raise ValueError('money_invalid')
 return d
def slow_average(signal):
 return money(signal['sma'+str(POLICY.get('slow_ma_period',100))])
def down(value):return value.quantize(Decimal('.01'),rounding=ROUND_DOWN)
def authorize_order(symbol,side):
 if not (symbol in CORE and side=='BUY' or symbol=='TQQQ' and side in ('BUY','SELL')):raise ValueError('order_policy_violation')
def action_key(day,order):return ':'.join((day,order['symbol'],order['side'],order['reason']))
def plan(snapshot,signal,journal,*,now,exclude_intent=None,risk_only=False,persist_allocation=False,evaluation_time=None):
 from .reference_planner import preview
 from .calendar import funding_session
 day=now.astimezone(__import__('zoneinfo').ZoneInfo('America/New_York')).date().isoformat()
 rotation=min(money(snapshot['safe_cash']),journal.rotation_available(day))
 example=dict(date=day,quantities=snapshot['quantities'],prices={s:snapshot['quotes'][s]['last'] for s in SYMBOLS},
  net_assets=snapshot['net_assets'],settled_cash=snapshot['cash_for_weight'],safe_cash=snapshot['safe_cash'],settled_rotation=str(rotation),
  unsettled_sales='0',dividend_receivable='0',unresolved_orders=False,own_settled_cash_verified=True)
 from .stop_loss import protection
 safety=protection(snapshot,signal,journal)
 if safety['triggered']:
  qty=money(snapshot['quantities']['TQQQ'])
  if money(snapshot['sellable']['TQQQ'])<qty:raise ValueError('stop_full_position_not_sellable')
  result=dict(signal='BUY_OR_HOLD' if money(signal['sma50'])>slow_average(signal) else 'DEFENSIVE',orders=[dict(symbol='TQQQ',side='SELL',quantity=str(qty),amount=str(qty*money(snapshot['quotes']['TQQQ']['bid'])),reason='STOP_LOSS_6_6_ALL_TQQQ')])
 elif risk_only:
  result=dict(signal='BUY_OR_HOLD' if money(signal['sma50'])>slow_average(signal) else 'DEFENSIVE',orders=[])
 else:
  result=preview(example,signal,weekly_funding_session=funding_session(day),bps=0,allow_tqqq_buy=safety['buy_allowed'])
  if not safety['buy_allowed']:
   result['orders']=[o for o in result['orders'] if not (o['symbol']=='TQQQ' and o['side']=='BUY')]
 from .buy_limits import slot_key,allowance,usage
 slot=slot_key(now);day=now.astimezone(__import__('zoneinfo').ZoneInfo('America/New_York')).date().isoformat()
 basket=None
 if POLICY.get('daily_buy_limit') is not None and not risk_only and not safety['triggered']:
  basket=journal.load('rf_window_allocation:'+str(slot))
  if basket is None and slot:
   buys=[o for o in result['orders'] if o['side']=='BUY']
   total=sum((money(o['amount']) for o in buys),Decimal(0))
   budget=min(money(snapshot['safe_cash']),allowance(journal,now))
   amounts={o['symbol']:str(down(min(money(o['amount']),budget*money(o['amount'])/total))) for o in buys} if total else {}
   basket=dict(policy_version=POLICY['version'],amounts=amounts,window_key=slot)
   if persist_allocation:journal.save('rf_window_allocation:'+slot,basket)
 intraday={}
 if POLICY.get('intraday_enabled') and not risk_only and not safety['triggered']:
  from .intraday import evaluate
  at=evaluation_time or __import__('etf.funds',fromlist=['timestamp']).timestamp(snapshot['as_of'])
  intraday={s:evaluate(snapshot.get('intraday',{}),s,snapshot['quotes'][s],at) for s in SYMBOLS}
 selected=None
 for candidate in result['orders']:
  candidate=dict(candidate,policy_version=POLICY['version']);candidate['price']=snapshot['quotes'][candidate['symbol']]['bid' if candidate['side']=='SELL' else 'ask'];key=action_key(day,candidate)
  if candidate['side']=='BUY' and POLICY.get('daily_buy_limit') is not None:key+=':'+str(slot)
  if journal.action_used(key,exclude_intent=exclude_intent):continue
  candidate['decision_id']=key
  if candidate['side']=='SELL':
   q=money(candidate['quantity']).quantize(Decimal('.000001'),rounding=ROUND_DOWN)
   if q<=0:continue
   candidate['quantity']=str(q)
   candidate['amount']=str(down(q*money(snapshot['quotes']['TQQQ']['bid'])))
   if money(candidate['amount'])<money(POLICY['broker_minimum']):continue
  else:
   if POLICY.get('intraday_enabled') and intraday[candidate['symbol']]['decision']!='BUY':continue
   if POLICY.get('daily_buy_limit') is not None:
    if not basket or basket.get('policy_version')!=POLICY['version']:continue
    remaining=money(basket['amounts'].get(candidate['symbol'],'0'))-usage(journal,day,slot=slot,symbol=candidate['symbol'],exclude_intent=exclude_intent)
    candidate['amount']=str(down(max(Decimal(0),min(money(candidate['amount']),remaining,allowance(journal,now,exclude_intent=exclude_intent),money(POLICY['per_order_limit'])))))
    candidate['window_key']=slot
   candidate['amount']=str(down(money(candidate['amount'])))
   if money(candidate['amount'])<money(POLICY['broker_minimum']):continue
   if POLICY.get('intraday_enabled'):candidate['intraday_evidence']=dict(intraday[candidate['symbol']],evidence_sha256=snapshot.get('intraday',{}).get('evidence_sha256'))
   candidate['rotation_amount']=candidate['amount'] if candidate['reason']=='FULL_TQQQ_PROCEEDS_TO_CORE' else '0'
  selected=candidate;break
 reason=selected['reason'] if selected else 'WAIT_STOP_NOT_TRIGGERED' if risk_only else 'WAIT_POLICY_OR_SESSION_USED'
 if not selected and not risk_only:
  if POLICY.get('daily_buy_limit') is not None and usage(journal,day,exclude_intent=exclude_intent)>=money(POLICY['daily_buy_limit']):reason='WAIT_DAILY_BUY_LIMIT'
  elif POLICY.get('daily_buy_limit') is not None and slot and allowance(journal,now,exclude_intent=exclude_intent)<money(POLICY['broker_minimum']):reason='WAIT_WINDOW_BUY_LIMIT'
  elif intraday and any(o['side']=='BUY' for o in result['orders']) and any(v['decision']=='WAIT' for v in intraday.values()):reason='WAIT_INTRADAY_FILTER'
 return dict(version=POLICY['version'],phase='ATTACK' if result['signal']=='BUY_OR_HOLD' else 'DEFENSIVE',selected=selected,
  reason=reason,safe_cash=snapshot['safe_cash'],net_assets=snapshot['net_assets'],
  intraday=intraday,window_allocation=basket,tqqq_protection=safety,signal_date=signal['date'],close=signal['close'],sma50=signal['sma50'],slow_ma_period=POLICY.get('slow_ma_period',100),slow_ma=str(slow_average(signal)),**{'sma'+str(POLICY.get('slow_ma_period',100)):str(slow_average(signal))})

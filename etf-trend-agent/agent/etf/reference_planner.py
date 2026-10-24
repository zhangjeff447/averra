"""Pure offline order illustrations. No broker, credentials, scheduler or execution API."""
from decimal import Decimal as D, ROUND_DOWN
import json
from pathlib import Path

SYMBOLS=('SMH','QQQ','TQQQ')
POLICY=json.loads(Path(__file__).with_name('policy.json').read_text())

def nonnegative(x):
 if isinstance(x,bool) or x is None:raise ValueError('missing_or_boolean_number')
 value=D(str(x))
 if not value.is_finite() or value<0:raise ValueError('invalid_number')
 return value

def cents(x):return max(D(0),x).quantize(D('.01'),rounding=ROUND_DOWN)

def preview(snapshot,signal,*,weekly_funding_session=False,bps=5,allow_tqqq_buy=True):
 """Use a supplied hypothetical/normalized snapshot; never fetch or submit anything.

 settled_rotation is already INSIDE safe_cash, not money to add to buying power.
 New SELL illustrations create unsettled proceeds; no same-step reuse.
 Signal SMA values must be calculated from completed preceding sessions.
 """
 if signal['date']>=snapshot['date']:raise ValueError('signal_must_precede_decision')
 if snapshot.get('unresolved_orders') is not False:raise ValueError('orders_not_reconciled')
 if snapshot.get('own_settled_cash_verified') is not True:raise ValueError('cash_not_verified')
 if set(snapshot['quantities'])!=set(SYMBOLS) or set(snapshot['prices'])!=set(SYMBOLS):raise ValueError('symbol_set_invalid')
 qty={s:nonnegative(snapshot['quantities'][s]) for s in SYMBOLS}
 px={s:nonnegative(snapshot['prices'][s]) for s in SYMBOLS}
 if min(px.values())<=0:raise ValueError('nonpositive_price')
 cash=nonnegative(snapshot['safe_cash']);rotation=nonnegative(snapshot['settled_rotation'])
 settled=nonnegative(snapshot['settled_cash']);unsettled=nonnegative(snapshot['unsettled_sales']);dividends=nonnegative(snapshot['dividend_receivable'])
 nav=nonnegative(snapshot['net_assets']);slip=nonnegative(bps)/10000
 if nav<=0 or slip>=1:raise ValueError('valuation_or_cost_invalid')
 if rotation>cash or cash>settled:raise ValueError('cash_or_rotation_double_count')
 if abs(sum(qty[s]*px[s] for s in SYMBOLS)+settled+unsettled+dividends-nav)>D('.01'):raise ValueError('valuation_unreconciled')
 from .cycle import slow_average
 fast=nonnegative(signal['sma50']);slow=slow_average(signal)
 if min(fast,slow)<=0:raise ValueError('invalid_moving_average')
 on=fast>slow;orders=[]
 def buy(symbol,amount,reason):
  nonlocal cash
  amount=cents(min(cash,amount))
  if amount<1:return D(0)
  price=px[symbol]*(1+slip);quantity=amount/price
  qty[symbol]+=quantity;cash-=amount
  orders.append(dict(symbol=symbol,side='BUY',amount=str(amount),quantity=str(quantity),price=str(price),reason=reason))
  return amount
 def core(budget,targets,reason):
  budget=min(cash,budget)
  gaps={s:max(D(0),nav*D(str(targets[s]))-qty[s]*px[s]) for s in ('SMH','QQQ')};denom=sum(gaps.values())
  amounts={s:min(gap,budget*gap/denom) if denom else D(0) for s,gap in gaps.items()}
  return sum((buy(s,a,reason) for s,a in amounts.items()),D(0))
 trim=weekly_funding_session and qty['TQQQ']*px['TQQQ']>nav*D('.35')
 new_proceeds=D(0)
 if qty['TQQQ'] and (not on or trim):
  quantity=qty['TQQQ'] if not on else max(D(0),qty['TQQQ']-nav*D('.30')/px['TQQQ'])
  price=px['TQQQ']*(1-slip);gross=quantity*price;fee=min(D('.01'),gross);new_proceeds=gross-fee
  orders.append(dict(symbol='TQQQ',side='SELL',amount=str(gross),net=str(new_proceeds),quantity=str(quantity),price=str(price),fee=str(fee),reason='TREND_EXIT' if not on else 'HARVEST_ABOVE35_TO30'))
  qty['TQQQ']-=quantity
 if rotation>=1:
  rotation-=core(rotation,{'SMH':.8,'QQQ':.2},'FULL_TQQQ_PROCEEDS_TO_CORE')
 if on:
  gap=max(D(0),nav*D('.3')-qty['TQQQ']*px['TQQQ'])
  if allow_tqqq_buy:buy('TQQQ',min(gap,max(D(0),cash-rotation)),'ATTACK_TARGET30')
  core(max(D(0),cash-rotation),{'SMH':.6,'QQQ':.1},'ATTACK_CORE')
 return dict(mode='OFFLINE_PREVIEW_ONLY',execution_supported=False,strategy_version=POLICY['version'],signal='BUY_OR_HOLD' if on else 'DEFENSIVE',orders=orders,safe_cash_after=str(cash),settled_rotation_after=str(rotation),new_unsettled_sale_proceeds=str(new_proceeds),projected_quantities={s:str(q) for s,q in qty.items()})

if __name__=='__main__':
 import argparse
 parser=argparse.ArgumentParser(description='Offline preview only. No live execution option.')
 parser.add_argument('input',type=Path,help='Local JSON with snapshot, completed signal and weekly_funding_session flag')
 args=parser.parse_args();data=json.loads(args.input.read_text())
 print(json.dumps(preview(data['snapshot'],data['signal'],weekly_funding_session=data.get('weekly_funding_session',False)),indent=2))

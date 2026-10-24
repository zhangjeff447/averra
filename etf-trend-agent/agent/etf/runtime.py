"""One bounded daily plan; restart recovery and fresh reconciliation precede every order."""
import hashlib,json
from datetime import datetime,timezone
from decimal import Decimal
from .cycle import POLICY,plan,money
from .funds import normalize
from .calendar import ET,previous_session
from .reconciliation import validate_history,validate_activation_positions

def checked(journal,raw,*,now):
 journal.reconcile(raw['orders'],complete=raw['complete_orders'])
 validate_history(journal,raw);validate_activation_positions(journal,raw)
 journal.sync_rotation();day=now.astimezone(ET).date().isoformat()
 if 'external_cash_events' in raw['funds']:journal.observe_external_cash_events(raw['funds']['external_cash_events'])
 snapshot=normalize(raw,now=now,held_cash=journal.unsettled_sale_cash(day));journal.bind_account(snapshot['account_hash'])
 return snapshot

def cycle(journal,broker,signal,*,live=False,now=None,max_orders=6,window=None,risk_only=False):
 now=now or datetime.now(timezone.utc);day=now.astimezone(ET).date().isoformat()
 if signal['date']!=previous_session(day):raise ValueError('signal_session_stale')
 fingerprint=hashlib.sha256(json.dumps(signal,sort_keys=True).encode()).hexdigest()
 signal_key='rf_signal:'+POLICY['version']+':'+day
 old=journal.load(signal_key)
 if old and old!=fingerprint:raise ValueError('completed_daily_signal_changed')
 if live:journal.save(signal_key,fingerprint)
 results=[]
 # A submitted-but-unconfirmed order blocks further orders until broker history
 # confirms it. The loop can advance multiple legs only after dated fills.
 for _ in range(max_orders):
  raw=broker.read();snapshot=checked(journal,raw,now=datetime.now(timezone.utc) if live else now)
  if live:journal.recover_unsubmitted()
  if journal.halt_reason():return dict(reason='TRADING_HALTED:'+journal.halt_reason(),selected=None,executions=results)
  if journal.active() or journal.legacy_active():return dict(reason='BLOCKED_BY_UNRESOLVED_INTENT',selected=None,executions=results)
  if raw.get('pending_approvals'):return dict(reason='WAIT_PENDING_APPROVAL',selected=None,executions=results)
  minute_context={}
  if POLICY.get('intraday_enabled') and not risk_only:
   from .stop_loss import protection
   if not protection(snapshot,signal,journal)['triggered']:
    try:minute_context=broker.minute_data(datetime.now(timezone.utc) if live else now)
    except Exception as exc:minute_context=dict(unavailable=type(exc).__name__)
  snapshot['intraday']=minute_context
  result=plan(snapshot,signal,journal,now=now,risk_only=risk_only,persist_allocation=live,evaluation_time=datetime.now(timezone.utc))
  if not live or not result['selected']:
   if not result['selected'] and any(x['plan']['reason']=='STOP_LOSS_6_6_ALL_TQQQ' for x in results):result['reason']='STOP_LOSS_FILLED_WAIT_SETTLEMENT'
   result['cash_basis']='VERIFIED_CURRENT';result['executions']=results;return result
  if result['selected']['reason']=='STOP_LOSS_6_6_ALL_TQQQ' and not journal.load('tqqq_stop_latch'):
   with journal.transaction():
    evidence=dict(signal_date=signal['date'],triggered_at=now.isoformat(),scope='ALL_ACCOUNT_SHARES',average_cost=snapshot['tqqq_average_cost'])
    journal.save('tqqq_stop_latch',evidence);journal.audit('TQQQ_STOP_TRIGGERED',evidence)
  elif result['selected']['symbol']=='TQQQ' and result['selected']['side']=='BUY' and journal.load('tqqq_stop_latch'):
   with journal.transaction():
    journal.audit('TQQQ_STOP_RECOVERED',journal.load('tqqq_stop_latch'));journal.db.execute("DELETE FROM etf_meta WHERE key='tqqq_stop_latch'")
  from .execution import execute
  def replan(fresh,exclude_intent=None):
   fresh['intraday']=minute_context
   return plan(fresh,signal,journal,now=now,exclude_intent=exclude_intent,risk_only=risk_only,evaluation_time=datetime.now(timezone.utc))
  execution=execute(journal,broker,result['selected']['decision_id'],result,replan=replan,live=True,window=window)
  actual_plan=json.loads(journal.get(execution['intent_id'])['plan']) if execution.get('intent_id') else result['selected']
  results.append(dict(plan=actual_plan,execution=execution))
  # Always re-read next iteration. Never assume an accepted response is a fill.
  if execution['status'] in ('EXECUTION_UNKNOWN','AWAITING_USER_APPROVAL','WAIT'):
   result['cash_basis']='PRE_ORDER';result['execution']=execution;result['executions']=results;return result
 result['cash_basis']='PRE_ORDER';result['executions']=results;result['reason']='BOUNDED_CYCLE_COMPLETE';return result

def report(result):
 trend='长期上涨条件成立' if result.get('phase')=='ATTACK' else '长期上涨条件未成立' if result.get('phase')=='DEFENSIVE' else '数据待核实'
 lines=['ETF Agent | '+POLICY['version'],'市场行情：'+trend]
 period=result.get('slow_ma_period',POLICY.get('slow_ma_period',100))
 if 'sma50' in result:lines.append('QQQ 均线：50日 '+format(Decimal(result['sma50']),'.2f')+' / '+str(period)+'日 '+format(Decimal(result['sma'+str(period)]),'.2f')+'（'+result['signal_date']+'收盘）')
 selected=result.get('selected')
 if result.get('executions'):lines.append('信号：'+' / '.join(item['plan']['side']+' '+item['plan']['symbol'] for item in result['executions']))
 else:lines.append('信号：'+(selected['side']+' '+selected['symbol']+' $'+selected['amount'] if selected else 'WAIT'))
 if 'safe_cash' in result:lines.append(('下单前可用自有资金：$' if result.get('cash_basis')=='PRE_ORDER' else '可用自有资金：$')+result['safe_cash'])
 for item in result.get('executions',[]):
  order=item['plan'];execution=item['execution'];price=order.get('price')
  lines.append(order['symbol']+' '+order['side']+' 计划 $'+order['amount']+' | 参考价 $'+format(Decimal(price),'.4f')+' | '+execution['status'])
 lines.append('原因：'+str(result.get('reason','待核实')))
 return '\n'.join(lines)

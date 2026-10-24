import copy
from datetime import datetime,timezone,timedelta
from decimal import Decimal
import pytest
from etf.cycle import advance,plan,authorize_order,money
from etf.funds import normalize
from etf.journal import Journal
from etf.broker import GuardedTransport
from etf.execution import execute
NOW=datetime.now(timezone.utc)
def raw(cash='100',sgov='100',smh='700',qqq='200'):
 values={'SMH':smh,'QQQ':qqq,'SGOV':sgov}
 return dict(source='broker_mcp',scope='ETF_ONLY',account_hash='a'*64,as_of=NOW.isoformat(),complete_orders=True,orders=[],reported_net_assets=str(sum(Decimal(v) for v in values.values())+Decimal(cash)),positions={s:{'quantity':str(Decimal(v)/10),'shares_available_for_sells':str(Decimal(v)/10)} for s,v in values.items()},quotes={s:dict(last='10',bid='9.99',ask='10.01',source='broker_mcp',timestamp=NOW.isoformat(),is_realtime=True,is_stale=False) for s in values},funds=dict(components_verified=True,settled_own_cash=cash,margin_used='0',reported_cash=cash,account_type='margin',settlements=[],pending_external_credits='0',pending_credits_in_reported_cash=False,unleveraged_buying_power='10000'))
def snap(**kw):return normalize(raw(**kw),now=NOW)
def phase(name='NORMAL'):return dict(phase=name,episode_id='p1')
def context():return dict(policy_version='cash_regime_v2',mode='RISK_RETENTION',observation_date='2026-10-13',episode_id='p1',depth={'SMH':.10,'QQQ':.10},stages={s:[dict(id='D10',drawdown='0.1',fraction='0.3',authorized_cents=10000,filled_cents=0,reserved_cents=0,eligible=True)] for s in ['SMH','QQQ']})
def propose(snapshot=None,state=None,**kwargs):return plan(snapshot or snap(),state or phase(),{'SMH':80,'QQQ':80},context(),buy_remaining=kwargs.pop('buy_remaining',100),sell_remaining=kwargs.pop('sell_remaining',100),**kwargs)
@pytest.mark.parametrize('symbol,side',[('SMH','SELL'),('QQQ','SELL'),('ARKG','BUY'),('SGOV','SHORT'),('SPY','BUY')])
def test_permission_guard(symbol,side):
 with pytest.raises(ValueError):authorize_order(symbol,side)
@pytest.mark.parametrize('symbol,side',[('SMH','BUY'),('QQQ','BUY'),('SGOV','BUY'),('SGOV','SELL')])
def test_authorized(symbol,side):authorize_order(symbol,side)
def test_bp_does_not_become_own_cash():assert money(snap()['safe_cash'])==Decimal('99.75')
@pytest.mark.parametrize('key',['margin_used','settled_own_cash','pending_external_credits','pending_credits_in_reported_cash'])
def test_missing_fund_fact_fails(key):
 r=raw();del r['funds'][key]
 with pytest.raises((ValueError,KeyError)):normalize(r,now=NOW)
def test_existing_margin_blocks():
 r=raw();r['funds']['margin_used']='0.01'
 with pytest.raises(ValueError,match='margin_used'):normalize(r,now=NOW)
def flow(id='s',amount='50',day='2026-10-15',kind='SALE'):return dict(id=id,net_amount=amount,settlement_date=day,source='broker',status='executed',kind=kind,reusable_now=True)
def test_filled_sale_reused_once():
 r=raw(cash='50');r['funds']['settled_own_cash']='0';r['funds']['settlements']=[flow()]
 n=normalize(r,now=NOW);assert n['cash_for_weight']=='50' and n['safe_cash']=='49.75'
def test_non_reusable_sale_only_in_nav():
 r=raw(cash='50');r['funds']['settled_own_cash']='0';f=flow();f['reusable_now']=False;r['funds']['settlements']=[f]
 assert normalize(r,now=NOW)['safe_cash']=='0.00'
def test_duplicate_proceeds_fail():
 r=raw(cash='50');r['funds']['settled_own_cash']='0';r['funds']['settlements']=[flow(),flow()]
 with pytest.raises(ValueError,match='duplicate'):normalize(r,now=NOW)
def test_projected_cash_shortfall_precedes_later_sale():
 r=raw(cash='0');r['funds']['settled_own_cash']='0';r['funds']['settlements']=[flow(day='2026-10-16'),flow(id='b',day='2026-10-15',kind='BUY_PAYABLE')]
 with pytest.raises(ValueError,match='projected_margin'):normalize(r,now=NOW)
def test_pending_deposit_excluded():
 r=raw(cash='50');r['funds'].update(settled_own_cash='0',pending_external_credits='50',pending_credits_in_reported_cash=True)
 n=normalize(r,now=NOW);assert n['safe_cash']=='0.00' and money(n['net_assets'])==1000
def obs(day,risk=False,recovery=False,normal=False,failed=False):return dict(date=day,risk=risk,recovery=recovery,normal=normal,failed_reversal=failed)
def test_same_date_cannot_count_three_times():
 o=obs('2026-10-01',risk=True);a=advance(None,o,release_complete=False)
 assert advance(a,o,release_complete=False)==a and a['phase']=='NORMAL'
 assert advance(a,obs('2026-10-02',risk=True),release_complete=False)['phase']=='PULLBACK'
def test_changed_same_date_rejected():
 a=advance(None,obs('2026-10-01',risk=True),release_complete=False)
 with pytest.raises(ValueError):advance(a,obs('2026-10-01'),release_complete=False)
def test_recovery_needs_three_and_normal_needs_task_complete():
 a=None
 for i in range(1,3):a=advance(a,obs(f'2026-09-0{i}',risk=True),release_complete=False)
 for i in range(3,6):a=advance(a,obs(f'2026-09-0{i}',recovery=True,normal=True),release_complete=False)
 assert a['phase']=='RECOVERY'
 for i in range(6,20):a=advance(a,obs(f'2026-09-{i:02}',recovery=True,normal=True),release_complete=False)
 assert a['phase']=='RECOVERY'
 assert advance(a,obs('2026-09-20',recovery=True,normal=True),release_complete=True)['phase']=='NORMAL'
def test_failed_recovery_does_not_clear_episode():
 a=dict(phase='RECOVERY',last_date=None,risk_count=0,recovery_count=0,normal_count=0,episode_id='p1')
 a=advance(a,obs('2026-09-01',failed=True),release_complete=False);assert a['phase']=='PULLBACK' and a['episode_id']=='p1'
def test_tolerance_is_percentage_points():
 assert propose(snap(cash='0'))['selected'] is None
 assert propose(snap(cash='4',sgov='96'))['selected'] is None
def test_continuing_deposit_rebuilds_sgov():
 r=propose(snap(cash='100',smh='2333.3333',qqq='666.6667',sgov='150'))
 assert r['selected']['symbol']=='SGOV' and money(r['selected']['amount'])<=100
def test_recovery_uses_cash_before_sell():
 r=propose(snap(cash='100',smh='600',sgov='200'),phase('RECOVERY'));assert r['selected']['side']=='BUY' and r['selected']['symbol'] in ('SMH','QQQ')
def test_recovery_sells_even_when_old_score_waits():
 r=plan(snap(cash='0',smh='600',sgov='200'),phase('RECOVERY'),{'SMH':0,'QQQ':0},context(),buy_remaining=100,sell_remaining=100);assert r['selected']['side']=='SELL' and r['selected']['symbol']=='SGOV'
def test_new_deposits_reduce_needed_release():
 r=propose(snap(cash='3100',smh='600',sgov='200'),phase('RECOVERY'));assert r['sgov_release_remaining']=='0.00' and r['selected']['side']=='BUY'
def test_recovery_never_buys_sgov():
 assert propose(snap(cash='100',smh='800',sgov='0'),phase('RECOVERY'),gates={'SMH':'limit','QQQ':'limit'})['selected'] is None
def test_sell_not_reset_buy_limit():assert propose(snap(cash='0',smh='600',sgov='200'),phase('RECOVERY'),buy_remaining=0)['selected'] is None
def test_pullback_core_buy_has_priority():
 r=propose(snap(cash='100',smh='600'),phase('PULLBACK'));assert r['selected']['symbol'] in ('SMH','QQQ') and r['selected']['side']=='BUY'
def test_pullback_retains_sgov_when_cash_sufficient():assert propose(snap(cash='100',smh='600',sgov='200'),phase('PULLBACK'))['selected']['side']=='BUY'
def test_pullback_cash_zero_evaluates_sell():
 r=propose(snap(cash='0',smh='600',sgov='200'),phase('PULLBACK'));assert r['selected']['side']=='SELL' and money(r['selected']['amount'])<=100
def test_cooldown_prevents_rebuy():assert propose(snap(cash='100',smh='800',sgov='0'),sgov_buy_cooled=False,gates={'SMH':'limit','QQQ':'limit'})['selected'] is None
class Transport:
 def __init__(self):self.calls=[]
 def call_tool(self,name,params):self.calls.append((name,params));return {}
def test_readonly_denies_preview_and_submission():
 t=Transport();g=GuardedTransport(t)
 for name in ['review_equity_order','place_equity_order']:
  with pytest.raises(PermissionError):g.call_tool(name,dict(symbol='SGOV',side='sell'))
 assert not t.calls
def test_live_denies_core_sell_before_transport():
 t=Transport();g=GuardedTransport(t,live=True)
 with pytest.raises(ValueError):g.call_tool('place_equity_order',dict(symbol='SMH',side='sell'))
 assert not t.calls
def journal(tmp_path):
 j=Journal(tmp_path/'state.sqlite');j.bind_account('a'*64);return j
def buy():return dict(symbol='SGOV',side='BUY',amount='10.00',reason='NORMAL_TARGET_BUY')
def sell():return dict(symbol='SGOV',side='SELL',amount='10.00',reason='RECOVERY_SGOV_RELEASE')
def order(row,state='filled',qty='1',fid='fill1'):
 return dict(id='broker1',ref_id=row['id'],symbol=row['symbol'],side=row['side'].lower(),state=state,cumulative_quantity=qty,executions=[dict(id=fid,price='10',quantity=qty,fees='0',timestamp=(NOW-timedelta(seconds=2)).isoformat(),currency='USD')] if Decimal(qty)>0 else [])
def test_one_intent_across_restart(tmp_path):
 j=journal(tmp_path);r=j.reserve('d1',buy(),snap());j.begin_submit(r['id']);j.status(r['id'],'EXECUTION_UNKNOWN');j.db.close();j=Journal(tmp_path/'state.sqlite')
 with pytest.raises(ValueError,match='one_unresolved'):j.reserve('d2',buy(),snap())
 j.reconcile([],complete=True);assert j.active()[0]['status']=='RECONCILIATION_REQUIRED'
def test_partial_fill_and_idempotent_fill(tmp_path):
 j=journal(tmp_path);r=j.reserve('d1',buy(),snap());j.begin_submit(r['id']);o=order(r,'partially_filled',qty='.5');j.reconcile([o],complete=True);j.reconcile([o],complete=True)
 assert j.get(r['id'])['status']=='PARTIALLY_FILLED' and j.filled_gross(r['id'])==5 and j.daily_spend('BUY',NOW)==10
 o['state']='filled';j.reconcile([o],complete=True);assert j.get(r['id'])['status']=='FILLED' and j.daily_spend('BUY',NOW)==5
def test_sale_proceeds_not_buy_credit(tmp_path):
 j=journal(tmp_path);r=j.reserve('d1',sell(),snap());j.begin_submit(r['id']);j.reconcile([order(r,qty='.9')],complete=True)
 assert j.daily_spend('BUY',NOW)==0 and j.daily_spend('SELL',NOW)==9 and j.total_flow('SELL')==9
def test_missing_terminal_fill_keeps_reserved(tmp_path):
 j=journal(tmp_path);r=j.reserve('d1',buy(),snap());j.begin_submit(r['id']);o=order(r);o['executions']=[];j.reconcile([o],complete=True);assert j.get(r['id'])['status']=='RECONCILIATION_REQUIRED'
def test_submit_attempt_not_retryable(tmp_path):
 j=journal(tmp_path);r=j.reserve('d1',buy(),snap());j.begin_submit(r['id'])
 with pytest.raises(ValueError,match='submit_once'):j.begin_submit(r['id'])
def test_slot_persisted(tmp_path):
 j=journal(tmp_path);assert j.claim_slot('2026-10-14:10:05');assert not j.claim_slot('2026-10-14:10:05')
class FakeBroker:
 def __init__(self):self.raw=raw();self.submits=0;self.fail=False
 def read(self):return copy.deepcopy(self.raw)
 def review(self,row):return {'data':{'funding_check':{'verified':True,'projected_margin_used':'0','fees':'0'}}}
 def submit(self,row):
  self.submits+=1
  if self.fail:raise TimeoutError()
  return {'data':{'order':{'id':'broker1','state':'queued','symbol':row['symbol'],'side':row['side'].lower()}}}
def test_executor_unknown_never_resubmits(tmp_path):
 j=journal(tmp_path);b=FakeBroker();b.fail=True;result={'selected':buy(),'reason':'NORMAL_TARGET_BUY'}
 def repl(s,**kw):return copy.deepcopy(result)
 assert execute(j,b,'d1',result,replan=repl,live=True)['status']=='EXECUTION_UNKNOWN' and b.submits==1
 execute(j,b,'d2',result,replan=repl,live=True);assert b.submits==1
def test_before_submit_changed_funds(tmp_path):
 j=journal(tmp_path);b=FakeBroker();result={'selected':buy(),'reason':'NORMAL_TARGET_BUY'}
 def repl(s,**kw):return {'selected':None,'reason':'changed'} if kw else copy.deepcopy(result)
 with pytest.raises(ValueError):execute(j,b,'d1',result,replan=repl,live=True)
 assert b.submits==0 and not j.active()

def test_qqq_second_buy_uses_first_qqq_score(tmp_path):
 j=journal(tmp_path);o=dict(symbol='QQQ',side='BUY',amount='10.00',score=60,all_scores={'QQQ':60,'SMH':20},reason='NORMAL_TARGET_BUY')
 r=j.reserve('q1',o,snap());j.begin_submit(r['id']);j.reconcile([order(r)],complete=True)
 assert not j.gates({'QQQ':60,'SMH':20},NOW).get('QQQ')
 assert j.gates({'QQQ':30,'SMH':90},NOW).get('QQQ')

def test_rejected_attempt_still_consumes_symbol_gate(tmp_path):
 j=journal(tmp_path);o=dict(symbol='SMH',side='BUY',amount='10.00',score=60,all_scores={'QQQ':60,'SMH':60},reason='NORMAL_TARGET_BUY')
 r=j.reserve('s1',o,snap());j.begin_submit(r['id']);j.status(r['id'],'REJECTED')
 assert j.gates({'QQQ':60,'SMH':60},NOW)['SMH']=='SMH_DAILY_BUY_LIMIT'

def test_external_manual_activity_not_added_to_agent_budget(tmp_path):
 j=journal(tmp_path)
 j.reconcile([dict(id='external',symbol='SGOV',side='sell',state='filled',cumulative_quantity='1',executions=[])],complete=True)
 assert not j.active() and j.daily_spend('BUY',NOW)==0 and j.total_flow('SELL')==0

@pytest.mark.parametrize('bad', ['NaN','Infinity','-1',True,None])
def test_invalid_money(bad):
 with pytest.raises((ValueError,ArithmeticError)):money(bad)

def test_stale_account_stops(tmp_path):
 r=raw();r['as_of']=(NOW-timedelta(minutes=2)).isoformat()
 with pytest.raises(ValueError,match='stale'):normalize(r,now=NOW)

def test_open_order_stops():
 r=raw();r['orders']=[{'state':'queued'}]
 with pytest.raises(ValueError,match='open'):normalize(r,now=NOW)

def test_share_short_sale_cannot_be_reserved(tmp_path):
 j=journal(tmp_path);s=snap(sgov='1');o=sell()
 with pytest.raises(ValueError,match='sell_share_limit'):j.reserve('s1',o,s)

def test_changed_fill_evidence_remains_blocked(tmp_path):
 j=journal(tmp_path);r=j.reserve('d1',buy(),snap());j.begin_submit(r['id']);o=order(r,'partially_filled',qty='.5');j.reconcile([o],complete=True)
 o['executions'][0]['price']='9';j.reconcile([o],complete=True)
 assert j.get(r['id'])['status']=='RECONCILIATION_REQUIRED' and j.filled_gross(r['id'])==5

def test_legacy_tables_all_preserved(tmp_path):
 import sqlite3
 from etf.journal import clone_legacy
 old=tmp_path/'old.sqlite';new=tmp_path/'new.sqlite'
 with sqlite3.connect(old) as db:db.executescript("CREATE TABLE custom_history(value TEXT);INSERT INTO custom_history VALUES('forward fact');")
 clone_legacy(old,new);j=Journal(new)
 assert j.db.execute('SELECT value FROM custom_history').fetchone()[0]=='forward fact'
 with pytest.raises(ValueError):clone_legacy(old,new)

def test_excluded_reservation_not_subtracted_twice(tmp_path):
 j=journal(tmp_path);r=j.reserve('d1',buy(),snap())
 assert j.daily_spend('BUY',NOW)==10 and j.daily_spend('BUY',NOW,exclude_intent=r['id'])==0

def test_sale_does_not_create_stage_capital():
 from strategies.qqq_smh_agent.cash_regime_v2 import add_cash_capacity,advance as old_advance
 from etf.runtime import OLD_POLICY
 o=dict(date='2026-10-01',risk=True,clear=False,volatility_pressure={'VIX':True,'VXN':True},symbols={s:dict(price=90,peak60=100,weak=True,strong=False,recovering=False) for s in ['SMH','QQQ']})
 st=old_advance(None,o,OLD_POLICY);o['date']='2026-10-02';st=old_advance(st,o,OLD_POLICY)
 gaps={'SMH':70,'QQQ':20};st=add_cash_capacity(st,10000,0,gaps,OLD_POLICY);grants=copy.deepcopy(st['grants'])
 # Internal sale: +$50 cash, -$50 cumulative net sales -> same mass.
 st=add_cash_capacity(st,15000,-5000,gaps,OLD_POLICY);assert st['grants']==grants
 st=add_cash_capacity(st,15000,5000,gaps,OLD_POLICY);assert st['grants']!=grants

def test_pending_partial_buy_reservation_includes_fees_once(tmp_path):
 j=journal(tmp_path);r=j.reserve('d1',buy(),snap());j.begin_submit(r['id']);o=order(r,'partially_filled',qty='.5');o['executions'][0]['fees']='.10';j.reconcile([o],complete=True)
 assert j.daily_spend('BUY',NOW)==10

def test_fuzz_plans_remain_bounded():
 import random
 random.seed(19)
 for _ in range(500):
  cash=str(random.randint(0,1000));values={s:str(random.randint(0,3000)) for s in ['smh','qqq','sgov']}
  if sum(map(int,values.values()))+int(cash)==0:continue
  s=snap(cash=cash,**values)
  for mode in ['NORMAL','PULLBACK','RECOVERY']:
   r=propose(s,phase(mode));o=r['selected']
   if o:
    authorize_order(o['symbol'],o['side']);assert 1<=money(o['amount'])<=100
    if o['side']=='BUY':assert money(o['amount'])<=money(s['safe_cash'])
    else:assert money(o['amount'])<=money(s['values']['SGOV'])

def test_runtime_daily_state_survives_same_day_restart(tmp_path,monkeypatch):
 from etf.runtime import signals
 from etf.history import load
 from tools.volatility_context import VolatilityProvider,unavailable_context
 from pathlib import Path
 dates,frames=load(Path(__file__).resolve().parents[1]/'data')
 monkeypatch.setattr(VolatilityProvider,'fetch',lambda self:unavailable_context('test'))
 j=journal(tmp_path);p1,score1,c1=signals(dates,frames,j,snap());p2,score2,c2=signals(dates,frames,j,snap())
 assert p1==p2 and score1==score2 and c1==c2

def test_net_deposit_and_dividend_audited_once(tmp_path):
 j=journal(tmp_path)
 events=[dict(id='deposit1',source='broker',status='completed',kind='DEPOSIT',net_amount='100',received_at=(NOW-timedelta(seconds=2)).isoformat()),dict(id='div1',source='broker',status='completed',kind='DIVIDEND',net_amount='4.17',received_at=(NOW-timedelta(seconds=2)).isoformat())]
 j.observe_external_cash_events(events);j.observe_external_cash_events(events)
 assert j.db.execute('SELECT COUNT(*) FROM etf_funding_events').fetchone()[0]==2
 assert j.daily_spend('BUY',NOW)==0 and snap()['safe_cash']=='99.75'
 events[0]['net_amount']='200'
 with pytest.raises(ValueError,match='conflict'):j.observe_external_cash_events(events)

def test_future_or_pending_deposit_not_credited(tmp_path):
 j=journal(tmp_path)
 event=dict(id='d1',source='broker',status='pending',kind='DEPOSIT',net_amount='100',received_at=NOW.isoformat())
 with pytest.raises(ValueError):j.observe_external_cash_events([event])
 assert j.db.execute('SELECT COUNT(*) FROM etf_funding_events').fetchone()[0]==0

def limited_raw(cash='100',unsettled='0'):
 r=raw(cash=cash);r.update(account_type='limited_margin',agentic_confirmed=True)
 r['funds']={k:r['funds'][k] for k in ['reported_cash','pending_external_credits','unleveraged_buying_power']}
 r['funds'].update(account_type='limited_margin',unsettled_funds=unsettled)
 return r

def test_real_limited_margin_contract_requires_no_synthetic_balance_fields():
 n=normalize(limited_raw(),now=NOW)
 assert n['safe_cash']=='99.75' and n['margin_evidence']=='verified_agentic_limited_margin_no_borrowing_capability'

def test_limited_margin_sale_proceeds_not_added_twice():
 n=normalize(limited_raw(cash='50',unsettled='50'),now=NOW)
 assert n['cash_for_weight']=='50' and n['safe_cash']=='49.75'

def test_limited_margin_pending_deposit_waits():
 r=limited_raw();r['funds']['pending_external_credits']='20'
 with pytest.raises(ValueError,match='pending_external_credit_wait'):normalize(r,now=NOW)

def test_limited_account_identity_required():
 r=limited_raw();r['agentic_confirmed']=False
 with pytest.raises(ValueError,match='unverified'):normalize(r,now=NOW)

def test_ordinary_margin_cannot_use_limited_capability():
 r=limited_raw();r['account_type']='margin';r['funds']['account_type']='margin'
 with pytest.raises(ValueError,match='components_unverified'):normalize(r,now=NOW)

def test_borrowing_evidence_conflict_blocks_limited():
 r=limited_raw();r['funds']['margin_used']='1'
 with pytest.raises(ValueError,match='conflict'):normalize(r,now=NOW)

def test_native_quote_source_is_accepted():
 r=limited_raw()
 for q in r['quotes'].values():q['source']='robinhood_mcp'
 assert normalize(r,now=NOW)['safe_cash']=='99.75'

def test_limited_buying_power_remains_cap():
 r=limited_raw();r['funds']['unleveraged_buying_power']='40'
 assert normalize(r,now=NOW)['safe_cash']=='39.75'

def native_preview(row):
 return {'data':dict(symbol=row['symbol'],side=row['side'].lower(),type='market',dollar_amount=row['amount'],order_checks={},quote_data=dict(state='active',has_traded=True,ask_price='10.01',bid_price='9.99'))}

def test_native_preview_contract():
 from etf.execution import preview_reason
 s=normalize(limited_raw(),now=NOW)
 for row in [buy(),sell()]:assert preview_reason(native_preview(row),row,s) is None

@pytest.mark.parametrize('failure',['symbol','amount','warning','quote'])
def test_native_preview_rejects_mismatch(failure):
 from etf.execution import preview_reason
 r=buy();p=native_preview(r);s=normalize(limited_raw(),now=NOW)
 if failure=='symbol':p['data']['symbol']='QQQ'
 elif failure=='amount':p['data']['dollar_amount']='11'
 elif failure=='warning':p['data']['order_checks']={'warning':'needs review'}
 else:p['data']['quote_data']={}
 assert preview_reason(p,r,s)

def test_halted_journal_cannot_reserve(tmp_path):
 j=journal(tmp_path);j.save('halt_reason','cash mismatch')
 with pytest.raises(ValueError,match='halted'):j.reserve('d1',buy(),snap())

def test_bootstrap_has_current_episode(tmp_path,monkeypatch):
 from etf.runtime import signals
 from etf.history import load
 from tools.volatility_context import VolatilityProvider,unavailable_context
 from pathlib import Path
 dates,frames=load(Path(__file__).resolve().parents[1]/'data')
 monkeypatch.setattr(VolatilityProvider,'fetch',lambda self:unavailable_context('test'))
 p,_,_=signals(dates,frames,journal(tmp_path),snap())
 assert not p.get('episode_id','').startswith('pullback:2016')

def test_native_customer_approval_flag():
 from tools.approval_compatibility import approval_required
 assert approval_required({'data':{'customer_approval_required':True}})

def test_verified_terminal_fill_revision_halts(tmp_path):
 from etf.reconciliation import validate_history
 j=journal(tmp_path);r=j.reserve('d1',buy(),snap());j.begin_submit(r['id']);o=order(r);j.reconcile([o],complete=True)
 validate_history(j,dict(complete_orders=True,account_hash='a'*64,orders=[o]))
 o['executions'][0]['price']='9'
 with pytest.raises(ValueError,match='changed'):validate_history(j,dict(complete_orders=True,account_hash='a'*64,orders=[o]))

@pytest.mark.parametrize('side',['BUY','SELL'])
def test_native_executor_submits_once_and_recognizes_approval(tmp_path,side):
 class NativeFake(FakeBroker):
  def __init__(self):super().__init__();self.raw=limited_raw()
  def review(self,row):return native_preview(row)
  def submit(self,row):
   self.submits+=1
   return {'data':{'customer_approval_required':True,'order':{'id':'broker1'}}}
 j=journal(tmp_path);b=NativeFake();result={'selected':buy() if side=='BUY' else sell(),'reason':'test'}
 repl=lambda s,**kw:copy.deepcopy(result)
 assert execute(j,b,'d1',result,replan=repl,live=True)['status']=='AWAITING_USER_APPROVAL'
 execute(j,b,'d2',result,replan=repl,live=True)
 assert b.submits==1 and len(j.active())==1

def test_native_executor_warning_blocks_submission(tmp_path):
 class NativeFake(FakeBroker):
  def __init__(self):super().__init__();self.raw=limited_raw()
  def review(self,row):
   p=native_preview(row);p['data']['order_checks']={'warning':'blocked'};return p
 j=journal(tmp_path);b=NativeFake();result={'selected':buy(),'reason':'test'}
 assert execute(j,b,'d1',result,replan=lambda s,**kw:result,live=True)['reason']=='broker_pretrade_warning'
 assert b.submits==0 and not j.active()



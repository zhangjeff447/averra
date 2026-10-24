import copy,json,sqlite3,hashlib,subprocess,sys,plistlib
from pathlib import Path
from datetime import datetime,timezone,timedelta
from decimal import Decimal as D
import pytest
from etf.cycle import POLICY,authorize_order,plan
from etf.funds import normalize
from etf.journal import Journal,clone_legacy
from etf.runtime import cycle,checked
from etf.broker import GuardedTransport,Broker
from etf.execution import execute,preview_reason
from etf.calendar import previous_session,scheduled,settlement_day,funding_session,ET
NOW=datetime.now(timezone.utc)
DAY=NOW.astimezone(ET).date().isoformat()
def raw(cash='1000',values=None):
 values=values or dict(SMH='0',QQQ='0',TQQQ='0')
 return dict(source='broker_mcp',scope='ETF_ONLY',account_hash='a'*64,as_of=datetime.now(timezone.utc).isoformat(),complete_orders=True,orders=[],reported_net_assets=str(sum(D(v) for v in values.values())+D(cash)),account_type='limited_margin',agentic_confirmed=True,pending_approvals=[],positions={s:dict(average_buy_price='10',quantity=str(D(v)/10),shares_available_for_sells=str(D(v)/10)) for s,v in values.items()},quotes={s:dict(last='10',bid='9.99',ask='10.01',source='robinhood_mcp',timestamp=datetime.now(timezone.utc).isoformat(),is_realtime=True,is_stale=False) for s in values},funds=dict(reported_cash=cash,pending_external_credits='0',unleveraged_buying_power='100000',unsettled_funds='0',account_type='limited_margin',margin_used='0'))
def sig(on=True):return dict(date=previous_session(DAY),sma50='110' if on else '90',sma100='100',close='110')
def journal(tmp_path):
 j=Journal(tmp_path/'account.sqlite');j.bind_account('a'*64);return j
def buy(symbol='SMH',amount='200.00',reason='ATTACK_CORE'):
 return dict(symbol=symbol,side='BUY',amount=amount,reason=reason,policy_version=POLICY['version'])
def sell(qty='10',amount='99.90'):
 return dict(symbol='TQQQ',side='SELL',amount=amount,quantity=qty,reason='TREND_EXIT',policy_version=POLICY['version'])
def native_preview(row):
 sizing=dict(quantity=row['quantity']) if row['side']=='SELL' else dict(dollar_amount=row['amount'])
 return {'data':dict(symbol=row['symbol'],side=row['side'].lower(),type='market',order_checks={},quote_data=dict(state='active',has_traded=True,ask_price='10.01',bid_price='9.99'),**sizing)}
class FakeBroker:
 def __init__(self,cash='1000',values=None):self.raw=raw(cash,values);self.submits=0;self.reviews=0;self.mode='fill';self.bad_preview=False
 def read(self):
  self.raw['as_of']=datetime.now(timezone.utc).isoformat()
  for q in self.raw['quotes'].values():q['timestamp']=self.raw['as_of']
  self.raw['reported_net_assets']=str(D(self.raw['funds']['reported_cash'])+sum(D(x['quantity'])*10 for x in self.raw['positions'].values()))
  return copy.deepcopy(self.raw)
 def review(self,row):
  self.reviews+=1;r=native_preview(row)
  if self.bad_preview:r['data']['order_checks']={'warning':'not approved'}
  return r
 def submit(self,row):
  self.submits+=1
  if self.mode=='timeout':raise TimeoutError()
  if self.mode=='approval':return {'data':{'approval_required':True}}
  q=D(row['quantity']) if row['side']=='SELL' else D(row['amount'])/10
  if self.mode=='partial':q/=2
  net=q*10;pos=self.raw['positions'][row['symbol']]
  pos['quantity']=str(D(pos['quantity'])+q*(1 if row['side']=='BUY' else -1));pos['shares_available_for_sells']=pos['quantity']
  self.raw['funds']['reported_cash']=str(D(self.raw['funds']['reported_cash'])+net*(-1 if row['side']=='BUY' else 1))
  if row['side']=='SELL':self.raw['funds']['unsettled_funds']=str(D(self.raw['funds']['unsettled_funds'])+net)
  o=dict(id='broker'+str(self.submits),ref_id=row['id'],symbol=row['symbol'],side=row['side'].lower(),state='partially_filled' if self.mode=='partial' else 'filled',cumulative_quantity=str(q),executions=[dict(id='fill'+str(self.submits),quantity=str(q),price='10',fees='0',timestamp=(datetime.now(timezone.utc)-timedelta(milliseconds=1)).isoformat(),currency='USD')])
  self.raw['orders'].append(o)
  return {'data':{'order':copy.deepcopy(o)}}

@pytest.mark.parametrize('symbol,side',[('QQQ','SELL'),('SMH','SELL'),('SGOV','BUY'),('SCHD','BUY'),('TQQQ','SHORT')])
def test_permissions(symbol,side):
 with pytest.raises(ValueError):authorize_order(symbol,side)
@pytest.mark.parametrize('symbol,side',[('QQQ','BUY'),('SMH','BUY'),('TQQQ','BUY'),('TQQQ','SELL')])
def test_allowed(symbol,side):authorize_order(symbol,side)
def test_large_order_is_own_cash_bounded(tmp_path):
 j=journal(tmp_path);s=normalize(raw());r=j.reserve('large',buy(amount='500.00'),s);assert r['amount']=='500.00'
def test_large_cash_overrun_rejected(tmp_path):
 j=journal(tmp_path)
 with pytest.raises(ValueError,match='cash_limit'):j.reserve('large',buy(amount='1000.00'),normalize(raw()))
def test_sale_uses_exact_available_shares(tmp_path):
 j=journal(tmp_path);s=normalize(raw(values=dict(SMH='0',QQQ='0',TQQQ='100')))
 r=j.reserve('sell',sell(qty='7'),s);assert r['quantity']=='7'
 with pytest.raises(ValueError):j.reserve('other',sell(qty='11'),s)
def test_oversell_without_active_intent(tmp_path):
 j=journal(tmp_path)
 with pytest.raises(ValueError,match='sell_share_limit'):j.reserve('sell',sell(qty='11'),normalize(raw(values=dict(SMH='0',QQQ='0',TQQQ='100'))))
def test_preview_then_native_fill_loop(tmp_path):
 j=journal(tmp_path);b=FakeBroker();result=cycle(j,b,sig(),live=True,now=NOW)
 assert b.submits==3;assert not j.active();assert {x['plan']['symbol'] for x in result['executions']}=={'SMH','QQQ','TQQQ'}
 assert all(x['side']=='buy' for x in b.raw['orders']);assert sum(D(f[0]) for f in j.db.execute('SELECT gross FROM etf_fills'))<=D('999.75')
 before=b.submits;cycle(j,b,sig(),live=True,now=NOW);assert b.submits==before
 j.db.close();j=Journal(tmp_path/'account.sqlite');cycle(j,b,sig(),live=True,now=NOW);assert b.submits==before
 assert all(r[0]==1 for r in j.db.execute('SELECT attempts FROM etf_intents'))
def test_readonly_never_reviews_or_submits(tmp_path):
 j=journal(tmp_path);b=FakeBroker();r=cycle(j,b,sig(),live=False,now=NOW);assert r['selected']['symbol']=='TQQQ';assert b.submits==b.reviews==0
 assert not j.db.execute('SELECT 1 FROM etf_intents').fetchone()
def test_unknown_submit_survives_restart_no_retry(tmp_path):
 j=journal(tmp_path);b=FakeBroker();b.mode='timeout';cycle(j,b,sig(),live=True,now=NOW);assert b.submits==1;assert j.active()[0]['status']=='EXECUTION_UNKNOWN'
 j.db.close();j=Journal(tmp_path/'account.sqlite');cycle(j,b,sig(),live=True,now=NOW);assert b.submits==1;assert j.active()[0]['status']=='RECONCILIATION_REQUIRED'
def test_approval_blocks_without_resubmit(tmp_path):
 j=journal(tmp_path);b=FakeBroker();b.mode='approval';r=cycle(j,b,sig(),live=True,now=NOW);assert r['execution']['status']=='AWAITING_USER_APPROVAL'
 cycle(j,b,sig(),live=True,now=NOW);assert b.submits==1
def test_partial_fill_reserves_remainder(tmp_path):
 j=journal(tmp_path);b=FakeBroker();b.mode='partial'
 with pytest.raises(ValueError,match='open_or_unknown'):cycle(j,b,sig(),live=True,now=NOW)
 assert b.submits==1;assert j.active()[0]['status']=='PARTIALLY_FILLED';assert j.filled_gross(j.active()[0]['id'])>0
 with pytest.raises(ValueError):cycle(j,b,sig(),live=True,now=NOW)
 assert b.submits==1
def test_pre_submit_crash_recovery_only_zero_attempt(tmp_path):
 j=journal(tmp_path);s=normalize(raw());r=j.reserve('abandoned',buy(),s);j.recover_unsubmitted();assert j.get(r['id'])['status']=='REJECTED';assert not j.action_used('abandoned')
 r=j.reserve('attempted',buy(),s);j.begin_submit(r['id']);j.recover_unsubmitted();assert j.get(r['id'])['status']=='SUBMITTING'
def test_sale_proceeds_not_immediately_used(tmp_path):
 j=journal(tmp_path);b=FakeBroker(cash='100',values=dict(SMH='600',QQQ='100',TQQQ='200'));r=cycle(j,b,sig(False),live=True,now=NOW)
 assert b.submits==1;assert b.raw['orders'][0]['side']=='sell';assert j.rotation_available(DAY)==0;assert j.unsettled_sale_cash(DAY)==200
 assert normalize(b.read(),held_cash=j.unsettled_sale_cash(DAY))['safe_cash']=='99.75'
def test_settlement_recorded_once(tmp_path):
 j=journal(tmp_path);b=FakeBroker(values=dict(SMH='600',QQQ='100',TQQQ='200'));cycle(j,b,sig(False),live=True,now=NOW)
 j.sync_rotation();j.sync_rotation();assert j.db.execute('SELECT COUNT(*) FROM rf_rotation').fetchone()[0]==1
 release=j.db.execute('SELECT release_date FROM rf_rotation').fetchone()[0];assert j.rotation_available(release)==200
 assert j.unsettled_sale_cash(release)==0
def test_bp_and_future_deposit_not_cash():
 r=raw(cash='50');r['funds']['unleveraged_buying_power']='1000000';assert normalize(r)['safe_cash']=='49.75'
 r['funds']['pending_external_credits']='100';
 with pytest.raises(ValueError,match='pending_external_credit_wait'):normalize(r)
def test_broker_and_local_unsettled_not_double_subtracted():
 r=raw(cash='100');r['funds']['unsettled_funds']='40';assert normalize(r,held_cash='40')['safe_cash']=='59.75'
 assert normalize(r,held_cash='60')['safe_cash']=='39.75'
@pytest.mark.parametrize('change,reason',[('margin','conflict'),('stale','stale'),('spread','spread'),('orders','open_or_unknown')])
def test_funds_fail_closed(change,reason):
 r=raw()
 if change=='margin':r['funds']['margin_used']='1'
 if change=='stale':r['quotes']['TQQQ']['timestamp']=(NOW-timedelta(minutes=5)).isoformat()
 if change=='spread':r['quotes']['TQQQ']['ask']='20'
 if change=='orders':r['orders']=[{'state':'queued'}]
 with pytest.raises(ValueError,match=reason):normalize(r)
def test_quote_and_history_only_completed(tmp_path):
 j=journal(tmp_path);b=FakeBroker();s=sig();s['date']=DAY
 with pytest.raises(ValueError,match='signal_session_stale'):cycle(j,b,s)
 assert not b.submits
def test_changed_daily_input_rejected(tmp_path):
 j=journal(tmp_path);b=FakeBroker();cycle(j,b,sig(),live=True,now=NOW);s=sig();s['sma50']='111'
 with pytest.raises(ValueError,match='signal_changed'):cycle(j,b,s,live=True,now=NOW)
def test_preview_warning_never_submits(tmp_path):
 j=journal(tmp_path);b=FakeBroker();b.bad_preview=True;cycle(j,b,sig(),live=True,now=NOW);assert b.submits==0;assert j.halt_reason()
def test_sell_preview_is_share_based():
 s=normalize(raw());o=sell();assert preview_reason(native_preview(o),o,s) is None
 p=native_preview(o);p['data']['quantity']='11';assert preview_reason(p,o,s)=='preview_quantity_mismatch'
class Transport:
 def __init__(self):self.calls=[]
 def call_tool(self,n,p):self.calls.append((n,p));return {}
 def list_tools(self):return []
def test_transport_readonly_blocks_all_writes():
 t=Transport();g=GuardedTransport(t)
 for n in ['review_equity_order','place_equity_order']:
  with pytest.raises(PermissionError):g.call_tool(n,{})
 assert not t.calls
def test_transport_quantity_sell_and_no_other_actions():
 t=Transport();g=GuardedTransport(t,live=True);base=dict(symbol='TQQQ',side='sell',type='market',market_hours='regular_hours')
 g.call_tool('place_equity_order',dict(base,quantity='2.5'));assert len(t.calls)==1
 with pytest.raises(PermissionError):g.call_tool('place_equity_order',dict(base,dollar_amount='25'))
 with pytest.raises(ValueError):g.call_tool('place_equity_order',dict(base,symbol='QQQ',quantity='1'))
 with pytest.raises(PermissionError):g.call_tool('cancel_equity_order',{})
def test_broker_parameters_no_notional_sell():
 b=Broker(None,expected_account_hash='a'*64);b.account='fixture'
 o=dict(sell(),id='ref');p=b.order_params(o);assert p['quantity']=='10' and 'dollar_amount' not in p and p['ref_id']=='ref'
 assert 'ref_id' not in b.order_params(o,preview=True)
def test_missing_schema_blocks_before_any_order():
 class Adapter:transport=Transport()
 with pytest.raises(ValueError,match='schema'):Broker(Adapter(),expected_account_hash='a'*64).verify_order_schema()
def test_duplicate_decision_is_atomic(tmp_path):
 j=journal(tmp_path);s=normalize(raw());r=j.reserve('same',buy(),s);j.status(r['id'],'REJECTED')
 with pytest.raises(ValueError,match='session_action'):j.reserve('same',buy(),s)
def test_legacy_snapshot_every_table_preserved(tmp_path):
 old=tmp_path/'old.sqlite'
 with sqlite3.connect(old) as db:db.executescript("CREATE TABLE my_history(value TEXT);INSERT INTO my_history VALUES('retained');")
 clone_legacy(old,tmp_path/'new.sqlite');j=Journal(tmp_path/'new.sqlite');assert j.db.execute('SELECT * FROM my_history').fetchone()[0]=='retained'
 with pytest.raises(ValueError):clone_legacy(old,tmp_path/'new.sqlite')
def test_migration_stages_only_and_preserves_halt(tmp_path):
 import deployment
 source=tmp_path/'old';(source/'config').mkdir(parents=True);(source/'state').mkdir()
 (source/'config/runtime.json').write_text(json.dumps(dict(deployment_authorized=True,account_hash='a'*64,credential_config='/private/reference-only',policy_hash='fixture-old-policy')))
 (source/'state/live_execution_state.json').write_text(json.dumps(dict(trading_halted=True)))
 with sqlite3.connect(source/'state/v1_state.sqlite') as db:db.executescript("CREATE TABLE preserved(x TEXT);INSERT INTO preserved VALUES('cash fact');")
 target=deployment.prepare(source,tmp_path/'new');cfg=json.loads((target/'config/runtime.json').read_text());assert cfg['deployment_authorized'] is False
 with sqlite3.connect(target/'state/v1_state.sqlite') as db:assert db.execute('SELECT * FROM preserved').fetchone()[0]=='cash fact'
 assert json.loads((target/'state/live_execution_state.json').read_text())['trading_halted'] is True
 deployment.render_plist(target,Path(sys.executable));plist=plistlib.loads((target/'com.qqqsmh.agent.return-first.plist').read_bytes());assert plist['Label']=='com.qqqsmh.agent.return-first'
 with pytest.raises(ValueError):deployment.prepare(source,source)
def test_early_close_and_dst_and_holiday():
 assert scheduled(datetime(2026,11,27,17,30,tzinfo=timezone.utc))=='12:30'
 assert scheduled(datetime(2026,11,27,20,30,tzinfo=timezone.utc)) is None
 assert scheduled(datetime(2026,10,15,14,5,tzinfo=timezone.utc))=='10:05'
 assert scheduled(datetime(2026,12,15,15,5,tzinfo=timezone.utc))=='10:05'
 assert scheduled(datetime(2026,12,25,15,5,tzinfo=timezone.utc)) is None
 assert funding_session('2026-09-08') is True;assert funding_session('2026-09-09') is False
def test_settlement_skips_weekend_and_bank_holiday():
 assert settlement_day('2026-10-09T19:00:00+00:00')=='2026-10-13'
 assert settlement_day('2026-11-10T19:00:00+00:00')=='2026-11-12'
def test_external_order_and_position_change_block(tmp_path):
 from etf.reconciliation import validate_activation_positions
 j=journal(tmp_path);r=raw();j.save('rf_baseline',dict(orders=[],fills=[],quantities={s:x['quantity'] for s,x in r['positions'].items()}))
 r['positions']['TQQQ']['quantity']='1'
 with pytest.raises(ValueError,match='position_history'):validate_activation_positions(j,r)
 r=raw();r['orders']=[dict(id='foreign',symbol='TQQQ',side='buy',state='filled')]
 with pytest.raises(ValueError,match='unattributed'):validate_activation_positions(j,r)
def test_fill_conflict_stays_blocked(tmp_path):
 j=journal(tmp_path);b=FakeBroker();cycle(j,b,sig(),live=True,now=NOW);b.raw['orders'][0]['executions'][0]['price']='11'
 with pytest.raises(ValueError,match='facts_changed'):cycle(j,b,sig(),live=False,now=NOW)
def test_no_io_before_outside_schedule(monkeypatch):
 import run_agent
 monkeypatch.setattr(sys,'argv',['run_agent.py','--live'])
 monkeypatch.setattr('etf.calendar.scheduled',lambda now:None)
 monkeypatch.setattr('etf.calendar.risk_session',lambda now:False)
 monkeypatch.setattr('etf.broker.NativeAdapter',lambda *a,**k:pytest.fail('adapter constructed outside session'))
 assert run_agent.main()==0

def test_settled_proceeds_spent_once(tmp_path):
 j=journal(tmp_path);s=normalize(raw(cash='100'));j.db.execute('INSERT INTO rf_rotation VALUES(?,?,?,?)',('sale','100',NOW.isoformat(),DAY))
 p=plan(s,sig(False),j,now=NOW)['selected'];assert p['reason']=='FULL_TQQQ_PROCEEDS_TO_CORE'
 row=j.reserve('rotate',p,s);j.begin_submit(row['id']);amount=D(row['amount'])
 o=dict(id='rotbuy',ref_id=row['id'],symbol='SMH',side='buy',state='filled',cumulative_quantity=str(amount/10),executions=[dict(id='rotfill',price='10',quantity=str(amount/10),fees='0',timestamp=(NOW-timedelta(seconds=1)).isoformat(),currency='USD')])
 j.reconcile([o],complete=True);j.reconcile([o],complete=True);assert j.rotation_spent()==amount;assert j.rotation_available(DAY)==100-amount

def test_cash_drop_after_preview_blocks(tmp_path):
 j=journal(tmp_path);b=FakeBroker();original=b.review
 def changed(row):reply=original(row);b.raw['funds']['reported_cash']='0';return reply
 b.review=changed
 with pytest.raises(ValueError):cycle(j,b,sig(),live=True,now=NOW)
 assert b.submits==0

def test_sellable_drop_after_preview_blocks(tmp_path):
 j=journal(tmp_path);b=FakeBroker(values=dict(SMH='0',QQQ='0',TQQQ='100'));original=b.review
 def changed(row):reply=original(row);b.raw['positions']['TQQQ']['shares_available_for_sells']='0';return reply
 b.review=changed
 with pytest.raises(ValueError,match='sell_shares'):cycle(j,b,sig(False),live=True,now=NOW)
 assert b.submits==0

def test_unknown_submit_recovered_by_original_ref(tmp_path):
 j=journal(tmp_path);b=FakeBroker();original=b.submit
 def fail_after_fill(row):original(row);raise TimeoutError()
 b.submit=fail_after_fill;cycle(j,b,sig(),live=True,now=NOW);assert b.submits==1
 b.submit=original;cycle(j,b,sig(),live=True,now=NOW);assert b.submits==3;assert len([o for o in b.raw['orders'] if o['symbol']=='TQQQ'])==1

def test_partial_then_cancel_never_retries_same_lane(tmp_path):
 j=journal(tmp_path);b=FakeBroker();b.mode='partial'
 with pytest.raises(ValueError):cycle(j,b,sig(),live=True,now=NOW)
 b.raw['orders'][0]['state']='partially_filled_rest_cancelled';b.mode='fill';cycle(j,b,sig(),live=True,now=NOW)
 assert len([o for o in b.raw['orders'] if o['symbol']=='TQQQ'])==1

def test_dust_and_precision_do_not_short(tmp_path):
 j=journal(tmp_path);s=normalize(raw(values=dict(SMH='0',QQQ='0',TQQQ='.001')));assert plan(s,sig(False),j,now=NOW)['selected'] is None
 with pytest.raises(ValueError,match='sell_precision'):j.reserve('bad',sell(qty='0.0000001'),normalize(raw(values=dict(SMH='0',QQQ='0',TQQQ='1'))))

def test_native_schema_both_size_modes():
 class T(Transport):
  def list_tools(self):return [dict(name=n,inputSchema=dict(properties={k:{} for k in ['account_number','symbol','side','type','quantity','dollar_amount','market_hours','time_in_force','ref_id']})) for n in ['review_equity_order','place_equity_order']]
 class A:transport=T()
 assert Broker(A(),expected_account_hash='a'*64).verify_order_schema()

def test_deficits_not_fixed_new_money(tmp_path):
 j=journal(tmp_path);s=normalize(raw(cash='100',values=dict(SMH='800',QQQ='0',TQQQ='300')));r=plan(s,sig(),j,now=NOW)
 assert r['selected']['symbol']=='TQQQ';j.db.execute('INSERT INTO rf_actions VALUES(?,?)',(r['selected']['decision_id'],'tried'))
 assert plan(s,sig(),j,now=NOW)['selected']['symbol']=='QQQ'

def test_daily_holdings_tqqq_no_legacy_budget(tmp_path):
 from etf.service_reports import holdings_report
 j=journal(tmp_path);text=holdings_report(j,raw(values=dict(SMH='700',QQQ='100',TQQQ='200')),dict(phase='ATTACK',sma50='110',sma100='100',signal_date=sig()['date']),NOW)
 assert 'TQQQ：' in text;assert '累计入金额度' not in text;assert '今日额度：$100' not in text;assert '长期上涨条件成立' in text

def test_report_delivery_once_and_uncertain_not_retried(tmp_path,monkeypatch):
 from agent_ops import Operations
 from etf.service_reports import drain
 import etf.service_reports as reports
 monkeypatch.setattr(reports,'configure_sender',lambda broker:None);store=Operations(tmp_path/'outbox.sqlite');store.enqueue('one','DECISION','BUY fixture');calls=[]
 drain(store,None,sender=lambda text,a:(calls.append(text) or dict(state='SENT',message_id=12)))
 drain(store,None,sender=lambda *a:pytest.fail('duplicate delivery'));assert calls==['BUY fixture'];assert store.message('one')['message_id']==12
 store.enqueue('two','DECISION','WAIT fixture')
 def fail(*a):raise TimeoutError()
 drain(store,None,sender=fail);assert store.message('two')['state']=='UNKNOWN';drain(store,None,sender=lambda *a:pytest.fail('retry unknown'))

@pytest.mark.parametrize('failure',[None,'stale','policy','account','implementation','halt','legacy_service'])
def test_local_acceptance_has_no_order_side_effects(tmp_path,monkeypatch,failure):
 import run_agent
 root=tmp_path/'runtime';a=root/'agent';(a/'etf').mkdir(parents=True);(root/'state').mkdir();(root/'config').mkdir();(root/'evidence').mkdir()
 (a/'etf/policy.json').write_text(json.dumps(POLICY));monkeypatch.setattr(run_agent,'ROOT',a)
 j=Journal(root/'state/v1_state.sqlite');j.bind_account('a'*64)
 if failure=='halt':j.save('halt_reason','preserved halt')
 j.db.close()
 cfg=dict(account_hash='a'*64,implementation_hash=run_agent.implementation_hash(),deployment_authorized=False)
 (root/'config/runtime.json').write_text(json.dumps(cfg))
 proof=dict(status='PASS',at=(NOW-timedelta(hours=1) if failure=='stale' else NOW).isoformat(),policy_hash=hashlib.sha256((a/'etf/policy.json').read_bytes()).hexdigest(),implementation_hash=run_agent.implementation_hash(),account_hash='a'*64,baseline=dict(quantities={'SMH':'0','QQQ':'0','TQQQ':'0'},orders=[],fills=[]))
 if failure in ('policy','account','implementation'):proof[{'policy':'policy_hash','account':'account_hash','implementation':'implementation_hash'}[failure]]='wrong'
 (root/'evidence/read-only-acceptance.json').write_text(json.dumps(proof));monkeypatch.setattr(sys,'argv',['run_agent.py','--accept-local'])
 def inventory():
  if failure=='legacy_service':raise ValueError('legacy_executor_still_loaded')
 monkeypatch.setattr(run_agent,'check_other_executors',inventory)
 monkeypatch.setattr('etf.broker.NativeAdapter',lambda *a,**k:pytest.fail('acceptance invoked network'))
 if failure:
  with pytest.raises(ValueError):run_agent.main()
  assert json.loads((root/'config/runtime.json').read_text())['deployment_authorized'] is False
 else:
  assert run_agent.main()==0;assert json.loads((root/'config/runtime.json').read_text())['deployment_authorized'] is True

def test_unsubmitted_pre_review_recovery_retains_history(tmp_path):
 j=journal(tmp_path);r=j.reserve('recover',buy(),normalize(raw()));j.recover_unsubmitted()
 assert j.get(r['id'])['status']=='REJECTED';assert j.db.execute('SELECT COUNT(*) FROM etf_audit').fetchone()[0]>=2

def test_ordinary_cash_never_uses_future_sale():
 r=raw(cash='100');r['account_type']='cash';r['agentic_confirmed']=False
 r['funds']=dict(components_verified=True,margin_used='0',settled_own_cash='0',settlements=[dict(id='future',kind='SALE',net_amount='100',settlement_date='2099-01-01',source='broker',status='executed',reusable_now=True)],reported_cash='100',pending_external_credits='0',pending_credits_in_reported_cash=False,unleveraged_buying_power='100',account_type='cash')
 assert normalize(r)['safe_cash']=='0.00'

def test_forward_rollback_keeps_new_fills_and_halt(tmp_path):
 import rollback
 old=tmp_path/'old';new=tmp_path/'new'
 for root in [old,new]:
  (root/'config').mkdir(parents=True);(root/'state').mkdir();(root/'agent/etf').mkdir(parents=True)
  (root/'config/runtime.json').write_text(json.dumps(dict(account_hash='a'*64)))
  with sqlite3.connect(root/'state/v1_state.sqlite') as db:db.executescript("CREATE TABLE etf_fills(id TEXT PRIMARY KEY,gross TEXT);INSERT INTO etf_fills VALUES('old-fill','20');")
 (old/'agent/etf/policy.json').write_text('old strategy unchanged')
 (new/'config/runtime.json').write_text(json.dumps(dict(account_hash='a'*64,previous_policy_hash=hashlib.sha256((old/'agent/etf/policy.json').read_bytes()).hexdigest())))
 with sqlite3.connect(new/'state/v1_state.sqlite') as db:db.execute("INSERT INTO etf_fills VALUES('new-fill','300')")
 (new/'state/live_execution_state.json').write_text(json.dumps(dict(trading_halted=True)))
 r=rollback.forward_to_old(new,old);assert not r['services_started'];assert (old/'agent/etf/policy.json').read_text()=='old strategy unchanged'
 with sqlite3.connect(old/'state/v1_state.sqlite') as db:assert dict(db.execute('SELECT * FROM etf_fills'))=={'old-fill':'20','new-fill':'300'}
 assert json.loads((old/'state/live_execution_state.json').read_text())['trading_halted'] is True

def test_rollback_rejects_missing_old_financial_facts(tmp_path):
 import rollback
 old=tmp_path/'old.sqlite';new=tmp_path/'new.sqlite'
 for p in [old,new]:
  with sqlite3.connect(p) as db:db.executescript('CREATE TABLE etf_fills(id TEXT PRIMARY KEY,gross TEXT);')
 with sqlite3.connect(old) as db:db.execute("INSERT INTO etf_fills VALUES('keep','20')")
 with pytest.raises(ValueError,match='forward_fill_missing'):rollback.verify_forward_fills(old,new)

def test_plist_preserves_venv_interpreter_symlink(tmp_path):
 import deployment
 (tmp_path/'logs').mkdir();(tmp_path/'venv/bin').mkdir(parents=True);interpreter=tmp_path/'venv/bin/python3';interpreter.symlink_to(sys.executable)
 path=deployment.render_plist(tmp_path,interpreter);plist=plistlib.loads(path.read_bytes());assert plist['ProgramArguments'][0]==str(interpreter.absolute())

@pytest.mark.parametrize('wanted,allowed',[('10.1',True),('9.9',False)])
def test_share_sell_fresh_target_never_overshoots(tmp_path,wanted,allowed):
 j=journal(tmp_path);b=FakeBroker(values=dict(SMH='0',QQQ='0',TQQQ='1000'));o=sell();o['reason']='HARVEST_ABOVE35_TO30'
 initial=dict(selected=o,reason=o['reason'])
 def replan(snap,exclude_intent=None):
  fresh=copy.deepcopy(initial)
  if exclude_intent:fresh['selected'].update(quantity=wanted,amount=str(D(wanted)*D('9.99')))
  return fresh
 if allowed:
  assert execute(j,b,'trim',initial,replan=replan,live=True)['status']=='SUBMITTED';assert b.raw['orders'][0]['cumulative_quantity']=='10'
 else:
  with pytest.raises(ValueError,match='post_preview_plan_changed|fresh_sell_shares_unavailable'):execute(j,b,'trim',initial,replan=replan,live=True)
  assert b.submits==0

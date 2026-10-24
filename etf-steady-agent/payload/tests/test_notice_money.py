"""Notification amounts/prices come from evidence, never from a planned fill."""
from datetime import datetime,timezone
from decimal import Decimal
import copy
import pytest
from etf.notice_details import collect
from etf.notifications import telegram_report
from etf.journal import Journal
from etf.funds import normalize
from test_etf_cycle_v3 import raw

SLOT='notice-money-test'
def fresh_raw(**kwargs):
 r=raw(**kwargs);r['as_of']=datetime.now(timezone.utc).isoformat()
 for q in r['quotes'].values():q['timestamp']=r['as_of']
 return r

def setup(tmp_path):
 r=fresh_raw();j=Journal(tmp_path/'db');j.bind_account('a'*64)
 plan=dict(symbol='SMH',side='BUY',amount='10.00',reason='ORIGINAL_8020_BUY')
 row=j.reserve(SLOT,plan,normalize(r));j.begin_submit(row['id']);j.status(row['id'],'SUBMITTED','broker-test')
 result=dict(status='LIVE',selected=plan,execution=dict(status='SUBMITTED',intent_id=row['id']),reason='ORIGINAL_8020_BUY',scores={'SMH':80,'QQQ':50})
 return j,r,row,result

def broker_order(row,state='filled',qty='1'):
 at=datetime.now(timezone.utc).isoformat()
 return dict(id='broker-test',ref_id=row['id'],symbol='SMH',side='buy',state=state,cumulative_quantity=qty,
             executions=[dict(id='fill-test',price='10',quantity=qty,fees='0',timestamp=at,currency='USD')] if Decimal(qty) else [])


def test_filled_price_cash_and_budget_are_evidence_based_without_ledger_mutation(tmp_path):
 j,r,row,result=setup(tmp_path);r=fresh_raw(cash='90',smh='710');r['orders']=[broker_order(row)]
 result['selected']=dict(result['selected'],amount='11.00')
 before=list(j.db.iterdump());d=collect(j,r,result,SLOT)
 assert d['cash']=='90' and d['safe_cash']=='89.75'
 assert d['order']==dict(symbol='SMH',status='FILLED',submitted='10.00',filled='10',fill_price='10')
 assert d['daily_budget']==dict(limit='100.00',committed='10',remaining='90.00')
 assert list(j.db.iterdump())==before and j.get(row['id'])['status']=='SUBMITTED' and j.filled_gross(row['id'])==0
 text=telegram_report(dict(result,notice=d))
 for item in ['计划金额：$11.00','提交金额：$10.00 | 成交金额：$10.00','成交均价：$10.0000','执行：已成交','Robinhood 现金：$90.00','剩余可用自有资金：$89.75','今日剩余额度：$90.00']:assert item in text
 j.db.close()


def test_failed_post_order_refresh_does_not_present_before_cash_as_remaining(tmp_path):
 j,r,row,result=setup(tmp_path);d=collect(j,r,result,SLOT,before_order=True)
 assert d['before_order'] and 'safe_cash' not in d and 'daily_budget' not in d
 assert d['order']['filled'] is None and d['order']['fill_price'] is None
 text=telegram_report(dict(result,notice=d))
 assert '下单前现金：$100.00' in text and '剩余可用自有资金：待确认' in text
 assert '成交金额：待确认' in text and '已成交' not in text
 j.db.close()


def test_open_order_never_becomes_a_fill_or_spendable_cash(tmp_path):
 j,r,row,result=setup(tmp_path);r['orders']=[broker_order(row,'confirmed','0')]
 d=collect(j,r,result,SLOT)
 assert d['order']['submitted']=='10.00' and d['order']['filled'] is None
 assert d['order']['fill_price'] is None and 'safe_cash' not in d
 assert d['daily_budget']['remaining']=='90.00'
 assert '已成交' not in telegram_report(dict(result,notice=d))
 j.db.close()


def test_partial_fill_uses_weighted_price_not_last_quote(tmp_path):
 j,r,row,result=setup(tmp_path);o=broker_order(row,'partially_filled','.4')
 o['executions'][0]['price']='9';o['executions'].append(dict(o['executions'][0],id='second-fill',price='11',quantity='.1'));o['cumulative_quantity']='.5';r['orders']=[o]
 d=collect(j,r,result,SLOT)
 assert d['order']['filled']=='4.7' and d['order']['fill_price']=='9.4'
 assert d['order']['status']=='PARTIALLY_FILLED' and d['daily_budget']['remaining']=='90.00'
 assert '成交均价：$9.4000' in telegram_report(dict(result,notice=d))
 j.db.close()


def test_previous_window_order_is_not_reported_as_this_windows_purchase(tmp_path):
 j,r,row,result=setup(tmp_path);r=fresh_raw(cash='90',smh='710');r['orders']=[broker_order(row)]
 d=collect(j,r,dict(selected=None), 'different-slot')
 assert d['order'] is None and d['daily_budget']['remaining']=='90.00'
 text=telegram_report(dict(status='LIVE',selected=None,reason='WAIT_ORIGINAL_8020',notice=d))
 assert text.count('提交金额：$0.00 | 成交金额：$0.00')==3
 assert text.count('成交均价：—（未成交）')==3
 assert '只读预览' in telegram_report(dict(status='READ_ONLY_ACCEPTED',selected=None,reason='WAIT_ORIGINAL_8020',notice=d))
 j.db.close()


@pytest.mark.parametrize('kind',['wrong-account','stale','conflicting-fill'])
def test_invalid_notice_data_is_rejected_without_changing_the_ledger(tmp_path,kind):
 j,r,row,result=setup(tmp_path);r['orders']=[broker_order(row)];before=list(j.db.iterdump())
 if kind=='wrong-account':r['account_hash']='b'*64
 elif kind=='stale':r['as_of']='2000-01-01T00:00:00+00:00'
 else:r['orders'][0]['executions'][0]['quantity']='2'
 with pytest.raises(ValueError):collect(j,r,result,SLOT)
 assert list(j.db.iterdump())==before
 j.db.close()


@pytest.mark.parametrize('failure',['refresh','format-data'])
def test_reporting_failure_cannot_change_execution_or_retry_an_order(tmp_path,monkeypatch,failure):
 from etf import runtime,execution,notice_details
 from test_etf_cycle_v3 import context,phase
 root=tmp_path/'agent';root.mkdir();(tmp_path/'evidence').mkdir()
 monkeypatch.setattr(runtime,'ROOT',root)
 ctx=context();ctx['market_trend']=dict(name='下跌趋势',date='2026-10-14',scope='QQQ/SMH')
 monkeypatch.setattr(runtime,'signals',lambda *a:(phase(),dict(SMH=80,QQQ=80),ctx))
 monkeypatch.setitem(runtime.POLICY,'activation','LIVE_VERIFIED')
 j=Journal(tmp_path/'db');j.bind_account('a'*64)
 calls=[]
 class Broker:
  def read(self):
   calls.append('read')
   if len(calls)>1 and failure=='refresh':raise TimeoutError('read only')
   return fresh_raw()
 def fake_execute(*a,**kw):return dict(status='SUBMITTED',intent_id='mock-order')
 monkeypatch.setattr(execution,'execute',fake_execute)
 def capture(*a,**kw):
  if failure=='format-data':raise ValueError('unavailable display data')
  assert kw['before_order'] is True
  return dict(before_order=True)
 monkeypatch.setattr(notice_details,'collect',capture)
 r=runtime.cycle(j,Broker(),[],{},live=True,slot=SLOT)
 assert r['execution']==dict(status='SUBMITTED',intent_id='mock-order')
 assert calls==['read','read'] and not j.active()
 assert r['notice']==({} if failure=='format-data' else dict(before_order=True))
 j.db.close()


def test_sgov_sale_notice_uses_sell_signal_and_actual_facts():
 text=telegram_report(dict(selected=dict(side='SELL',symbol='SGOV',amount='25.00'),notice=dict(order=dict(symbol='SGOV',status='FILLED',submitted='25.00',filled='24.99',fill_price='100.01'),cash='35.00',safe_cash='34.75',quotes=dict(SGOV='100.02'))))
 assert 'SGOV | 信号：SELL' in text
 assert '提交金额：$25.00 | 成交金额：$24.99' in text
 assert '参考价：$100.02 | 成交均价：$100.0100' in text
 assert '剩余可用自有资金：$34.75' in text

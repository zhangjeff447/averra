import copy,hashlib,json
from datetime import datetime,timezone
from zoneinfo import ZoneInfo
from pathlib import Path
import pytest
from agent_ops import Operations
from etf import service_reports as reports
from etf.journal import Journal
from etf.cycle import POLICY
from test_etf_cycle_v3 import raw


def test_version_history_is_scoped_and_never_stores_credentials(tmp_path):
 s=Operations(tmp_path/'ops.db')
 a=s.version('LOCAL',{'targets':{'SMH':'.80'},'access_token':'secret-test'})
 assert s.version('LOCAL',{'targets':{'SMH':'.80'},'access_token':'changed-secret'})==a
 s.version('WEBSITE',{'targets':{'SMH':'.70','SGOV':'.10'}})
 s.version('LOCAL',{'targets':{'SMH':'.75'}},'USER_CONFIRMED_UI')
 assert len(s.versions())==3 and len(s.versions('LOCAL'))==2
 assert 'secret-test' not in s.path.read_bytes().decode(errors='ignore')
 assert s.versions('LOCAL')[0]['origin']=='USER_CONFIRMED_UI'
 s.close()


def test_message_is_durable_once_and_unknown_is_never_retried(tmp_path):
 p=tmp_path/'ops.db';s=Operations(p);s.enqueue('d','DECISION','first');s.enqueue('d','DECISION','duplicate')
 assert s.message('d')['body']=='first' and s.claim('d') and not s.claim('d');s.close()
 s=Operations(p);s.interrupted_sends();assert s.message('d')['state']=='UNKNOWN' and not s.claim('d')
 assert s.diagnoses()[0]['category']=='DELIVERY_UNKNOWN';s.close()


def test_confirmed_delivery_requires_positive_message_id(tmp_path):
 s=Operations(tmp_path/'db');s.enqueue('d','DAILY_HOLDINGS','body');s.claim('d')
 with pytest.raises(ValueError):s.finish('d','SENT')
 s.finish('d','SENT',message_id=42);assert s.message('d')['message_id']==42;s.close()


def test_research_search_cannot_touch_financial_records(tmp_path):
 s=Operations(tmp_path/'db');s.index('AMD','2026-10-14','research.json','official earnings revised')
 s.index('AMD','2026-10-14','research.json','official earnings revised');s.index('META','2026-10-14','research.json','other')
 assert len(s.search('AMD','earnings'))==1 and not s.search("AMD' OR 1=1 --")
 assert s.status()['research_documents']==2;s.close()


@pytest.mark.parametrize('time,expected',[
 ('2026-10-15T15:30:00-04:00',False),('2026-10-15T15:31:00-04:00',True),
 ('2026-10-15T17:00:00-04:00',True),('2026-10-17T15:31:00-04:00',False),
 ('2026-10-15T20:00:00-04:00',False),('2026-11-02T15:31:00-05:00',True)])
def test_daily_schedule(time,expected):assert reports.due(datetime.fromisoformat(time)) is expected


def fresh_raw():
 r=raw();r['as_of']=datetime.now(timezone.utc).isoformat()
 for q in r['quotes'].values():q['timestamp']=r['as_of']
 return r


def test_holdings_report_preserves_ledger_and_shows_separate_cash_budget(tmp_path):
 j=Journal(tmp_path/'db');j.bind_account('a'*64);r=fresh_raw();before=list(j.db.iterdump())
 text=reports.holdings_report(j,r,{'market_trend':{'name':'下跌趋势','date':'2026-10-14'},'scores':{'QQQ':30,'SMH':60}},datetime.now(timezone.utc))
 for part in ['每日持仓','QQQ：','SMH：','Robinhood 现金：$100.00','总资产（券商）：$1100.00','可用自有资金：$99.75','今日剩余额度：$100.00','下跌趋势','只读持仓报告']:assert part in text
 assert list(j.db.iterdump())==before;j.db.close()


def test_legacy_monthly_credits_are_never_reported_as_available_funds(tmp_path):
 j=Journal(tmp_path/'db');j.bind_account('a'*64)
 j.db.execute('CREATE TABLE credits(month TEXT PRIMARY KEY,cents INTEGER NOT NULL)')
 j.db.executemany('INSERT INTO credits VALUES(?,?)',[('2026-09',30000),('2026-10',30000)])
 j.db.execute('CREATE TABLE baseline_spend(cents INTEGER NOT NULL)')
 j.db.execute('INSERT INTO baseline_spend VALUES(17210)')
 before=list(j.db.iterdump())
 text=reports.holdings_report(j,fresh_raw(),{},datetime.now(timezone.utc))
 for removed in ['累计入金额度','账本剩余','Agent 策略账本','$600.00','$427.90']:
  assert removed not in text
 for retained in ['Robinhood 现金：$100.00','可用自有资金：$99.75','今日剩余额度：$100.00','不代表账户现金或可买金额']:
  assert retained in text
 assert list(j.db.iterdump())==before
 assert j.db.execute('SELECT SUM(cents) FROM credits').fetchone()[0]==60000
 j.db.close()


def test_post_close_quote_is_labelled_without_claiming_tradable_funds(tmp_path):
 j=Journal(tmp_path/'db');j.bind_account('a'*64);r=fresh_raw()
 for q in r['quotes'].values():q['timestamp']='2026-10-15T20:00:00+00:00'
 # Use an earlier stale timestamp independent of the machine's current clock.
 for q in r['quotes'].values():q['timestamp']='2026-10-14T20:00:00+00:00'
 text=reports.holdings_report(j,r,{},datetime.now(timezone.utc))
 assert '10-14 16:00 ET' in text and '可用自有资金：数据待确认' in text
 j.db.close()


def test_delivery_failure_does_not_retry_or_invoke_broker(tmp_path,monkeypatch):
 s=Operations(tmp_path/'ops.db');s.enqueue('decision:slot','DECISION','body');calls=[]
 monkeypatch.setattr(reports,'configure_sender',lambda broker:None)
 def sender(text,adapter):calls.append(text);raise TimeoutError()
 reports.drain(s,object(),sender);reports.drain(s,object(),sender)
 assert calls==['body'] and s.message('decision:slot')['state']=='UNKNOWN';s.close()


def test_scheduled_daily_catchup_sends_once_with_zero_order_capability(tmp_path,monkeypatch):
 monkeypatch.setitem(POLICY,'activation','LIVE_VERIFIED')
 root=tmp_path/'agent';(root/'etf').mkdir(parents=True);(root/'etf/policy.json').write_text(json.dumps(POLICY))
 state=tmp_path/'state';state.mkdir();(tmp_path/'config').mkdir()
 (tmp_path/'config/runtime.json').write_text(json.dumps(dict(deployment_authorized=True,policy_hash=hashlib.sha256((root/'etf/policy.json').read_bytes()).hexdigest(),credential_config='not-read',account_hash='a'*64)))
 j=Journal(state/'v1_state.sqlite');j.bind_account('a'*64);before=list(j.db.iterdump());j.close=j.db.close;j.close()
 reads=[];sends=[];modes=[]
 class Adapter:
  def __init__(self,*a,live):modes.append(live);self.transport=type('T',(),{'calls':[]})()
 class Broker:
  def __init__(self,*a,**kw):pass
  def read(self):reads.append(1);return fresh_raw()
 monkeypatch.setattr(reports,'Broker',Broker);monkeypatch.setattr(reports,'configure_sender',lambda broker:None)
 monkeypatch.setattr('tools.report_only.append_to',lambda text,*a:(text,{'status':'MOCK_ONLY'}))
 at=datetime(2026,10,15,17,0,tzinfo=ZoneInfo('America/New_York'))
 def send(text,adapter):sends.append(text);return dict(state='SENT',message_id=78)
 assert reports.run(root,at,adapter_factory=Adapter,sender=send)==0
 assert reports.run(root,at,adapter_factory=Adapter,sender=send)==0
 assert reads==[1] and len(sends)==1 and modes==[False,False] and '每日持仓' in sends[0]
 j=Journal(state/'v1_state.sqlite');assert list(j.db.iterdump())==before;j.db.close()


def test_live_notice_enqueues_without_sender_or_broker(monkeypatch,tmp_path):
 monkeypatch.setitem(POLICY,'activation','LIVE_VERIFIED')
 from etf import notifications,operational
 monkeypatch.setattr(operational,'DB',tmp_path/'ops.db')
 monkeypatch.setattr(notifications,'configure_sender',lambda *a:pytest.fail('sender must run only in worker'))
 result=dict(status='LIVE',selected=None,reason='WAIT_ORIGINAL_8020',notification_key='2026-10-15:15:30')
 first=notifications.notify(result,None,live=True);second=notifications.notify(result,None,live=True)
 assert first==second and first['queued'] and first['state']=='PENDING'
 s=Operations(tmp_path/'ops.db');assert s.db.execute('select count(*) from outbox').fetchone()[0]==1;s.close()


def test_retry_is_allowed_only_for_explicitly_rejected_messages(tmp_path):
 s=Operations(tmp_path/'ops.db');s.enqueue('decision:slot','DECISION','body');s.claim('decision:slot');s.finish('decision:slot','REJECTED',error='HTTP_400')
 assert s.retry_rejected('decision:slot');assert s.claim('decision:slot');s.finish('decision:slot','SENT',message_id=23)
 assert not s.retry_rejected('decision:slot') and s.diagnoses()[0]['resolved_at']
 assert s.events('slot')[0]['stage']=='NOTIFICATION_SENT'
 s.enqueue('unknown','DECISION','body');s.claim('unknown');s.finish('unknown','UNKNOWN')
 assert not s.retry_rejected('unknown');s.close()


def test_public_daily_worker_never_reads_account_or_sends(tmp_path):
 def forbidden(*a,**kw):raise AssertionError('public download must not activate report service')
 assert POLICY['activation']=='PUBLIC_READ_ONLY'
 assert reports.run(tmp_path,datetime(2026,10,15,20,0,tzinfo=timezone.utc),adapter_factory=forbidden,sender=forbidden)==0

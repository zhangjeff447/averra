import json,sys
from datetime import datetime,timezone
from urllib.error import HTTPError,URLError
import pytest
from etf.recovery import retry_read,window_key,require_window
from etf.broker import Broker,NativeAdapter,GuardedTransport
from etf.runtime import cycle
from test_integration import FakeBroker,journal,sig,NOW


@pytest.mark.parametrize('error',[TimeoutError(),ConnectionError(),URLError('DNS'),RuntimeError('session disconnected'),HTTPError('https://test',404,'expired session',{},None),HTTPError('https://test',503,'busy',{},None)])
def test_network_reads_reconnect_then_restart(error):
    calls=[];waits=[]
    def read():
        calls.append('read')
        if len(calls)==1:raise error
        return 'fresh entire snapshot'
    assert retry_read(read,reconnect=lambda:calls.append('reconnect'),sleep=waits.append)=='fresh entire snapshot'
    assert calls==['read','reconnect','read'] and waits==[2]


@pytest.mark.parametrize('error',[ValueError('portfolio_value_unreconciled'),ValueError('unattributed_post_activation_order'),ValueError('quote_stale:QQQ'),PermissionError('denied'),HTTPError('https://test',401,'unauthorized',{},None),HTTPError('https://test',403,'forbidden',{},None)])
def test_safety_and_auth_failures_are_not_retried(error):
    calls=[]
    def fail():calls.append(1);raise error
    with pytest.raises(type(error)):retry_read(fail,reconnect=lambda:pytest.fail('unsafe reconnect'),sleep=lambda _:pytest.fail('unsafe retry'))
    assert len(calls)==1


def test_reads_have_finite_retry_budget():
    calls=[];waits=[]
    def fail():calls.append(1);raise TimeoutError()
    with pytest.raises(TimeoutError):retry_read(fail,sleep=waits.append)
    assert len(calls)==3 and waits==[2,5]


def test_broker_restarts_whole_snapshot(monkeypatch):
    class Adapter:
        def reconnect_read(self):events.append('reconnect')
    b=Broker(Adapter(),expected_account_hash='a'*64);events=[]
    def read():
        events.append('accounts')
        if len(events)==1:events.append('positions');raise TimeoutError()
        return {'fresh':True}
    monkeypatch.setattr(b,'_read_once',read)
    monkeypatch.setattr('etf.broker.retry_read',lambda operation,**kw:retry_read(operation,sleep=lambda _:None,**kw))
    assert b.read()=={'fresh':True}
    assert events==['accounts','positions','reconnect','accounts']


def test_reconnect_keeps_read_only_and_call_history(monkeypatch):
    a=object.__new__(NativeAdapter);a.transport=GuardedTransport(object(),live=False);a.transport.calls=['get_accounts']
    a._mcp_session_generation=0;a.runtime_tools=['stale'];a._build_direct_transport=lambda:object();a._log_snapshot_retry=lambda **_:None
    a.reconnect_read()
    assert a.runtime_tools is None and a.transport.calls==['get_accounts']
    with pytest.raises(PermissionError,match='denied_before_transport'):a.transport.call_tool('place_equity_order',{})


def test_preview_disconnect_can_replan_only_before_submission(tmp_path):
    j=journal(tmp_path);b=FakeBroker()
    b.review=lambda _:(_ for _ in ()).throw(TimeoutError())
    with pytest.raises(TimeoutError):cycle(j,b,sig(),live=True,now=NOW)
    assert b.submits==0 and not j.active()
    assert not j.db.execute('SELECT 1 FROM rf_actions').fetchone()
    from test_integration import native_preview
    b.review=native_preview
    cycle(j,b,sig(),live=True,now=NOW)
    assert b.submits>0


def test_submission_timeout_is_never_replayed_after_restart(tmp_path):
    j=journal(tmp_path);b=FakeBroker();b.mode='timeout'
    cycle(j,b,sig(),live=True,now=NOW)
    assert b.submits==1 and j.active()[0]['status']=='EXECUTION_UNKNOWN'
    cycle(j,b,sig(),live=True,now=NOW)
    assert b.submits==1 and j.active()[0]['status']=='RECONCILIATION_REQUIRED'


def test_late_recovery_cannot_submit(tmp_path,monkeypatch):
    j=journal(tmp_path);b=FakeBroker();checks=[]
    def gate(window):
        checks.append(window)
        if len(checks)==2:raise ValueError('recovery_window_expired')
    monkeypatch.setattr('etf.execution.require_window',gate)
    with pytest.raises(ValueError,match='recovery_window_expired'):cycle(j,b,sig(),live=True,now=NOW,window='expected-window')
    assert b.submits==0 and not j.active() and not j.db.execute('SELECT 1 FROM rf_actions').fetchone()


def test_weekend_and_late_window_do_not_catch_up():
    assert window_key(datetime(2026,10,17,14,5,tzinfo=timezone.utc)) is None
    assert window_key(datetime(2026,10,19,14,9,tzinfo=timezone.utc))=='2026-10-19:10:05'
    assert window_key(datetime(2026,10,19,14,10,tzinfo=timezone.utc)) is None
    with pytest.raises(ValueError,match='recovery_window_expired'):require_window('1999-01-01:10:05')


def test_scheduler_recovers_without_second_executor():
    import plistlib
    from pathlib import Path
    p=Path(__file__).resolve().parents[2]/'com.qqqsmh.agent.return-first.plist'
    d=plistlib.loads(p.read_bytes())
    assert d['Label']=='com.qqqsmh.agent.return-first' and d['StartInterval']==60 and d['RunAtLoad'] is True
    assert [(v['Hour'],v['Minute']) for v in d['StartCalendarInterval']]==[(10,5),(12,30),(15,30)]


def test_completed_window_still_checks_stop_without_repeating_buys(tmp_path,monkeypatch):
    import run_agent
    from pathlib import Path
    from etf.journal import Journal
    from etf.cycle import POLICY
    import hashlib
    a=tmp_path/'agent';(a/'etf').mkdir(parents=True);(a/'etf/policy.json').write_text(json.dumps(POLICY))
    (tmp_path/'config').mkdir();(tmp_path/'state').mkdir()
    monkeypatch.setattr(run_agent,'ROOT',a)
    (tmp_path/'config/runtime.json').write_text(json.dumps(dict(implementation_hash=run_agent.implementation_hash(),deployment_authorized=True,policy_hash=hashlib.sha256((a/'etf/policy.json').read_bytes()).hexdigest(),shared_executor_lock=str(tmp_path/'lock'),credential_config='fake',account_hash='a'*64)))
    (tmp_path/'state/live_execution_state.json').write_text('{"trading_halted": false}')
    j=Journal(tmp_path/'state/v1_state.sqlite');j.save('rf_window_completed:test-window',{'reason':'WAIT','selected':None});j.db.close()
    monkeypatch.setattr(sys,'argv',['run_agent.py','--live'])
    monkeypatch.setattr('etf.calendar.scheduled',lambda _: '10:05')
    monkeypatch.setattr('etf.recovery.window_key',lambda _: 'test-window')
    monkeypatch.setattr(run_agent,'check_other_executors',lambda:None)
    monkeypatch.setattr('etf.history.signal',lambda *a,**k:sig())
    monkeypatch.setattr('etf.broker.NativeAdapter',lambda *a,**k:object())
    monkeypatch.setattr('etf.broker.Broker',lambda *a,**k:type('ReadOnlyFake',(),{'verify_order_schema':lambda _:True})())
    checks=[]
    def check_stop(*a,**k):
        checks.append(k)
        return {'reason':'WAIT_STOP_NOT_TRIGGERED','selected':None}
    monkeypatch.setattr('etf.runtime.cycle',check_stop)
    messages=[];monkeypatch.setattr('etf.notifications.enqueue',lambda r,j:messages.append(r))
    assert run_agent.main()==0 and messages==[{'reason':'WAIT','selected':None}]
    assert checks[0]['risk_only'] is True and checks[0]['window'].startswith('STOP:')

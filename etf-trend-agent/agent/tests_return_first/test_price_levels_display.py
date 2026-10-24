"""Display correctness and isolation from order planning/account state."""
from datetime import datetime, timezone
from pathlib import Path
from copy import deepcopy
from decimal import Decimal as D
import csv
import hashlib
import json
import pytest
from etf import price_levels as levels

NOW = datetime(2026,10,23,14,5,tzinfo=timezone.utc)


def write_symbol(root, symbol, rows):
    path=root/(symbol+'.csv')
    with path.open('w') as stream:
        writer=csv.DictWriter(stream,fieldnames=['Date','Open','High','Low','Close'])
        writer.writeheader();writer.writerows(rows)
    manifest_path=root/'manifest.json'
    manifest=json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    manifest[symbol]={'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}
    manifest_path.write_text(json.dumps(manifest))


@pytest.fixture
def data(tmp_path):
    root=tmp_path/'data';root.mkdir()
    days=levels.exchange().sessions_in_range('2026-09-01','2026-10-22').strftime('%Y-%m-%d').tolist()[-21:]
    rows=[dict(Date=day,Open=str(100+i),High=str(102+i),Low=str(99+i),Close=str(100+i)) for i,day in enumerate(days)]
    rows[0].update(Low='1',High='2000')  # Must be outside the 20-session window.
    for symbol in levels.SYMBOLS:write_symbol(root,symbol,rows)
    return root,rows


def test_exact_twenty_complete_sessions(data):
    root,rows=data;result=levels.build(root,NOW)
    assert result['display_only'] and result['sessions']==20 and result['as_of']=='2026-10-22'
    for symbol in levels.SYMBOLS:
        item=result['symbols'][symbol]
        assert item['status']=='OK' and D(item['support'])==100 and D(item['breakout'])==122
        assert item['first_session']==rows[1]['Date']


def test_forming_current_day_cannot_change_levels(data):
    root,rows=data;before=levels.build(root,NOW)
    future=dict(Date='2026-10-23',Open='100',High='999999',Low='.01',Close='100')
    for symbol in levels.SYMBOLS:write_symbol(root,symbol,rows+[future])
    after=levels.build(root,NOW)
    for symbol in levels.SYMBOLS:
        for key in ('support','breakout','first_session','last_session'):
            assert before['symbols'][symbol][key]==after['symbols'][symbol][key]


@pytest.mark.parametrize('problem',['stale','gap','duplicate','bad_ohlc','tampered'])
def test_bad_data_is_local_to_one_etf(data,problem):
    root,rows=data;rows=deepcopy(rows)
    if problem=='stale':rows.pop()
    elif problem=='gap':rows.pop(-5)
    elif problem=='duplicate':rows.append(rows[-1])
    elif problem=='bad_ohlc':rows[-1]['Low']='NaN'
    write_symbol(root,'QQQ',rows)
    if problem=='tampered':
        path=root/'QQQ.csv';path.write_text(path.read_text()+'\n')
    result=levels.build(root,NOW)
    assert result['symbols']['QQQ']['status']=='UNAVAILABLE'
    assert result['symbols']['SMH']['status']=='OK'


def test_display_failure_preserves_original_notification(monkeypatch,tmp_path):
    def fail(*args):raise OSError('read unavailable')
    monkeypatch.setattr(levels,'build',fail)
    text=levels.append_to('原来的决策通知',tmp_path,NOW)
    assert text.startswith('原来的决策通知\n\n')
    assert 'QQQ：支撑／突破参考数据待更新' in text
    assert 'SMH：支撑／突破参考数据待更新' in text
    assert '仅展示，不参与交易。' in text


def test_notification_addition_does_not_mutate_result_or_dedup_key(data,monkeypatch):
    from etf import notifications
    import agent_ops
    root,_=data;captured={}
    class Clock(datetime):
        @classmethod
        def now(cls,tz=None):return NOW.astimezone(tz) if tz else NOW.replace(tzinfo=None)
    class Store:
        def __init__(self,path):pass
        def close(self):pass
        def enqueue(self,key,kind,text):captured.update(key=key,kind=kind,text=text);return captured
    monkeypatch.setattr(agent_ops,'Operations',Store)
    monkeypatch.setattr(notifications,'datetime',Clock)
    monkeypatch.setattr(notifications,'ROOT',root.parent/'agent')
    result=dict(version='test',phase='DEFENSIVE',selected=None,reason='WAIT',executions=[])
    original=deepcopy(result);notifications.enqueue(result,None)
    event=dict(day='2026-10-23',version='test',phase='DEFENSIVE',selected=None,reason='WAIT',executions=[])
    expected='rf-decision:'+hashlib.sha256(json.dumps(event,sort_keys=True).encode()).hexdigest()
    assert result==original and captured['key']==expected
    assert 'QQQ：支撑 $100.00｜突破参考 $122.00' in captured['text']
    assert 'SMH：支撑 $100.00｜突破参考 $122.00' in captured['text']


def test_daily_report_contains_both_etfs(tmp_path,monkeypatch):
    import test_integration as fixtures
    from etf.service_reports import holdings_report
    monkeypatch.setattr(levels,'build',lambda *args:dict(as_of='2026-10-15',symbols={
        s:dict(status='OK',support='100',breakout='122') for s in levels.SYMBOLS}))
    journal=fixtures.journal(tmp_path)
    text=holdings_report(journal,fixtures.raw(),{},fixtures.NOW)
    assert '每日持仓' in text and '未完成订单：否' in text
    assert text.count('突破参考 $122.00')==2


def test_display_has_no_effect_on_actual_policy_planner_or_ledger(tmp_path,monkeypatch):
    import test_integration as fixtures
    from etf.cycle import POLICY,plan
    from etf.funds import normalize
    policy=json.loads((Path(__file__).resolve().parents[1]/'etf/policy.json').read_text())
    for key,value in policy.items():monkeypatch.setitem(POLICY,key,value)
    journal=fixtures.journal(tmp_path)
    snapshot=normalize(fixtures.raw());signal=dict(fixtures.sig(),sma200='100')
    before=plan(snapshot,signal,journal,now=fixtures.NOW)
    ledger='\n'.join(journal.db.iterdump())
    monkeypatch.setattr(levels,'build',lambda *args: (_ for _ in ()).throw(RuntimeError('display failed')))
    levels.append_to('报告',tmp_path,NOW)
    after=plan(snapshot,signal,journal,now=fixtures.NOW)
    assert before==after and ledger=='\n'.join(journal.db.iterdump())

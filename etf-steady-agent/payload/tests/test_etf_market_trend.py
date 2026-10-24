import copy
import pytest

from etf.cycle import plan
from etf.runtime import market_trend, report


def item(weak=False, strong=False, recovering=False):
    return dict(weak=weak, strong=strong, recovering=recovering)


@pytest.mark.parametrize('smh,qqq,risk,normal,recovery,name', [
    (item(strong=True), item(strong=True), False, True, True, '上涨趋势'),
    (item(strong=True), item(strong=True), False, False, True, '复苏反弹'),
    (item(weak=True), item(weak=True), True, False, False, '下跌趋势'),
    (item(weak=True), item(weak=True), False, False, False, '下跌趋势'),
    (item(weak=True, recovering=True), item(weak=True, recovering=True), True, False, False, '下跌中反弹'),
    (item(weak=True, recovering=True), item(weak=True), True, False, False, '下跌趋势'),
    (item(weak=True), item(strong=True), True, False, False, '偏弱回调'),
    (item(weak=True), item(strong=True), False, False, False, '走势分化'),
    (item(), item(strong=True), False, False, False, '走势分化'),
    (item(), item(), False, False, False, '正常震荡'),
    (item(strong=True), item(strong=True), False, False, False, '正常震荡'),
])
def test_completed_market_names(smh, qqq, risk, normal, recovery, name):
    observation=dict(date='2026-10-13', symbols=dict(SMH=smh, QQQ=qqq), risk=risk)
    before=copy.deepcopy(observation)
    result=market_trend(observation, dict(normal=normal, recovery=recovery))
    assert result==dict(name=name, date='2026-10-13', scope='QQQ/SMH')
    assert observation==before


def test_report_separates_market_from_delayed_strategy_phase():
    result=dict(phase='PULLBACK', selected=None, reason='WAIT',
                market_trend=dict(name='复苏反弹', date='2026-10-13', scope='QQQ/SMH'))
    text=report(result)
    assert '市场行情：复苏反弹（QQQ/SMH，2026-10-13 收盘）' in text
    assert '策略阶段：回调' in text
    assert '计划：WAIT' in text


def test_blocked_report_does_not_invent_a_market_name():
    text=report(dict(reason='BLOCKED_BY_UNRESOLVED_INTENT', selected=None))
    assert '市场行情：数据待确认' in text
    assert '上涨趋势' not in text and '正常震荡' not in text


@pytest.mark.parametrize('mode', ['NORMAL', 'PULLBACK', 'RECOVERY'])
def test_market_name_has_no_effect_on_orders(mode, tmp_path):
    from etf.runtime import planned
    from test_etf_cycle_v3 import snap, phase, context, journal
    ctx=context()
    baseline=plan(snap(), phase(mode), dict(SMH=80, QQQ=80), copy.deepcopy(ctx),
                  buy_remaining=100, sell_remaining=100)
    ctx['market_trend']=dict(name='下跌趋势', date='2026-10-13', scope='QQQ/SMH')
    assert planned(journal(tmp_path), snap(), [], phase(mode), dict(SMH=80, QQQ=80), ctx)==baseline


@pytest.mark.parametrize('live', [False, True])
def test_cycle_passes_market_name_without_sending_a_pretrade_notice(tmp_path, monkeypatch, live):
    from etf import runtime, notifications, execution
    from test_etf_cycle_v3 import journal, FakeBroker, phase, context
    root=tmp_path/'agent';root.mkdir();(tmp_path/'evidence').mkdir()
    monkeypatch.setattr(runtime, 'ROOT', root)
    trend=dict(name='上涨趋势', date='2026-10-13', scope='QQQ/SMH')
    ctx=context();ctx['market_trend']=trend
    monkeypatch.setattr(runtime, 'signals', lambda *args: (phase(), dict(SMH=80, QQQ=80), ctx))
    monkeypatch.setitem(runtime.POLICY, 'activation', 'LIVE_VERIFIED')
    notices=[]
    monkeypatch.setattr(notifications, 'notify', lambda result, *args, **kwargs: notices.append(report(result)))
    monkeypatch.setattr(execution, 'execute', lambda *args, **kwargs: dict(status='MOCK_ONLY'))
    broker=FakeBroker()
    result=runtime.cycle(journal(tmp_path), broker, [], {}, live=live, slot='test-slot')
    assert result['market_trend']==trend
    assert '市场行情：上涨趋势' in report(result)
    assert notices==[]  # The entry point sends the single final execution notice.
    assert broker.submits==0


def test_actual_history_labels_latest_completed_session(tmp_path, monkeypatch):
    from pathlib import Path
    from etf.runtime import signals
    from etf.history import load
    from test_etf_cycle_v3 import journal, snap
    from tools.volatility_context import VolatilityProvider, unavailable_context
    monkeypatch.setattr(VolatilityProvider, 'fetch', lambda self: unavailable_context('test'))
    dates, frames=load(Path(__file__).resolve().parents[1]/'data')
    _, _, ctx=signals(dates, frames, journal(tmp_path), snap())
    assert ctx['market_trend']['date']==dates[-1]
    assert ctx['market_trend']['scope']=='QQQ/SMH'


def test_notification_preview_uses_the_same_market_line():
    from etf.notifications import notify
    result=dict(phase='NORMAL', selected=None, reason='WAIT',
                market_trend=dict(name='正常震荡', date='2026-10-13', scope='QQQ/SMH'))
    preview=notify(result, None, live=False)
    assert preview['preview_only'] is True
    assert '市场行情：正常震荡（QQQ/SMH，2026-10-13 收盘）' in preview['text']

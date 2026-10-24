"""Actual restored policy; other modules retain explicit frozen regression inputs."""
from datetime import datetime, timezone
from decimal import Decimal as D
import json
import sys
import pandas as pd
import pytest
import test_integration as fixtures
from etf.cycle import POLICY, plan
from etf.funds import normalize
from etf.runtime import cycle
from etf.broker import GuardedTransport
from etf.journal import Journal


def signal(on=True):
    return dict(fixtures.sig(on), sma200='100')


def test_actual_restored_configuration():
    assert POLICY['slow_ma_period'] == 200
    assert POLICY['slots_et'] == ['10:05', '12:30', '15:30']
    assert all(POLICY[k] is None for k in ('daily_buy_limit', 'per_order_limit', 'per_window_buy_limit'))
    assert POLICY['intraday_enabled'] is False
    assert POLICY['tqqq_stop_loss_enabled'] is False
    assert POLICY['tqqq_buy_requires_close_above_sma100'] is False


def test_history_really_calculates_200_days(tmp_path, monkeypatch):
    from etf import history
    monkeypatch.setattr(history, 'load', lambda _: (['2026-10-21']*300,
                         {'QQQ': pd.DataFrame({'Adj Close': range(1,301)})}))
    (tmp_path/'QQQ.csv').write_text('history')
    result = history.signal(tmp_path, now=datetime(2026,10,22,16,tzinfo=timezone.utc))
    assert D(result['sma200']) == D('200.5')
    assert D(result['sma50']) == D('275.5')
    assert 'sma100' not in result


def test_uses_200_not_100_and_has_no_extra_close_gate(tmp_path):
    j = fixtures.journal(tmp_path)
    sig = dict(signal(), sma100='120', close='90')
    result = plan(normalize(fixtures.raw()), sig, j, now=fixtures.NOW)
    assert result['phase'] == 'ATTACK'
    assert result['selected']['symbol'] == 'TQQQ'
    assert result['slow_ma_period'] == 200
    assert result['sma200'] == '100'
    assert result['tqqq_protection']['buy_allowed']


def test_trend_exit_still_sells_full_tqqq_position(tmp_path):
    j = fixtures.journal(tmp_path)
    snapshot = normalize(fixtures.raw(values=dict(SMH='600', QQQ='100', TQQQ='200')))
    result = plan(snapshot, dict(signal(), sma200='120'), j, now=fixtures.NOW)
    assert result['selected']['reason'] == 'TREND_EXIT'
    assert result['selected']['quantity'] == '20.000000'


def test_disabled_stop_preserves_but_ignores_historical_latch(tmp_path):
    j = fixtures.journal(tmp_path)
    latch = dict(signal_date=signal()['date'], scope='ALL_ACCOUNT_SHARES')
    j.save('tqqq_stop_latch', latch)
    raw = fixtures.raw(values=dict(SMH='0', QQQ='0', TQQQ='100'))
    raw['positions']['TQQQ']['average_buy_price'] = '100'
    snapshot = normalize(raw)
    result = plan(snapshot, signal(), j, now=fixtures.NOW, risk_only=True)
    assert result['selected'] is None
    assert result['tqqq_protection']['enabled'] is False
    assert j.load('tqqq_stop_latch') == latch


def test_large_own_cash_purchase_and_daily_dedup_survive_restart(tmp_path):
    j = fixtures.journal(tmp_path); broker = fixtures.FakeBroker()
    result = cycle(j, broker, signal(), live=True, now=fixtures.NOW)
    assert broker.submits == 3
    assert D(100) < sum(D(r[0]) for r in j.db.execute('SELECT gross FROM etf_fills')) <= D('999.75')
    assert all(x['plan']['side'] == 'BUY' for x in result['executions'])
    assert not j.active()
    j.db.close(); j = Journal(tmp_path/'account.sqlite')
    cycle(j, broker, signal(), live=True, now=fixtures.NOW.replace(hour=16, minute=30))
    assert broker.submits == 3


@pytest.mark.parametrize('mode,expected', [('timeout','EXECUTION_UNKNOWN'), ('approval','AWAITING_USER_APPROVAL')])
def test_uncertainty_still_blocks_without_repeat_submission(tmp_path, mode, expected):
    j = fixtures.journal(tmp_path); broker = fixtures.FakeBroker(); broker.mode = mode
    cycle(j, broker, signal(), live=True, now=fixtures.NOW)
    assert broker.submits == 1 and j.active()[0]['status'] == expected
    j.db.close(); j = Journal(tmp_path/'account.sqlite')
    cycle(j, broker, signal(), live=True, now=fixtures.NOW)
    assert broker.submits == 1 and j.active()


@pytest.mark.parametrize('tool', ['review_equity_order', 'place_equity_order'])
def test_readonly_denies_writes_before_transport(tool):
    class Never:
        def call_tool(self, *args):
            raise AssertionError('transport called')
    transport = GuardedTransport(Never(), live=False)
    with pytest.raises(PermissionError, match='denied_before_transport'):
        transport.call_tool(tool, {})
    assert transport.calls == []


def test_telegram_describes_actual_restored_rules(tmp_path):
    from etf.telegram_reports import decision
    j = fixtures.journal(tmp_path)
    result = plan(normalize(fixtures.raw()), signal(), j, now=fixtures.NOW)
    text = decision(result)
    assert 'SMA200' in text and 'SMA100' not in text
    assert '6.6%' not in text and '每窗口合计$20' not in text
    assert '无策略每日／窗口金额上限' in text


def test_removed_window_returns_before_account_io(monkeypatch):
    import run_agent
    class RemovedSlot(datetime):
        @classmethod
        def now(cls, tz=None):
            value=cls(2026,10,16,14,30,tzinfo=timezone.utc)
            return value.astimezone(tz) if tz else value.replace(tzinfo=None)
    monkeypatch.setattr(run_agent, 'datetime', RemovedSlot)
    monkeypatch.setattr(sys, 'argv', ['run_agent.py','--live'])
    assert run_agent.main() == 0


def test_missing_200_day_evidence_fails_closed(tmp_path):
    j = fixtures.journal(tmp_path)
    with pytest.raises(KeyError):
        plan(normalize(fixtures.raw()), fixtures.sig(), j, now=fixtures.NOW)

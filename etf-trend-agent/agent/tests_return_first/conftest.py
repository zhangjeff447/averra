"""Keep synthetic executions on a real session, independently of test-run day."""
import sys
from datetime import datetime,timezone
import pytest

class SessionClock(datetime):
    @classmethod
    def now(cls,tz=None):
        value=cls(2026,10,16,14,5,tzinfo=timezone.utc)
        return value.astimezone(tz) if tz else value.replace(tzinfo=None)

@pytest.fixture(autouse=True)
def fixed_execution_clock(monkeypatch):
    from etf.calendar import ET
    for name in ('etf.runtime','etf.execution','etf.journal','etf.funds','run_agent'):
        module=__import__(name,fromlist=['datetime'])
        monkeypatch.setattr(module,'datetime',SessionClock)
    for name,module in list(sys.modules.items()):
        if name.split('.')[-1] in ('test_integration','test_recovery'):
            monkeypatch.setattr(module,'datetime',SessionClock)
            if hasattr(module,'NOW'):monkeypatch.setattr(module,'NOW',SessionClock.now(timezone.utc))
            if hasattr(module,'DAY'):monkeypatch.setattr(module,'DAY',SessionClock.now(ET).date().isoformat())


@pytest.fixture(autouse=True)
def frozen_legacy_sizing_assumptions(request,monkeypatch):
    # Original safety regression fixtures use uncapped historical amounts and
    # no minute feed. New cap/minute tests run the actual production policy.
    if request.node.module.__name__.split('.')[-1] in ('test_integration','test_recovery','test_manual_orders','test_stop_loss'):
        from etf.cycle import POLICY
        monkeypatch.setitem(POLICY,'daily_buy_limit',None)
        monkeypatch.setitem(POLICY,'intraday_enabled',False)


@pytest.fixture(autouse=True)
def frozen_previous_policy(request,monkeypatch):
    if request.node.module.__name__.split('.')[-1]=='test_restored_50200':return
    from etf.cycle import POLICY
    monkeypatch.setitem(POLICY,'slow_ma_period',100)
    monkeypatch.setitem(POLICY,'tqqq_stop_loss_enabled',True)
    monkeypatch.setitem(POLICY,'tqqq_stop_loss_pct','6.6')
    monkeypatch.setitem(POLICY,'tqqq_buy_requires_close_above_sma100',True)
    if request.node.module.__name__.split('.')[-1]=='test_intraday_limits':
        monkeypatch.setitem(POLICY,'daily_buy_limit','100.00')
        monkeypatch.setitem(POLICY,'per_order_limit','20.00')
        monkeypatch.setitem(POLICY,'per_window_buy_limit','20.00')
        monkeypatch.setitem(POLICY,'intraday_enabled',True)
        monkeypatch.setitem(POLICY,'slots_et',['10:05','10:30','12:30','15:30','15:45'])

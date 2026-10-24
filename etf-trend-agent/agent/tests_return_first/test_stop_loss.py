from decimal import Decimal as D
from datetime import datetime,timezone
import pytest
from test_integration import journal,raw,sig,FakeBroker
from etf.funds import normalize
from etf.cycle import plan
from etf.runtime import cycle
from etf.calendar import risk_session
from etf.recovery import require_window


def snapshot(bid='9.34',qty='10',cost='10'):
    r=raw(values=dict(SMH='0',QQQ='0',TQQQ=str(D(qty)*10)))
    r['positions']['TQQQ']['average_buy_price']=cost
    r['quotes']['TQQQ'].update(last=bid,bid=bid,ask=str(D(bid)+D('.01')))
    r['reported_net_assets']=str(D(r['funds']['reported_cash'])+D(qty)*D(bid))
    return normalize(r)


@pytest.mark.parametrize('bid,hit',[('9.3401',False),('9.34',True),('8',True)])
def test_cost_stop_boundary_all_account_shares(tmp_path,bid,hit):
    j=journal(tmp_path);r=plan(snapshot(bid,qty='13.123456'),sig(),j,now=datetime.now(timezone.utc),risk_only=True)
    assert bool(r['selected'])==hit
    if hit:
        assert r['selected']['side']=='SELL' and r['selected']['symbol']=='TQQQ'
        assert r['selected']['quantity']=='13.123456'
        assert r['selected']['reason']=='STOP_LOSS_6_6_ALL_TQQQ'
    assert j.load('tqqq_stop_latch') is None  # preview cannot arm live latch


def test_missing_cost_and_unavailable_shares_fail_closed(tmp_path):
    j=journal(tmp_path)
    with pytest.raises(ValueError):snapshot(cost=None)
    s=snapshot();s['sellable']['TQQQ']='9'
    with pytest.raises(ValueError,match='full_position_not_sellable'):plan(s,sig(),j,now=datetime.now(timezone.utc))


def test_close_below_100_blocks_only_new_tqqq(tmp_path):
    j=journal(tmp_path);s=snapshot('10',qty='0');market=sig();market['close']='99'
    r=plan(s,market,j,now=datetime.now(timezone.utc))
    assert r['phase']=='ATTACK' and r['selected']['symbol'] in ('SMH','QQQ')
    assert r['tqqq_protection']['buy_allowed'] is False
    market['close']='100'
    assert plan(s,market,j,now=datetime.now(timezone.utc))['selected']['symbol']=='TQQQ'


def test_latch_survives_restart_and_prevents_same_day_reentry(tmp_path):
    from etf.journal import Journal
    j=journal(tmp_path);market=sig();j.save('tqqq_stop_latch',dict(signal_date=market['date']))
    j.db.close();j=Journal(tmp_path/'account.sqlite')
    r=plan(snapshot('10',qty='0'),market,j,now=datetime.now(timezone.utc))
    assert r['selected']['symbol']!='TQQQ'
    market['date']='2026-10-21';market['close']='99'
    assert plan(snapshot('10',qty='0'),market,j,now=datetime.now(timezone.utc))['selected']['symbol']!='TQQQ'
    market['close']='100'
    assert plan(snapshot('10',qty='0'),market,j,now=datetime.now(timezone.utc))['selected']['symbol']=='TQQQ'


class StopBroker(FakeBroker):
    def read(self):
        r=super().read();r['quotes']['TQQQ'].update(last='9.34',bid='9.34',ask='9.35')
        r['reported_net_assets']=str(D(r['funds']['reported_cash'])+sum(D(p['quantity'])*D(r['quotes'][s]['last']) for s,p in r['positions'].items()))
        return r


@pytest.mark.parametrize('mode',['fill','timeout','approval','partial'])
def test_stop_execution_protected_and_no_rebuy(tmp_path,mode):
    j=journal(tmp_path);b=StopBroker(cash='0',values=dict(SMH='0',QQQ='0',TQQQ='100'));b.mode=mode
    from test_integration import NOW
    if mode=='partial':
        with pytest.raises(ValueError,match='open_or_unknown_broker_order'):cycle(j,b,sig(),live=True,now=NOW,risk_only=True)
        assert b.submits==1 and j.active() and j.load('tqqq_stop_latch')
        return
    result=cycle(j,b,sig(),live=True,now=NOW,risk_only=True)
    assert b.submits==1 and j.load('tqqq_stop_latch')
    assert all(x['plan']['symbol']=='TQQQ' and x['plan']['side']=='SELL' for x in result['executions'])
    assert j.rotation_available(NOW.date().isoformat())==0
    if mode=='fill':
        assert not j.active() and D(b.raw['positions']['TQQQ']['quantity'])==0
        assert result['reason']=='STOP_LOSS_FILLED_WAIT_SETTLEMENT'
    elif mode=='timeout':assert j.active()[0]['status']=='EXECUTION_UNKNOWN'
    elif mode=='approval':assert j.active()[0]['status']=='AWAITING_USER_APPROVAL'
    else:assert j.active()


def test_regular_hours_stop_gate_and_weekend():
    assert risk_session(datetime(2026,10,22,14,11,tzinfo=timezone.utc))
    assert not risk_session(datetime(2026,10,22,20,1,tzinfo=timezone.utc))
    assert not risk_session(datetime(2026,10,24,15,tzinfo=timezone.utc))
    with pytest.raises(ValueError,match='window_expired'):require_window('STOP:1999-01-01')


def test_falling_price_does_not_block_full_share_stop_with_five_dollar_rule(tmp_path):
    class Falling(StopBroker):
        reads=0
        def read(self):
            self.reads+=1;r=super().read()
            if self.reads>=3:
                r['quotes']['TQQQ'].update(last='8',bid='8',ask='8.01')
                r['reported_net_assets']=str(D(r['funds']['reported_cash'])+sum(D(p['quantity'])*D(r['quotes'][s]['last']) for s,p in r['positions'].items()))
            return r
    j=journal(tmp_path);b=Falling(cash='0',values=dict(SMH='0',QQQ='0',TQQQ='100'))
    from test_integration import NOW
    cycle(j,b,sig(),live=True,now=NOW,risk_only=True)
    assert b.submits==1 and not j.active() and D(b.raw['positions']['TQQQ']['quantity'])==0

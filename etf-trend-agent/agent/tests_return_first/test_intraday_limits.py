import copy,json
from datetime import datetime,timedelta,timezone
from decimal import Decimal as D
import pytest
from test_integration import FakeBroker,journal,raw,sig,buy
from etf.calendar import ET,scheduled
from etf.cycle import POLICY,plan
from etf.funds import normalize
from etf.intraday import evaluate
from etf.buy_limits import usage,allowance,slot_key
from etf.runtime import cycle


def at_slot(h=10,m=5,day=16):return datetime(2026,10,day,h,m,tzinfo=ET).astimezone(timezone.utc)


def package(now):
    start=now.astimezone(ET).replace(hour=9,minute=30,second=0,microsecond=0).astimezone(timezone.utc)
    bars=[];at=start
    while at+timedelta(minutes=1)<=now:
        bars.append(dict(begins_at=at.isoformat(),open_price='9.99',high_price='10.01',low_price='9.98',close_price='10',volume=1000,session='reg',interpolated=False));at+=timedelta(minutes=1)
    return dict(source='broker_mcp',tool='get_equity_historicals',retrieved_at=now.isoformat(),symbols={s:dict(symbol=s,interval='minute',bounds='regular',bars=copy.deepcopy(bars)) for s in ('SMH','QQQ','TQQQ')})


def quote():return dict(last='10',bid='9.999',ask='10.001')


class MinuteBroker(FakeBroker):
    def read(self):
        r=super().read()
        for q in r['quotes'].values():q.update(quote())
        return r
    def minute_data(self,now):return package(now)


def test_production_caps_and_slots():
    assert POLICY['daily_buy_limit']=='100.00' and POLICY['per_window_buy_limit']=='20.00'
    assert POLICY['intraday_enabled'] is True
    assert scheduled(at_slot(10,30))=='10:30' and scheduled(at_slot(15,45))=='15:45'
    assert scheduled(at_slot(15,55)) is None


def test_completed_bars_buy_and_forming_bar_ignored():
    now=at_slot();p=package(now)
    r=evaluate(p,'SMH',quote(),now)
    assert r['decision']=='BUY' and r['five_minute_end']==now.isoformat()
    assert r['fifteen_minute_end']==at_slot(10,0).isoformat()
    forming=dict(p['symbols']['SMH']['bars'][-1],begins_at=now.isoformat(),close_price='99999')
    p['symbols']['SMH']['bars'].append(forming)
    assert evaluate(p,'SMH',quote(),now)==r


@pytest.mark.parametrize('bad',['stale','future','gap','duplicate','synthetic','premarket','zero_volume','bad_ohlc','wrong_symbol','wrong_interval','old_minutes','future_bar'])
def test_invalid_minutes_wait(bad):
    now=at_slot();p=package(now);bars=p['symbols']['SMH']['bars']
    if bad=='stale':p['retrieved_at']=(now-timedelta(seconds=61)).isoformat()
    elif bad=='future':p['retrieved_at']=(now+timedelta(seconds=1)).isoformat()
    elif bad=='gap':bars.pop(10)
    elif bad=='duplicate':bars.append(copy.deepcopy(bars[-1]))
    elif bad=='synthetic':bars[5]['interpolated']=True
    elif bad=='premarket':bars[0]['session']='pre'
    elif bad=='zero_volume':bars[5]['volume']=0
    elif bad=='bad_ohlc':bars[5]['close_price']='999'
    elif bad=='wrong_symbol':p['symbols'].pop('SMH')
    elif bad=='wrong_interval':p['symbols']['SMH']['interval']='5minute'
    elif bad=='old_minutes':del bars[-2:]
    elif bad=='future_bar':bars[-1]['begins_at']=(now+timedelta(minutes=1)).isoformat()
    assert evaluate(p,'SMH',quote(),now)['decision']=='WAIT'


@pytest.mark.parametrize('kind,reason',[('chase','INTRADAY_ABOVE_VWAP_BAND'),('fall','INTRADAY_NOT_STABILIZED'),('volume','INTRADAY_VOLUME_THIN'),('spread','INTRADAY_SPREAD_WIDE')])
def test_numeric_intraday_wait_rules(kind,reason):
    now=at_slot();p=package(now);q=quote();bars=p['symbols']['SMH']['bars']
    if kind=='chase':q['ask']='10.10'
    elif kind=='fall':bars[-1]['close_price']='9.99';bars[-5]['open_price']='10.01'
    elif kind=='volume':
        for b in bars[-5:]:b['volume']=1
    else:q['ask']='10.02';q['bid']='9.98'
    r=evaluate(p,'SMH',q,now)
    assert r['decision']=='WAIT' and reason in r['reasons']


def test_live_fake_three_symbols_share_twenty_dollars(tmp_path):
    from test_integration import NOW
    j=journal(tmp_path);b=MinuteBroker()
    r=cycle(j,b,sig(),live=True,now=NOW)
    assert b.submits==3 and usage(j,NOW.astimezone(ET).date().isoformat())==D('19.98')
    amounts={x['plan']['symbol']:D(x['plan']['amount']) for x in r['executions']}
    assert amounts=={'TQQQ':D('6'),'SMH':D('11.99'),'QQQ':D('1.99')}
    assert b.raw['positions']['TQQQ']['quantity']=='0.60'
    cycle(j,b,sig(),live=True,now=NOW)
    assert b.submits==3 and allowance(j,NOW)==D('.02')


def reserve(j,now,amount='20',symbol='SMH'):
    key=slot_key(now)
    j.save('rf_window_allocation:'+key,dict(policy_version=POLICY['version'],amounts={symbol:'20'},window_key=key))
    order=dict(buy(symbol,amount),window_key=key)
    return j.reserve(key+symbol,order,normalize(raw(),now=at_slot()),now=now)


def fill(j,row,now,amount):
    j.db.execute("UPDATE etf_intents SET status='FILLED',attempts=1 WHERE id=?",(row['id'],))
    j.db.execute('INSERT INTO etf_fills VALUES(?,?,?,?,?,?)',(row['id'],row['id'],amount,'0','1',now.isoformat()))


def test_five_windows_shared_daily_cap_and_restart(tmp_path):
    from etf.journal import Journal
    j=journal(tmp_path)
    for h,m in [(10,5),(10,30),(12,30),(15,30),(15,45)]:
        now=at_slot(h,m);row=reserve(j,now);fill(j,row,now,'20')
    j.db.close();j=Journal(tmp_path/'account.sqlite')
    assert usage(j,'2026-10-16')==100
    assert allowance(j,at_slot(15,45))==0
    with pytest.raises(ValueError,match='shared_buy_limit'):reserve(j,at_slot(15,45),amount='1',symbol='QQQ')
    assert allowance(j,at_slot(day=19))==20


def test_partial_unknown_reservation_not_released_by_day_change(tmp_path):
    j=journal(tmp_path);row=reserve(j,at_slot(),amount='20')
    j.db.execute("UPDATE etf_intents SET status='EXECUTION_UNKNOWN',attempts=1 WHERE id=?",(row['id'],))
    j.db.execute('INSERT INTO etf_fills VALUES(?,?,?,?,?,?)',('partial',row['id'],'7','0','.7',at_slot().isoformat()))
    assert usage(j,'2026-10-16')==20
    assert usage(j,'2026-10-19')==13
    j.db.execute("UPDATE etf_intents SET status='CANCELED' WHERE id=?",(row['id'],))
    assert usage(j,'2026-10-16')==7 and usage(j,'2026-10-19')==0


def test_limits_at_reserve_and_before_submit(tmp_path):
    j=journal(tmp_path)
    with pytest.raises(ValueError,match='shared_buy_limit'):reserve(j,at_slot(),amount='20.01')
    row=reserve(j,at_slot(),amount='20')
    # Simulate another already-dated Agent fill discovered before transport.
    j.db.execute("INSERT INTO etf_intents VALUES(?,?,?,?,?,?,?,?,?,?,?)",('prior','prior','QQQ','BUY','90','0','FILLED','known',1,at_slot().isoformat(),'{}'))
    j.db.execute('INSERT INTO etf_fills VALUES(?,?,?,?,?,?)',('priorfill','prior','90','0','9',at_slot().isoformat()))
    with pytest.raises(ValueError,match='shared_buy_limit'):j.begin_submit(row['id'])
    assert j.get(row['id'])['attempts']==0


def test_wait_symbol_keeps_its_window_allocation(tmp_path):
    from test_integration import NOW
    class OneWait(MinuteBroker):
        def minute_data(self,now):
            p=package(now);p['symbols'].pop('TQQQ');return p
    j=journal(tmp_path);b=OneWait();r=cycle(j,b,sig(),live=True,now=NOW)
    assert b.submits==2
    amounts={x['plan']['symbol']:D(x['plan']['amount']) for x in r['executions']}
    assert amounts=={'SMH':D('11.99'),'QQQ':D('1.99')}
    assert usage(j,'2026-10-16')==D('13.98')
    assert r['intraday']['TQQQ']['decision']=='WAIT'


def test_minute_outage_cannot_block_stop_or_allow_buy(tmp_path):
    from test_integration import NOW
    class Missing(FakeBroker):
        def minute_data(self,now):raise TimeoutError()
    j=journal(tmp_path);b=Missing();r=cycle(j,b,sig(),live=True,now=NOW)
    assert not b.submits and r['selected'] is None
    from test_stop_loss import StopBroker
    b=StopBroker(cash='0',values=dict(SMH='0',QQQ='0',TQQQ='100'))
    b.minute_data=lambda _:pytest.fail('stop must not wait for minute feed')
    r=cycle(j,b,sig(),live=True,now=NOW,risk_only=True)
    assert b.submits==1 and r['executions'][0]['plan']['side']=='SELL'


def test_previous_policy_rotation_not_reset(tmp_path):
    j=journal(tmp_path)
    j.db.execute("INSERT INTO etf_intents VALUES(?,?,?,?,?,?,?,?,?,?,?)",('old','old','SMH','BUY','10','0','FILLED','oldbroker',1,at_slot().isoformat(),json.dumps(dict(policy_version='previous-version',reason='FULL_TQQQ_PROCEEDS_TO_CORE'))))
    j.db.execute('INSERT INTO etf_fills VALUES(?,?,?,?,?,?)',('oldfill','old','10','0','1',at_slot().isoformat()))
    assert j.rotation_spent()==10


def test_native_optional_false_flag_requires_actual_trade_volume():
    now=at_slot();p=package(now)
    for b in p['symbols']['SMH']['bars']:b.pop('interpolated')
    assert evaluate(p,'SMH',quote(),now)['decision']=='BUY'
    p['symbols']['SMH']['bars'][0]['volume']=0
    assert evaluate(p,'SMH',quote(),now)['decision']=='WAIT'


def test_history_tool_is_read_only_and_shape_bounded():
    from etf.broker import NativeAdapter,GuardedTransport
    class Transport:
        def call_tool(self,name,params):return {'data':{'results':[]}}
    assert 'get_equity_historicals' in NativeAdapter.ALLOWED_MCP_TOOLS
    guarded=GuardedTransport(Transport(),live=False)
    with pytest.raises(PermissionError):guarded.call_tool('place_equity_order',{})
    with pytest.raises(PermissionError):guarded.call_tool('get_equity_historicals',dict(symbols=['SMH'],interval='minute',bounds='extended',adjustment_type='none'))
    result=guarded.call_tool('get_equity_historicals',dict(symbols=['SMH','QQQ','TQQQ'],interval='minute',bounds='regular',adjustment_type='none'))
    assert result['data']['results']==[]


def test_report_displays_shared_caps_without_unlimited_claim(tmp_path):
    from etf.service_reports import holdings_report
    from test_integration import NOW
    j=journal(tmp_path)
    body=holdings_report(j,raw(),{},NOW)
    assert '每日合计$100.00' in body and '每窗口合计$20.00' in body
    assert '不设固定' not in body


def test_rotation_history_stays_identical_across_policy_versions(tmp_path):
    j=journal(tmp_path)
    j.db.execute("INSERT INTO etf_intents VALUES(?,?,?,?,?,?,?,?,?,?,?)",('s','s','TQQQ','SELL','100','10','FILLED','sale',1,at_slot().isoformat(),json.dumps(dict(policy_version='older-policy',reason='TREND_EXIT'))))
    j.db.execute('INSERT INTO etf_fills VALUES(?,?,?,?,?,?)',('sf','s','100','0','10',at_slot().isoformat()))
    j.sync_rotation();before=[tuple(r) for r in j.db.execute('SELECT * FROM rf_rotation')]
    j.sync_rotation()
    assert before==[tuple(r) for r in j.db.execute('SELECT * FROM rf_rotation')] and len(before)==1
    assert j.unsettled_sale_cash('2026-10-16')==100


def test_same_etf_can_buy_in_second_window_after_restart(tmp_path,monkeypatch):
    from etf.journal import Journal
    import importlib
    from test_integration import NOW
    j=journal(tmp_path);b=MinuteBroker()
    cycle(j,b,sig(),live=True,now=NOW)
    assert b.submits==3
    j.db.close();j=Journal(tmp_path/'account.sqlite')
    later=at_slot(10,30)
    class Later(datetime):
        @classmethod
        def now(cls,tz=None):return later.astimezone(tz) if tz else later.replace(tzinfo=None)
    for module in ('etf.runtime','etf.execution','etf.journal','etf.funds','test_integration'):
        monkeypatch.setattr(importlib.import_module(module),'datetime',Later)
    cycle(j,b,sig(),live=True,now=later)
    assert b.submits==6
    for symbol in ('QQQ','SMH','TQQQ'):
        plans=[json.loads(r[0]) for r in j.db.execute("SELECT plan FROM etf_intents WHERE side='BUY' AND symbol=?",(symbol,))]
        assert {p['window_key'] for p in plans}=={'2026-10-16:10:05','2026-10-16:10:30'}
    assert usage(j,'2026-10-16')<=40
    cycle(j,b,sig(),live=True,now=later)
    assert b.submits==6

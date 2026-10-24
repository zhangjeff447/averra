import copy,json
from datetime import datetime,timezone,timedelta
from decimal import Decimal
import pytest
import test_integration as fixture
from etf.reconciliation import validate_activation_positions
from etf.runtime import cycle
from etf.funds import normalize


def manual(tmp_path,symbol='SMH',side='buy',state='filled',quantity='1'):
    j=fixture.journal(tmp_path);r=fixture.raw(values={s:'20' for s in ('SMH','QQQ','TQQQ')})
    j.save('rf_baseline',dict(orders=[],fills=[],quantities={s:p['quantity'] for s,p in r['positions'].items()}))
    at=(datetime.now(timezone.utc)-timedelta(minutes=1)).isoformat()
    o=dict(id='manual-order',ref_id='user-reference',symbol=symbol,side=side,state=state,placed_agent='user',agent_initiated=False,created_at=at,cumulative_quantity=quantity,executions=[])
    if Decimal(quantity):o['executions']=[dict(id='manual-fill',quantity=quantity,price='10',fees='0',currency='USD',timestamp=at)]
    r['orders']=[o];r['positions'][symbol]['quantity']=str(Decimal(r['positions'][symbol]['quantity'])+Decimal(quantity)*(1 if side=='buy' else -1))
    return j,r


@pytest.mark.parametrize('symbol',['QQQ','SMH','TQQQ'])
@pytest.mark.parametrize('side',['buy','sell'])
def test_manual_fills_reconcile_without_agent_spend_or_limits(tmp_path,symbol,side):
    j,r=manual(tmp_path,symbol,side);baseline=j.load('rf_baseline')
    validate_activation_positions(j,r);validate_activation_positions(j,r)
    assert j.load('rf_baseline')==baseline
    assert j.db.execute('SELECT count(*) FROM etf_intents').fetchone()[0]==0
    assert j.db.execute('SELECT count(*) FROM etf_fills').fetchone()[0]==0
    assert j.db.execute('SELECT count(*) FROM rf_actions').fetchone()[0]==0
    assert j.db.execute('SELECT count(*) FROM rf_rotation').fetchone()[0]==0
    assert j.db.execute("SELECT count(*) FROM etf_audit WHERE event='EXTERNAL_OR_MANUAL_ACTIVITY'").fetchone()[0]==1
    assert j.load('rf_external_order:manual-order')['agent_spend_attributed'] is False
    assert j.daily_spend('BUY',fixture.NOW)==0 and not j.gates({},fixture.NOW)


@pytest.mark.parametrize('state',['canceled','cancelled','rejected','failed','expired','voided'])
def test_terminal_manual_orders_without_fills_are_not_agent_activity(tmp_path,state):
    j,r=manual(tmp_path,state=state,quantity='0');validate_activation_positions(j,r)
    assert not j.active()


def test_partial_fill_with_rest_cancelled_reconciles_only_actual_quantity(tmp_path):
    j,r=manual(tmp_path,state='partially_filled_rest_cancelled',quantity='.5');validate_activation_positions(j,r)
    assert j.load('rf_external_order:manual-order')['executions'][0]['quantity']=='0.5'


@pytest.mark.parametrize('change,reason',[
    (lambda r:r['orders'][0].update(agent_initiated=True),'unattributed'),
    (lambda r:r['orders'][0].update(agent_initiated=None),'unattributed'),
    (lambda r:r['orders'][0].update(placed_agent='unknown'),'unattributed'),
    (lambda r:r['orders'][0].update(state='partially_filled'),'not_terminal'),
    (lambda r:r['orders'][0].update(cumulative_quantity='2'),'cumulative'),
    (lambda r:r['orders'][0].update(executions=None),'evidence_missing'),
    (lambda r:r['orders'][0]['executions'][0].update(currency='CAD'),'evidence_invalid'),
    (lambda r:r['positions']['SMH'].update(quantity='99'),'position_history'),
    (lambda r:r.update(complete_orders=False),'history_incomplete'),
    (lambda r:r.update(account_hash='b'*64),'account_mismatch'),
    (lambda r:r['orders'][0]['executions'][0].update(timestamp=(datetime.now(timezone.utc)+timedelta(days=1)).isoformat()),'evidence_invalid'),
])
def test_inconsistent_manual_evidence_still_blocks_without_audit(tmp_path,change,reason):
    j,r=manual(tmp_path);change(r)
    with pytest.raises(ValueError,match=reason):validate_activation_positions(j,r)
    assert j.load('rf_external_order:manual-order') is None


def test_manual_label_cannot_hide_local_submission(tmp_path):
    j,r=manual(tmp_path)
    row=j.reserve('local',fixture.buy(),normalize(fixture.raw()))
    r['orders'][0]['ref_id']=row['id']
    with pytest.raises(ValueError,match='identity_conflict'):validate_activation_positions(j,r)


def test_manual_fill_does_not_clear_unknown_agent_intent(tmp_path):
    j,r=manual(tmp_path);row=j.reserve('local',fixture.buy(),normalize(fixture.raw()))
    j.begin_submit(row['id']);j.status(row['id'],'EXECUTION_UNKNOWN')
    validate_activation_positions(j,r)
    assert j.active()[0]['status']=='EXECUTION_UNKNOWN' and j.active()[0]['attempts']==1
    assert j.daily_spend('BUY',fixture.NOW)==Decimal('200')


@pytest.mark.parametrize('mutation,reason',[
    (lambda r:r['orders'][0]['executions'][0].update(price='11'),'facts_changed'),
    (lambda r:r.update(orders=[]),'history_missing'),
])
def test_recorded_external_facts_cannot_change_or_disappear(tmp_path,mutation,reason):
    j,r=manual(tmp_path);validate_activation_positions(j,r);mutation(r)
    with pytest.raises(ValueError,match=reason):validate_activation_positions(j,r)


def test_manual_fill_does_not_block_natural_wait_or_create_order(tmp_path):
    j,r=manual(tmp_path);b=fixture.FakeBroker(cash='0');b.raw=r
    b.raw['funds']['reported_cash']='0'
    result=cycle(j,b,fixture.sig(),live=False,now=fixture.NOW)
    assert result['selected'] is None and b.submits==b.reviews==0


def test_manual_fill_does_not_bypass_funds_verification(tmp_path):
    j,r=manual(tmp_path);b=fixture.FakeBroker();b.raw=r;b.raw['funds']['margin_used']='1'
    with pytest.raises(ValueError,match='margin'):cycle(j,b,fixture.sig(),live=False,now=fixture.NOW)
    assert b.submits==b.reviews==0

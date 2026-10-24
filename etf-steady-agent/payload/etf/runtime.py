"""One cycle and account journal; no large-model calls or continuous monitor."""
import json
from datetime import datetime,timezone
from decimal import Decimal
from pathlib import Path

from .cycle import POLICY,advance,phase_observation,plan,money
from .funds import normalize
from strategies.qqq_smh_agent.cash_regime_v2 import observations,advance as old_advance,add_cash_capacity,release_context
from strategies.qqq_smh_agent.smh_long_term import features
from strategies.qqq_smh_agent.indicators import rsi_from_closes
from strategies.qqq_smh_agent.strategy_agent import StrategyAgent

ROOT=Path(__file__).resolve().parents[1]
OLD_POLICY=json.loads((ROOT/'config/cash_regime_policy.json').read_text())


def market_trend(observation, current):
    """Describe completed core-market signals; never change trading eligibility."""
    items=[observation['symbols'][s] for s in ('SMH','QQQ')]
    if current['normal']:
        name='上涨趋势'
    elif current['recovery']:
        name='复苏反弹'
    elif all(x['weak'] for x in items):
        name='下跌中反弹' if all(x['recovering'] for x in items) else '下跌趋势'
    elif observation['risk']:
        name='偏弱回调'
    elif any(x['weak'] for x in items) or (any(x['strong'] for x in items) and not all(x['strong'] for x in items)):
        name='走势分化'
    else:
        name='正常震荡'
    return dict(name=name,date=observation['date'],scope='QQQ/SMH')


def signals(dates,frames,journal,snapshot):
    # Dividend-adjusted closes are used only for the existing signal engine.
    closes={s:[float(x) for x in frames[s]['Adj Close']] for s in ('SMH','QQQ')}
    volatility={s:[float(x) for x in frames['^'+s]['Close']] for s in ('VIX','VXN')}
    obs=observations(dates,closes,volatility,OLD_POLICY)
    legacy=journal.load('legacy_regime')
    phase=journal.load('phase')
    complete=money(snapshot['values']['SGOV'])<=money(snapshot['net_assets'])*Decimal('.05')
    bootstrapping=phase is None
    # A new input hash belongs to v3. Preserve migrated anchors/grants, and
    # never replay a completed legacy date with a different history horizon.
    import hashlib
    input_hash=hashlib.sha256(json.dumps(dict(date=dates[-1],closes=closes,volatility=volatility),sort_keys=True).encode()).hexdigest()
    previous_input=journal.load('v3_daily_input')
    if previous_input and previous_input['date']==dates[-1] and previous_input['hash']!=input_hash:
        raise ValueError('completed_input_changed')
    # No historical rotation task existed before v3. Replay market phases
    # independently of today's holdings, then create only the current episode.
    for o in obs:
        if legacy is None or o['date']>legacy['last_date']:
            legacy=old_advance(legacy,o,OLD_POLICY)
        i=dates.index(o['date'])
        sliced={s:closes[s][:i+1] for s in ('SMH','QQQ')}
        current=phase_observation(o,dates[:i+1],sliced)
        phase=advance(phase,current,release_complete=True if bootstrapping else complete)
    from datetime import timedelta,date
    first=(date.fromisoformat(dates[-1])-timedelta(days=499)).isoformat()
    start=next(i for i,d in enumerate(dates) if d>=first)
    recent_dates=dates[start:]
    recent_closes={s:v[start:] for s,v in closes.items()}
    latest=features(recent_dates,recent_closes['SMH'],rsi_from_closes(recent_closes['SMH']))
    legacy['smh_feature']=latest
    if legacy.get('smh_feature_date')!=dates[-1]:
        legacy['smh_clear_count']=legacy.get('smh_clear_count',0)+1 if not latest['weak'] else 0
        legacy['smh_feature_date']=dates[-1]
    total=money(snapshot['net_assets'])
    gaps={s:max(Decimal(0),total*w-money(snapshot['values'][s])) for s,w in [('SMH',Decimal('.70')),('QQQ',Decimal('.20'))]}
    # Cash+buys-sales is invariant under internal SGOV rotation. Proceeds cannot
    # reappear as a new contribution/stage grant after every sale.
    flow=journal.total_flow('BUY')-journal.total_flow('SELL')
    legacy=add_cash_capacity(legacy,int(money(snapshot['cash_for_weight'])*100),int(flow*100),gaps,OLD_POLICY)
    with journal.transaction():
        journal.save('legacy_regime',legacy);journal.save('phase',phase)
        journal.save('v3_daily_input',dict(date=dates[-1],hash=input_hash))
    usage={}
    for row in journal.db.execute("SELECT id,symbol,amount,status,plan FROM etf_intents WHERE side='BUY'"):
        order=json.loads(row['plan']);ctx=order.get('legacy_context') or {}
        if ctx.get('episode_id')!=legacy.get('episode_id') or not ctx.get('stage_id'):continue
        item=usage.setdefault(row['symbol'],{}).setdefault(ctx['stage_id'],dict(filled_cents=0,reserved_cents=0))
        filled=journal.filled_gross(row['id']);item['filled_cents']+=int(filled*100)
        if row['status'] not in ('FILLED','CANCELED','REJECTED'):item['reserved_cents']+=int(max(Decimal(0),money(row['amount'])-filled)*100)
    # Migrated old grants/fills are counted before any new release.
    tables={r[0] for r in journal.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if 'regime_intents' in tables:
        for row in journal.db.execute('SELECT r.symbol,r.stage_id,i.cumulative_filled_cents,i.reserved_remaining_cents FROM regime_intents r JOIN intents i ON i.intent_id=r.intent_id WHERE r.episode_id=?',(legacy.get('episode_id'),)):
            item=usage.setdefault(row[0],{}).setdefault(row[1],dict(filled_cents=0,reserved_cents=0));item['filled_cents']+=row[2];item['reserved_cents']+=row[3]
    context=release_context(legacy,usage,OLD_POLICY)
    context.update(smh_policy_version='SMH_EMA20_PULLBACK',smh_feature=latest,
                   smh_local_clear=legacy.get('smh_clear_count',0)>=2,
                   depth={s:latest['drawdown60'] if s=='SMH' else max(0,1-closes[s][-1]/max(closes[s][-60:])) for s in ('SMH','QQQ')})
    context['market_trend']=market_trend(o,current)
    # Existing scoring receives the same volatility adjustment as production.
    from tools.volatility_context import VolatilityProvider
    provider=VolatilityProvider()
    vol=provider.fetch()
    decisions=StrategyAgent().evaluate_signals({},0,recent_closes,volatility_context=vol)['decisions']
    scores={s:d['metadata']['signal_score'] for s,d in decisions.items()}
    return phase,scores,context


def planned(journal,snapshot,dates,phase,scores,context,*,exclude_intent=None):
    now=datetime.now(timezone.utc)
    context={k:v for k,v in context.items() if k!='market_trend'}
    return plan(snapshot,phase,scores,context,
                buy_remaining=max(Decimal(0),100-journal.daily_spend('BUY',now,exclude_intent=exclude_intent)),
                sell_remaining=max(Decimal(0),100-journal.daily_spend('SELL',now,exclude_intent=exclude_intent)),
                gates=journal.gates(scores,now),
                sgov_buy_cooled=journal.cooled('BUY',dates),sgov_sell_cooled=journal.cooled('SELL',dates))


def report(result):
    labels={'NORMAL':'正常配置','PULLBACK':'回调','RECOVERY':'复苏'}
    trend=result.get('market_trend')
    lines=['ETF Agent · SMH / QQQ / SGOV', '市场行情：'+(trend['name']+'（'+trend['scope']+'，'+trend['date']+' 收盘）' if trend else '数据待确认'),
           '策略阶段：'+labels.get(result.get('phase'),str(result.get('phase','未核实')))]
    if result.get('scores'):
        lines+=['评分：'+' / '.join(s+' '+str(v) for s,v in result['scores'].items())]
    if result.get('weights'):
        lines+=['持仓：'+' / '.join(s+' '+format(Decimal(w)*100,'.2f')+'%' for s,w in result['weights'].items()),
                '可用自有资金：$'+result['safe_cash']]
    selected=result.get('selected')
    reason=str(result.get('reason'))
    reasons={'ORIGINAL_8020_BUY':'满足策略的买入条件',
             'WAIT_ORIGINAL_8020':'当前没有同时满足评分、配比和资金要求的买入机会',
             'ValueError:portfolio_value_unreconciled':'账户总值与持仓估值暂未对齐；保留对账保护',
             'BLOCKED_BY_UNRESOLVED_INTENT':'已有订单等待对账，暂不新增订单',
             'WAIT_PENDING_APPROVAL':'等待券商审批，不重复提交'}
    execution=result.get('execution','只读预览' if result.get('status')!='LIVE' else '尚未执行')
    if isinstance(execution,dict):
        status=execution.get('status','未确认')
        execution={'SUBMITTED':'订单已提交，等待券商确认成交','FILLED':'已成交',
                   'AWAITING_USER_APPROVAL':'等待人工审批，不重复提交',
                   'EXECUTION_UNKNOWN':'券商结果未确认；保留订单并等待对账，不重复提交',
                   'WAIT':'本次未下单','RECONCILIATION_REQUIRED':'等待订单对账，不新增订单'}.get(status,status)
    lines+=['计划：'+(selected['side']+' '+selected['symbol']+' $'+selected['amount'] if selected else 'WAIT'),
            '原因：'+reasons.get(reason,reason), '执行状态：'+str(execution)]
    return '\n'.join(lines)


def cycle(journal,broker,dates,frames,*,live=False,slot=None):
    if live and slot and journal.db.execute('SELECT 1 FROM etf_slots WHERE slot=?',(slot,)).fetchone() is not None:
        return dict(reason='WAIT_SLOT_ALREADY_USED',selected=None,suppress_notification=True)
    if journal.halt_reason():return dict(reason='TRADING_HALTED:'+journal.halt_reason(),selected=None)
    raw=broker.read()
    from .operational import record
    record(slot or 'READ_ONLY','ACCOUNT_READ',dict(account_as_of=raw.get('as_of'),source=raw.get('source')))
    journal.reconcile(raw['orders'],complete=raw['complete_orders'])
    from .reconciliation import validate_history
    history_check=validate_history(journal,raw)
    if journal.active() or journal.legacy_active():return dict(reason='BLOCKED_BY_UNRESOLVED_INTENT',selected=None)
    if raw.get('pending_approvals'):return dict(reason='WAIT_PENDING_APPROVAL',selected=None)
    acceptance=dict(history_check=history_check,account_identity_verified=True,complete_orders=raw['complete_orders'],
                    account_type=raw.get('account_type'),agentic_confirmed=raw.get('agentic_confirmed'),
                    optional_component_fields_absent=raw.get('missing_funding_fields',[]),
                    funds_basis='Agentic limited margin capability plus current cash and unleveraged buying power' if raw.get('account_type')=='limited_margin' else 'explicit component reconciliation',
                    approval_enabled=raw.get('approval_enabled'),
                    pending_approval_count=len(raw.get('pending_approvals',[])),
                    broker_write_calls=0 if not live else None)
    (ROOT.parent/'evidence/broker-acceptance-evidence.json').write_text(json.dumps(acceptance,indent=2))
    snapshot=normalize(raw);journal.bind_account(snapshot['account_hash'])
    acceptance.update(funds_normalization='PASS',margin_evidence=snapshot['margin_evidence'])
    (ROOT.parent/'evidence/broker-acceptance-evidence.json').write_text(json.dumps(acceptance,indent=2))
    if 'external_cash_events' in snapshot['funds']:
        journal.observe_external_cash_events(snapshot['funds']['external_cash_events'])
    phase,scores,context=signals(dates,frames,journal,snapshot)
    result=planned(journal,snapshot,dates,phase,scores,context)
    result['market_trend']=context['market_trend']
    result['scores']=scores
    result['score_at']=datetime.now(timezone.utc).isoformat()
    record(slot or 'READ_ONLY','DECISION',dict(selected=result.get('selected'),scores=scores,market_trend=result['market_trend'],policy_version=POLICY['version']))
    if live:
        if POLICY['activation']!='LIVE_VERIFIED':raise PermissionError('broker_acceptance_required')
        if not slot or not journal.claim_slot(slot):
            return dict(reason='WAIT_SLOT_ALREADY_USED',selected=None,suppress_notification=True)
        order=result.get('selected')
        if order:order['all_scores']=scores
        from .execution import execute
        def fresh_plan(fresh,exclude_intent=None):
            if (datetime.now(timezone.utc)-datetime.fromisoformat(result['score_at'])).total_seconds()>60:
                raise ValueError('score_stale_before_submit')
            planned_result=planned(journal,fresh,dates,phase,scores,context,exclude_intent=exclude_intent)
            if planned_result.get('selected'):planned_result['selected']['all_scores']=scores
            return planned_result
        result['execution']=execute(journal,broker,slot,result,replan=fresh_plan,live=True)
        record(slot,'ORDER_EXECUTION',result['execution'])
    # Display data cannot alter an order, funds gate, or the production ledger.
    from .notice_details import collect
    notice_raw=raw;before_order=False
    if live and result.get('execution',{}).get('intent_id'):
        try:notice_raw=broker.read()
        except Exception:before_order=True
    try:result['notice']=collect(journal,notice_raw,result,slot,before_order=before_order)
    except Exception as exc:
        result['notice']={}
        from .operational import failure
        failure(slot or 'READ_ONLY',type(exc).__name__+':'+str(exc))
    record(slot or 'READ_ONLY','REPORT_SNAPSHOT',dict(notice=result['notice'],market_trend=result.get('market_trend'),scores=result.get('scores')))
    return result

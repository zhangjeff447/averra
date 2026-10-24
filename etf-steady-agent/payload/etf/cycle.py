"""Persistent completed-session phase rules and pure three-ETF planning."""
import copy
import hashlib
import json
from decimal import Decimal, ROUND_DOWN
from pathlib import Path

from strategies.qqq_smh_agent.score_dynamic_v1 import plan as original_plan

SYMBOLS = ('SMH', 'QQQ', 'SGOV')
CORE = ('SMH', 'QQQ')
ZERO = Decimal('0')
POLICY = json.loads(Path(__file__).with_name('policy.json').read_text())


def money(value):
    if value is None or isinstance(value, bool):
        raise ValueError('money_missing')
    result = Decimal(str(value))
    if not result.is_finite() or result < 0:
        raise ValueError('money_invalid')
    return result


def down(value):
    return value.quantize(Decimal('.01'), rounding=ROUND_DOWN)


def advance(previous, observation, *, release_complete, policy=POLICY):
    state = copy.deepcopy(previous or dict(phase='NORMAL', last_date=None, risk_count=0,
                                         recovery_count=0, normal_count=0, episode_id=None))
    raw = json.dumps(observation, sort_keys=True)
    fingerprint = hashlib.sha256(raw.encode()).hexdigest()
    day = observation['date']
    if state['last_date'] and day <= state['last_date']:
        if day == state['last_date'] and state['input_hash'] != fingerprint:
            raise ValueError('same_session_data_changed')
        return state
    state.update(last_date=day, input_hash=fingerprint)
    state['risk_count'] = state['risk_count'] + 1 if observation['risk'] else 0
    state['recovery_count'] = state['recovery_count'] + 1 if observation['recovery'] else 0
    state['normal_count'] = state['normal_count'] + 1 if observation['normal'] else 0
    if state['phase'] == 'NORMAL' and state['risk_count'] >= policy['entry_sessions']:
        state.update(phase='PULLBACK', episode_id='pullback:' + day, recovery_count=0, normal_count=0)
    elif state['phase'] == 'PULLBACK' and state['recovery_count'] >= policy['recovery_sessions']:
        state.update(phase='RECOVERY', normal_count=0)
    elif state['phase'] == 'RECOVERY':
        if observation['failed_reversal'] or state['risk_count'] >= policy['entry_sessions']:
            state.update(phase='PULLBACK', recovery_count=0, normal_count=0)
        elif release_complete and state['normal_count'] >= policy['normal_sessions']:
            state.update(phase='NORMAL', risk_count=0, recovery_count=0, normal_count=0)
    else:
        if state['phase'] not in ('NORMAL', 'PULLBACK', 'RECOVERY'):
            raise ValueError('phase_invalid')
    return state


def phase_observation(old, dates, closes):
    """Keep original risk detection; add separately named reversal/stability rules."""
    recovery, normal, failed = [], [], []
    for symbol in CORE:
        prices = closes[symbol]
        if len(prices) < 60 or len(prices) != len(dates):
            raise ValueError('phase_history_invalid')
        item = old['symbols'][symbol]
        recovery.append(item['strong'] and prices[-1] > sum(prices[-20:]) / 20
                        and prices[-1] >= min(prices[-6:-1]))
        normal.append(prices[-1] > sum(prices[-50:]) / 50 and item['strong'])
        failed.append(prices[-1] < min(prices[-6:-1]))
    return dict(date=old['date'], risk=old['risk'], recovery=all(recovery),
                normal=all(normal), failed_reversal=any(failed))


def authorize_order(symbol, side):
    if symbol not in SYMBOLS or side not in ('BUY', 'SELL') or (side == 'SELL' and symbol != 'SGOV'):
        raise ValueError('order_policy_violation')


def _core_plan(snapshot, scores, context, targets, capital, *, daily_remaining, gates):
    # SGOV is included ONLY in the valuation denominator. It is never added to
    # executable cash. The original core engine still receives its two symbols.
    normalized = dict(cash_semantics_verified=True,
        market_values={s: snapshot['values'][s] for s in CORE},
        cash_for_weight=str(money(snapshot['net_assets']) - sum(money(snapshot['values'][s]) for s in CORE)),
        safe_spendable_cash=str(capital), cash_basis='three_etf_valuation_only',
        local_unreflected_reservations='0', local_unreflected_by_symbol={s: '0' for s in CORE})
    legacy = dict(connected=True, positions_readable=True, orders_readable=True, budget_readable=True,
                  quotes_readable=True, open_orders=[], cash_normalization=normalized,
                  monthly_filled_buys=0, daily_filled_buys=money(POLICY['daily_buy_limit']) - daily_remaining,
                  monthly_open_buy_amount=0, daily_open_buy_amount=0, cash_regime_v2=context)
    result = original_plan(legacy, scores, daily_limit=POLICY['daily_buy_limit'],
                           per_order_limit=POLICY['per_order_limit'], target_weights=targets)
    candidates = []
    for symbol, item in result.get('etfs', {}).items():
        if item['decision'] == 'BUY' and not gates.get(symbol):
            candidates.append(dict(symbol=symbol, side='BUY', amount=item['final_permitted_amount'],
                                   reason='PULLBACK_ORIGINAL_BUY', score=item['final_score'],
                                   legacy_context=item.get('regime_context'), legacy_item=item))
    candidates.sort(key=lambda x: (money(x['score']), money(x['amount'])), reverse=True)
    return result, candidates


def plan(snapshot, state, scores, context, *, buy_remaining, sell_remaining, gates=None,
         sgov_buy_cooled=True, sgov_sell_cooled=True, policy=POLICY):
    """Return one executable order, plus the original core decision evidence."""
    gates = gates or {}
    values = {s: money(snapshot['values'][s]) for s in SYMBOLS}
    total = money(snapshot['net_assets'])
    cash = money(snapshot['safe_cash'])
    buy_cap = min(money(buy_remaining), money(policy['per_order_limit']))
    sell_cap = min(money(sell_remaining), money(policy['per_order_limit']))
    phase = state['phase']
    if total <= 0 or phase not in ('NORMAL', 'PULLBACK', 'RECOVERY'):
        raise ValueError('valuation_or_phase_invalid')
    sgov_target = money(policy['targets']['SGOV'])
    if phase == 'RECOVERY':
        sgov_target = money(policy['recovery_sgov_cap'])
    elif phase == 'PULLBACK':
        # Gradual optional accumulation, rather than a mandate to fill 20%.
        depth = max((money(x) for x in context.get('depth', {}).values()), default=ZERO)
        sgov_target = min(money(policy['pullback_sgov_cap']), Decimal('.10') + depth / 2)
    targets = dict(SMH=(1-sgov_target)*Decimal(7)/9, QQQ=(1-sgov_target)*Decimal(2)/9)
    gaps = {s: max(ZERO, total*targets[s] - values[s]) for s in CORE}
    output = dict(name=policy['name'], phase=phase, episode_id=state.get('episode_id'), selected=None,
                  reason='WAIT', safe_cash=str(down(cash)), net_assets=str(down(total)),
                  weights={s: str(values[s]/total) for s in SYMBOLS},
                  tolerance_pp='0.5', targets={s: str(w) for s,w in dict(targets,SGOV=sgov_target).items()},
                  sgov_release_remaining=str(down(max(ZERO,values['SGOV']-total*Decimal('.05')))))
    minimum = money(policy['broker_minimum'])
    def select(symbol, side, amount, reason, **details):
        authorize_order(symbol, side)
        amount = down(amount)
        if amount < minimum:
            return False
        output.update(selected=dict(symbol=symbol, side=side, amount=str(amount), reason=reason, **details), reason=reason)
        return True
    if phase == 'PULLBACK':
        result, funded = _core_plan(snapshot,scores,context,targets,min(cash,buy_cap),
                                    daily_remaining=money(buy_remaining),gates=gates)
        output['original_core_plan'] = result
        if funded:
            output['selected'] = funded[0]
            output['reason'] = funded[0]['reason']
            return output
        # Compute cash-independent demand for funding; never expose it as cash.
        _, demand = _core_plan(snapshot,scores,context,targets,min(cash+values['SGOV'],buy_cap),
                               daily_remaining=money(buy_remaining),gates=gates)
        output['funding_demand'] = demand
        if demand and sgov_sell_cooled and buy_cap >= minimum:
            deficit=max(ZERO,money(demand[0]['amount'])-cash)
            if select('SGOV','SELL',min(deficit,sell_cap,values['SGOV']), 'SGOV_FUND_ORIGINAL_BUY'):
                return output
        # Never allocate a candidate's reserved demand to SGOV.
        if demand:
            output['reason']='WAIT_CORE_FUNDING_OR_COOLDOWN'
            return output
    else:
        # Normal allocation and recovery rotation have a distinct trigger; they
        # do not require the pullback strategy to remain oversold.
        eligible=[s for s in CORE if not gates.get(s) and gaps[s]>total*money(policy['normal_tolerance'])]
        if phase=='NORMAL':
            eligible += ['SGOV'] if sgov_buy_cooled and total*sgov_target-values['SGOV']>total*money(policy['normal_tolerance']) else []
        all_gaps=dict(gaps,SGOV=max(ZERO,total*sgov_target-values['SGOV']))
        if eligible:
            s=max(eligible,key=lambda s: all_gaps[s]/total)
            if select(s,'BUY',min(cash,buy_cap,all_gaps[s]),
                      'RECOVERY_ROTATION_BUY' if phase=='RECOVERY' else 'NORMAL_TARGET_BUY',score=scores.get(s)):
                return output
        sell_target=sgov_target
        excess=max(ZERO,values['SGOV']-total*sell_target)
        trigger=ZERO if phase=='RECOVERY' else total*money(policy['normal_tolerance'])
        # Cash-first determines sequence; the recovery task stays pending even
        # when cash temporarily covered earlier orders.
        if excess>trigger and sgov_sell_cooled and buy_cap>=minimum and any(gaps[s]>=minimum and not gates.get(s) for s in CORE):
            if select('SGOV','SELL',min(excess,sell_cap,sum(gaps.values()),buy_cap),
                      'RECOVERY_SGOV_RELEASE' if phase=='RECOVERY' else 'NORMAL_SGOV_REBALANCE'):
                return output
        if phase=='RECOVERY':
            output['reason']='WAIT_RECOVERY_LIMIT_OR_FUNDS' if excess>0 else 'WAIT_RECOVERY_STABILITY'
            return output
    if sgov_buy_cooled:
        gap=max(ZERO,total*sgov_target-values['SGOV'])
        if gap>total*money(policy['normal_tolerance']):
            select('SGOV','BUY',min(cash,buy_cap,gap), 'SGOV_ACCUMULATE',score=None)
    if not output['selected']:
        output['reason']='WAIT_TARGET_OR_LIMIT' if phase=='NORMAL' else 'WAIT_PULLBACK'
    return output

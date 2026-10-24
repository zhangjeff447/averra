"""Completed-day regime and finite cash release. No I/O or trading transport."""
import copy
import hashlib
import json
import math
import statistics
from decimal import Decimal

POLICY_VERSION = 'cash_regime_v2'
SYMBOLS = ('SMH', 'QQQ')


def light_tier(score):
    from .score_dynamic_v1 import decimal, PlanningUnavailable
    try:
        value = decimal(score, 'score')
    except PlanningUnavailable:
        return 'SCORE_INVALID', Decimal('0')
    if value > 100:
        return 'SCORE_INVALID', Decimal('0')
    if value < 25:
        return 'WAIT', Decimal('0')
    return 'LIGHT_TIMING_BUY', Decimal('.25') + Decimal('.75') * value / 100


def _ema_series(values, period):
    result = [None] * len(values)
    if len(values) < period:
        return result
    value = sum(values[:period]) / period
    result[period - 1] = value
    alpha = 2 / (period + 1)
    for i in range(period, len(values)):
        value = alpha * values[i] + (1 - alpha) * value
        result[i] = value
    return result


def observations(dates, closes, volatility, policy, *, last_only=False):
    """Return only completed, aligned dates; callers verify session freshness."""
    median_window = policy['volatility_median_sessions']
    peak_window = policy['peak_lookback_sessions']
    lag = policy['volatility_change_sessions']
    start = max(median_window, peak_window, lag + 1, 34)
    if dates != sorted(set(dates)) or len(dates) <= start:
        raise ValueError('regime_history_insufficient_or_unordered')
    series = dict(closes, **volatility)
    if set(closes) != set(SYMBOLS) or set(volatility) != {'VIX', 'VXN'}:
        raise ValueError('regime_inputs_incomplete')
    if any(len(v) != len(dates) or any(isinstance(x, bool) or not math.isfinite(x) or x <= 0 for x in v)
           for v in series.values()):
        raise ValueError('regime_history_invalid')
    ema20, hist = {}, {}
    for symbol in SYMBOLS:
        values = closes[symbol]
        ema20[symbol] = _ema_series(values, 20)
        fast, slow = _ema_series(values, 12), _ema_series(values, 26)
        macd = [fast[i] - slow[i] for i in range(25, len(values))]
        signal = _ema_series(macd, 9)
        hist[symbol] = [None] * 25 + [m - s if s is not None else None for m, s in zip(macd, signal)]
    result = []
    for i in range(len(dates) - 1 if last_only else start, len(dates)):
        pressure = {}
        for name, values in volatility.items():
            pressure[name] = values[i] > statistics.median(values[max(0, i - median_window + 1):i + 1]) and (
                values[i] / values[i - lag] - 1 >= float(policy['volatility_increase']))
        items = {}
        for symbol in SYMBOLS:
            values = closes[symbol]
            items[symbol] = {
                'price': values[i], 'peak60': max(values[i - peak_window + 1:i + 1]),
                'weak': values[i] < ema20[symbol][i] and hist[symbol][i] < 0,
                'strong': values[i] >= ema20[symbol][i] and hist[symbol][i] >= 0,
                'recovering': values[i] > values[i - 1] and hist[symbol][i] > hist[symbol][i - 1],
            }
        result.append({'date': dates[i], 'symbols': items, 'volatility_pressure': pressure,
                       'risk': any(x['weak'] for x in items.values()) and any(pressure.values()),
                       'clear': all(x['strong'] for x in items.values()) and not any(pressure.values())})
    return result


def advance(previous, observation, policy):
    state = copy.deepcopy(previous or {'version': POLICY_VERSION, 'mode': 'EARLY_INVEST',
                                     'entry_count': 0, 'exit_count': 0, 'last_date': None})
    if state.get('version') != POLICY_VERSION:
        raise ValueError('regime_state_version_invalid')
    fingerprint = hashlib.sha256(json.dumps(observation, sort_keys=True).encode()).hexdigest()
    last = state['last_date']
    if last and observation['date'] <= last:
        if observation['date'] == last and state['observation_hash'] != fingerprint:
            raise ValueError('regime_same_day_input_changed')
        return state
    state.update(last_date=observation['date'], observation_hash=fingerprint,
                 features=observation)
    if state['mode'] == 'EARLY_INVEST':
        state['entry_count'] = state['entry_count'] + 1 if observation['risk'] else 0
        if state['entry_count'] >= policy['entry_confirmation_sessions']:
            state.update(mode='RISK_RETENTION', episode_id='risk:' + observation['date'],
                         anchors={s: x['peak60'] for s, x in observation['symbols'].items()},
                         depth={s: 0 for s in SYMBOLS}, recovery={s: 0 for s in SYMBOLS},
                         grants={s: {x['id']: 0 for x in policy['stages']} for s in SYMBOLS},
                         cash_lots=[], cash_mass_highwater=None, exit_count=0)
    if state['mode'] == 'RISK_RETENTION':
        state['exit_count'] = state['exit_count'] + 1 if observation['clear'] else 0
        if state['exit_count'] >= policy['exit_confirmation_sessions']:
            state.update(mode='EARLY_INVEST', entry_count=0, exit_count=0)
        else:
            for symbol, item in observation['symbols'].items():
                depth = max(0, 1 - item['price'] / state['anchors'][symbol])
                state['depth'][symbol] = max(state['depth'][symbol], depth)
                state['recovery'][symbol] = state['recovery'][symbol] + 1 if item['recovering'] else 0
    return state


def add_cash_capacity(state, cash_cents, total_agent_fills_cents, gaps, policy):
    """High-water cash+dated own fills: releasing holds never creates capital."""
    state = copy.deepcopy(state)
    if state['mode'] != 'RISK_RETENTION':
        return state
    mass = cash_cents + total_agent_fills_cents
    previous = state['cash_mass_highwater']
    grant = cash_cents if previous is None else max(0, mass - previous)
    state['cash_mass_highwater'] = mass if previous is None else max(previous, mass)
    gap_total = sum(Decimal(str(x)) for x in gaps.values())
    if grant <= 0 or gap_total <= 0:
        return state
    amounts = {s: int(Decimal(grant) * Decimal(str(gaps[s])) / gap_total) for s in SYMBOLS}
    state['cash_lots'].append({'id': state['episode_id'] + ':cash:' + str(len(state['cash_lots']) + 1),
                               'observed_date': state['last_date'], 'cents': grant,
                               'amounts': amounts, 'kind': 'VERIFIED_CASH_CAPACITY_NOT_DEPOSIT_CLASSIFICATION'})
    for symbol in SYMBOLS:
        assigned = 0
        for stage in policy['stages']:
            amount = int(Decimal(amounts[symbol]) * Decimal(stage['fraction']))
            state['grants'][symbol][stage['id']] += amount
            assigned += amount
        # Rounding residue is held, not force-allocated to a buy.
        if assigned > amounts[symbol]:
            raise ValueError('stage_grants_exceed_cash')
    return state


def release_context(state, used, policy):
    context = {'policy_version': POLICY_VERSION, 'mode': state['mode'],
               'observation_date': state['last_date'], 'episode_id': state.get('episode_id'),
               'stages': {}, 'features': state.get('features')}
    if state['mode'] == 'EARLY_INVEST':
        return context
    for symbol in SYMBOLS:
        eligible = []
        for stage in policy['stages']:
            amounts = used.get(symbol, {}).get(stage['id'], {})
            eligible.append(dict(stage, authorized_cents=state['grants'][symbol][stage['id']],
                                 filled_cents=amounts.get('filled_cents', 0),
                                 reserved_cents=amounts.get('reserved_cents', 0),
                                 eligible=(state['depth'][symbol] >= float(stage['drawdown'])
                                           and state['recovery'][symbol] >= policy['recovery_confirmation_sessions'])))
        context['stages'][symbol] = eligible
    return context


def stage_amount(context, symbol, score):
    from .score_dynamic_v1 import score_tier
    tier, factor = score_tier(score)
    if factor == 0:
        return tier, factor, None, Decimal('0')
    for stage in context.get('stages', {}).get(symbol, []):
        allowed = max(Decimal('0'), Decimal(stage['authorized_cents']) * factor
                      - stage['filled_cents'] - stage['reserved_cents']) / 100
        if stage['eligible'] and allowed >= 1:
            return tier, factor, stage['id'], allowed
    return tier, factor, None, Decimal('0')

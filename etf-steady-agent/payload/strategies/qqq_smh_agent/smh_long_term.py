"""SMH long-term pullback signals; pure calculation, no broker access."""
import math
import statistics
from decimal import Decimal

POLICY_VERSION = 'SMH_EMA20_PULLBACK'


def clip(value):
    return min(1.0, max(0.0, value))


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


def features(dates, closes, rsi):
    if (len(closes) < 272 or len(dates) != len(closes)
            or dates != sorted(set(dates))
            or any(isinstance(x, bool) or not math.isfinite(x) or x <= 0 for x in closes)
            or isinstance(rsi, bool) or not math.isfinite(rsi) or not 0 <= rsi <= 100):
        raise ValueError('smh_completed_inputs_invalid')
    ema20 = _ema_series(closes, 20)
    ema50 = _ema_series(closes, 50)
    fast = _ema_series(closes, 12)
    slow = _ema_series(closes, 26)
    macd = [fast[i] - slow[i] for i in range(25, len(closes))]
    signal = _ema_series(macd, 9)
    hist = [None] * 25 + [m - s if s is not None else None for m, s in zip(macd, signal)]
    daily = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
    return dict(date=dates[-1], price=closes[-1], ema20=ema20[-1], ema50=ema50[-1],
                drawdown60=max(0, 1 - closes[-1] / max(closes[-60:])), rsi14=rsi,
                vol20=statistics.pstdev(daily[-20:]),
                weak=closes[-1] < ema20[-1] and hist[-1] < 0)


def validate_feature(feature):
    if not isinstance(feature, dict):
        raise ValueError('smh_feature_missing')
    keys = ('price', 'ema20', 'ema50', 'drawdown60', 'rsi14', 'vol20')
    if any(isinstance(feature.get(k), bool) or not isinstance(feature.get(k), (int, float))
           or not math.isfinite(feature[k]) for k in keys):
        raise ValueError('smh_feature_invalid')
    if (min(feature[k] for k in ('price', 'ema20', 'ema50')) <= 0
            or not 0 <= feature['drawdown60'] < 1
            or not 0 <= feature['rsi14'] <= 100 or feature['vol20'] < 0
            or type(feature.get('weak')) is not bool):
        raise ValueError('smh_feature_invalid')


def normal_signal(feature):
    validate_feature(feature)
    f = feature
    if f['weak'] and f['ema20'] < f['ema50'] and f['price'] < f['ema50']:
        return 0, Decimal('0'), 'WAIT_SMH_WEAK_TREND'
    heat = max(0, f['price'] / f['ema20'] - 1) / f['vol20'] if f['vol20'] else 0
    if heat > 3 and f['rsi14'] >= 70:
        return 0, Decimal('0'), 'WAIT_SMH_OVERHEAT'
    if not (f['price'] <= f['ema20'] or f['rsi14'] <= 50):
        return 0, Decimal('0'), 'WAIT_SMH_NO_PULLBACK'
    distance = (f['ema20'] / f['price'] - 1) / (2 * f['vol20']) if f['vol20'] else 0
    score = round(min(100, 50 + 30 * clip(distance) + 20 * clip((60 - f['rsi14']) / 30)), 2)
    return score, Decimal('1'), 'BUY'


def risk_score(feature):
    validate_feature(feature)
    return round(60 + 40 * clip(feature['drawdown60'] / .20), 2)


def qqq_normal_tier(raw):
    from .score_dynamic_v1 import decimal, PlanningUnavailable
    try:
        score = decimal(raw, 'score')
    except PlanningUnavailable:
        return 'SCORE_INVALID', Decimal('0')
    if score > 100:
        return 'SCORE_INVALID', Decimal('0')
    if score < 25:
        return 'WAIT', Decimal('0')
    factor = Decimal('1') if score >= 75 else (Decimal('.5') + Decimal('.5') *
             (((score - 25) / 50).exp() - 1) / (Decimal('1').exp() - 1))
    return 'QQQ_ANCHOR_BUY', factor

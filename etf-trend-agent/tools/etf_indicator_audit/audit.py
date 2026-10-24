"""Offline, informational indicator audit. No network or trading integration."""
import argparse
import csv
import hashlib
import json
import math
from datetime import date
from pathlib import Path

SYMBOLS = ('QQQ', 'SMH', 'TQQQ')
PROFILE = 'SMA_SEEDED_EMA_WILDER_RSI14_MACD12_26_9_v1'

def smooth(values, period, alpha):
    out = [None] * len(values)
    if len(values) >= period:
        out[period - 1] = sum(values[:period]) / period
        for i in range(period, len(values)):
            out[i] = alpha * values[i] + (1 - alpha) * out[i - 1]
    return out

def indicators(closes):
    def sma(n):
        return sum(closes[-n:]) / n if len(closes) >= n else None
    def last(values):
        return values[-1] if values else None
    changes = [b - a for a, b in zip(closes, closes[1:])]
    gains = [max(0, x) for x in changes]
    losses = [max(0, -x) for x in changes]
    gain = last(smooth(gains, 14, 1 / 14))
    loss = last(smooth(losses, 14, 1 / 14))
    # With no gains AND no losses the RSI ratio is undefined, not a BUY signal.
    rsi = None if gain is None or gain + loss == 0 else 100 * gain / (gain + loss)
    fast, slow = smooth(closes, 12, 2 / 13), smooth(closes, 26, 2 / 27)
    macd = [a - b for a, b in zip(fast, slow) if a is not None and b is not None]
    signal = last(smooth(macd, 9, 2 / 10))
    value = last(macd)
    return {'SMA50': sma(50), 'SMA200': sma(200),
            'EMA20': last(smooth(closes, 20, 2 / 21)), 'RSI14_WILDER': rsi,
            'MACD': value, 'MACD_SIGNAL': signal,
            'MACD_HIST': value - signal if signal is not None else None}

def legacy_rsi(closes):
    if len(closes) < 15:
        return None
    changes = [b - a for a, b in zip(closes[-15:], closes[-14:])]
    gain = sum(max(0, x) for x in changes)
    loss = sum(max(0, -x) for x in changes)
    return 50.0 if gain + loss == 0 else 100 * gain / (gain + loss)

def read_history(path, meta, through, sessions):
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if digest != meta['sha256']:
        raise ValueError('HISTORY_HASH_MISMATCH')
    with path.open() as f:
        raw = list(csv.DictReader(f))
    dates = [row['Date'] for row in raw]
    if not dates or dates != sorted(set(dates)):
        raise ValueError('DATE_ORDER_OR_DUPLICATE')
    if (dates[0], dates[-1], len(dates)) != (meta['first'], meta['last'], meta['rows']):
        raise ValueError('MANIFEST_RANGE_MISMATCH')
    kept = []
    for row in raw:
        date.fromisoformat(row['Date'])
        if row['Date'] > through:
            continue
        values = [float(row[k]) for k in ('Open', 'High', 'Low', 'Close', 'Adj Close')]
        if any(not math.isfinite(v) or v <= 0 for v in values):
            raise ValueError('INVALID_PRICE')
        o, h, low, c, adj = values
        if not low <= min(o, c) <= max(o, c) <= h:
            raise ValueError('INVALID_OHLC')
        kept.append(row)
    if not kept:
        raise ValueError('NO_COMPLETED_HISTORY')
    actual = [row['Date'] for row in kept]
    expected = sessions(actual[0], actual[-1])
    if actual != expected:
        raise ValueError('MISSING_OR_NON_SESSION_BAR')
    if not meta.get('source'):
        raise ValueError('SOURCE_UNSPECIFIED')
    return kept, digest

def compare(local, reference):
    if not reference:
        return {'status': 'NOT_VERIFIED', 'reason': 'NO_AUTHORIZED_REFERENCE_EXPORT'}
    required = ('symbol', 'through', 'timeframe', 'price_basis', 'formula_profile', 'history_sha256')
    if any(reference.get(k) != local.get(k) for k in required) or not reference.get('source'):
        return {'status': 'NOT_COMPARABLE', 'reason': 'METADATA_OR_FORMULA_MISMATCH'}
    result = {}
    for key, value in local['values'].items():
        ref = reference.get('values', {}).get(key)
        if value is None or type(ref) not in (int, float) or not math.isfinite(ref):
            result[key] = {'status': 'UNAVAILABLE'}
        else:
            difference = value - ref
            tolerance = 1e-6 + abs(value) * 1e-8
            result[key] = {'status': 'MATCH' if abs(difference) <= tolerance else 'DIFFERENT',
                           'reference': ref, 'difference': difference, 'tolerance': tolerance}
    status = 'DIFFERENT' if any(v['status'] == 'DIFFERENT' for v in result.values()) else 'INSUFFICIENT' if any(v['status'] == 'UNAVAILABLE' for v in result.values()) else 'MATCH'
    return {'status': status, 'fields': result,
            'meaning': 'Numerical comparison only; no platform certification or trading permission'}

def audit(root, through, sessions, reference=None):
    date.fromisoformat(through)
    if sessions(through, through) != [through]:
        raise ValueError('THROUGH_MUST_BE_COMPLETED_EXCHANGE_SESSION')
    manifest = json.loads((root / 'manifest.json').read_text())
    results = {}
    for symbol in SYMBOLS:
        try:
            meta = manifest[symbol]
            rows, digest = read_history(root / (symbol + '.csv'), meta, through, sessions)
            closes = [float(r['Adj Close']) for r in rows]
            current = rows[-1]['Date']
            local = {'symbol': symbol, 'through': current, 'requested_through': through,
                     'status': 'CURRENT_TO_REQUESTED_SESSION' if current == through else 'STALE',
                     'timeframe': '1d', 'price_basis': 'Adj Close', 'formula_profile': PROFILE,
                     'history_sha256': digest, 'source': meta['source'],
                     'retrieved_at': meta.get('retrieved_at'), 'warmup_first': rows[0]['Date'],
                     'history_rows': len(rows), 'values': indicators(closes)}
            local['legacy_simple_RSI14'] = legacy_rsi(closes)
            rsi = local['values']['RSI14_WILDER']
            local['rsi_formula_difference'] = None if rsi is None or local['legacy_simple_RSI14'] is None else rsi - local['legacy_simple_RSI14']
            local['reference_check'] = compare(local, (reference or {}).get(symbol))
            results[symbol] = local
        except (ValueError, KeyError, OSError) as exc:
            results[symbol] = {'symbol': symbol, 'status': 'UNAVAILABLE', 'reason': str(exc)}
    return {'mode': 'OFFLINE_INFORMATION_ONLY', 'affects_trading': False,
            'through': through, 'symbols': results,
            'reference_verified': False if not reference else all(x.get('status') == 'CURRENT_TO_REQUESTED_SESSION' and x.get('reference_check', {}).get('status') == 'MATCH' for x in results.values())}

def markdown(result):
    lines = ['# ETF 指标核对', '', '仅离线观察，不参与 BUY/WAIT、资金、风控或下单。',
             '要求数据截至：' + result['through'], '',
             '| 标的 | 数据状态 | 实际截至 | Wilder RSI14 | 旧简单RSI14 | SMA50 | SMA200 | 外部数值核对 |',
             '|---|---|---|---:|---:|---:|---:|---|']
    def fmt(x):
        return '待更新' if x is None else f'{x:.4f}'
    labels = {'STALE': '已过期', 'UNAVAILABLE': '不可用', 'CURRENT_TO_REQUESTED_SESSION': '截至要求日期',
              'NOT_VERIFIED': '未验证', 'NOT_COMPARABLE': '口径不同', 'MATCH': '数值一致',
              'DIFFERENT': '存在差异', 'INSUFFICIENT': '数据不足'}
    for symbol, row in result['symbols'].items():
        v = row.get('values', {})
        lines.append('| ' + ' | '.join([symbol, labels[row['status']], row.get('through', '待更新'),
                     fmt(v.get('RSI14_WILDER')), fmt(row.get('legacy_simple_RSI14')),
                     fmt(v.get('SMA50')), fmt(v.get('SMA200')),
                     labels[row.get('reference_check', {}).get('status', 'NOT_VERIFIED')]]) + ' |')
    lines += ['', 'EMA使用首N期SMA种子；RSI使用首14个涨跌均值种子和Wilder递推；MACD为12/26 EMA及9期EMA信号。',
              '全部指标使用同一Adj Close序列；这不自动等于TradingView图表的复权设置。',
              '未取得同数据、同日期、同口径的授权外部导出时，不声称已与TradingView一致。',
              '过期值只作历史观察，不是当前行情。旧RSI口径差异不表示当前主策略使用RSI。']
    return '\n'.join(lines) + '\n'

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', required=True, type=Path)
    parser.add_argument('--through', required=True, help='Explicit completed NYSE session YYYY-MM-DD')
    parser.add_argument('--output-dir', required=True, type=Path)
    parser.add_argument('--reference', type=Path, help='Optional authorized local export; no fetching')
    args = parser.parse_args()
    import exchange_calendars as xc
    calendar = xc.get_calendar('XNYS')
    sessions = lambda a, b: calendar.sessions_in_range(a, b).strftime('%Y-%m-%d').tolist()
    from datetime import datetime
    from zoneinfo import ZoneInfo
    if args.through >= datetime.now(ZoneInfo('America/New_York')).date().isoformat():
        parser.error('--through must precede today; forming sessions are excluded conservatively')
    out = args.output_dir.resolve()
    if out == args.data_dir.resolve() or args.data_dir.resolve() in out.parents:
        parser.error('output must be separate from input history')
    here = Path(__file__).resolve().parents[2]
    if out == here / 'production' or here / 'production' in out.parents:
        parser.error('production output is forbidden')
    reference = json.loads(args.reference.read_text()) if args.reference else None
    result = audit(args.data_dir, args.through, sessions, reference)
    out.mkdir(parents=True, exist_ok=True)
    (out / 'indicator-audit.json').write_text(json.dumps(result, indent=2, allow_nan=False))
    (out / 'INDICATOR_AUDIT.md').write_text(markdown(result))
    print(json.dumps({'mode': result['mode'], 'statuses': {s: r['status'] for s, r in result['symbols'].items()}, 'reference_verified': result['reference_verified']}))

if __name__ == '__main__':
    main()

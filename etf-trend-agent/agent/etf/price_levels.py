"""Display-only 20-session price channels. No strategy, journal or broker calls."""
import csv
import hashlib
import json
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from .calendar import ET, exchange, previous_session

SYMBOLS = ('QQQ', 'SMH')
SESSIONS = 20


def _symbol(root, manifest, symbol, cutoff):
    path = root / (symbol + '.csv')
    content = path.read_bytes()
    if hashlib.sha256(content).hexdigest() != manifest[symbol]['sha256']:
        raise ValueError('history_hash_changed')
    rows = list(csv.DictReader(content.decode().splitlines()))
    dates = [row['Date'] for row in rows]
    if dates != sorted(set(dates)):
        raise ValueError('history_dates_invalid')
    # The current forming daily candle and any later rows cannot set a level.
    past = [row for row in rows if date.fromisoformat(row['Date']).isoformat() <= cutoff]
    window = past[-SESSIONS:]
    if len(window) != SESSIONS or window[-1]['Date'] != cutoff:
        raise ValueError('history_not_current')
    expected = exchange().sessions_in_range(window[0]['Date'], cutoff).strftime('%Y-%m-%d').tolist()
    if [row['Date'] for row in window] != expected:
        raise ValueError('history_session_gap')
    values = []
    for row in window:
        o, h, l, c = (Decimal(row[key]) for key in ('Open', 'High', 'Low', 'Close'))
        if not all(x.is_finite() and x > 0 for x in (o, h, l, c)) or not l <= min(o, c) <= max(o, c) <= h:
            raise ValueError('history_ohlc_invalid')
        values.append((row['Date'], l, h))
    support = min(values, key=lambda x: x[1])
    breakout = max(values, key=lambda x: x[2])
    return dict(status='OK', support=str(support[1]), breakout=str(breakout[2]),
                support_date=support[0], breakout_date=breakout[0],
                first_session=window[0]['Date'], last_session=cutoff,
                history_sha256=manifest[symbol]['sha256'])


def build(directory, now):
    root = Path(directory)
    cutoff = previous_session(now.astimezone(ET).date().isoformat())
    result = dict(display_only=True, sessions=SESSIONS, as_of=cutoff, symbols={})
    try:
        manifest = json.loads((root / 'manifest.json').read_text())
    except Exception:
        manifest = {}
    for symbol in SYMBOLS:
        try:
            result['symbols'][symbol] = _symbol(root, manifest, symbol, cutoff)
        except Exception:
            result['symbols'][symbol] = dict(status='UNAVAILABLE')
    return result


def render(levels):
    lines = ['价格参考｜近20个完整交易日']
    for symbol in SYMBOLS:
        item = levels.get('symbols', {}).get(symbol, {})
        if item.get('status') == 'OK':
            lines.append(symbol + '：支撑 $' + format(Decimal(item['support']), '.2f')
                         + '｜突破参考 $' + format(Decimal(item['breakout']), '.2f'))
        else:
            lines.append(symbol + '：支撑／突破参考数据待更新')
    if levels.get('as_of'):
        lines.append('日线截至：' + levels['as_of'])
    lines.append('仅展示，不参与交易。')
    return '\n'.join(lines)


def append_to(text, directory, now=None):
    try:
        levels = build(directory, now or datetime.now(timezone.utc))
        addition = render(levels)
    except Exception:
        addition = render({})
    return text + '\n\n' + addition

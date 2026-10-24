"""Real completed daily observations. Never falls back to test data or repeated quotes."""
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo


class HistoryUnavailable(RuntimeError):
    pass


class DailyHistory:
    def __init__(self, download=None):
        self.download = download
        self.cache = {}
        self.audit_metadata = {}
        self.completed_dates = {}

    def get_closes(self, symbol, now=None):
        if symbol not in ("QQQ", "SMH", "^VIX", "^VXN"):
            raise HistoryUnavailable("symbol_not_allowed")
        now = now or datetime.now(timezone.utc)
        day = now.astimezone(ZoneInfo("America/New_York")).date()
        if (symbol, day) in self.cache:
            return list(self.cache[(symbol, day)])
        try:
            download = self.download
            if download is None:
                import yfinance as yf
                yf.set_tz_cache_location(str(Path(__file__).resolve().parents[1] / 'state/yfinance_cache'))
                download = yf.download
            frame = download(symbol, start=str(day - timedelta(days=500)), end=str(day),
                             interval="1d", auto_adjust=True, progress=False, threads=False,
                             timeout=20, multi_level_index=False)
            from tools.robinhood_runtime import _market_holidays
            previous = day - timedelta(days=1)
            while previous.weekday() >= 5 or previous in _market_holidays(previous.year):
                previous -= timedelta(days=1)
            dates = [d.date() for d in frame.index]
            values = [float(x) for x in frame["Close"].tolist()]
            if len(values) < 200 or len(dates) != len(set(dates)) or dates != sorted(dates):
                raise HistoryUnavailable("history_insufficient_or_unordered")
            if dates[-1] != previous or any(d >= day for d in dates):
                raise HistoryUnavailable("history_stale_or_future")
            available = set(dates)
            cursor = dates[-200]
            while cursor <= previous:
                if cursor.weekday() < 5 and cursor not in _market_holidays(cursor.year) and cursor not in available:
                    raise HistoryUnavailable("history_missing_session")
                cursor += timedelta(days=1)
            if any(not math.isfinite(x) or x <= 0 for x in values):
                raise HistoryUnavailable("history_invalid_price")
            self.cache[(symbol, day)] = values
            self.completed_dates[symbol] = [str(d) for d in dates]
            import hashlib
            import json
            self.audit_metadata[symbol] = {'source': 'yfinance_completed_adjusted_daily_close',
                'first_date': str(dates[0]), 'last_completed_date': str(dates[-1]),
                'observations': len(values), 'input_sha256': hashlib.sha256(json.dumps(values).encode()).hexdigest(),
                'retrieved_at': datetime.now(timezone.utc).isoformat()}
            return list(values)
        except HistoryUnavailable:
            raise
        except Exception as exc:
            raise HistoryUnavailable("daily_history_fetch_failed:" + type(exc).__name__) from None

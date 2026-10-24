"""Deterministic synthetic data for local simulation only."""
from datetime import date, timedelta, datetime, timezone
import math


def bars(start, trend, phase=0, volume=1_000_000):
    out = []
    for i in range(260):
        close = start * (1 + trend) ** i * (1 + 0.012 * math.sin(i * 0.31 + phase))
        out.append({"date": (date.today() - timedelta(days=259 - i)).isoformat(),
                    "open": round(close * 0.997, 4), "high": round(close * 1.012, 4),
                    "low": round(close * 0.988, 4), "close": round(close, 4),
                    "volume": round(volume * (1 + 0.2 * math.sin(i * 0.23 + phase)))})
    return out


def snapshot():
    spy = bars(400, 0.0005)
    qqq = bars(350, 0.0007, 0.3)
    xlk = bars(180, 0.0008, 0.5)
    xlf = bars(35, 0.0004, 1.0)
    def stock(ticker, start, trend, sector, benchmark, phase):
        return {"ticker": ticker, "asset_type": "COMMON_STOCK", "country": "US",
                "exchange": "NASDAQ" if sector == "Technology" else "NYSE",
                "sector": sector, "sector_benchmark": benchmark, "bars": bars(start, trend, phase),
                "fundamentals": {"revenue_growth_yoy": 0.65, "earnings_growth_yoy": 0.60,
                                 "operating_margin": 0.30, "debt_to_equity": 0.1,
                                 "estimate_revision_90d": 0.04}, "events": []}
    return {"asof": datetime.now(timezone.utc).isoformat(timespec="seconds"), "session": "OPEN",
            "market": {"vix": 18.0, "vxn": 22.0},
            "benchmarks": {"SPY": spy, "QQQ": qqq, "XLK": xlk, "XLF": xlf},
            "stocks": [stock("MOCKA", 70, 0.0015, "Technology", "XLK", 0.2),
                       stock("MOCKB", 45, 0.0012, "Technology", "XLK", 1.1),
                       stock("MOCKC", 60, 0.0011, "Financials", "XLF", 2.2)]}

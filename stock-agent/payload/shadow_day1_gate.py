#!/usr/bin/env python3
"""Read-only date gate for the one-time 2026-09-28 Shadow close run."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from quant_screen import ROOT

DAY = "2026-09-28"
ET = ZoneInfo("America/New_York")
BENCHMARKS = ("SPY", "QQQ", "XLK", "XLF", "XLV", "XLY", "XLP", "XLI",
              "XLE", "XLU", "XLB", "XLRE", "XLC")
VOLATILITY = ("VIX", "VXN")


def check(db: sqlite3.Connection, now: datetime) -> dict:
    """A missing or stale component is WAITING, never a usable close decision."""
    if now.tzinfo is None:
        raise ValueError("timezone-aware current time required")
    eastern = now.astimezone(ET)
    missing = []
    required_close = datetime(2026, 9, 28, 16, 15, tzinfo=ET)
    if eastern < required_close:
        missing.append("FULL_2026_09_28_CLOSE_NOT_YET_AVAILABLE")
    bars_have_source = "source" in {col[1] for col in db.execute("PRAGMA table_info(daily_bars)")}
    for ticker in BENCHMARKS:
        row = db.execute("SELECT MAX(date) FROM daily_bars WHERE ticker=?", (ticker,)).fetchone()
        if not row or row[0] != DAY:
            missing.append(f"{ticker}_DAILY_STALE:{row[0] if row else None}")
        elif bars_have_source:
            source = db.execute("SELECT source FROM daily_bars WHERE ticker=? AND date=?",
                                (ticker, DAY)).fetchone()
            if not source or source[0] != "ALPACA_SIP":
                missing.append(f"{ticker}_SOURCE_NOT_ALPACA_SIP")
    for symbol in VOLATILITY:
        row = db.execute("SELECT MAX(date) FROM volatility_daily WHERE symbol=?", (symbol,)).fetchone()
        if not row or row[0] != DAY:
            missing.append(f"{symbol}_DAILY_STALE:{row[0] if row else None}")
    sync_has_source = "source" in {col[1] for col in db.execute("PRAGMA table_info(market_sync_runs)")}
    sync = db.execute("SELECT status" + (",source" if sync_has_source else "") +
                      " FROM market_sync_runs WHERE asof=? ORDER BY id DESC LIMIT 1", (DAY,)).fetchone()
    if not sync or sync[0] != "COMPLETE" or (sync_has_source and sync[1] != "ALPACA_SIP"):
        missing.append("INCREMENTAL_SYNC_NOT_COMPLETE")
    day_has_source = "source" in {col[1] for col in db.execute("PRAGMA table_info(market_sync_days)")}
    day = db.execute("SELECT " + ("source" if day_has_source else "1") +
                     " FROM market_sync_days WHERE date=?", (DAY,)).fetchone()
    if not day or (day_has_source and day[0] != "ALPACA_SIP"):
        missing.append("GROUPED_DAILY_NOT_STORED")
    return {"status": "SHADOW_DAY1_WAITING_FOR_DATA" if missing else "SHADOW_DAY1_DATA_READY",
            "required_trading_date": DAY, "checked_at_et": eastern.isoformat(),
            "data_source": "ALPACA_SIP" if not missing else None,
            "missing": missing, "entry_decision_allowed": not missing,
            "ai_research_allowed": not missing}


def main(db_path: Path = ROOT / "data" / "market_data.db") -> dict:
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as db:
        result = check(db, datetime.now(ET))
    print(json.dumps(result, indent=2))
    return result


if __name__ == "__main__":
    main()

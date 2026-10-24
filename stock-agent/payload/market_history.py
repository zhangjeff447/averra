"""Recent market-session continuity checks and bounded incremental gap repair."""
from __future__ import annotations

import json
import sqlite3
from datetime import date

from alpaca_market import sync, valid_daily
from market_regime import market_session, previous_session
from shadow_day1_gate import BENCHMARKS


def check_recent_history(db, day, *, lookback_sessions=20, minimum_coverage=0.95):
    """Read only; a current close alone cannot establish complete recent history."""
    target = date.fromisoformat(day)
    if not market_session(target) or not 1 <= lookback_sessions <= 400:
        raise ValueError("INVALID_HISTORY_WINDOW")
    if not 0 < minimum_coverage <= 1:
        raise ValueError("INVALID_HISTORY_COVERAGE")
    sessions = [target]
    for _ in range(lookback_sessions):
        sessions.append(previous_session(sessions[-1]))
    # One extra preceding session establishes coverage for the oldest checked date.
    sessions = [item.isoformat() for item in reversed(sessions)]
    active = {r[0] for r in db.execute("SELECT ticker FROM securities WHERE is_active=1")}
    active.update(BENCHMARKS)
    rows = db.execute("""SELECT ticker,date,open,high,low,close,volume,source
        FROM daily_bars WHERE date>=? AND date<=?""", (sessions[0], day))
    stored = {session: {} for session in sessions}
    invalid = {session: [] for session in sessions}
    for ticker, session, opened, high, low, close, volume, source in rows:
        if ticker not in active or session not in stored:
            continue
        if valid_daily(dict(t=session, o=opened, h=high, l=low, c=close, v=volume), session):
            stored[session][ticker] = source
        else:
            invalid[session].append(ticker)
    baseline_count = max((len(items) for items in stored.values()), default=0)
    details = []
    for session in sessions[1:]:
        symbols = stored[session]
        missing_benchmarks = sorted(set(BENCHMARKS) - symbols.keys())
        coverage = len(symbols) / baseline_count if baseline_count else 0
        reasons = []
        if not baseline_count:
            reasons.append("NO_POPULATED_HISTORY_BASELINE")
        if missing_benchmarks:
            reasons.append("MISSING_BENCHMARK_HISTORY")
        if coverage < minimum_coverage:
            reasons.append("INCOMPLETE_MARKET_SESSION")
        details.append({"date": session, "valid_symbols": len(symbols),
                        "coverage": round(coverage, 6),
                        "missing_benchmarks": missing_benchmarks,
                        "invalid_symbols": sorted(invalid[session]),
                        "sources": sorted(set(symbols.values())), "reasons": reasons})
    missing = [item["date"] for item in details if item["reasons"]]
    return {"status": "HISTORY_GAP" if missing else "HISTORY_READY",
            "asof": day, "required_sessions": sessions[1:],
            "baseline_symbols": baseline_count, "minimum_coverage": minimum_coverage,
            "missing_sessions": missing, "sessions": details}


def repair_recent_history(day, db_path, *, lookback_sessions=20, max_repair_sessions=3,
                          minimum_coverage=0.95, sync_fn=None):
    """Fetch only deficient sessions, never a fresh 400-day download or a model call."""
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as db:
        before = check_recent_history(db, day, lookback_sessions=lookback_sessions,
                                      minimum_coverage=minimum_coverage)
    if (not before["baseline_symbols"] or
            len(before["missing_sessions"]) > max_repair_sessions):
        raise RuntimeError(json.dumps({"status": "HISTORY_REPAIR_BLOCKED",
                                       "max_repair_sessions": max_repair_sessions,
                                       "history": before}))
    updater = sync_fn or sync
    updates = [updater(session, db_path, preserve_existing=True)
               for session in before["missing_sessions"]]
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as db:
        after = check_recent_history(db, day, lookback_sessions=lookback_sessions,
                                     minimum_coverage=minimum_coverage)
    result = {**after, "repaired_sessions": before["missing_sessions"], "updates": updates,
              "requests": sum(item.get("requests", 0) for item in updates),
              "rows": sum(item.get("rows", 0) for item in updates)}
    if after["status"] != "HISTORY_READY":
        raise RuntimeError(json.dumps({**result, "status": "HISTORY_REPAIR_INCOMPLETE"}))
    return result

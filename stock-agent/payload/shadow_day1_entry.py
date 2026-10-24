#!/usr/bin/env python3
"""Alpaca historical SIP close confirmation for final Day 1 research only."""
from __future__ import annotations

import json
import sqlite3
import time
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from alpaca_market import AlpacaData, SOURCE, credentials
from phase5_entry import (cache_intraday, current_regime, daily_bars, decide,
                          init_db, load_config, regular_bar, save_decision,
                          validate_aggregate)
from quant_screen import ROOT
from shadow_day1_gate import DAY

ET = ZoneInfo("America/New_York")
REPORT = ROOT / "data" / "shadow_day1_entry_report.json"


def sync_5m(db, client, ticker, asof=DAY, *, days=45):
    session_start = datetime.fromisoformat(asof).replace(tzinfo=ET)
    session_end = session_start + timedelta(days=1)
    today = db.execute("""SELECT COUNT(*),MAX(ts_ms) FROM entry_intraday_bars
        WHERE ticker=? AND source=? AND ts_ms>=? AND ts_ms<?""",
        (ticker, SOURCE, int(session_start.timestamp() * 1000),
         int(session_end.timestamp() * 1000))).fetchone()
    total = db.execute("SELECT COUNT(*) FROM entry_intraday_bars WHERE ticker=?", (ticker,)).fetchone()[0]
    if today[0] >= 78 and total >= 2400:
        return 0, "CACHED"
    first = date.fromisoformat(asof) - timedelta(days=days)
    latest = db.execute("SELECT MAX(ts_ms) FROM entry_intraday_bars WHERE ticker=? AND source=?",
                        (ticker, SOURCE)).fetchone()[0]
    if latest and total >= 2000:
        overlap = datetime.fromtimestamp(latest / 1000, ET).date() - timedelta(days=1)
        first = max(first, overlap)
    start = first.isoformat()
    payload = client.bars([ticker], "5Min", start + "T09:30:00-04:00",
                          asof + "T16:00:00-04:00")[ticker]
    batch = []
    for item in payload:
        stamp = datetime.fromisoformat(item["t"].replace("Z", "+00:00"))
        ts_ms = int(stamp.timestamp() * 1000)
        if not regular_bar(ts_ms):
            continue
        opened, high, low, close, volume, vwap = validate_aggregate(item)
        batch.append((ticker, ts_ms, opened, high, low, close, volume, vwap,
                      SOURCE, datetime.now(timezone.utc).isoformat()))
    if len({row[1] for row in batch}) != len(batch):
        raise ValueError("duplicate Alpaca 5M timestamp")
    with db:
        db.executemany("""INSERT INTO entry_intraday_bars VALUES(?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(ticker,ts_ms) DO UPDATE SET open=excluded.open,high=excluded.high,
            low=excluded.low,close=excluded.close,volume=excluded.volume,
            bar_vwap=excluded.bar_vwap,source=excluded.source,fetched_at=excluded.fetched_at""", batch)
        db.execute("""INSERT INTO entry_sync VALUES(?,?,?,?,?,?) ON CONFLICT(ticker,interval)
            DO UPDATE SET last_asof=excluded.last_asof,last_fetch_at=excluded.last_fetch_at,
            status=excluded.status,rows_downloaded=entry_sync.rows_downloaded+excluded.rows_downloaded""",
            (ticker, "5m", asof, datetime.now(timezone.utc).isoformat(), "READY", len(batch)))
    return len(batch), "DOWNLOADED"


def intraday_return(market, entry, ticker, asof=DAY):
    bars = [r for r in cache_intraday(entry, ticker, asof) if r["date"] == asof]
    prior = market.execute("SELECT close FROM daily_bars WHERE ticker=? AND date<? ORDER BY date DESC LIMIT 1",
                           (ticker, asof)).fetchone()
    if not bars or not prior or bars[-1]["minute"] != 955:
        return None
    return (bars[-1]["close"] / prior[0] - 1) * 100


def run(research_report, client=None):
    started = time.monotonic()
    cfg = load_config()
    quant_params = json.loads((ROOT / "quant_config.json").read_text())["feature_parameters"]
    market = sqlite3.connect(f"file:{ROOT / 'data' / 'market_data.db'}?mode=ro", uri=True)
    entry = sqlite3.connect(ROOT / "data" / "entry_data.db")
    market.row_factory = entry.row_factory = sqlite3.Row
    init_db(entry)
    if entry.execute("""SELECT 1 FROM entry_runs WHERE asof=? AND source=? LIMIT 1""",
                     (DAY, SOURCE)).fetchone():
        raise ValueError("DAY1_ENTRY_ALREADY_STARTED")
    if research_report["run_id"] != "SHADOW_DAY1_20260928_INITIAL":
        raise ValueError("DAY1_RESEARCH_RUN_MISMATCH")
    final = [r for r in research_report["cases"] if
             r["final_status"] == "FINAL_RESEARCH_AVAILABLE"]
    final.sort(key=lambda r: (-r["candidate_score"], r["ticker"]))
    final = final[:cfg["candidate_limit"]]
    sectors = {r["ticker"]: market.execute("SELECT mapped_sector_etf FROM securities WHERE ticker=?",
                                            (r["ticker"],)).fetchone()[0] for r in final}
    names = sorted({"SPY", "QQQ", *[r["ticker"] for r in final],
                    *[v for v in sectors.values() if v]})
    client = client or AlpacaData(credentials())
    begun = datetime.now(timezone.utc).isoformat()
    with entry:
        run_id = entry.execute("""INSERT INTO entry_runs(asof,started_at,status,source,config_json)
            VALUES(?,?,?,?,?)""", (DAY, begun, "RUNNING", SOURCE,
                                 json.dumps(cfg, sort_keys=True))).lastrowid
    rows, errors = 0, {}
    for ticker in names:
        try:
            count, _ = sync_5m(entry, client, ticker)
            rows += count
        except (ValueError, RuntimeError, KeyError, TypeError) as exc:
            errors[ticker] = "MARKET_DATA:" + type(exc).__name__
    regime, regime_source = current_regime(market, DAY)
    benchmarks = {ticker: intraday_return(market, entry, ticker) for ticker in ("SPY", "QQQ")}
    results = []
    for item in final:
        ticker = item["ticker"]
        try:
            if ticker in errors:
                raise ValueError(errors[ticker])
            daily = daily_bars(market, entry, ticker, DAY)
            intra = cache_intraday(entry, ticker, DAY)
            if not daily or daily[-1]["date"] != DAY or not intra or intra[-1]["date"] != DAY:
                raise ValueError("missing current Daily or 5M data")
            reference = {"candidate_score": item["candidate_score"],
                         "research_score": item["research_score"],
                         "catalyst_score": item["catalyst_score"],
                         "conviction_score": item["conviction_score"] or item["candidate_score"],
                         "created_at": item["research_completed_at"]}
            result = decide(daily, intra, benchmarks,
                            intraday_return(market, entry, sectors[ticker]) if sectors[ticker] else None,
                            reference, regime, cfg, quant_params, mode="CLOSE")
            completed = datetime.fromisoformat(item["research_completed_at"].replace("Z", "+00:00"))
            close = datetime(2026, 9, 28, 16, tzinfo=ET)
            post_close = completed.astimezone(ET) > close
            result["signal_validity"] = "NEXT_SESSION_CANDIDATE" if post_close else "POINT_IN_TIME"
            result["research_timing"] = "POST_CLOSE_RESEARCH" if post_close else "PRE_CLOSE_RESEARCH"
            result["target_session"] = "2026-09-29"
            if result["entry_state"] not in {"BUY_TOMORROW_CANDIDATE", "WATCH", "REJECT_ENTRY"}:
                raise ValueError("BUY_NOW_NOT_ALLOWED")
        except (ValueError, TypeError, KeyError, ZeroDivisionError) as exc:
            errors[ticker] = "ENTRY:" + type(exc).__name__ + ":" + str(exc)[:120]
            result = {"entry_state": "REJECT_ENTRY", "entry_score": None,
                      "signal_validity": "DATA_INSUFFICIENT", "target_session": "2026-09-29",
                      "research_timing": None, "why_wait": [errors[ticker]]}
        result["ticker"] = ticker
        save_decision(entry, run_id, ticker, result, item)
        results.append(result)
    with entry:
        entry.execute("""UPDATE entry_runs SET ended_at=?,status=?,api_requests=?,rows_downloaded=?
            WHERE id=?""", (datetime.now(timezone.utc).isoformat(),
                           "COMPLETE" if not errors else "PARTIAL", client.requests, rows, run_id))
    out = {"run_id": run_id, "asof": DAY, "target_session": "2026-09-29",
           "source": SOURCE, "market_regime": regime, "market_regime_source": regime_source,
           "results": results, "errors": errors, "api_requests": client.requests,
           "downloaded_5m_rows": rows, "runtime_seconds": round(time.monotonic() - started, 2),
           "ai_calls": 0, "sol_calls": 0, "telegram_messages": 0}
    temporary = REPORT.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(out, indent=2, ensure_ascii=False) + "\n")
    temporary.replace(REPORT)
    market.close(); entry.close()
    return out


if __name__ == "__main__":
    report = run(json.loads((ROOT / "data" / "shadow_day1_initial_report.json").read_text()))
    print(json.dumps({"count": len(report["results"]), "states": {r["ticker"]: r["entry_state"]
                      for r in report["results"]}, "api_requests": report["api_requests"],
                      "errors": report["errors"]}, indent=2))

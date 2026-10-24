#!/usr/bin/env python3
"""Save one truthful Day 1 shadow result and pending forward observations."""
from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timezone

from phase6_portfolio import allocate, load_config
from quant_screen import ROOT
from shadow_day1_gate import BENCHMARKS, DAY, check
from shadow_day1_state import connect, row as state_row

OUTPUT = ROOT / "data" / "shadow_day1_final.json"
RESULT_DB = ROOT / "data" / "shadow_day1_results.db"
HORIZONS = (1, 5, 10, 20, 60)


class ZeroCashBook:
    def summary(self):
        return {"portfolio_value_cents": 0, "cash_cents": 0,
                "positions": [], "stale_tickers": []}


def candidate_changes(fundamental):
    old = {r[0]: r[1] for r in fundamental.execute("""SELECT ticker,candidate_rank FROM fundamental_scores
        WHERE run_id=(SELECT id FROM fundamental_runs WHERE scan_date='2026-09-25'
                      ORDER BY id DESC LIMIT 1) AND candidate_rank<=30""")}
    current = {r[0]: r[1] for r in fundamental.execute("""SELECT ticker,candidate_rank FROM fundamental_scores
        WHERE run_id=(SELECT id FROM fundamental_runs WHERE scan_date=? ORDER BY id DESC LIMIT 1)
        AND candidate_rank<=30""", (DAY,))}
    if len(old) != 30 or len(current) != 30:
        raise ValueError("CANDIDATE_TOP30_INCOMPLETE")
    return {"NEW_ENTRY": sorted(current.keys() - old.keys()),
            "STILL_CANDIDATE": sorted(current.keys() & old.keys()),
            "RANK_UP": {t: {"from": old[t], "to": current[t]} for t in current.keys() & old.keys()
                        if current[t] < old[t]},
            "RANK_DOWN": {t: {"from": old[t], "to": current[t]} for t in current.keys() & old.keys()
                          if current[t] > old[t]},
            "DROPPED": sorted(old.keys() - current.keys())}


def build():
    started = time.monotonic()
    with connect() as state:
        if state_row(state)["state"] != "RUNNING" or state_row(state)["resume_count"] != 1:
            raise ValueError("DAY1_MANUAL_RESUME_NOT_RUNNING")
    quant = json.loads((ROOT / "data" / "real_top100.json").read_text())
    fundamental_report = json.loads((ROOT / "data" / "phase3_report.json").read_text())
    research = json.loads((ROOT / "data" / "shadow_day1_initial_report.json").read_text())
    entry = json.loads((ROOT / "data" / "shadow_day1_entry_report.json").read_text())
    if (quant["ranking_asof"] != DAY or fundamental_report["scan_date"] != DAY or
            entry["asof"] != DAY or entry["source"] != "ALPACA_SIP" or
            research["source_research_run"] != 10 or research["sol_calls"] != 0 or
            research["luna_calls"] > 10 or research["confirmed_cost_usd"] > 0.05 or
            research["unknown_cost_reservations_usd"]):
        raise ValueError("DAY1_SAVED_INPUTS_OR_AI_BUDGET_INVALID")
    market = sqlite3.connect(f"file:{ROOT / 'data' / 'market_data.db'}?mode=ro", uri=True)
    fundamental = sqlite3.connect(f"file:{ROOT / 'data' / 'fundamental_data.db'}?mode=ro", uri=True)
    formal = sqlite3.connect(f"file:{ROOT / 'data' / 'stock_agent.db'}?mode=ro", uri=True)
    research_db = sqlite3.connect(f"file:{ROOT / 'data' / 'research_data.db'}?mode=ro", uri=True)
    research_times = research_db.execute("SELECT started_at,closed_at FROM day01_runs WHERE id=?",
                                         (research["run_id"],)).fetchone()
    if not research_times or not research_times[1]:
        raise ValueError("DAY1_RESEARCH_RUN_NOT_CLOSED")
    research_runtime = (datetime.fromisoformat(research_times[1]) -
                        datetime.fromisoformat(research_times[0])).total_seconds()
    market.row_factory = fundamental.row_factory = sqlite3.Row
    gate = check(market, datetime.now(timezone.utc))
    if gate["status"] != "SHADOW_DAY1_DATA_READY":
        raise ValueError("DAY1_MARKET_DATE_GATE_FAILED")
    if formal.execute("SELECT COUNT(*) FROM ledger").fetchone()[0] or \
            formal.execute("SELECT COUNT(*) FROM positions").fetchone()[0]:
        raise ValueError("FORMAL_PORTFOLIO_NOT_ZERO")
    changes = candidate_changes(fundamental)
    case_by_ticker = {r["ticker"]: r for r in research["cases"]}
    entry_by_ticker = {r["ticker"]: r for r in entry["results"]}
    source_run = fundamental_report["fundamental_run_id"]
    candidates = [dict(r) for r in fundamental.execute("""SELECT ticker,candidate_rank,
        candidate_score,fundamental_score,quant_score FROM fundamental_scores
        WHERE run_id=? AND candidate_rank<=30 ORDER BY candidate_rank""", (source_run,))]
    if len(candidates) != 30:
        raise ValueError("DAY1_CANDIDATES_NOT_SAVED")
    spy = market.execute("SELECT close FROM daily_bars WHERE ticker='SPY' AND date=?", (DAY,)).fetchone()[0]
    qqq = market.execute("SELECT close FROM daily_bars WHERE ticker='QQQ' AND date=?", (DAY,)).fetchone()[0]
    sizing_inputs = []
    for item in entry["results"]:
        ticker = item["ticker"]
        qr = market.execute("""SELECT q.risk_score,q.features_json,s.normalized_sector,s.industry,
            b.close FROM quant_scores q JOIN quant_runs r ON r.id=q.run_id
            JOIN securities s ON s.ticker=q.ticker
            JOIN daily_bars b ON b.ticker=q.ticker AND b.date=?
            WHERE r.asof=? AND r.data_mode='live' AND q.ticker=? ORDER BY r.id DESC LIMIT 1""",
            (DAY, DAY, ticker)).fetchone()
        if not qr:
            continue
        features = json.loads(qr[1])
        sizing_inputs.append({"ticker": ticker, "conviction": case_by_ticker[ticker]["conviction_score"] or 0,
                              "entry": item.get("entry_score") or 0, "risk_score": qr[0],
                              "atr_pct": features.get("atr_pct", 0) * 100,
                              "realized_vol_pct": features.get("realized_vol20", 0) * 100,
                              "drawdown_pct": abs(features.get("max_drawdown120", 0)) * 100,
                              "state": item["entry_state"], "price": qr[4],
                              "price_at": "2026-09-28T16:00:00-04:00", "sector": qr[2],
                              "industry": qr[3], "chase_risk": item.get("chase_risk"),
                              "regime": entry["market_regime"], "simulation": True})
    allocations = allocate(ZeroCashBook(), sizing_inputs, asof=DAY,
                           cfg=load_config(), persist=False, spy_close=spy, qqq_close=qqq)
    by_allocation = {r["ticker"]: r for r in allocations}
    if any(r["suggested_cents"] != 0 or "NO_CAPITAL_CONFIGURED" not in r["reasons"]
           for r in allocations):
        raise ValueError("ZERO_CASH_ALLOCATION_GUARD_FAILED")
    result_db = sqlite3.connect(RESULT_DB)
    result_db.executescript((ROOT / "shadow_day1_results.sql").read_text())
    if result_db.execute("SELECT 1 FROM shadow_day1_snapshots WHERE run_id=? LIMIT 1",
                         ("SHADOW_DAY1_20260928",)).fetchone():
        raise ValueError("DAY1_FORWARD_SNAPSHOT_ALREADY_EXISTS")
    timestamp = datetime.now(timezone.utc).isoformat()
    with result_db:
        for candidate in candidates:
            ticker = candidate["ticker"]
            bar = market.execute("SELECT close,source FROM daily_bars WHERE ticker=? AND date=?",
                                 (ticker, DAY)).fetchone()
            if not bar or bar[1] != "ALPACA_SIP":
                raise ValueError(f"{ticker}: DAY1_DAILY_SOURCE_INVALID")
            case = case_by_ticker.get(ticker)
            signal = entry_by_ticker.get(ticker)
            result_db.execute("""INSERT INTO shadow_day1_snapshots VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                ("SHADOW_DAY1_20260928", ticker, DAY, "ALPACA_SIP",
                 candidate["candidate_rank"], candidate["candidate_score"],
                 candidate["quant_score"], candidate["fundamental_score"],
                 case["final_status"] if case else "NOT_RESEARCHED",
                 case["research_score"] if case else None,
                 case["conviction_score"] if case else None,
                 signal["entry_state"] if signal else None,
                 signal.get("entry_score") if signal else None,
                 json.dumps(by_allocation.get(ticker)) if ticker in by_allocation else None,
                 bar[0], spy, qqq, timestamp))
            result_db.executemany("""INSERT INTO shadow_day1_forward
                (run_id,ticker,horizon,status) VALUES(?,?,?,'PENDING')""",
                [("SHADOW_DAY1_20260928", ticker, horizon) for horizon in HORIZONS])
    observed = result_db.execute("SELECT COUNT(*) FROM shadow_day1_snapshots").fetchone()[0]
    pending = result_db.execute("SELECT COUNT(*) FROM shadow_day1_forward WHERE status='PENDING'").fetchone()[0]
    outcome = {"status": "PIPELINE_COMPLETE", "trading_date": DAY,
               "market_data_as_of": DAY, "target_session": "2026-09-29",
               "data_sources": {"daily_today": "ALPACA_SIP", "history": "MASSIVE",
                                "intraday": "ALPACA_SIP", "volatility": "YAHOO"},
               "market_regime": entry["market_regime"], "phase1_passed": quant["phase1_passed"],
               "phase2_success": quant["phase2_success"], "phase2_failed": quant["phase2_failed"],
               "quant_top100": quant.get("overall_top100", []),
               "candidate_top30": fundamental_report["candidate_top30"],
               "candidate_changes": changes,
               "research": {k: v for k, v in research.items() if k != "cases"},
               "research_cases": research["cases"],
               "entry_results": entry["results"], "entry_errors": entry["errors"],
               "entry_api_requests": entry["api_requests"],
               "entry_downloaded_5m_rows": entry["downloaded_5m_rows"],
               "portfolio": {"cash_usd": 0, "suggested_allocation_usd": 0,
                             "reason": "NO_CAPITAL_CONFIGURED", "assessments": allocations},
               "forward_tracking": {"snapshots": observed, "pending_horizons": pending,
                                    "horizons": list(HORIZONS)},
               "phase_statuses": {"market": "COMPLETE", "phase1": "COMPLETE", "phase2": "COMPLETE",
                                  "phase3": "COMPLETE", "research": "COMPLETE", "entry": "COMPLETE",
                                  "forward_tracking": "COMPLETE", "telegram_summary": "READY"},
               "ai_calls": research["luna_calls"], "sol_calls": 0,
               "ai_cost_usd": research["confirmed_cost_usd"],
               "incremental_runtime_seconds": round(time.monotonic() - started +
                                                    entry["runtime_seconds"] + research_runtime, 2),
               "no_trade_placed": True}
    temp = OUTPUT.with_suffix(".json.tmp")
    temp.write_text(json.dumps(outcome, indent=2, ensure_ascii=False) + "\n")
    temp.replace(OUTPUT)
    market.close(); fundamental.close(); formal.close(); research_db.close(); result_db.close()
    return outcome


if __name__ == "__main__":
    report = build()
    print(json.dumps({"status": report["status"], "forward_snapshots":
                      report["forward_tracking"]["snapshots"], "entry_results":
                      {r["ticker"]: r["entry_state"] for r in report["entry_results"]}}, indent=2))

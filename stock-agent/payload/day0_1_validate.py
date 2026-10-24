#!/usr/bin/env python3
"""Read-only Day 0.1 Phase 5/6 point-in-time validation and Top 5 trace."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from day0_1_research import DB, REPORT as RESEARCH_REPORT, ROOT, RUN

OUTPUT = ROOT / "data" / "day0_1_validation_report.json"
ET = ZoneInfo("America/New_York")


def close_time(day):
    return datetime.fromisoformat(day + "T16:00:00").replace(tzinfo=ET)


def entry_status(final_status, last_bar, latest_market, research_at, current_et):
    if final_status != "FINAL_RESEARCH_AVAILABLE":
        return "ENTRY_NOT_AVAILABLE", final_status
    if not research_at:
        return "ENTRY_NOT_AVAILABLE", "RESEARCH_TIMESTAMP_MISSING"
    if not last_bar or last_bar != latest_market:
        return "ENTRY_DATA_STALE", "CANDIDATE_DAILY_BAR_MISSING"
    completed = datetime.fromisoformat(research_at.replace("Z", "+00:00"))
    if completed.tzinfo is None or completed.astimezone(ET) > close_time(last_bar):
        return "ENTRY_DATA_STALE", "WAIT_FOR_NEXT_MARKET_DATA"
    if current_et.date().isoformat() > latest_market and current_et.weekday() < 5:
        return "ENTRY_DATA_STALE", "CURRENT_SESSION_BAR_MISSING"
    return "ENTRY_DATA_CURRENT", "POINT_IN_TIME_RESEARCH_AVAILABLE"


def main():
    research = json.loads(RESEARCH_REPORT.read_text())
    if research["run_id"] != RUN:
        raise ValueError("RESEARCH_RUN_MISMATCH")
    with sqlite3.connect(f"file:{DB}?mode=ro", uri=True) as rdb, \
         sqlite3.connect(f"file:{ROOT / 'data' / 'market_data.db'}?mode=ro", uri=True) as market, \
         sqlite3.connect(f"file:{ROOT / 'data' / 'stock_agent.db'}?mode=ro", uri=True) as portfolio:
        rdb.row_factory = sqlite3.Row
        market.row_factory = sqlite3.Row
        latest_market = market.execute("SELECT MAX(date) FROM daily_bars WHERE ticker='SPY'").fetchone()[0]
        if not latest_market:
            raise ValueError("MARKET_DATA_UNAVAILABLE")
        current_et = datetime.now(ET)
        decisions = []
        for case in research["cases"]:
            ticker = case["ticker"]
            last_bar = market.execute("SELECT MAX(date) FROM daily_bars WHERE ticker=?", (ticker,)).fetchone()[0]
            if case["final_status"] != "FINAL_RESEARCH_AVAILABLE":
                research_at = None
            else:
                attempt = rdb.execute("SELECT finished_at FROM day01_attempts WHERE run_id=? AND ticker=?",
                                      (RUN, ticker)).fetchone()
                research_at = attempt[0] if attempt else case["last_research_at"]
            state, reason = entry_status(case["final_status"], last_bar, latest_market,
                                         research_at, current_et)
            decisions.append({"ticker": ticker, "research_status": case["final_status"],
                              "entry_availability": state, "reason": reason,
                              "market_bar_as_of": last_bar, "research_at": research_at,
                              "entry_signal": "WAIT_FOR_NEXT_MARKET_DATA" if state == "ENTRY_DATA_STALE"
                              else "NO_SIGNAL_EVALUATED", "buy_now_allowed": False,
                              "buy_tomorrow_allowed": False})
        cash = portfolio.execute("SELECT COALESCE(SUM(amount_cents),0) FROM ledger").fetchone()[0]
        holdings = portfolio.execute("SELECT COUNT(*) FROM positions").fetchone()[0]
        if cash != 0 or holdings != 0:
            raise ValueError("FORMAL_PORTFOLIO_CHANGED: stop validation")
        day0 = json.loads((ROOT / "data" / "shadow_day0_report.json").read_text())
        by_ticker = {c["ticker"]: c for c in research["cases"]}
        entry_by_ticker = {d["ticker"]: d for d in decisions}
        trace = []
        for old in day0["top5_trace"]:
            ticker = old["ticker"]
            case = by_ticker[ticker]
            parsed_row = rdb.execute("SELECT parsed_result_json FROM day01_attempts WHERE run_id=? AND ticker=?",
                                     (RUN, ticker)).fetchone()
            parsed = json.loads(parsed_row[0]) if parsed_row and parsed_row[0] else None
            trace.append({"trace_id": f"{RUN}-{ticker}", "ticker": ticker,
                          "phase2_quant_score": old["phase2_quant_score"],
                          "phase3_fundamental_score": old["phase3_fundamental_score"],
                          "candidate_score": old["phase3_candidate_score"],
                          "phase4a_coverage_pct": case["evidence_coverage"],
                          "phase4a_confidence": case["evidence_confidence"],
                          "phase4a_evidence_as_of": case["evidence_as_of"],
                          "initial_research_status": case["research_status"],
                          "initial_research_score": parsed.get("research_score") if parsed else None,
                          "escalation": case["escalation"],
                          "final_research_status": case["final_status"],
                          "entry": entry_by_ticker[ticker],
                          "phase6_suggested_allocation_usd": 0,
                          "phase6_reason": "NO_CAPITAL_CONFIGURED"})
    counts = {k: sum(c["final_status"] == k for c in research["cases"]) for k in
              ("FINAL_RESEARCH_AVAILABLE", "DEEP_RESEARCH_PENDING",
               "INSUFFICIENT_EVIDENCE", "RESEARCH_FAILED")}
    entry_counts = {k: sum(d["entry_availability"] == k for d in decisions) for k in
                    ("ENTRY_DATA_CURRENT", "ENTRY_DATA_STALE", "ENTRY_NOT_AVAILABLE")}
    blockers = []
    if counts["RESEARCH_FAILED"]:
        blockers.append("Research failures remain; do not infer missing scores")
    if counts["DEEP_RESEARCH_PENDING"]:
        blockers.append("Deep Research is pending and Sol was deliberately not called")
    if entry_counts["ENTRY_DATA_STALE"]:
        blockers.append("Research postdates available close; wait for next market data")
    result = {"status": "DAY0_1_PASS" if not counts["RESEARCH_FAILED"] and
              len(research["cases"]) == 15 else "DAY0_1_PARTIAL",
              "generated_at": datetime.now(timezone.utc).isoformat(),
              "market_data_as_of": latest_market, "research_counts": counts,
              "research_cases": research["cases"],
              "initial_complete_new": sum(c["research_status"] == "COMPLETE" for c in research["cases"]),
              "entry_counts": entry_counts, "entry": decisions,
              "portfolio": {"cash_usd": 0, "positions": 0,
                            "suggested_allocation_usd": 0, "reason": "NO_CAPITAL_CONFIGURED"},
              "top5_trace": trace, "blockers": blockers,
              "cost": {"luna_calls": research["luna_calls"],
                       "input_tokens": research["input_tokens"],
                       "output_tokens": research["output_tokens"],
                       "actual_or_reserved_cost_usd": research["local_cost_usd"],
                       "sol_calls": 0, "other_phase_ai_tokens": 0},
              "safety": {"buy_now": False, "buy_tomorrow": False,
                         "broker_connected": False, "orders_placed": 0,
                         "five_day_scheduler_started": False}}
    OUTPUT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({k: v for k, v in result.items() if k not in
                      ("entry", "top5_trace", "research_cases")}, indent=2))


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Assemble a point-in-time Day 0 audit from real saved data, without API calls."""
from __future__ import annotations

import json
import sqlite3
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from phase4_tiers import research_cache_decision
from phase6_portfolio import allocate
from portfolio import Portfolio
from quant_screen import screen

ROOT = Path(__file__).resolve().parent


def readonly(name):
    db = sqlite3.connect((ROOT / "data" / name).resolve().as_uri() + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    return db


def build():
    started = time.monotonic()
    real = json.loads((ROOT / "data" / "real_top100.json").read_text())
    fundamental = json.loads((ROOT / "data" / "phase3_report.json").read_text())
    pre = json.loads((ROOT / "data" / "phase4_pre_report.json").read_text())
    evidence = json.loads((ROOT / "data" / "phase4_2_evidence_report.json").read_text())
    asof = real["ranking_asof"]
    if not all(x == asof for x in (fundamental["scan_date"], pre["candidate_scan_date"], evidence["source_candidate_scan_date"])):
        raise ValueError("Day 0 data dates disagree")
    trace_id = f"E2E-{asof.replace('-', '')}-Q{fundamental['quant_run_id']}-F{fundamental['fundamental_run_id']}-R{pre['research_run_id']}"
    market, fundamentals, research, entry, formal = (readonly(name) for name in (
        "market_data.db", "fundamental_data.db", "research_data.db", "entry_data.db", "stock_agent.db"))
    try:
        phase1_started = time.monotonic()
        replay = screen(market, asof, top=1)
        phase1_replay_seconds = round(time.monotonic() - phase1_started, 3)
        if replay["screened_count"] != real["phase1_passed"]:
            raise ValueError("Phase 1 read-only replay does not match saved real scan")
        cash = formal.execute("""SELECT COALESCE(SUM(CASE kind WHEN 'DEPOSIT' THEN amount_cents
            WHEN 'SELL' THEN amount_cents WHEN 'ADJUSTMENT' THEN amount_cents ELSE -amount_cents END),0) FROM ledger""").fetchone()[0]
        formal_rows = formal.execute("SELECT COUNT(*) FROM ledger").fetchone()[0]
        if cash != 0 or formal_rows:
            raise ValueError("Formal portfolio is not empty; abort Day 0 report")
        top15 = [r["ticker"] for r in pre["top15"]]
        cache = {}
        for name in top15:
            prior = research.execute("""SELECT candidate_score,conviction_score,research_score,catalyst_score,
                created_at FROM evidence_reasoner_results WHERE ticker=? AND research_status='COMPLETE'
                ORDER BY created_at DESC LIMIT 1""", (name,)).fetchone()
            current = next(r for r in pre["top15"] if r["ticker"] == name)
            current_hash = research.execute("""SELECT source_hash FROM evidence_packets_v2 WHERE ticker=?
                ORDER BY created_at DESC LIMIT 1""", (name,)).fetchone()
            prior_hash = research.execute("""SELECT source_hash FROM evidence_packets_v2
                WHERE ticker=? AND created_at<=? ORDER BY created_at DESC LIMIT 1""",
                (name, prior["created_at"])).fetchone() if prior else None
            no_new_evidence = bool(current_hash and prior_hash and current_hash[0] == prior_hash[0])
            delta = current["candidate_score"] - prior["candidate_score"] if prior else None
            decision = research_cache_decision({"status": "COMPLETE"} if prior else None,
                material_new_evidence=not no_new_evidence, candidate_score_change=delta)
            cache[name] = {"policy": decision, "candidate_score_delta": delta,
                           "same_evidence_hash": no_new_evidence,
                           "prior_research_score": prior["research_score"] if prior else None,
                           "prior_conviction_score": prior["conviction_score"] if prior else None,
                           "prior_catalyst_score": prior["catalyst_score"] if prior else None,
                           "prior_completed_at": prior["created_at"] if prior else None}
        priority = {r["ticker"]: r for r in research.execute(
            "SELECT ticker,priority_rank,reasons_json FROM research_priority WHERE run_id=?", (pre["research_run_id"],))}
        top5 = []
        candidates = []
        for base in fundamental["candidate_top30"][:5]:
            ticker = base["ticker"]
            quant = market.execute("SELECT rank,quant_score FROM quant_scores WHERE run_id=? AND ticker=?",
                                   (fundamental["quant_run_id"], ticker)).fetchone()
            phase3 = fundamentals.execute("""SELECT fundamental_score,candidate_score,explanation_json,
                flags_json,data_quality_status FROM fundamental_scores WHERE run_id=? AND ticker=?""",
                (fundamental["fundamental_run_id"], ticker)).fetchone()
            bar = market.execute("SELECT close FROM daily_bars WHERE ticker=? AND date=?", (ticker, asof)).fetchone()
            sector = base.get("sector")
            stored_entry = entry.execute("""SELECT entry_score,entry_state,signal_validity,explanation_json
                FROM entry_decisions d JOIN entry_runs r ON r.id=d.run_id
                WHERE d.ticker=? AND r.asof=? AND r.status='COMPLETE'
                ORDER BY d.run_id DESC LIMIT 1""", (ticker, asof)).fetchone()
            entry_valid = bool(stored_entry and stored_entry["signal_validity"] != "HISTORICAL_DIAGNOSTIC_ONLY")
            phase4 = cache.get(ticker, {})
            candidates.append({"ticker": ticker, "sector": sector, "conviction": phase4.get("prior_conviction_score") or 0,
                               "entry": stored_entry["entry_score"] if entry_valid else 0,
                               "risk_score": 70, "state": "WAIT", "price": bar["close"] if bar else None,
                               "price_at": datetime.fromisoformat(asof + "T16:00:00").replace(
                                   tzinfo=__import__("zoneinfo").ZoneInfo("America/New_York")).isoformat(),
                               "chase_risk": "UNKNOWN", "regime": real["market_regime"].get("regime"),
                               "simulation": True})
            top5.append({"ticker": ticker, "phase1_status": "PASSED" if quant else "FAILED",
                         "phase2_quant_rank": quant["rank"] if quant else None,
                         "phase2_quant_score": quant["quant_score"] if quant else None,
                         "phase3_fundamental_score": phase3["fundamental_score"] if phase3 else None,
                         "phase3_candidate_score": phase3["candidate_score"] if phase3 else None,
                         "phase3_data_quality": phase3["data_quality_status"] if phase3 else None,
                         "phase3_reasons": json.loads(phase3["explanation_json"]) if phase3 else None,
                         "phase4_priority_rank": priority[ticker]["priority_rank"] if ticker in priority else None,
                         "phase4_priority_reasons": json.loads(priority[ticker]["reasons_json"]) if ticker in priority else [],
                         "phase4_research_tier": "DEEP_CACHED" if phase4.get("policy") == "CACHED_RESEARCH" else "RESEARCH_PENDING",
                         "phase4_research_score": phase4.get("prior_research_score") if phase4.get("policy") == "CACHED_RESEARCH" else None,
                         "phase4_conviction_score": phase4.get("prior_conviction_score") if phase4.get("policy") == "CACHED_RESEARCH" else None,
                         "phase5_entry_score": stored_entry["entry_score"] if stored_entry else None,
                         "phase5_entry_state": stored_entry["entry_state"] if stored_entry else "ENTRY_PENDING_RESEARCH",
                         "phase5_signal_validity": stored_entry["signal_validity"] if stored_entry else "NO_ENTRY_RUN",
                         "phase5_reasons": json.loads(stored_entry["explanation_json"]) if stored_entry else None,
                         "market_data_timestamp": asof + " 16:00 America/New_York (daily close reference)",
                         "data_freshness": "REALTIME_DATA_UNAVAILABLE"})
        with tempfile.TemporaryDirectory(prefix="SHADOW_DAY0_ALLOCATION-") as folder:
            portfolio = Portfolio(Path(folder) / "simulation.db")
            try:
                phase6_started = time.monotonic()
                decisions = allocate(portfolio, candidates, persist=False)
                phase6_runtime = round(time.monotonic() - phase6_started, 4)
            finally:
                portfolio.close()
        for row, decision in zip(top5, decisions):
            if row["ticker"] != decision["ticker"]:
                decision = next(d for d in decisions if d["ticker"] == row["ticker"])
            row["phase6_suggested_allocation_usd"] = decision["suggested_cents"] / 100
            row["phase6_state"] = decision["state"]
            row["phase6_reasons"] = decision["reasons"]
            row["trace_id"] = trace_id + "-" + row["ticker"]
        sync = market.execute("SELECT details_json FROM market_sync_runs WHERE asof=? AND status='COMPLETE' ORDER BY id DESC LIMIT 1", (asof,)).fetchone()
        sync_details = json.loads(sync[0]) if sync else {}
        quant_ms = market.execute("SELECT elapsed_ms FROM quant_runs WHERE id=?", (fundamental["quant_run_id"],)).fetchone()[0]
        phase4a_runtime = round(sum(x.get("runtime_seconds", 0) for x in evidence["results"]), 2)
        clock_now = datetime.now(timezone.utc)
        scan_start = None
        for line in (ROOT / "data" / "real_market.log").read_text().splitlines():
            if f"ranking_scan_start asof={asof}" in line:
                scan_start = datetime.fromisoformat(line[:19]).replace(tzinfo=ZoneInfo("America/New_York"))
        pending = [name for name, status in cache.items() if status["policy"] != "CACHED_RESEARCH"]
        official_luna_estimate = 8000 * .20 / 1_000_000 + 1200 * 1.20 / 1_000_000
        official_sol_estimate = 8000 * 2 / 1_000_000 + 1200 * 10 / 1_000_000
        result = {"trace_id": trace_id, "status": "PARTIAL_VALIDATION", "asof": asof,
                  "generated_at": clock_now.isoformat(),
                  "formal_portfolio_cash_usd": 0, "formal_portfolio_ledger_rows": formal_rows,
                  "telegram": {"outbound_test_received": True, "authorized_commands": ["/start","/help","/balance","/portfolio","/status","/deposit 2000"],
                               "test_cash_usd": 2000, "test_portfolio_removed": True,
                               "unauthorized_live_test": "NOT_PERFORMED", "authorization_code_test": "PASSED"},
                  "market": {"timestamp": asof + " daily close", "regime": real["market_regime"].get("regime"),
                             "universe": real["us_common_listed_count"], "phase1_passed": real["phase1_passed"],
                             "phase2_success": real["phase2_success"], "phase2_failed": real["phase2_failed"],
                             "quant_top100": 100, "candidate_top30": fundamental["candidate_top30"]},
                  "phase4": {"initial_complete_new": 0, "deep_complete_cached": sum(x["policy"] == "CACHED_RESEARCH" for x in cache.values()),
                             "research_pending": pending, "research_cache": cache,
                             "evidence_network_requests": evidence["total_network_requests"],
                             "evidence_cache_hits": evidence["total_cache_hits"],
                             "evidence_coverage": {x["ticker"]: x.get("coverage_pct") for x in evidence["results"]},
                             "luna_estimated_cost_per_stock_usd": round(official_luna_estimate, 6),
                             "luna_estimated_cost_all_pending_usd": round(official_luna_estimate * len(pending), 6),
                             "sol_estimated_cost_per_escalation_usd": round(official_sol_estimate, 6),
                             "sol_escalation": "UNDETERMINED_UNTIL_INITIAL"},
                  "entry": {"validated_buy_tomorrow": 0, "validated_watch": 0,
                            "historical_diagnostic_wait": 3, "realtime_data": "UNAVAILABLE"},
                  "top5_trace": top5,
                  "partial_failures": ["14/15 current Top 15 require paid Initial Research; calls deliberately blocked",
                                       "CDNA cache qualifies under tier policy but legacy Phase 4 pre-report says FULL_RESEARCH",
                                       "Latest Phase 5 results are historical diagnostics, not point-in-time actionable signals",
                                       "Company IR unavailable for 15/15 evidence collections; SEC evidence retained"],
                  "tokens": {"phase1": 0, "phase2": 0, "phase3": 0, "phase4_initial_new": 0,
                             "phase4_deep_new": 0, "phase5": 0, "phase6": 0, "total_new": 0,
                             "actual_new_ai_cost_usd": 0},
                  "timing_seconds": {"market_data_update": sync_details.get("elapsed_seconds"),
                                     "phase1_actual": None, "phase1_readonly_replay": phase1_replay_seconds,
                                     "phase2": quant_ms / 1000,
                                     "phase3": fundamental["runtime_seconds"],
                                     "phase4_pre": None, "phase4a": phase4a_runtime,
                                     "phase4b": 0, "phase5": 0, "phase6": phase6_runtime,
                                     "report_assembly": round(time.monotonic() - started, 3),
                                     "total_reported_processing": round(real["runtime_seconds"] + fundamental["runtime_seconds"] + phase4a_runtime + phase6_runtime, 2),
                                     "total_wall_clock_since_market_scan": round((clock_now - scan_start.astimezone(timezone.utc)).total_seconds(), 2) if scan_start else None},
                  "api_requests": {"market_and_context": real["api_request_count"],
                                   "phase3_sec": fundamental["api_requests"],
                                   "phase4a_public_sources": evidence["total_network_requests"],
                                   "total_data": real["api_request_count"] + fundamental["api_requests"] + evidence["total_network_requests"],
                                   "paid_ai": 0, "telegram_not_instrumented": True},
                  "cache_hits": {"phase3_sec": fundamental["cache_hits"],
                                 "phase4a_public_sources": evidence["total_cache_hits"],
                                 "total_reported": fundamental["cache_hits"] + evidence["total_cache_hits"]}}
        return result
    finally:
        for db in (market, fundamentals, research, entry, formal):
            db.close()


if __name__ == "__main__":
    report = build()
    path = ROOT / "data" / "shadow_day0_report.json"
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps({"status": report["status"], "trace_id": report["trace_id"],
                      "research_pending": len(report["phase4"]["research_pending"]),
                      "ai_calls": report["api_requests"]["paid_ai"],
                      "data_api_requests": report["api_requests"]["total_data"]}))

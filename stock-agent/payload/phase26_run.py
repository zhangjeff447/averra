#!/usr/bin/env python3
"""Offline Phase 2.6 calibration scan over an already synced REAL market database."""
from __future__ import annotations

import argparse
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import sqlite3
import subprocess
import sys
import time
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from phase26_diagnostics import report as diagnostic_report
from phase26_forward import register_scan, statistics_report, update
from phase26_sector import apply_mapping
from quant_ranking import LOG as QUANT_LOG, load_config, scan
from quant_screen import ROOT, init_db, screen
from real_report import report as ranking_report


def historical_regime(db, asof, cfg):
    values = {}
    for symbol in ("VIX", "VXN"):
        row = db.execute("SELECT date,close,source FROM volatility_daily WHERE symbol=? AND date<=? ORDER BY date DESC LIMIT 1",
                         (symbol, asof)).fetchone()
        values[symbol] = dict(row) if row else None
    if any(values[s] is None or values[s]["date"] != asof for s in values):
        return "UNKNOWN_ASOF", values
    cuts = cfg["volatility"]
    levels = [sum(values[s]["close"] >= threshold for threshold in cuts[s.lower() + "_regime_cuts"])
              for s in values]
    return ("CALM", "NORMAL", "ELEVATED", "HIGH_RISK", "EXTREME")[max(levels)], values


def run(db, asof, baseline, quant_cfg, phase_cfg, market_cfg):
    started = time.monotonic()
    started_at = datetime.now(timezone.utc).isoformat()
    date.fromisoformat(asof)
    latest = db.execute("SELECT MAX(date) FROM daily_bars WHERE ticker='SPY'").fetchone()[0]
    if latest != asof:
        raise ValueError(f"REAL DATA FAILED: latest SPY bar {latest!r} does not equal {asof}")
    if db.execute("SELECT COUNT(*) FROM market_sync_days WHERE date=?", (asof,)).fetchone()[0] != 1:
        raise ValueError("REAL DATA FAILED: no completed daily sync for asof")
    apply_mapping(db, phase_cfg["sector_mapping"])
    phase1 = screen(db, asof, top=10000)
    ranked = scan(db, asof, quant_cfg, data_mode="live", top=100, phase26_cfg=phase_cfg)
    run_id = ranked["run_id"]
    full = ranking_report(db, run_id)
    regime, vol = historical_regime(db, asof, market_cfg)
    if baseline.get("run_id"):
        register_scan(db, baseline["run_id"], "UNKNOWN_ASOF")
    register_scan(db, run_id, regime)
    forward_update = update(db, phase_cfg["forward"])
    forward = statistics_report(db, phase_cfg["forward"])
    diagnostics = diagnostic_report(db, run_id, quant_cfg, phase_cfg)
    old_top = {row["ticker"]: row for row in baseline["overall_top100"]}
    new_top = {row["ticker"]: row for row in full["overall_top100"]}
    action_counts = Counter()
    old_suspicious = [r[0] for r in db.execute("""SELECT ticker FROM quant_failures
        WHERE run_id=? AND reason='possible_split_or_extreme_price'""", (baseline["run_id"],))]
    passed = {row["ticker"] for row in phase1["candidates"]}
    old_suspicious = [ticker for ticker in old_suspicious if ticker in passed]
    now_scored = {r[0] for r in db.execute("SELECT ticker FROM quant_scores WHERE run_id=?", (run_id,))}
    classifications = {}
    primary_counts = Counter()
    for ticker in old_suspicious:
        checks = [dict(r) for r in db.execute("SELECT * FROM action_validations WHERE ticker=?", (ticker,))]
        kinds = Counter(c["corporate_action_type"] for c in checks)
        for kind in kinds:
            action_counts[kind] += 1
        priority = ("CONFIRMED_REVERSE_SPLIT", "CONFIRMED_SPLIT", "OTHER_CORPORATE_ACTION",
                    "UNEXPLAINED_ANOMALY", "INSUFFICIENT_DATA", "TRUE_PRICE_MOVE")
        primary = next((kind for kind in priority if kind in kinds), "INSUFFICIENT_DATA")
        primary_counts[primary] += 1
        classifications[ticker] = {"types": sorted(kinds), "scored_now": ticker in now_scored,
                                   "primary_category": primary,
                                   "checks": [{k: c[k] for k in ("jump_date", "corporate_action_type", "corporate_action_date",
                                                                  "split_ratio", "source", "validation_status")} for c in checks]}
    def reason(ticker):
        checks = classifications.get(ticker, {}).get("checks", [])
        if any(c["validation_status"] == "RECOVERED_IN_MEMORY" for c in checks):
            return "CORPORATE_ACTION_RECOVERY"
        old = db.execute("SELECT features_json FROM quant_scores WHERE run_id=? AND ticker=?",
                         (baseline["run_id"], ticker)).fetchone()
        new = db.execute("SELECT features_json FROM quant_scores WHERE run_id=? AND ticker=?", (run_id, ticker)).fetchone()
        if old and new and json.loads(old[0]).get("sector_etf") != json.loads(new[0]).get("sector_etf"):
            return "SECTOR_MAPPING_CORRECTION"
        return "OTHER_OR_SCORE_RENORMALIZATION"
    comparison = {
        "added": [{"ticker": t, "new_rank": new_top[t]["rank"], "reason": reason(t)} for t in new_top.keys() - old_top.keys()],
        "removed": [{"ticker": t, "old_rank": old_top[t]["rank"], "reason": reason(t)} for t in old_top.keys() - new_top.keys()],
        "rank_increased": [{"ticker": t, "from": old_top[t]["rank"], "to": new_top[t]["rank"], "reason": reason(t)}
                           for t in old_top.keys() & new_top.keys() if new_top[t]["rank"] < old_top[t]["rank"]],
        "rank_decreased": [{"ticker": t, "from": old_top[t]["rank"], "to": new_top[t]["rank"], "reason": reason(t)}
                           for t in old_top.keys() & new_top.keys() if new_top[t]["rank"] > old_top[t]["rank"]],
    }
    for key in comparison:
        comparison[key].sort(key=lambda r: r.get("new_rank", r.get("to", r.get("old_rank", 0))))
    confidence = Counter(r[0] or "UNKNOWN" for r in db.execute("""SELECT s.mapping_confidence
        FROM quant_scores q JOIN securities s ON s.ticker=q.ticker WHERE q.run_id=? AND q.rank<=100""", (run_id,)))
    all_confidence = Counter(r[0] or "UNKNOWN" for r in db.execute("""SELECT s.mapping_confidence
        FROM quant_scores q JOIN securities s ON s.ticker=q.ticker WHERE q.run_id=?""", (run_id,)))
    reasons = Counter(r[0] for r in db.execute("SELECT reason FROM quant_failures WHERE run_id=? AND ticker IN (%s)" %
                     ",".join("?" for _ in passed), (run_id, *passed))) if passed else Counter()
    action_quarantined = len(set(old_suspicious) - now_scored)
    dashboard = {"universe_size": db.execute("SELECT COUNT(*) FROM securities").fetchone()[0],
                 "phase1_passed": len(passed), "phase2_success": ranked["scored_count"],
                 "phase2_failed_after_phase1": sum(reasons.values()),
                 "insufficient_history": reasons["insufficient_history"], "missing_bars": reasons["missing_bars"],
                 "confirmed_splits": action_counts["CONFIRMED_SPLIT"],
                 "reverse_splits": action_counts["CONFIRMED_REVERSE_SPLIT"],
                 "recovered_split_symbols": sum(t in now_scored and any(c["validation_status"] == "RECOVERED_IN_MEMORY"
                     for c in classifications[t]["checks"]) for t in old_suspicious),
                 "unexplained_anomalies": action_counts["UNEXPLAINED_ANOMALY"],
                 "quarantined_original_suspicious": action_quarantined,
                 "sector_confidence_top100_counts": {k: confidence[k] for k in ("HIGH", "MEDIUM", "LOW", "UNKNOWN")},
                 "sector_confidence_top100_percent": {k: round(confidence[k] / max(1, sum(confidence.values())) * 100, 2)
                                                      for k in ("HIGH", "MEDIUM", "LOW", "UNKNOWN")},
                 "sector_confidence_all_scored_counts": {k: all_confidence[k] for k in ("HIGH", "MEDIUM", "LOW", "UNKNOWN")},
                 "sector_confidence_all_scored_percent": {k: round(all_confidence[k] / max(1, sum(all_confidence.values())) * 100, 2)
                                                        for k in ("HIGH", "MEDIUM", "LOW", "UNKNOWN")},
                 "market_data_asof": asof, "market_data_freshness_days": (datetime.now(ZoneInfo("America/New_York")).date() - date.fromisoformat(asof)).days,
                 "vix_status": "AVAILABLE" if vol["VIX"] else "MISSING",
                 "vxn_status": "AVAILABLE" if vol["VXN"] else "MISSING",
                 "market_regime": regime, "regime_source": "YAHOO_STORED_DAILY" if regime != "UNKNOWN_ASOF" else "UNKNOWN_ASOF",
                 "forward_pending": forward["pending"], "forward_completed": forward["completed_observations"]}
    result = {"status": "COMPLETE", "asof": asof, "run_id": run_id, "baseline_run_id": baseline["run_id"],
              "started_at": started_at, "ended_at": datetime.now(timezone.utc).isoformat(),
              "data_source": "MASSIVE_STORED_REAL_DAILY", "api_requests": 0, "llm_api_calls": 0,
              "ranking": full, "comparison": comparison, "original_suspicious_count": len(old_suspicious),
              "original_suspicious_classifications": classifications,
              "original_suspicious_primary_category_counts": dict(primary_counts),
              "original_suspicious_category_counts": dict(action_counts),
              "original_suspicious_recovered_count": len(set(old_suspicious) & now_scored),
              "original_suspicious_still_quarantined_count": action_quarantined,
              "technical_diagnostics": diagnostics, "forward_update": forward_update,
              "forward_tracking": forward, "dashboard": dashboard,
              "vix_vxn_asof": vol, "elapsed_seconds": round(time.monotonic() - started, 2)}
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=ROOT / "data" / "market_data.db")
    parser.add_argument("--baseline", type=Path, default=ROOT / "data" / "phase25_baseline.json")
    parser.add_argument("--output", type=Path, default=ROOT / "data" / "phase26_report.json")
    parser.add_argument("--asof", help="Already synced trading date; defaults to latest stored SPY day")
    parser.add_argument("--sync-first", action="store_true", help="First run the existing real market sync, then update Phase 2.6")
    args = parser.parse_args()
    if not QUANT_LOG.handlers:
        QUANT_LOG.addHandler(RotatingFileHandler(ROOT / "data" / "quant_rank.log", maxBytes=2_000_000, backupCount=2))
    QUANT_LOG.setLevel(logging.INFO)
    QUANT_LOG.propagate = False
    if args.sync_first:
        command = [sys.executable, str(ROOT / "real_scan.py"), "--db", str(args.db)]
        if args.asof:
            command += ["--asof", args.asof]
        subprocess.run(command, check=True)
    baseline = json.loads(args.baseline.read_text())
    quant_cfg = load_config(ROOT / "quant_config.json")
    phase_cfg = json.loads((ROOT / "phase26_config.json").read_text())
    market_cfg = json.loads((ROOT / "real_market_config.json").read_text())
    with sqlite3.connect(args.db, timeout=60) as db:
        db.row_factory = sqlite3.Row
        init_db(db)
        asof = args.asof or db.execute("SELECT MAX(date) FROM daily_bars WHERE ticker='SPY'").fetchone()[0]
        result = run(db, asof, baseline, quant_cfg, phase_cfg, market_cfg)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_name(args.output.name + ".tmp")
    temporary.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    os.replace(temporary, args.output)
    print(json.dumps({"status": result["status"], "run_id": result["run_id"],
                      "scored": result["dashboard"]["phase2_success"],
                      "original_suspicious_recovered": result["original_suspicious_recovered_count"],
                      "elapsed_seconds": result["elapsed_seconds"], "output": str(args.output)}, indent=2))


if __name__ == "__main__":
    main()

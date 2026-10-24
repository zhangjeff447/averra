#!/usr/bin/env python3
"""Local-only Day 1 Phase 1/2 over the completed Alpaca daily import."""
import json
import sqlite3
import time
from collections import Counter

from quant_ranking import load_config, scan
from quant_screen import ROOT, screen
from real_report import report
from shadow_day1_gate import DAY, check
from shadow_day1_state import connect, row
from datetime import datetime, timezone


def main():
    started = time.monotonic()
    with connect() as state, sqlite3.connect(ROOT / "data" / "market_data.db", timeout=60) as db:
        if row(state)["state"] != "RUNNING":
            raise RuntimeError("SHADOW_DAY1_NOT_RUNNING")
        db.row_factory = sqlite3.Row
        if not check(db, datetime.now(timezone.utc))["entry_decision_allowed"]:
            raise RuntimeError("SHADOW_DAY1_DATA_NOT_READY")
        old = db.execute("SELECT id FROM quant_runs WHERE asof=? AND data_mode='live' ORDER BY id DESC LIMIT 1",
                         (DAY,)).fetchone()
        if old:
            raise RuntimeError("SHADOW_DAY1_QUANT_ALREADY_EXISTS")
        phase1 = screen(db, DAY, top=10000)
        phase2 = scan(db, DAY, load_config(ROOT / "quant_config.json"), data_mode="live", top=100)
        passed = {r["ticker"] for r in phase1["candidates"]}
        scored = {r[0] for r in db.execute("SELECT ticker FROM quant_scores WHERE run_id=?", (phase2["run_id"],))}
        failures = Counter(r[1] for r in db.execute("SELECT ticker,reason FROM quant_failures WHERE run_id=?",
                                                   (phase2["run_id"],)) if r[0] in passed)
        if scored - passed or len(scored) + sum(failures.values()) != len(passed):
            raise RuntimeError("PHASE1_PHASE2_RECONCILIATION_FAILED")
        ranked = report(db, phase2["run_id"])
        db.execute("""UPDATE market_sync_runs SET phase1_passed=?,phase2_success=?,phase2_failed=?
            WHERE id=(SELECT id FROM market_sync_runs WHERE asof=? AND source='ALPACA_SIP'
                      AND status='COMPLETE' ORDER BY id DESC LIMIT 1)""",
                   (phase1["screened_count"], phase2["scored_count"], sum(failures.values()), DAY))
        db.commit()
    out = {"status": "COMPLETE", "ranking_asof": DAY, "market_data_as_of": DAY,
           "data_sources": {"prices_today": "ALPACA_SIP", "historical_prices": "MASSIVE",
                            "volatility": "YAHOO"},
           "phase1_passed": phase1["screened_count"], "phase2_success": phase2["scored_count"],
           "phase2_failed": sum(failures.values()), "phase2_failure_reasons": dict(failures),
           "runtime_seconds": round(time.monotonic() - started, 2), **ranked}
    path = ROOT / "data" / "real_top100.json"
    temp = path.with_suffix(".json.tmp")
    temp.write_text(json.dumps(out, indent=2) + "\n")
    temp.replace(path)
    print(json.dumps({"status": out["status"], "phase1_passed": out["phase1_passed"],
                      "phase2_success": out["phase2_success"], "phase2_failed": out["phase2_failed"],
                      "runtime_seconds": out["runtime_seconds"]}))


if __name__ == "__main__":
    main()

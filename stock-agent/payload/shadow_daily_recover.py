#!/usr/bin/env python3
"""Explicit finalization-only recovery for an already-started Shadow daily run."""
from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone

from quant_screen import ROOT
from shadow_daily_pipeline import (RESULTS_DB, data_ready, finalize, paths, save)
from shadow_daily_scheduler import execution_lock, send_only
from shadow_daily_state import (connect, fail, manual_recover_finalization,
                                pipeline_complete, record)


def saved_inputs(day):
    base = paths(day)
    names = ("quant", "fundamental", "evidence", "research", "eligibility",
             "deep_plan", "entry", "portfolio")
    items = {name: json.loads((base / (name + ".json")).read_text()) for name in names}
    q, f, e, r, eligible, deep, entry, portfolio = (items[name] for name in names)
    if (not data_ready(day, require_session_date=False) or q["trading_date"] != day or
            f["scan_date"] != day or r["run_id"] != eligible["run_id"] or
            e["run_id"] != r["source_research_run"] or entry["trading_date"] != day or
            portfolio["forward_snapshots"] != 30 or len(eligible["cases"]) != 15 or
            r["luna_calls"] > 10 or r["confirmed_cost_usd"] > 0.05 or
            deep["sol_calls"] != 0):
        raise ValueError("SAVED_DAILY_ARTIFACTS_INVALID")
    with sqlite3.connect(f"file:{RESULTS_DB}?mode=ro", uri=True) as db:
        snapshots = db.execute("SELECT COUNT(*) FROM snapshots WHERE trading_date=?", (day,)).fetchone()[0]
        forward = db.execute("SELECT COUNT(*) FROM forward WHERE trading_date=?", (day,)).fetchone()[0]
    if (snapshots, forward) != (30, 150):
        raise ValueError("SAVED_FORWARD_INCOMPLETE")
    with sqlite3.connect(f"file:{ROOT / 'data' / 'research_data.db'}?mode=ro", uri=True) as db:
        attempts = db.execute("SELECT COUNT(*) FROM day01_attempts WHERE run_id=?",
                              (r["run_id"],)).fetchone()[0]
        unsettled = db.execute("""SELECT COUNT(*) FROM tier_budget_ledger WHERE run_id=?
            AND state IN ('RESERVED','IN_FLIGHT','FAILED_UNKNOWN')""", (r["run_id"],)).fetchone()[0]
    if attempts != r["luna_calls"] or unsettled:
        raise ValueError("RESEARCH_USAGE_UNSETTLED")
    with sqlite3.connect(f"file:{ROOT / 'data' / 'market_data.db'}?mode=ro", uri=True) as db:
        market = db.execute("""SELECT api_requests,downloaded_rows FROM market_sync_runs
            WHERE asof=? AND source='ALPACA_SIP' AND status='COMPLETE'
            ORDER BY id DESC LIMIT 1""", (day,)).fetchone()
    if not market:
        raise ValueError("MARKET_SYNC_RESULT_MISSING")
    market_stats = {"status": "COMPLETE", "date": day, "source": "ALPACA_SIP",
                    "requests": market[0], "rows": market[1],
                    "volatility_source": json.loads((base / "market_regime.json").read_text())["regime_source"]}
    phase_stats = {"quant_run_id": q["quant_run_id"],
                   "fundamental_run_id": f["fundamental_run_id"],
                   "phase1_passed": q["phase1_passed"],
                   "phase3_api_requests": f.get("api_requests", 0),
                   "phase3_cache_hits": f.get("cache_hits", 0)}
    return items, market_stats, phase_stats


def _resume(day):
    items, market_stats, phase_stats = saved_inputs(day)
    with connect() as db:
        item = manual_recover_finalization(db, day, artifacts_ready=True)
        try:
            started = datetime.fromisoformat(item["started_at"])
            elapsed = (datetime.now(timezone.utc) - started).total_seconds()
            path, report = finalize(day, market_stats, phase_stats, items["evidence"],
                items["research"], items["eligibility"], items["entry"],
                items["portfolio"], elapsed)
            report["deep_triage"] = items["deep_plan"]
            report["recovery"] = "FINALIZATION_ONLY_FROM_SAVED_ARTIFACTS_NO_AI_REPLAY"
            save(path, report)
            pipeline_complete(db, day, path, {"luna_calls": report["luna_calls"],
                "input_tokens": report["luna_input_tokens"],
                "output_tokens": report["luna_output_tokens"],
                "estimated_cost_usd": report["total_ai_cost_local_estimate_usd"],
                "sol_calls": 0})
        except Exception as exc:
            fail(db, day, "FINALIZE_RECOVERY", exc)
            raise
        message_id = send_only(db, day, path)
        return {"state": record(db, day)["state"], "telegram_message_id": message_id,
                "luna_calls_replayed": 0, "total_luna_calls": report["luna_calls"],
                "total_ai_cost_local_estimate_usd": report["total_ai_cost_local_estimate_usd"]}


def resume(day):
    with execution_lock() as acquired:
        if not acquired:
            raise RuntimeError("SHADOW_PIPELINE_ALREADY_RUNNING")
        return _resume(day)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--day", required=True)
    args = parser.parse_args()
    print(json.dumps(resume(args.day), sort_keys=True))

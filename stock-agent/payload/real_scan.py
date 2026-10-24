#!/usr/bin/env python3
"""One-command Phase 2.5 real daily sync, local ranking and market context."""
from __future__ import annotations

import argparse
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import sqlite3
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from market_regime import evaluate, previous_session
from quant_ranking import LOG as QUANT_LOG, load_config, scan
from quant_screen import ROOT, init_db, screen
from real_market import LOG, MassiveClient, MarketDataError, load_key, setup_logging, sync
from real_report import report
from sector_metadata import enrich, fetch_rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--asof", default=previous_session(datetime.now(ZoneInfo("America/New_York")).date()).isoformat())
    parser.add_argument("--db", type=Path, default=ROOT / "data" / "market_data.db")
    parser.add_argument("--no-sync", action="store_true", help="Use a previously completed 400-session real data backfill")
    args = parser.parse_args()
    started = time.monotonic()
    cfg = json.loads((ROOT / "real_market_config.json").read_text())
    args.db.parent.mkdir(parents=True, exist_ok=True)
    setup_logging()
    if not QUANT_LOG.handlers:
        handler = RotatingFileHandler(ROOT / "data" / "quant_rank.log", maxBytes=2_000_000, backupCount=2)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        QUANT_LOG.addHandler(handler)
    QUANT_LOG.setLevel(logging.INFO)
    LOG.info("ranking_scan_start asof=%s no_sync=%s", args.asof, args.no_sync)
    try:
        with sqlite3.connect(args.db, timeout=60) as db:
            db.row_factory = sqlite3.Row
            init_db(db)
            if args.no_sync:
                recent = db.execute("""SELECT date FROM daily_bars WHERE ticker='SPY' AND date<=?
                    ORDER BY date DESC LIMIT ?""", (args.asof, cfg["history_sessions"])).fetchall()
                synced = {row[0] for row in db.execute("SELECT date FROM market_sync_days WHERE date<=?", (args.asof,))}
                if (len(recent) != cfg["history_sessions"] or recent[0][0] != args.asof
                        or any(row[0] not in synced for row in recent)):
                    raise MarketDataError("MISSING_DATA", f"complete {cfg['history_sessions']}-session backfill not found")
                sync_run = db.execute("""SELECT * FROM market_sync_runs WHERE status='COMPLETE' AND asof=?
                    ORDER BY id DESC LIMIT 1""", (args.asof,)).fetchone()
                if sync_run is None:
                    raise MarketDataError("MISSING_DATA", "no completed real sync run")
                sync_run_id = sync_run["id"]
                sync_stats = json.loads(sync_run["details_json"])
            else:
                client = MassiveClient(load_key(ROOT / ".env"), cfg["free_plan_min_request_spacing_seconds"])
                sync_run_id, sync_stats = sync(db, client, args.asof, cfg,
                                               progress=lambda n, total, day, count: print(f"download {n}/{total} {day} accepted={count}", flush=True))
            metadata_metrics = {}
            metadata_rows = fetch_rows(metrics=metadata_metrics)
            sector_stats = enrich(db, metadata_rows, cfg)
            phase1 = screen(db, args.asof, top=10000)
            phase2 = scan(db, args.asof, load_config(ROOT / "quant_config.json"), data_mode="live", top=100)
            passed = {row["ticker"] for row in phase1["candidates"]}
            scored = {row[0] for row in db.execute("SELECT ticker FROM quant_scores WHERE run_id=?", (phase2["run_id"],))}
            failures = [(row[0], row[1]) for row in db.execute(
                "SELECT ticker,reason FROM quant_failures WHERE run_id=?", (phase2["run_id"],))]
            failed_after_phase1 = Counter(reason for ticker, reason in failures if ticker in passed)
            if scored - passed or len(scored) + sum(failed_after_phase1.values()) != len(passed):
                raise MarketDataError("MISSING_DATA", "Phase 1 and Phase 2 result sets do not reconcile")
            local_today = datetime.now(ZoneInfo("America/New_York")).date().isoformat()
            market_asof = max(args.asof, local_today)
            yahoo_metrics = {}
            market = evaluate(db, market_asof, datetime.now(timezone.utc), cfg["volatility"],
                              api_metrics=yahoo_metrics)
            LOG.info("market_regime asof=%s source=%s regime=%s confidence=%s",
                     market_asof, market["regime_source"], market["regime"], market["confidence"])
            ranked = report(db, phase2["run_id"])
            memory_rows = db.execute("SELECT COUNT(*) FROM quant_scores WHERE run_id=?", (phase2["run_id"],)).fetchone()[0]
            history = db.execute("""SELECT COUNT(*),SUM(downloaded_rows),SUM(accepted_rows),SUM(rejected_rows)
                FROM market_sync_days WHERE date<=?""", (args.asof,)).fetchone()
            actions = db.execute("SELECT COUNT(*),COUNT(DISTINCT ticker) FROM corporate_actions WHERE action_date<=?",
                                 (args.asof,)).fetchone()
            db_size = args.db.stat().st_size
            securities_count = db.execute("SELECT COUNT(*) FROM securities WHERE source='MASSIVE'").fetchone()[0]
            with db:
                db.execute("""UPDATE market_sync_runs SET phase1_passed=?,phase2_success=?,phase2_failed=?
                    WHERE id=?""", (phase1["screened_count"], phase2["scored_count"],
                                     sum(failed_after_phase1.values()), sync_run_id))
        result = {"status": "COMPLETE", "ranking_asof": args.asof, "market_regime_asof": market_asof,
                  "data_sources": {"prices": "MASSIVE", "sector": "NASDAQ_SCREENER", "volatility": "YAHOO"},
                  "securities_count": securities_count,
                  "us_common_listed_count": phase1["us_common_listed_count"],
                  "phase1_passed": phase1["screened_count"],
                  "phase1_rejected": phase1["rejected"], "phase2_success": phase2["scored_count"],
                  "phase2_failed": sum(failed_after_phase1.values()),
                  "phase2_failure_reasons": dict(sorted(failed_after_phase1.items())),
                  "phase2_all_eligible_failures": phase2["failed_count"],
                  "phase2_all_eligible_failure_reasons": phase2["failure_reasons"],
                  "database_bytes": db_size, "sync": sync_stats, "sector_metadata": sector_stats,
                  "stored_history": {"trading_days": history[0], "downloaded_rows": history[1],
                                     "accepted_rows": history[2], "rejected_rows": history[3]},
                  "corporate_actions_stored": actions[0], "corporate_action_symbols": actions[1],
                  "api_request_count": sync_stats.get("api_requests", 0) + metadata_metrics.get("api_requests", 0)
                  + yahoo_metrics.get("api_requests", 0),
                  "runtime_seconds": round(time.monotonic() - started, 2),
                  "ranking_memory_rows": memory_rows, "ranking_memory_ok": memory_rows == phase2["scored_count"],
                  "market_regime": market, **ranked}
        output = ROOT / "data" / "real_top100.json"
        temporary = output.with_name(output.name + ".tmp")
        temporary.write_text(json.dumps(result, indent=2) + "\n")
        os.replace(temporary, output)
        LOG.info("ranking_scan_end asof=%s phase1=%s phase2_success=%s phase2_failed=%s elapsed=%.2fs",
                 args.asof, phase1["screened_count"], phase2["scored_count"], sum(failed_after_phase1.values()),
                 result["runtime_seconds"])
        print(json.dumps({key: result[key] for key in ("status", "ranking_asof", "market_regime_asof",
                                                    "securities_count", "phase1_passed", "phase2_success",
                                                    "phase2_failed", "database_bytes", "api_request_count",
                                                    "runtime_seconds", "ranking_memory_ok")}, indent=2))
    except Exception as exc:
        LOG.exception("REAL DATA FAILED")
        raise SystemExit(f"REAL DATA FAILED: {type(exc).__name__}: {exc}") from exc


if __name__ == "__main__":
    main()

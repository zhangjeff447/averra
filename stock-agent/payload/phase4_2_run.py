#!/usr/bin/env python3
"""Phase 4A evidence-only collection for the stored AI Priority Top 15; no model calls."""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from phase3_sec import load_user_agent
from phase4_2_evidence import collect_ticker
from quant_screen import ROOT


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tickers", help="Comma-separated subset of AI Priority Top 15; default all 15")
    parser.add_argument("--asof", help="UTC ISO timestamp; defaults to now")
    args = parser.parse_args()
    cutoff = datetime.fromisoformat(args.asof.replace("Z", "+00:00")) if args.asof else datetime.now(timezone.utc)
    if cutoff.tzinfo is None or cutoff > datetime.now(timezone.utc):
        raise SystemExit("POINT_IN_TIME: asof must be UTC-aware and no later than now")
    cfg = json.loads((ROOT / "phase4_2_config.json").read_text())
    user_agent = load_user_agent(ROOT / ".env")
    with sqlite3.connect(ROOT / "data" / "market_data.db") as market, \
         sqlite3.connect(ROOT / "data" / "fundamental_data.db") as fundamental, \
         sqlite3.connect(ROOT / "data" / "research_data.db") as research:
        for db in (market, fundamental, research):
            db.row_factory = sqlite3.Row
        research.executescript((ROOT / "phase4_schema.sql").read_text())
        run = research.execute("SELECT id,candidate_scan_date FROM research_runs ORDER BY id DESC LIMIT 1").fetchone()
        if not run:
            raise SystemExit("AI Priority Top 15 unavailable")
        top15 = [row[0] for row in research.execute("""SELECT ticker FROM research_priority
            WHERE run_id=? AND priority_rank<=15 ORDER BY priority_rank""", (run["id"],))]
        if len(top15) != 15:
            raise SystemExit("AI Priority Top 15 incomplete")
        requested = [ticker.strip().upper() for ticker in args.tickers.split(",")] if args.tickers else top15
        if not requested or len(requested) != len(set(requested)) or set(requested) - set(top15):
            raise SystemExit("tickers must be a unique subset of the stored AI Priority Top 15")
        outcomes = []
        for ticker in requested:
            try:
                outcomes.append(collect_ticker(market, fundamental, research, ticker, cutoff.isoformat(),
                                               cfg, user_agent, historical_asof=bool(args.asof)))
            except (OSError, ValueError, sqlite3.Error) as exc:
                outcomes.append({"ticker": ticker, "status": "EVIDENCE_COLLECTION_FAILED",
                                 "error_class": type(exc).__name__, "network_requests": 0,
                                 "cache_hits": 0})
        report = {"phase": "4A", "priority_run_id": run["id"], "source_candidate_scan_date": run["candidate_scan_date"],
                  "evidence_asof": cutoff.isoformat(), "selected_tickers": requested,
                  "ai_api_calls": 0, "ai_api_cost_usd": 0,
                  "total_network_requests": sum(r["network_requests"] for r in outcomes),
                  "total_cache_hits": sum(r["cache_hits"] for r in outcomes), "results": outcomes}
    path = ROOT / "data" / "phase4_2_evidence_report.json"
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    os.replace(temporary, path)
    print(json.dumps({"phase": "4A", "tickers": requested,
                      "network_requests": report["total_network_requests"],
                      "cache_hits": report["total_cache_hits"],
                      "coverage": {r["ticker"]: r.get("coverage_pct") for r in outcomes},
                      "ai_api_calls": 0}, indent=2))


if __name__ == "__main__":
    main()

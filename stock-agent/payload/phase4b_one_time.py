#!/usr/bin/env python3
"""Explicit, single-use CDNA/AMD Sol baseline override. Default is offline preflight."""
from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone

from phase4_budget import budget_report, now, transition
from phase4_engine import compact
from phase4_run import load_key
from phase4b_evidence import cached_inputs, estimate, request_body
from phase4b_pilot import create_run, run_one
from quant_screen import ROOT

PILOT_ID = "CDNA_AMD_SOL_MANUAL_RETRY_2026_09_27"
TICKERS = ("CDNA", "AMD")
MODEL = "gpt-6-sol"
MAX_CALLS = 2
INPUT_ALLOWANCE = 20_000
OUTPUT_ALLOWANCE = 4_000
COST_ALLOWANCE = 0.10
SCHEMA = """
CREATE TABLE IF NOT EXISTS one_time_pilot (
    id TEXT PRIMARY KEY, status TEXT NOT NULL, model TEXT NOT NULL,
    max_calls INTEGER NOT NULL, input_allowance INTEGER NOT NULL,
    output_allowance INTEGER NOT NULL, cost_allowance REAL NOT NULL,
    run_id INTEGER, created_at TEXT NOT NULL, closed_at TEXT
);
CREATE TABLE IF NOT EXISTS one_time_pilot_attempts (
    pilot_id TEXT NOT NULL REFERENCES one_time_pilot(id),
    ticker TEXT NOT NULL CHECK(ticker IN ('CDNA','AMD')),
    prior_unknown_ledger_id INTEGER NOT NULL,
    authorization TEXT NOT NULL,
    status TEXT NOT NULL,
    estimated_input INTEGER NOT NULL,
    estimated_output INTEGER NOT NULL,
    estimated_cost REAL NOT NULL,
    actual_input INTEGER, actual_output INTEGER, actual_cost REAL,
    created_at TEXT NOT NULL, finished_at TEXT,
    PRIMARY KEY(pilot_id,ticker)
);
"""


def load_state(db):
    cfg = json.loads((ROOT / "phase4_config.json").read_text())
    if (cfg["model"] != MODEL or cfg["paid_ai_enabled"] or cfg["web_search_escalation_enabled"]
            or cfg["max_daily_input_tokens"] != 75_000 or cfg["max_daily_output_tokens"] != 18_000
            or cfg["max_daily_cost_usd"] != .35):
        raise ValueError("SAFETY_BLOCK: normal Phase 4 configuration changed")
    existing = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='one_time_pilot'").fetchone()
    if existing and db.execute("SELECT 1 FROM one_time_pilot WHERE id=?", (PILOT_ID,)).fetchone():
        raise ValueError("ONE_TIME_PILOT_ALREADY_USED: cannot restart or retry")
    inputs = {}
    estimates = {}
    previous = {}
    for ticker in TICKERS:
        completed = db.execute("""SELECT 1 FROM evidence_reasoner_results WHERE ticker=?
            AND research_status='COMPLETE' LIMIT 1""", (ticker,)).fetchone()
        if completed:
            raise ValueError(f"{ticker}: Sol baseline already complete")
        row = db.execute("""SELECT l.id FROM research_token_ledger l
            JOIN research_request_state s ON s.ledger_id=l.id
            WHERE l.ticker=? AND s.state='FAILED_UNKNOWN' ORDER BY l.id DESC LIMIT 1""",
            (ticker,)).fetchone()
        if not row:
            raise ValueError(f"{ticker}: no FAILED_UNKNOWN to manually authorize")
        previous[ticker] = row[0]
        inputs[ticker] = cached_inputs(db, ticker, cfg)  # Cached SQLite documents only; no fetch.
        body = request_body(inputs[ticker], cfg)
        if body["model"] != MODEL or body.get("tools") or body.get("max_tool_calls"):
            raise ValueError("MODEL_OR_WEB_SEARCH_SAFETY_BLOCK")
        if inputs[ticker]["evidence_policy"]["status"] != "READY":
            raise ValueError(f"{ticker}: EVIDENCE_PACKET_OVERSIZE")
        estimates[ticker] = estimate(body, cfg)
        if (estimates[ticker]["estimated_input_tokens"] > cfg["hard_input_tokens_per_stock"]
                or estimates[ticker]["estimated_total_cost_usd"] > cfg["max_cost_per_stock_usd"]):
            raise ValueError(f"{ticker}: per-stock budget blocked")
    return cfg, inputs, estimates, previous


def usage(db):
    rows = db.execute("""SELECT status,estimated_input,estimated_output,estimated_cost,
        actual_input,actual_output,actual_cost FROM one_time_pilot_attempts WHERE pilot_id=?""",
        (PILOT_ID,)).fetchall()
    # Unknown requests retain their estimate as a conservative pending reservation.
    charged = [r for r in rows if r[0] not in {"BUDGET_BLOCKED", "FAILED_NO_REQUEST"}]
    return {"calls_used": len(charged),
            "input_allowance_used": sum(r[4] if r[4] is not None else r[1] for r in charged),
            "output_allowance_used": sum(r[5] if r[5] is not None else r[2] for r in charged),
            "cost_allowance_used": round(sum(r[6] if r[6] is not None else r[3] for r in charged), 6)}


def permitted(used, prediction):
    return (used["calls_used"] < MAX_CALLS
            and used["input_allowance_used"] + prediction["estimated_input_tokens"] <= INPUT_ALLOWANCE
            and used["output_allowance_used"] + prediction["estimated_output_tokens"] <= OUTPUT_ALLOWANCE
            and used["cost_allowance_used"] + prediction["estimated_total_cost_usd"] <= COST_ALLOWANCE)


def preflight(db):
    cfg, inputs, estimates, previous = load_state(db)
    total = {"calls_used": 0, "input_allowance_used": 0, "output_allowance_used": 0,
             "cost_allowance_used": 0.0}
    for ticker in TICKERS:
        prediction = estimates[ticker]
        if not permitted(total, prediction):
            raise ValueError("ONE_TIME_PILOT_BUDGET_BLOCKED")
        total = {"calls_used": total["calls_used"] + 1,
                 "input_allowance_used": total["input_allowance_used"] + prediction["estimated_input_tokens"],
                 "output_allowance_used": total["output_allowance_used"] + prediction["estimated_output_tokens"],
                 "cost_allowance_used": total["cost_allowance_used"] + prediction["estimated_total_cost_usd"]}
    day = datetime.now(timezone.utc).date().isoformat()
    regular = budget_report(db, day, cfg)
    if (regular["ai_calls"] + MAX_CALLS > cfg["max_daily_ai_calls"]
            or regular["budget_input_tokens"] + total["input_allowance_used"] >
               cfg["max_daily_input_tokens"] + INPUT_ALLOWANCE
            or regular["budget_output_tokens"] + total["output_allowance_used"] >
               cfg["max_daily_output_tokens"] + OUTPUT_ALLOWANCE
            or regular["budget_cost_usd_estimate"] + total["cost_allowance_used"] >
               cfg["max_daily_cost_usd"] + COST_ALLOWANCE):
        raise ValueError("COMBINED_NORMAL_AND_ONE_TIME_BUDGET_BLOCKED")
    return cfg, inputs, estimates, previous, regular, total


def execute(db, key):
    cfg, inputs, estimates, previous, regular, forecast = preflight(db)
    if not key:
        raise ValueError("API_KEY_MISSING")
    extended = {**cfg, "max_daily_input_tokens": cfg["max_daily_input_tokens"] + INPUT_ALLOWANCE,
                "max_daily_output_tokens": cfg["max_daily_output_tokens"] + OUTPUT_ALLOWANCE,
                "max_daily_cost_usd": cfg["max_daily_cost_usd"] + COST_ALLOWANCE}
    with db:
        db.execute("""INSERT INTO one_time_pilot VALUES(?,?,?,?,?,?,?,?,?,?)""",
                   (PILOT_ID, "ACTIVE", MODEL, MAX_CALLS, INPUT_ALLOWANCE, OUTPUT_ALLOWANCE,
                    COST_ALLOWANCE, None, now(), None))
    report = {"pilot_id": PILOT_ID, "model": MODEL, "web_search": "OFF",
              "normal_daily_budget_usd": cfg["max_daily_cost_usd"],
              "one_time_allowances": {"calls": MAX_CALLS, "input_tokens": INPUT_ALLOWANCE,
                                      "output_tokens": OUTPUT_ALLOWANCE, "cost_usd": COST_ALLOWANCE},
              "preflight_daily": regular, "preflight_estimated_usage": forecast, "results": []}
    run_id = None
    try:
        run_id = create_run(db, inputs, cfg)
        with db:
            db.execute("UPDATE research_runs SET mode='ONE_TIME_PILOT' WHERE id=?", (run_id,))
            db.execute("UPDATE one_time_pilot SET run_id=? WHERE id=?", (run_id, PILOT_ID))
        report["run_id"] = run_id
        for ticker in TICKERS:
            prediction = estimates[ticker]
            if not permitted(usage(db), prediction):
                report["results"].append({"ticker": ticker, "research_status": "ONE_TIME_BUDGET_BLOCKED"})
                continue
            with db:
                db.execute("""INSERT INTO one_time_pilot_attempts
                    (pilot_id,ticker,prior_unknown_ledger_id,authorization,status,
                     estimated_input,estimated_output,estimated_cost,created_at)
                    VALUES(?,?,?,?,?,?,?,?,?)""",
                    (PILOT_ID, ticker, previous[ticker], "MANUAL_RETRY_AUTHORIZED", "IN_FLIGHT",
                     prediction["estimated_input_tokens"], prediction["estimated_output_tokens"],
                     prediction["estimated_total_cost_usd"], now()))
            try:
                result = run_one(db, run_id, ticker, inputs[ticker], extended, key)
            except Exception as exc:
                # The request may have reached the provider. Never retry this ticker here.
                pending = db.execute("""SELECT s.ledger_id FROM research_request_state s
                    JOIN research_token_ledger l ON l.id=s.ledger_id
                    WHERE l.run_id=? AND l.ticker=? AND s.state='IN_FLIGHT'
                    ORDER BY l.id DESC LIMIT 1""", (run_id, ticker)).fetchone()
                if pending:
                    transition(db, pending[0], "FAILED_UNKNOWN", error_category="UNKNOWN_NETWORK_ERROR")
                result = {"ticker": ticker, "research_status": "FAILED_UNKNOWN",
                          "reason": type(exc).__name__}
            with db:
                db.execute("""UPDATE one_time_pilot_attempts SET status=?,actual_input=?,
                    actual_output=?,actual_cost=?,finished_at=? WHERE pilot_id=? AND ticker=?""",
                    (result["research_status"], result.get("input_tokens"), result.get("output_tokens"),
                     result.get("local_cost_usd"), now(), PILOT_ID, ticker))
            report["results"].append(result)
    finally:
        with db:
            db.execute("UPDATE one_time_pilot SET status='CLOSED',closed_at=? WHERE id=?", (now(), PILOT_ID))
            if run_id is not None:
                db.execute("UPDATE research_runs SET status=? WHERE id=?",
                           ("COMPLETE" if len(report["results"]) == 2 and
                            all(r["research_status"] == "COMPLETE" for r in report["results"]) else "PARTIAL",
                            run_id))
        report["status"] = "CLOSED"
        report["usage"] = usage(db)
        report["normal_daily_budget_after"] = budget_report(
            db, datetime.now(timezone.utc).date().isoformat(), cfg)
        path = ROOT / "data" / "phase4b_one_time_report.json"
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", help="perform the authorized two paid retries")
    args = parser.parse_args()
    with sqlite3.connect(ROOT / "data" / "research_data.db") as db:
        db.row_factory = sqlite3.Row
        if args.execute:
            db.executescript(SCHEMA)
            report = execute(db, load_key(ROOT / ".env"))
            print(json.dumps({"status": report["status"], "usage": report["usage"],
                              "results": [{"ticker": r["ticker"], "status": r["research_status"]}
                                          for r in report["results"]]}, indent=2))
        else:
            _, inputs, estimates, previous, regular, forecast = preflight(db)
            print(json.dumps({"status": "OFFLINE_PREFLIGHT", "cache_integrity": "PASS",
                              "tickers": list(TICKERS), "model": MODEL, "web_search": "OFF",
                              "evidence_tokens": {t: inputs[t]["evidence_packet_tokens_estimate"] for t in TICKERS},
                              "estimated_usage": forecast, "estimates": estimates,
                              "prior_unknown_ledger_ids": previous,
                              "regular_daily_budget": regular, "api_calls_this_run": 0}, indent=2))


if __name__ == "__main__":
    main()

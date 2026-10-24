#!/usr/bin/env python3
"""Single-use frozen-evidence A/B run: GPT-5.6 Luna versus stored GPT-6 Sol."""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import time
import urllib.error
from datetime import datetime, timezone

from phase4_ab_evaluator import evaluate, final_decision
from phase4_budget import classify_network_error, now
from phase4_engine import compact
from phase4_run import api_research, load_key, model_cost
from phase4b_evidence import cached_inputs, estimate, request_body, validate_response
from quant_screen import ROOT

PILOT_ID = "LUNA_FROZEN_AB_META_CDNA_AMD_2026_09_27"
MODEL = "gpt-5.6-luna"
TICKERS = ("META", "CDNA", "AMD")
SOL_RUNS = {"META": 7, "CDNA": 8, "AMD": 8}
RATES = {"model_input_usd_per_million": .20, "model_output_usd_per_million": 1.20}
MAX_CALLS, MAX_INPUT, MAX_OUTPUT, MAX_COST = 3, 30_000, 4_000, .02
SCHEMA = """
CREATE TABLE IF NOT EXISTS phase4_ab_runs(
 id TEXT PRIMARY KEY, model TEXT NOT NULL, status TEXT NOT NULL,
 allowed_tickers_json TEXT NOT NULL, max_calls INTEGER NOT NULL,
 max_input INTEGER NOT NULL, max_output INTEGER NOT NULL, max_cost REAL NOT NULL,
 created_at TEXT NOT NULL, closed_at TEXT
);
CREATE TABLE IF NOT EXISTS phase4_ab_cases(
 run_id TEXT NOT NULL REFERENCES phase4_ab_runs(id), ticker TEXT NOT NULL,
 sol_run_id INTEGER NOT NULL, evidence_asof TEXT NOT NULL,
 fundamental_run_id INTEGER NOT NULL, quant_run_id INTEGER NOT NULL,
 evidence_hash TEXT NOT NULL, model_independent_input_hash TEXT NOT NULL,
 sol_request_json TEXT NOT NULL, luna_request_json TEXT NOT NULL,
 estimated_input INTEGER NOT NULL, estimated_output INTEGER NOT NULL,
 estimated_cost REAL NOT NULL, baseline_json TEXT NOT NULL,
 PRIMARY KEY(run_id,ticker)
);
CREATE TABLE IF NOT EXISTS phase4_ab_attempts(
 run_id TEXT NOT NULL REFERENCES phase4_ab_runs(id), ticker TEXT NOT NULL,
 status TEXT NOT NULL, request_started_at TEXT NOT NULL,
 estimated_input INTEGER NOT NULL, estimated_output INTEGER NOT NULL, estimated_cost REAL NOT NULL,
 returned_model TEXT, request_id TEXT, raw_response_json TEXT,
 raw_response_saved INTEGER NOT NULL DEFAULT 0,
 input_tokens INTEGER, output_tokens INTEGER, local_cost REAL,
 runtime_seconds REAL, result_json TEXT, evaluator_json TEXT,
 error_category TEXT, finished_at TEXT,
 PRIMARY KEY(run_id,ticker)
);
"""


def digest(value):
    return hashlib.sha256(compact(value).encode()).hexdigest()


def prepare(db):
    cfg = json.loads((ROOT / "phase4_config.json").read_text())
    if (cfg["model"] != "gpt-6-sol" or cfg["web_search_escalation_enabled"]
            or cfg["paid_ai_enabled"] or cfg["max_daily_cost_usd"] != .35):
        raise ValueError("SAFETY_BLOCK: Phase 4 baseline config differs")
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='phase4_ab_runs'").fetchone():
        if db.execute("SELECT 1 FROM phase4_ab_runs WHERE id=?", (PILOT_ID,)).fetchone():
            raise ValueError("AB_PILOT_ALREADY_USED: single-use experiment")
    prepared, baselines, events, totals = {}, {}, {}, {"input": 0, "output": 0, "cost": 0.0}
    for ticker in TICKERS:
        baseline_row = db.execute("""SELECT evidence_asof,research_status,result_json,candidate_score,
            input_tokens,output_tokens,local_cost_usd FROM evidence_reasoner_results
            WHERE run_id=? AND ticker=?""", (SOL_RUNS[ticker], ticker)).fetchone()
        if not baseline_row or baseline_row["research_status"] != "COMPLETE":
            raise ValueError(f"{ticker}: completed Sol baseline missing")
        inputs = cached_inputs(db, ticker, cfg)
        base_run = db.execute("SELECT fundamental_run_id,quant_run_id,config_json FROM research_runs WHERE id=?",
                              (SOL_RUNS[ticker],)).fetchone()
        if (not base_run or inputs["evidence_asof"] != baseline_row["evidence_asof"]
                or inputs["fundamental_run_id"] != base_run["fundamental_run_id"]
                or inputs["quant_run_id"] != base_run["quant_run_id"]):
            raise ValueError(f"{ticker}: frozen Phase 2/3/evidence mismatch")
        prior_cfg = json.loads(base_run["config_json"])
        if any(prior_cfg.get(k) != cfg.get(k) for k in
               ("model", "max_output_tokens_per_stock", "conviction_weights", "web_search_escalation_enabled")):
            raise ValueError(f"{ticker}: Sol request configuration mismatch")
        sol_body = request_body(inputs, cfg)
        luna_body = {**sol_body, "model": MODEL}
        if sol_body.get("tools") or luna_body.get("tools"):
            raise ValueError("WEB_SEARCH_SAFETY_BLOCK")
        sol_normalized = {k: v for k, v in sol_body.items() if k != "model"}
        luna_normalized = {k: v for k, v in luna_body.items() if k != "model"}
        if sol_normalized != luna_normalized:
            raise ValueError(f"{ticker}: model is not the only request variable")
        prediction = estimate(luna_body, {**cfg, **RATES})
        if (inputs["evidence_policy"]["status"] != "READY" or
                prediction["estimated_input_tokens"] > cfg["hard_input_tokens_per_stock"] or
                prediction["estimated_total_cost_usd"] > cfg["max_cost_per_stock_usd"]):
            raise ValueError(f"{ticker}: per-stock evidence or budget guard")
        totals["input"] += prediction["estimated_input_tokens"]
        totals["output"] += prediction["estimated_output_tokens"]
        totals["cost"] += prediction["estimated_total_cost_usd"]
        prepared[ticker] = (inputs, sol_body, luna_body, prediction)
        baselines[ticker] = {"research_score": json.loads(baseline_row["result_json"]).get("research_score"),
            "catalyst_score": json.loads(baseline_row["result_json"]).get("catalyst_score"),
            "thesis_confidence": json.loads(baseline_row["result_json"]).get("thesis_confidence"),
            "input_tokens": baseline_row["input_tokens"], "output_tokens": baseline_row["output_tokens"],
            "total_tokens": (baseline_row["input_tokens"] or 0) + (baseline_row["output_tokens"] or 0),
            "local_cost_usd": baseline_row["local_cost_usd"],
            "result": json.loads(baseline_row["result_json"])}
        event_rows = db.execute("""SELECT event_json FROM evidence_events_v2
            WHERE ticker=? AND evidence_asof=?""", (ticker, inputs["evidence_asof"])).fetchall()
        parsed = [json.loads(r[0]) for r in event_rows]
        events[ticker] = {e["duplicate_group"]: e.get("event_type", "UNKNOWN") for e in parsed}
    if totals["input"] > MAX_INPUT or totals["output"] > MAX_OUTPUT or totals["cost"] > MAX_COST:
        raise ValueError("ONE_TIME_AB_BUDGET_BLOCKED")
    return cfg, prepared, baselines, events, totals


def used(db):
    rows = db.execute("""SELECT status, input_tokens, output_tokens, local_cost, estimated_input,
        estimated_output, estimated_cost FROM phase4_ab_attempts WHERE run_id=?""", (PILOT_ID,)).fetchall()
    pending = [r for r in rows if r[0] != "FAILED_NO_REQUEST"]
    return {"calls": len(pending),
            "input": sum(r[1] if r[1] is not None else r[4] for r in pending),
            "output": sum(r[2] if r[2] is not None else r[5] for r in pending),
            "cost": round(sum(r[3] if r[3] is not None else r[6] for r in pending), 8)}


def run(db, key):
    cfg, prepared, baselines, events, forecast = prepare(db)
    if not key:
        raise ValueError("API_KEY_MISSING")
    with db:
        db.execute("""INSERT INTO phase4_ab_runs VALUES(?,?,?,?,?,?,?,?,?,NULL)""",
                   (PILOT_ID, MODEL, "ACTIVE", json.dumps(TICKERS), MAX_CALLS,
                    MAX_INPUT, MAX_OUTPUT, MAX_COST, now()))
        for ticker in TICKERS:
            inputs, sol_body, luna_body, pred = prepared[ticker]
            db.execute("""INSERT INTO phase4_ab_cases VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (PILOT_ID, ticker, SOL_RUNS[ticker], inputs["evidence_asof"],
                 inputs["fundamental_run_id"], inputs["quant_run_id"],
                 digest(inputs["prompt"]["phase4a_evidence"]),
                 digest({k:v for k,v in sol_body.items() if k!="model"}),
                 compact(sol_body), compact(luna_body), pred["estimated_input_tokens"],
                 pred["estimated_output_tokens"], pred["estimated_total_cost_usd"],
                 compact(baselines[ticker]["result"])))
    report = {"status": "IN_PROGRESS", "pilot_id": PILOT_ID, "model": MODEL,
        "web_search": "OFF", "one_time_allowance": {"calls": MAX_CALLS,
        "input_tokens": MAX_INPUT, "output_tokens": MAX_OUTPUT, "cost_usd": MAX_COST},
        "estimated_total": forecast, "results": []}
    try:
        for ticker in TICKERS:
            inputs, _, body, prediction = prepared[ticker]
            current = used(db)
            if (current["calls"] >= MAX_CALLS or current["input"] + prediction["estimated_input_tokens"] > MAX_INPUT
                    or current["output"] + prediction["estimated_output_tokens"] > MAX_OUTPUT
                    or current["cost"] + prediction["estimated_total_cost_usd"] > MAX_COST):
                report["results"].append({"ticker": ticker, "research_status": "ONE_TIME_AB_BUDGET_BLOCKED"})
                continue
            started = time.monotonic()
            with db:
                db.execute("""INSERT INTO phase4_ab_attempts(run_id,ticker,status,request_started_at,
                    estimated_input,estimated_output,estimated_cost) VALUES(?,?,?,?,?,?,?)""",
                    (PILOT_ID, ticker, "IN_FLIGHT", now(), prediction["estimated_input_tokens"],
                     prediction["estimated_output_tokens"], prediction["estimated_total_cost_usd"]))
            result = {"ticker": ticker, "research_status": "FAILED_UNKNOWN"}
            try:
                raw, metadata = api_research(body, key)
                # First durable write after the API response is the complete raw response.
                with db:
                    db.execute("""UPDATE phase4_ab_attempts SET raw_response_json=?,raw_response_saved=1,
                        returned_model=?,request_id=? WHERE run_id=? AND ticker=?""",
                        (compact(raw), raw.get("model"), metadata.get("request_id"), PILOT_ID, ticker))
                usage = raw.get("usage") or {}
                input_tokens, output_tokens = usage.get("input_tokens"), usage.get("output_tokens")
                local_cost = (round(model_cost({**cfg, **RATES}, input_tokens, output_tokens), 8)
                              if input_tokens is not None and output_tokens is not None else None)
                with db:
                    db.execute("UPDATE phase4_ab_attempts SET input_tokens=?,output_tokens=?,local_cost=? WHERE run_id=? AND ticker=?",
                               (input_tokens, output_tokens, local_cost, PILOT_ID, ticker))
                parsed = validate_response(raw, inputs["allowed_evidence_ids"], inputs["candidate_score"], cfg)
                result = {**parsed, "ticker": ticker, "research_status": parsed["research_status"],
                    "evidence_tokens": inputs["evidence_packet_tokens_estimate"],
                    "input_tokens": input_tokens, "output_tokens": output_tokens,
                    "total_tokens": input_tokens + output_tokens if input_tokens is not None and output_tokens is not None else None,
                    "runtime_seconds": round(time.monotonic() - started, 2),
                    "local_cost_usd": local_cost, "estimated_cost_usd": prediction["estimated_total_cost_usd"],
                    "web_search_calls": 0,
                    "new_information_vs_phase2_3": parsed.get("claims", {}).get("NEW_INFORMATION", []),
                    "evidence_ids": sorted({eid for items in parsed.get("claims", {}).values()
                                             for claim in items for eid in claim.get("evidence_ids", [])}),
                    "unsupported_uncertain_claims": parsed.get("evidence_limitations", [])}
            except urllib.error.HTTPError as exc:
                category, state = classify_network_error(exc, True)
                result = {"ticker": ticker, "research_status": state, "network_error": category}
                exc.close()
            except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
                category, state = classify_network_error(exc)
                result = {"ticker": ticker, "research_status": state, "network_error": category}
            except Exception as exc:
                result = {"ticker": ticker, "research_status": "PARSE_FAILED",
                          "error_class": type(exc).__name__}
            evaluator = evaluate(ticker, result, events[ticker], inputs["allowed_evidence_ids"])
            with db:
                db.execute("""UPDATE phase4_ab_attempts SET status=?,runtime_seconds=?,result_json=?,
                    evaluator_json=?,error_category=?,finished_at=? WHERE run_id=? AND ticker=?""",
                    (result["research_status"], result.get("runtime_seconds"), compact(result),
                     compact(evaluator), result.get("network_error"), now(), PILOT_ID, ticker))
            result["evaluator"] = evaluator
            report["results"].append(result)
            report["usage"] = used(db)
            write_report(report)
        successful = {r["ticker"]: r["evaluator"] for r in report["results"]
                      if r.get("research_status") == "COMPLETE" and r.get("evaluator")}
        baseline_eval = {t: evaluate(t, baselines[t]["result"], events[t],
                           prepared[t][0]["allowed_evidence_ids"]) for t in TICKERS}
        report["sol_baseline_evaluators"] = baseline_eval
        report["average_luna_evaluator"] = final_decision(successful) if len(successful) == len(TICKERS) else None
        report["cost_reduction_pct"] = {
            r["ticker"]: round(100 * (baselines[r["ticker"]]["local_cost_usd"] - r["local_cost_usd"]) /
                               baselines[r["ticker"]]["local_cost_usd"], 2)
            for r in report["results"] if r.get("research_status") == "COMPLETE" and r.get("local_cost_usd") is not None}
        report["recommendation"] = (report["average_luna_evaluator"]["recommendation"]
            if report["average_luna_evaluator"] else "LUNA_INITIAL_CANDIDATE_FAIL")
        report["status"] = "COMPLETE" if len(successful) == 3 else "PARTIAL"
        return report
    finally:
        with db:
            db.execute("UPDATE phase4_ab_runs SET status='CLOSED',closed_at=? WHERE id=?", (now(), PILOT_ID))
        report["status"] = "CLOSED" if report.get("status") != "COMPLETE" else "COMPLETE_CLOSED"
        report["usage"] = used(db)
        write_report(report)


def write_report(report):
    (ROOT / "data" / "phase4_ab_luna_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", help="execute the one-time three-call A/B test")
    args = parser.parse_args()
    db_path = ROOT / "data" / "research_data.db"
    with sqlite3.connect(f"file:{db_path}?mode={'rw' if args.execute else 'ro'}", uri=True) as db:
        db.row_factory = sqlite3.Row
        if args.execute:
            db.executescript(SCHEMA)
            report = run(db, load_key(ROOT / ".env"))
            print(json.dumps({"status": report["status"], "usage": report["usage"],
                "recommendation": report.get("recommendation"),
                "results": [{"ticker": r["ticker"], "status": r["research_status"]}
                            for r in report["results"]]}, indent=2))
        else:
            cfg, prepared, baselines, _, total = prepare(db)
            print(json.dumps({"status": "OFFLINE_PREFLIGHT", "model": MODEL, "web_search": "OFF",
                "tickers": {t: {"sol_run_id": SOL_RUNS[t], "evidence_asof": v[0]["evidence_asof"],
                    "evidence_tokens": v[0]["evidence_packet_tokens_estimate"],
                    "request_hash": digest({k:x for k,x in v[1].items() if k!="model"}),
                    "sol_research_score": baselines[t]["research_score"],
                    "sol_catalyst_score": baselines[t]["catalyst_score"]} for t,v in prepared.items()},
                "one_time_budget": {"calls": MAX_CALLS, "input_tokens": MAX_INPUT,
                    "output_tokens": MAX_OUTPUT, "cost_usd": MAX_COST},
                "estimated_total": total, "api_calls_this_run": 0}, indent=2))


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""One-off CDNA/AMD Phase 4B continuation. Default is offline preflight."""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import time
import urllib.error
import zlib
from datetime import datetime, timezone

from phase4_budget import (budget_report, classify_network_error, mark_stage, migrate_legacy,
                           now, record_budget_block, transition)
from phase4_engine import compact
from phase4_run import BudgetBlocked, api_research, load_key, model_cost, reserve_tokens, token_breakdown
from phase4b_evidence import cached_inputs, estimate, request_body, validate_response
from quant_screen import ROOT

DEFERRED_TICKERS = ("CDNA", "AMD")
NOT_BEFORE_UTC = datetime(2026, 9, 28, 0, 15, tzinfo=timezone.utc)


def save_outcome(db, run_id, ticker, inputs, result, usage, cost, elapsed):
    with db:
        db.execute("""INSERT INTO evidence_reasoner_results
            (run_id,ticker,evidence_asof,research_status,candidate_score,research_score,
             catalyst_score,conviction_score,thesis_confidence,result_json,input_tokens,
             output_tokens,estimated_cost_usd,local_cost_usd,runtime_seconds,created_at)
             VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (run_id, ticker, inputs["evidence_asof"], result["research_status"],
             inputs["candidate_score"], result.get("research_score"), result.get("catalyst_score"),
             result.get("conviction_score"), result.get("thesis_confidence"), compact(result),
             usage.get("input_tokens"), usage.get("output_tokens"),
             usage.get("estimated_cost_usd"), cost, elapsed, now()))


def create_run(db, inputs, cfg):
    first = next(iter(inputs.values()))
    if any((value["fundamental_run_id"], value["quant_run_id"], value["candidate_scan_date"]) !=
           (first["fundamental_run_id"], first["quant_run_id"], first["candidate_scan_date"])
           for value in inputs.values()):
        raise ValueError("CACHE_INTEGRITY_FAILED: mixed Phase 2/3 snapshots")
    with db:
        cur = db.execute("""INSERT INTO research_runs(fundamental_run_id,quant_run_id,
            candidate_scan_date,research_cutoff,created_at,config_json,mode,status)
            VALUES(?,?,?,?,?,?,?,?)""",
            (first["fundamental_run_id"], first["quant_run_id"], first["candidate_scan_date"],
             now(), now(), compact(cfg), "EVIDENCE_ONLY_PILOT", "IN_PROGRESS"))
    return cur.lastrowid


def run_one(db, run_id, ticker, inputs, cfg, key):
    started = time.monotonic()
    body = request_body(inputs, cfg)
    prediction = estimate(body, cfg)
    day = datetime.now(timezone.utc).date().isoformat()
    basic = {"ticker": ticker, "evidence_packet_tokens": inputs["evidence_packet_tokens_estimate"],
             "original_evidence_packet_tokens": inputs["evidence_packet_original_tokens_estimate"],
             "evidence_policy": inputs["evidence_policy"],
             "estimated_input_tokens": prediction["estimated_input_tokens"],
             "estimated_cost_usd": prediction["estimated_total_cost_usd"],
             "web_search_calls": 0, "candidate_score": inputs["candidate_score"]}
    if inputs["evidence_policy"]["status"] == "EVIDENCE_PACKET_OVERSIZE":
        result = {**basic, "research_status": "EVIDENCE_PACKET_OVERSIZE",
                  "reason": "protected evidence exceeds hard packet limit; manual review required",
                  "runtime_seconds": round(time.monotonic() - started, 2)}
        save_outcome(db, run_id, ticker, inputs, result,
                     {"estimated_cost_usd": prediction["estimated_total_cost_usd"]},
                     None, result["runtime_seconds"])
        return result
    try:
        ledger_id = reserve_tokens(db, cfg, day, run_id, ticker, prediction)
    except BudgetBlocked as exc:
        record_budget_block(db, day, run_id, ticker, str(exc))
        result = {**basic, "research_status": "BUDGET_BLOCKED", "reason": str(exc),
                  "runtime_seconds": round(time.monotonic() - started, 2)}
        save_outcome(db, run_id, ticker, inputs, result, {"estimated_cost_usd": prediction["estimated_total_cost_usd"]},
                     None, result["runtime_seconds"])
        return result
    transition(db, ledger_id, "IN_FLIGHT", request_started_at=now())
    raw = None
    actual_in = actual_out = None
    result = None
    error_category = None
    state = None
    try:
        raw, metadata = api_research(body, key)
        if metadata:
            mark_stage(db, ledger_id, **metadata)
        # This transaction must commit before usage parsing or research parsing.
        with db:
            db.execute("INSERT INTO research_response_archive VALUES(?,?,?,?)",
                       (run_id, ticker, compact(raw), now()))
        mark_stage(db, ledger_id, raw_response_saved=1)
        usage = raw.get("usage") or {}
        actual_in, actual_out = usage.get("input_tokens"), usage.get("output_tokens")
        if actual_in is not None and actual_out is not None:
            mark_stage(db, ledger_id, usage_received=1)
        try:
            result = validate_response(raw, inputs["allowed_evidence_ids"], inputs["candidate_score"], cfg)
        except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
            result = {"research_status": "PARSE_FAILED", "reason": type(exc).__name__,
                      "candidate_score": inputs["candidate_score"]}
        state = "COMPLETED"
    except urllib.error.HTTPError as exc:
        error_category, state = classify_network_error(exc, True)
        mark_stage(db, ledger_id, response_started=1, connection_established=1,
                   request_id=exc.headers.get("x-request-id") if exc.headers else None)
        exc.close()
    except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
        metadata = getattr(exc, "request_metadata", {})
        if metadata:
            mark_stage(db, ledger_id, **metadata)
        error_category, state = classify_network_error(exc, metadata.get("response_started"))
    if result is None:
        result = {"research_status": state or "FAILED_UNKNOWN", "reason": error_category or "UNKNOWN_NETWORK_ERROR",
                  "candidate_score": inputs["candidate_score"]}
    transition(db, ledger_id, state or "FAILED_UNKNOWN", error_category=error_category)
    web_calls = sum(item.get("type") == "web_search_call" for item in raw.get("output", [])) if raw else None
    flags = []
    if actual_in is not None:
        if actual_in > cfg["target_input_tokens_per_stock"] or actual_in > prediction["estimated_input_tokens"]:
            flags.append("TOKEN_BUDGET_OVERRUN")
        if actual_in > cfg["hard_input_tokens_per_stock"]:
            flags.append("HARD_INPUT_OVERRUN")
    if web_calls:
        flags.append("UNEXPECTED_WEB_SEARCH")
    if raw and (actual_in is None or actual_out is None):
        flags.append("USAGE_UNAVAILABLE")
    local_cost = (round(model_cost(cfg, actual_in, actual_out) +
                        web_calls * cfg["web_search_usd_per_call"], 6)
                  if actual_in is not None and actual_out is not None and web_calls is not None else None)
    if local_cost is not None and local_cost > cfg["max_cost_per_stock_usd"]:
        flags.append("COST_OVERRUN")
    breakdown = token_breakdown(prediction, actual_in, raw) if raw else {"pre_call_estimate": prediction}
    with db:
        db.execute("UPDATE research_token_ledger SET actual_input=?,actual_output=?,status=? WHERE id=?",
                   (actual_in, actual_out, result["research_status"], ledger_id))
        db.execute("""UPDATE research_usage_details SET actual_model_cost_usd=?,actual_search_cost_usd=?,
            actual_total_cost_usd=?,web_search_calls=?,token_breakdown_json=?,flags_json=? WHERE ledger_id=?""",
            (round(model_cost(cfg, actual_in, actual_out), 6) if local_cost is not None else None,
             round(web_calls * cfg["web_search_usd_per_call"], 6) if web_calls is not None else None,
             local_cost, web_calls, compact(breakdown), compact(flags), ledger_id))
    if budget_report(db, day, cfg)["budget_used_pct"] > 100:
        flags.append("DAILY_BUDGET_OVERRUN")
        with db:
            db.execute("UPDATE research_usage_details SET flags_json=? WHERE ledger_id=?",
                       (compact(flags), ledger_id))
    elapsed = round(time.monotonic() - started, 2)
    result.update({**basic, "input_tokens": actual_in, "output_tokens": actual_out,
                   "total_tokens": actual_in + actual_out if actual_in is not None and actual_out is not None else None,
                   "web_search_calls": web_calls, "local_cost_usd": local_cost,
                   "token_budget_overrun": "TOKEN_BUDGET_OVERRUN" in flags,
                   "flags": flags, "runtime_seconds": elapsed})
    save_outcome(db, run_id, ticker, inputs, result,
                 {"input_tokens": actual_in, "output_tokens": actual_out,
                  "estimated_cost_usd": prediction["estimated_total_cost_usd"]}, local_cost, elapsed)
    return result


def write_report(report):
    path = ROOT / "data" / "phase4b_cdna_amd_report.json"
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    os.replace(temporary, path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", help="explicit one-off paid pilot; still obeys all budgets")
    args = parser.parse_args()
    cfg = json.loads((ROOT / "phase4_config.json").read_text())
    if cfg["paid_ai_enabled"] or cfg["web_search_escalation_enabled"]:
        raise SystemExit("SAFETY_BLOCK: persistent paid AI and web escalation must remain disabled")
    if args.execute and datetime.now(timezone.utc) < NOT_BEFORE_UTC:
        raise SystemExit("WAIT_FOR_NEW_DAILY_WINDOW: no AI call made")
    key = load_key(ROOT / ".env") if args.execute else None
    if args.execute and not key:
        raise SystemExit("API_KEY_MISSING: no paid request made")
    with sqlite3.connect(ROOT / "data" / "research_data.db") as db:
        db.row_factory = sqlite3.Row
        db.executescript((ROOT / "phase4_schema.sql").read_text())
        migrate_legacy(db)
        inputs, input_errors = {}, {}
        for ticker in DEFERRED_TICKERS:
            try:
                inputs[ticker] = cached_inputs(db, ticker, cfg)
            except (ValueError, KeyError, sqlite3.Error, zlib.error) as exc:
                input_errors[ticker] = f"CACHE_INTEGRITY_FAILED: {type(exc).__name__}"
        if not inputs:
            raise SystemExit("CACHE_INTEGRITY_FAILED: no verified evidence; no API call made")
        estimates = {ticker: estimate(request_body(value, cfg), cfg) for ticker, value in inputs.items()}
        if not args.execute:
            report = {"status": "OFFLINE_PREFLIGHT", "tickers": list(DEFERRED_TICKERS),
                      "estimates": estimates, "cache_errors": input_errors,
                      "evidence_policies": {ticker: value["evidence_policy"] for ticker, value in inputs.items()},
                      "not_before_utc": NOT_BEFORE_UTC.isoformat(),
                      "daily_budget": budget_report(db, datetime.now(timezone.utc).date().isoformat(), cfg),
                      "ai_api_calls_this_run": 0}
            print(json.dumps(report, indent=2, sort_keys=True))
            return
        run_id = create_run(db, inputs, cfg)
        report = {"status": "IN_PROGRESS", "run_id": run_id, "tickers": list(DEFERRED_TICKERS),
                  "results": [], "ai_api_calls_this_run": 0}
        write_report(report)
        for ticker in DEFERRED_TICKERS:
            previous = db.execute("""SELECT research_status FROM evidence_reasoner_results
                WHERE ticker=? AND research_status IN
                ('COMPLETE','INSUFFICIENT_EVIDENCE','PARSE_FAILED','FAILED_UNKNOWN')
                ORDER BY run_id DESC LIMIT 1""", (ticker,)).fetchone()
            if previous:
                result = {"ticker": ticker, "research_status": "ALREADY_ATTEMPTED",
                          "reason": previous[0]}
            elif ticker in inputs:
                result = run_one(db, run_id, ticker, inputs[ticker], cfg, key)
            else:
                result = {"ticker": ticker, "research_status": "CACHE_INTEGRITY_FAILED",
                          "reason": input_errors[ticker]}
            report["results"].append(result)
            report["ai_api_calls_this_run"] = sum(r["research_status"] not in
                {"BUDGET_BLOCKED", "FAILED_NO_REQUEST", "CACHE_INTEGRITY_FAILED",
                 "EVIDENCE_PACKET_OVERSIZE", "ALREADY_ATTEMPTED"} for r in report["results"])
            report["successful_calls"] = sum(r["research_status"] in
                {"COMPLETE", "INSUFFICIENT_EVIDENCE", "PARSE_FAILED"} for r in report["results"])
            report["failed_calls"] = report["ai_api_calls_this_run"] - report["successful_calls"]
            report["total_input_tokens"] = sum(r.get("input_tokens") or 0 for r in report["results"])
            report["total_output_tokens"] = sum(r.get("output_tokens") or 0 for r in report["results"])
            report["total_local_cost_usd"] = round(sum(r.get("local_cost_usd") or 0
                                                       for r in report["results"]), 6)
            report["daily_budget"] = budget_report(db, datetime.now(timezone.utc).date().isoformat(), cfg)
            write_report(report)
        report["status"] = "COMPLETE" if all(r["research_status"] == "COMPLETE" for r in report["results"]) else "PARTIAL"
        with db:
            db.execute("UPDATE research_runs SET status=? WHERE id=?", (report["status"], run_id))
        write_report(report)
        print(json.dumps({"status": report["status"], "run_id": run_id,
                          "results": [{"ticker": r["ticker"], "status": r["research_status"]}
                                      for r in report["results"]],
                          "ai_api_calls_this_run": report["ai_api_calls_this_run"]}, indent=2))


if __name__ == "__main__":
    main()

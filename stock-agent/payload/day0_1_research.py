#!/usr/bin/env python3
"""One-time Day 0.1 Initial Research from the existing Top 15 evidence cache."""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import time
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

from phase4_budget import classify_network_error
from phase4_engine import compact
from phase4_run import api_research, load_key
from phase4_tiers import escalation, research_cache_decision
from phase4b_evidence import cached_inputs, estimate, request_body, validate_response
from quant_screen import ROOT

DB = ROOT / "data" / "research_data.db"
PLAN = ROOT / "data" / "day0_1_research_batch_plan.json"
REPORT = ROOT / "data" / "day0_1_research_report.json"
RUN = "DAY0_1_20260925_R9"
MODEL = "gpt-5.6-luna"
MAX_COST = 0.10
INPUT_RATE = 0.20
OUTPUT_RATE = 1.20
MIN_COVERAGE = 40
RISK_TYPES = {"RISK_FACTORS", "LIQUIDITY", "DEBT", "LEGAL_PROCEEDINGS",
              "MATERIAL_COMMITMENT", "SHARE_ISSUANCE", "SHARE_OFFERING",
              "FINANCING", "RESTATEMENT", "AUDITOR_CHANGE", "ACCOUNTING_ISSUE",
              "REGULATORY_EVENT", "FRAUD_INVESTIGATION", "ACCOUNTING_INVESTIGATION"}
SCHEMA = """
CREATE TABLE IF NOT EXISTS day01_runs(
 id TEXT PRIMARY KEY, source_research_run INTEGER NOT NULL, model TEXT NOT NULL,
 status TEXT NOT NULL, max_calls INTEGER NOT NULL, max_cost_usd REAL NOT NULL,
 plan_hash TEXT NOT NULL, started_at TEXT NOT NULL, closed_at TEXT);
CREATE TABLE IF NOT EXISTS day01_attempts(
 run_id TEXT NOT NULL, ticker TEXT NOT NULL, state TEXT NOT NULL,
 request_started_at TEXT NOT NULL, estimated_input INTEGER NOT NULL,
 estimated_output INTEGER NOT NULL, estimated_cost REAL NOT NULL,
 requested_model TEXT NOT NULL, returned_model TEXT, request_id TEXT,
 raw_response_json TEXT, raw_response_saved INTEGER NOT NULL DEFAULT 0,
 input_tokens INTEGER, output_tokens INTEGER, local_cost_usd REAL,
 parsed_result_json TEXT, escalation_json TEXT, error_class TEXT,
 finished_at TEXT, PRIMARY KEY(run_id,ticker));
CREATE TABLE IF NOT EXISTS day01_states(
 run_id TEXT NOT NULL, ticker TEXT NOT NULL, cache_status TEXT NOT NULL,
 reason TEXT NOT NULL, last_research_at TEXT, evidence_as_of TEXT,
 candidate_score_at_research REAL, current_candidate_score REAL,
 material_change_detected INTEGER NOT NULL, coverage_pct REAL,
 evidence_confidence TEXT NOT NULL, evidence_tokens INTEGER,
 research_status TEXT NOT NULL, final_status TEXT NOT NULL,
 PRIMARY KEY(run_id,ticker));
"""


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(value):
    return hashlib.sha256(compact(value).encode()).hexdigest()


def configuration():
    cfg = json.loads((ROOT / "phase4_config.json").read_text())
    tiers = json.loads((ROOT / "phase4_tiers_config.json").read_text())
    if (cfg["paid_ai_enabled"] or cfg["web_search_escalation_enabled"] or
            tiers["initial_research_model"] != MODEL or tiers["deep_research_model"] != "gpt-6-sol" or
            tiers["web_search_enabled"] or tiers["architecture_status"] != "FROZEN_V1"):
        raise ValueError("FROZEN_CONFIG_SAFETY_BLOCK")
    cfg.update(model=MODEL, model_input_usd_per_million=INPUT_RATE,
               model_output_usd_per_million=OUTPUT_RATE)
    return cfg, tiers


def previous_research(db, ticker):
    return db.execute("""SELECT run_id,evidence_asof,research_status,candidate_score,
        research_score,catalyst_score,conviction_score,thesis_confidence,created_at
        FROM evidence_reasoner_results WHERE ticker=? AND research_status='COMPLETE'
        ORDER BY created_at DESC LIMIT 1""", (ticker,)).fetchone()


def preflight(db):
    cfg, tiers = configuration()
    run = db.execute("SELECT id,candidate_scan_date FROM research_runs ORDER BY id DESC LIMIT 1").fetchone()
    if not run or run["id"] != 9 or run["candidate_scan_date"] != "2026-09-25":
        raise ValueError("SOURCE_RUN_CHANGED: no new full-market work authorized")
    tickers = [r[0] for r in db.execute("""SELECT ticker FROM research_priority
        WHERE run_id=? AND priority_rank<=15 ORDER BY priority_rank""", (run["id"],))]
    if len(tickers) != 15 or len(set(tickers)) != 15:
        raise ValueError("TOP15_INCOMPLETE")
    cases, total_input, total_output, total_cost = [], 0, 0, 0.0
    for ticker in tickers:
        try:
            inputs = cached_inputs(db, ticker, cfg, allowed_tickers=tickers)
            evidence = inputs["prompt"]["phase4a_evidence"]
            coverage = evidence["coverage_pct"]
            sec_ok = evidence["coverage"].get("SEC_CONTENT") == "AVAILABLE"
            events = evidence["recent_material_events"] + evidence["other_events"]
            official = any(any(s.get("source_type", "").startswith("SEC") for s in e["sources"])
                           for e in events)
            if inputs["evidence_policy"]["status"] == "EVIDENCE_PACKET_OVERSIZE":
                reason, status = "Protected evidence exceeds hard packet limit", "INSUFFICIENT_EVIDENCE"
            elif coverage < MIN_COVERAGE or not sec_ok or not official:
                reason, status = "Insufficient verified SEC coverage", "INSUFFICIENT_EVIDENCE"
            else:
                reason, status = None, None
        except (ValueError, KeyError, sqlite3.Error) as exc:
            cases.append({"ticker": ticker, "cache_status": "UNAVAILABLE",
                          "reason": f"CACHE_INTEGRITY_FAILED:{type(exc).__name__}",
                          "last_research_at": None, "evidence_as_of": None,
                          "candidate_score_at_research": None, "current_candidate_score": None,
                          "material_change_detected": False, "evidence_coverage": None,
                          "evidence_confidence": "UNKNOWN", "evidence_tokens": None})
            continue
        prior = previous_research(db, ticker)
        old_hash = None
        if prior:
            old = db.execute("""SELECT source_hash FROM evidence_packets_v2
                WHERE ticker=? AND evidence_asof=? ORDER BY created_at DESC LIMIT 1""",
                (ticker, prior["evidence_asof"])).fetchone()
            old_hash = old[0] if old else None
        current_hash = db.execute("""SELECT source_hash FROM evidence_packets_v2
            WHERE ticker=? AND evidence_asof=? ORDER BY created_at DESC LIMIT 1""",
            (ticker, inputs["evidence_asof"])).fetchone()[0]
        delta = (inputs["candidate_score"] - prior["candidate_score"]) if prior else None
        material_change = bool(prior and (old_hash != current_hash or
                                           abs(delta) >= tiers["candidate_score_refresh_threshold"]))
        policy = research_cache_decision(
            {"status": "COMPLETE"} if prior else None,
            material_new_evidence=bool(prior and old_hash != current_hash),
            candidate_score_change=delta,
            threshold=tiers["candidate_score_refresh_threshold"])
        if status:
            cache_status = status
        elif policy == "CACHED_RESEARCH":
            cache_status, reason = "VALID_CACHE", "Completed research; same evidence hash and small Candidate change"
        elif prior:
            cache_status, reason = "INITIAL_REFRESH_REQUIRED", "Material evidence or Candidate change"
        else:
            cache_status, reason = "FULL_RESEARCH_REQUIRED", "No complete prior research"
        case = {"ticker": ticker, "cache_status": cache_status, "reason": reason,
                "last_research_at": prior["created_at"] if prior else None,
                "evidence_as_of": inputs["evidence_asof"],
                "candidate_score_at_research": prior["candidate_score"] if prior else None,
                "current_candidate_score": inputs["candidate_score"],
                "material_change_detected": material_change,
                "evidence_coverage": coverage, "evidence_confidence": evidence["confidence"],
                "evidence_tokens": inputs["evidence_packet_tokens_estimate"],
                "evidence_hash": current_hash, "input_hash": digest(inputs["prompt"]),
                "ir_unavailable": evidence["coverage"].get("IR") == "UNAVAILABLE"}
        if cache_status in {"INITIAL_REFRESH_REQUIRED", "FULL_RESEARCH_REQUIRED"}:
            body = request_body(inputs, cfg)
            estimate_result = estimate(body, cfg)
            if body.get("tools") or body["model"] != MODEL:
                raise ValueError("MODEL_OR_WEB_SEARCH_SAFETY_BLOCK")
            case.update({"estimated_input": estimate_result["estimated_input_tokens"],
                         "estimated_output": estimate_result["estimated_output_tokens"],
                         "estimated_cost": estimate_result["estimated_total_cost_usd"]})
            total_input += case["estimated_input"]
            total_output += case["estimated_output"]
            total_cost += case["estimated_cost"]
        cases.append(case)
    calls = sum(c["cache_status"] in {"INITIAL_REFRESH_REQUIRED", "FULL_RESEARCH_REQUIRED"} for c in cases)
    plan = {"run_id": RUN, "source_research_run": run["id"], "model": MODEL,
            "web_search": "OFF", "max_calls": calls, "max_total_cost_usd": MAX_COST,
            "estimated_calls": calls, "estimated_input_tokens": total_input,
            "estimated_output_tokens": total_output,
            "estimated_total_cost_usd": round(total_cost, 6), "cases": cases}
    plan["plan_hash"] = digest(plan)
    return plan


def evidence_for_escalation(inputs):
    packet = inputs["prompt"]["phase4a_evidence"]
    events = packet["recent_material_events"] + packet["other_events"]
    material = [e["duplicate_group"] for e in events if e["materiality"] in {"MATERIAL", "HIGH"}]
    risks = [e["duplicate_group"] for e in events if e["materiality"] in {"MATERIAL", "HIGH"}
             and e["event_type"] in RISK_TYPES]
    return {"confidence": packet["confidence"], "events": events,
            "material_event_ids": material, "high_risk_event_ids": risks,
            "material_event_count": len(material), "material_catalyst": bool(material),
            "material_risk": bool(risks)}


def cost(input_tokens, output_tokens, details=None):
    details = details or {}
    writes = details.get("cache_write_tokens") or 0
    cached = details.get("cached_tokens") or 0
    if writes + cached > input_tokens:
        raise ValueError("INVALID_PROVIDER_USAGE")
    return round(((input_tokens - writes - cached) * INPUT_RATE +
                  writes * INPUT_RATE * 1.25 + cached * INPUT_RATE * 0.10 +
                  output_tokens * OUTPUT_RATE) / 1_000_000, 8)


def result_report(db, plan):
    rows = {r["ticker"]: dict(r) for r in db.execute(
        "SELECT * FROM day01_attempts WHERE run_id=?", (RUN,))}
    cases = []
    for case in plan["cases"]:
        row = rows.get(case["ticker"])
        status = "FINAL_RESEARCH_AVAILABLE" if case["cache_status"] == "VALID_CACHE" else (
            "INSUFFICIENT_EVIDENCE" if case["cache_status"] == "INSUFFICIENT_EVIDENCE" else
            "RESEARCH_FAILED" if case["cache_status"] == "UNAVAILABLE" else
            "RESEARCH_FAILED" if row and row["state"] not in {"COMPLETE", "INSUFFICIENT_EVIDENCE"} else
            "DEEP_RESEARCH_PENDING" if row and row["escalation_json"] and
            json.loads(row["escalation_json"])["decision"] == "ESCALATE_SOL" else
            "INSUFFICIENT_EVIDENCE" if row and row["state"] == "INSUFFICIENT_EVIDENCE" else
            "FINAL_RESEARCH_AVAILABLE" if row and row["state"] == "COMPLETE" else "RESEARCH_FAILED")
        cases.append({**case, "research_status": row["state"] if row else
                      ("CACHED_RESEARCH" if status == "FINAL_RESEARCH_AVAILABLE" else status),
                      "final_status": status,
                      "input_tokens": row["input_tokens"] if row else 0,
                      "output_tokens": row["output_tokens"] if row else 0,
                      "local_cost_usd": row["local_cost_usd"] if row else 0,
                      "escalation": json.loads(row["escalation_json"]) if row and row["escalation_json"] else None,
                      "error_class": row["error_class"] if row else None})
    return {"run_id": RUN, "status": "DAY0_1_RESEARCH_COMPLETE" if all(c["final_status"] in
            {"FINAL_RESEARCH_AVAILABLE", "DEEP_RESEARCH_PENDING", "INSUFFICIENT_EVIDENCE", "RESEARCH_FAILED"}
            for c in cases) else "PARTIAL", "model": MODEL, "web_search": "OFF", "cases": cases,
            "luna_calls": len(rows), "input_tokens": sum(r["input_tokens"] or 0 for r in rows.values()),
            "output_tokens": sum(r["output_tokens"] or 0 for r in rows.values()),
            "local_cost_usd": round(sum(r["local_cost_usd"] or r["estimated_cost"]
                                        for r in rows.values()), 8), "sol_calls": 0}


def execute(db, plan):
    cfg, tiers = configuration()
    if plan["estimated_total_cost_usd"] > MAX_COST or plan["estimated_calls"] > plan["max_calls"]:
        raise ValueError("DAY0_1_RESEARCH_BUDGET_BLOCKED")
    key = load_key(ROOT / ".env")
    if not key:
        raise ValueError("API_KEY_MISSING")
    db.executescript(SCHEMA)
    previous = db.execute("SELECT status FROM day01_runs WHERE id=?", (RUN,)).fetchone()
    if previous:
        raise ValueError("DAY0_1_ALREADY_STARTED: no automatic retry")
    with db:
        db.execute("INSERT INTO day01_runs VALUES(?,?,?,?,?,?,?,?,NULL)",
                   (RUN, plan["source_research_run"], MODEL, "ACTIVE", plan["max_calls"],
                    MAX_COST, plan["plan_hash"], now()))
        for case in plan["cases"]:
            db.execute("""INSERT INTO day01_states VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (RUN, case["ticker"], case["cache_status"], case["reason"],
                 case["last_research_at"], case["evidence_as_of"],
                 case["candidate_score_at_research"], case["current_candidate_score"],
                 int(case["material_change_detected"]), case["evidence_coverage"],
                 case["evidence_confidence"], case["evidence_tokens"],
                 "NOT_RUN", "NOT_RUN"))
    used_cost = 0.0
    try:
        for case in plan["cases"]:
            if case["cache_status"] not in {"INITIAL_REFRESH_REQUIRED", "FULL_RESEARCH_REQUIRED"}:
                continue
            ticker = case["ticker"]
            if used_cost + case["estimated_cost"] > MAX_COST:
                break
            inputs = cached_inputs(db, ticker, cfg,
                                   allowed_tickers=[c["ticker"] for c in plan["cases"]])
            if digest(inputs["prompt"]) != case["input_hash"]:
                raise ValueError(f"{ticker}: FROZEN_INPUT_CHANGED")
            body = request_body(inputs, cfg)
            if (case["estimated_input"] > cfg["hard_input_tokens_per_stock"] or
                    case["estimated_cost"] > cfg["max_cost_per_stock_usd"]):
                raise ValueError(f"{ticker}: PER_STOCK_BUDGET_BLOCKED")
            started = time.monotonic()
            with db:
                db.execute("""INSERT INTO day01_attempts(run_id,ticker,state,request_started_at,
                    estimated_input,estimated_output,estimated_cost,requested_model)
                    VALUES(?,?,?,?,?,?,?,?)""", (RUN, ticker, "IN_FLIGHT", now(),
                    case["estimated_input"], case["estimated_output"], case["estimated_cost"], MODEL))
            state, parsed, esc, error = "FAILED_UNKNOWN", None, None, None
            try:
                raw, metadata = api_research(body, key)
                with db:
                    db.execute("""UPDATE day01_attempts SET raw_response_json=?,raw_response_saved=1,
                        returned_model=?,request_id=? WHERE run_id=? AND ticker=?""",
                        (compact(raw), raw.get("model"), metadata.get("request_id"), RUN, ticker))
                usage = raw.get("usage") or {}
                actual_in, actual_out = usage.get("input_tokens"), usage.get("output_tokens")
                actual_cost = (cost(actual_in, actual_out, usage.get("input_tokens_details"))
                               if actual_in is not None and actual_out is not None else None)
                with db:
                    db.execute("""UPDATE day01_attempts SET input_tokens=?,output_tokens=?,local_cost_usd=?
                        WHERE run_id=? AND ticker=?""", (actual_in, actual_out, actual_cost, RUN, ticker))
                used_cost += actual_cost if actual_cost is not None else case["estimated_cost"]
                parsed = validate_response(raw, inputs["allowed_evidence_ids"], inputs["candidate_score"], cfg)
                state = parsed["research_status"]
                if state == "COMPLETE":
                    initial = {**parsed, "status": "COMPLETE", "confidence": parsed["thesis_confidence"],
                               "handled_evidence_ids": sorted(set(parsed["score_evidence_ids"] +
                                                                   parsed["catalyst_evidence_ids"]))}
                    esc = escalation(inputs["candidate_score"], initial,
                                     evidence_for_escalation(inputs), tiers)
                elif state == "INSUFFICIENT_EVIDENCE":
                    initial = {**parsed, "status": state, "confidence": "LOW"}
                    esc = escalation(inputs["candidate_score"], initial,
                                     evidence_for_escalation(inputs), tiers)
            except urllib.error.HTTPError as exc:
                error, state = classify_network_error(exc, True)
                used_cost += case["estimated_cost"]
                exc.close()
            except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
                error, state = classify_network_error(exc)
                used_cost += case["estimated_cost"]
            except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
                error, state = type(exc).__name__, "PARSE_FAILED"
            with db:
                db.execute("""UPDATE day01_attempts SET state=?,parsed_result_json=?,escalation_json=?,
                    error_class=?,finished_at=? WHERE run_id=? AND ticker=?""",
                    (state, compact(parsed) if parsed else None, compact(esc) if esc else None,
                     error, now(), RUN, ticker))
            print(f"DAY0_1 {ticker} {state} elapsed={time.monotonic()-started:.1f}s", flush=True)
            REPORT.write_text(json.dumps(result_report(db, plan), ensure_ascii=False, indent=2) + "\n")
    finally:
        with db:
            db.execute("UPDATE day01_runs SET status='CLOSED',closed_at=? WHERE id=?", (now(), RUN))
        report = result_report(db, plan)
        with db:
            db.executemany("""UPDATE day01_states SET research_status=?,final_status=?
                WHERE run_id=? AND ticker=?""",
                [(c["research_status"], c["final_status"], RUN, c["ticker"]) for c in report["cases"]])
        REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--reconcile-costs", action="store_true",
                        help="offline recalculation from already saved provider usage")
    parser.add_argument("--reconcile-escalations", action="store_true",
                        help="offline escalation for preserved insufficient Initial responses")
    args = parser.parse_args()
    mode = "rw" if args.execute or args.reconcile_costs or args.reconcile_escalations else "ro"
    with sqlite3.connect(f"file:{DB}?mode={mode}", uri=True) as db:
        db.row_factory = sqlite3.Row
        plan = preflight(db)
        if args.reconcile_escalations:
            run = db.execute("SELECT status FROM day01_runs WHERE id=?", (RUN,)).fetchone()
            if not run or run[0] != "CLOSED":
                raise SystemExit("RECONCILIATION_REQUIRES_CLOSED_RUN")
            _, tiers = configuration()
            tickers = [c["ticker"] for c in plan["cases"]]
            rows = db.execute("""SELECT ticker,parsed_result_json FROM day01_attempts
                WHERE run_id=? AND state='INSUFFICIENT_EVIDENCE'""", (RUN,)).fetchall()
            with db:
                for row in rows:
                    parsed = json.loads(row["parsed_result_json"])
                    inputs = cached_inputs(db, row["ticker"], configuration()[0],
                                           allowed_tickers=tickers)
                    initial = {**parsed, "status": "INSUFFICIENT_EVIDENCE", "confidence": "LOW"}
                    decision = escalation(inputs["candidate_score"], initial,
                                          evidence_for_escalation(inputs), tiers)
                    db.execute("UPDATE day01_attempts SET escalation_json=? WHERE run_id=? AND ticker=?",
                               (compact(decision), RUN, row["ticker"]))
            report = result_report(db, plan)
            with db:
                db.executemany("""UPDATE day01_states SET research_status=?,final_status=?
                    WHERE run_id=? AND ticker=?""",
                    [(c["research_status"], c["final_status"], RUN, c["ticker"])
                     for c in report["cases"]])
            REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
            print(json.dumps({"offline_cases": len(rows), "deep_pending": sum(
                c["final_status"] == "DEEP_RESEARCH_PENDING" for c in report["cases"]),
                "new_api_calls": 0}))
            return
        if args.reconcile_costs:
            run = db.execute("SELECT status FROM day01_runs WHERE id=?", (RUN,)).fetchone()
            if not run or run[0] != "CLOSED":
                raise SystemExit("RECONCILIATION_REQUIRES_CLOSED_RUN")
            rows = db.execute("SELECT ticker,raw_response_json FROM day01_attempts WHERE run_id=?", (RUN,)).fetchall()
            with db:
                for row in rows:
                    if not row["raw_response_json"]:
                        continue
                    usage = json.loads(row["raw_response_json"]).get("usage") or {}
                    if usage.get("input_tokens") is None or usage.get("output_tokens") is None:
                        continue
                    actual = cost(usage["input_tokens"], usage["output_tokens"],
                                  usage.get("input_tokens_details"))
                    db.execute("UPDATE day01_attempts SET local_cost_usd=? WHERE run_id=? AND ticker=?",
                               (actual, RUN, row["ticker"]))
            report = result_report(db, plan)
            REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
            print(json.dumps({"reconciled_calls": len(rows), "local_cost_usd": report["local_cost_usd"]}))
            return
        if not args.execute:
            PLAN.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n")
            print(json.dumps({k: v for k, v in plan.items() if k != "cases"}, indent=2))
            print("CASES", [(c["ticker"], c["cache_status"], c.get("estimated_cost")) for c in plan["cases"]])
            return
        saved = json.loads(PLAN.read_text())
        if saved != plan:
            raise SystemExit("BATCH_PLAN_CHANGED: no API call")
        report = execute(db, plan)
        print(json.dumps({k: v for k, v in report.items() if k != "cases"}, indent=2))


if __name__ == "__main__":
    main()

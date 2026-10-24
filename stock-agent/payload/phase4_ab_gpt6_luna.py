#!/usr/bin/env python3
"""One-time GPT-6 Luna frozen-evidence A/B against stored Sol and GPT-5.6 Luna."""
from __future__ import annotations

import argparse
import json
import sqlite3
import time
from pathlib import Path

from phase4_ab_evaluator import evaluate, final_decision
from phase4_engine import compact
from phase4_run import api_research, load_key
from phase4_tiers import escalation, load_config as load_tier_config
from phase4b_evidence import cached_inputs, estimate, request_body, validate_response
from quant_screen import ROOT

PILOT_ID = "GPT6_LUNA_FROZEN_AB_META_CDNA_AMD_2026_09_27"
MODEL = "gpt-6-luna"
TICKERS = ("META", "CDNA", "AMD")
SOL_RUNS = {"META": 7, "CDNA": 8, "AMD": 8}
OLD_PILOT = "LUNA_FROZEN_AB_META_CDNA_AMD_2026_09_27"
RATES = {"model_input_usd_per_million": .10, "model_output_usd_per_million": .50}
# Cost reservation prices every input token at the official cache-write rate.
MAX_CALLS, MAX_INPUT, MAX_OUTPUT, MAX_COST = 3, 30_000, 4_000, .01
INPUT_LIMIT_PER_CALL = 16_000
REPORT_PATH = ROOT / "data" / "phase4_ab_gpt6_luna_report.json"


def digest(value):
    import hashlib
    return hashlib.sha256(compact(value).encode()).hexdigest()


def precise_cost(usage):
    total_input = usage.get("input_tokens")
    output = usage.get("output_tokens")
    if total_input is None or output is None:
        return None
    details = usage.get("input_tokens_details") or {}
    cached = min(total_input, details.get("cached_tokens", 0) or 0)
    writes = min(total_input - cached, details.get("cache_write_tokens", 0) or 0)
    regular = total_input - cached - writes
    return round((regular * .10 + cached * .01 + writes * .125 + output * .50) / 1_000_000, 8)


def validate_frozen_case(db, ticker, cfg):
    baseline = db.execute("""SELECT evidence_asof,research_status,result_json,candidate_score,
        input_tokens,output_tokens,local_cost_usd FROM evidence_reasoner_results
        WHERE run_id=? AND ticker=?""", (SOL_RUNS[ticker], ticker)).fetchone()
    if not baseline or baseline["research_status"] != "COMPLETE":
        raise ValueError(f"{ticker}: stored GPT-6 Sol baseline unavailable")
    old_case = db.execute("SELECT * FROM phase4_ab_cases WHERE run_id=? AND ticker=?",
                          (OLD_PILOT, ticker)).fetchone()
    if not old_case:
        raise ValueError(f"{ticker}: GPT-5.6 Luna frozen A/B archive unavailable")
    inputs = cached_inputs(db, ticker, cfg)
    sol_body = request_body(inputs, cfg)
    luna_body = {**sol_body, "model": MODEL}
    previous_sol = json.loads(old_case["sol_request_json"])
    previous_luna = json.loads(old_case["luna_request_json"])
    normalized = {k: v for k, v in luna_body.items() if k != "model"}
    if normalized != {k: v for k, v in previous_sol.items() if k != "model"}:
        raise ValueError(f"{ticker}: Phase 4B/Sol frozen request mismatch")
    if normalized != {k: v for k, v in previous_luna.items() if k != "model"}:
        raise ValueError(f"{ticker}: GPT-5.6 Luna frozen request mismatch")
    if inputs["evidence_asof"] != old_case["evidence_asof"] or inputs["evidence_asof"] != baseline["evidence_asof"]:
        raise ValueError(f"{ticker}: evidence cutoff mismatch")
    if (inputs["fundamental_run_id"] != old_case["fundamental_run_id"] or
            inputs["quant_run_id"] != old_case["quant_run_id"]):
        raise ValueError(f"{ticker}: frozen Phase 2/3 run mismatch")
    if luna_body.get("tools") or sol_body.get("tools"):
        raise ValueError("Web Search/tool usage must be OFF")
    prediction = estimate(luna_body, {**cfg, **RATES})
    if prediction["estimated_input_tokens"] > INPUT_LIMIT_PER_CALL:
        raise ValueError(f"{ticker}: per-call input hard limit exceeded")
    return inputs, sol_body, luna_body, prediction


def prepare(db):
    cfg = json.loads((ROOT / "phase4_config.json").read_text())
    if (cfg["model"] != "gpt-6-sol" or cfg["paid_ai_enabled"] or
            cfg["web_search_escalation_enabled"] or cfg["max_daily_cost_usd"] != .35):
        raise ValueError("SAFETY_BLOCK: normal Phase 4 config differs; no calls made")
    if db.execute("SELECT 1 FROM phase4_ab_runs WHERE id=?", (PILOT_ID,)).fetchone():
        raise ValueError("ONE_TIME_GPT6_LUNA_AB_BUDGET already used; no rerun permitted")
    cases = {ticker: validate_frozen_case(db, ticker, cfg) for ticker in TICKERS}
    total_in = sum(row[3]["estimated_input_tokens"] for row in cases.values())
    total_out = sum(row[3]["estimated_output_tokens"] for row in cases.values())
    total_cost = sum((row[3]["estimated_input_tokens"] * .125 +
                      row[3]["estimated_output_tokens"] * .50) / 1_000_000
                     for row in cases.values())
    if (total_in > MAX_INPUT or total_out > MAX_OUTPUT or total_cost > MAX_COST):
        raise ValueError("ONE_TIME_GPT6_LUNA_AB_BUDGET_BLOCKED")
    return cfg, cases, {"estimated_input_tokens": total_in, "max_output_tokens": total_out,
                        "conservative_cost_usd": round(total_cost, 8)}


def write_report(report):
    REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")


def _initial_for_escalation(parsed):
    return {"status": "COMPLETE", "research_status": parsed.get("research_status"),
            "research_score": parsed.get("research_score"), "catalyst_score": parsed.get("catalyst_score"),
            "confidence": parsed.get("thesis_confidence", "UNKNOWN"), "claims": parsed.get("claims", {}),
            "handled_evidence_ids": parsed.get("score_evidence_ids", []) + parsed.get("catalyst_evidence_ids", []),
            "unresolved_conflicts": parsed.get("evidence_conflicts", []),
            "uncertainty": bool(parsed.get("evidence_conflicts") or parsed.get("evidence_limitations"))}


def run(db, key):
    cfg, cases, forecast = prepare(db)
    if not key:
        raise ValueError("API_KEY_MISSING")
    budget = {"calls": MAX_CALLS, "input_tokens": MAX_INPUT,
              "output_tokens": MAX_OUTPUT, "cost_usd": MAX_COST,
              "price_basis": "official standard, conservative cache-write input $0.125/M; output $0.50/M"}
    db.execute("""INSERT INTO phase4_ab_runs VALUES(?,?,?,?,?,?,?,?,?,NULL)""",
               (PILOT_ID, MODEL, "ACTIVE", json.dumps(TICKERS), MAX_CALLS,
                MAX_INPUT, MAX_OUTPUT, MAX_COST, __import__("datetime").datetime.now(
                    __import__("datetime").timezone.utc).isoformat()))
    db.commit()
    report = {"status": "IN_PROGRESS", "pilot_id": PILOT_ID, "requested_model_id": MODEL,
              "web_search": "OFF", "one_time_budget": budget, "forecast": forecast,
              "results": []}
    db.row_factory = sqlite3.Row
    try:
        for ticker in TICKERS:
            inputs, sol_body, body, prediction = cases[ticker]
            old_case = db.execute("SELECT * FROM phase4_ab_cases WHERE run_id=? AND ticker=?",
                                  (OLD_PILOT, ticker)).fetchone()
            sol_baseline = db.execute("""SELECT result_json FROM evidence_reasoner_results
                WHERE run_id=? AND ticker=?""", (SOL_RUNS[ticker], ticker)).fetchone()
            evidence_hash = digest(inputs["prompt"]["phase4a_evidence"])
            with db:
                db.execute("""INSERT INTO phase4_ab_cases VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (PILOT_ID, ticker, SOL_RUNS[ticker], inputs["evidence_asof"],
                     inputs["fundamental_run_id"], inputs["quant_run_id"], evidence_hash,
                     digest({k:v for k,v in sol_body.items() if k!="model"}),
                     compact(sol_body), compact(body), prediction["estimated_input_tokens"],
                     prediction["estimated_output_tokens"],
                     (prediction["estimated_input_tokens"] * .125 + prediction["estimated_output_tokens"] * .50) / 1_000_000,
                     old_case["baseline_json"]))
            with db:
                db.execute("""INSERT INTO phase4_ab_attempts(run_id,ticker,status,request_started_at,
                    estimated_input,estimated_output,estimated_cost) VALUES(?,?,?,?,?,?,?)""",
                    (PILOT_ID, ticker, "IN_FLIGHT", __import__("datetime").datetime.now(
                        __import__("datetime").timezone.utc).isoformat(), prediction["estimated_input_tokens"],
                     prediction["estimated_output_tokens"],
                     (prediction["estimated_input_tokens"] * .125 + prediction["estimated_output_tokens"] * .50) / 1_000_000))
            start = time.monotonic()
            result = {"ticker": ticker, "research_status": "FAILED_UNKNOWN"}
            try:
                raw, metadata = api_research(body, key)
                # Preserve raw response before inspecting, parsing, or evaluating it.
                with db:
                    db.execute("""UPDATE phase4_ab_attempts SET raw_response_json=?,raw_response_saved=1,
                        returned_model=?,request_id=? WHERE run_id=? AND ticker=?""",
                        (compact(raw), raw.get("model"), metadata.get("request_id"), PILOT_ID, ticker))
                usage = raw.get("usage") or {}
                cost = precise_cost(usage)
                with db:
                    db.execute("UPDATE phase4_ab_attempts SET input_tokens=?,output_tokens=?,local_cost=? WHERE run_id=? AND ticker=?",
                               (usage.get("input_tokens"), usage.get("output_tokens"), cost, PILOT_ID, ticker))
                if raw.get("model") != MODEL:
                    raise ValueError("RETURNED_MODEL_ID_MISMATCH")
                parsed = validate_response(raw, inputs["allowed_evidence_ids"], inputs["candidate_score"], cfg)
                runtime = round(time.monotonic() - start, 2)
                result = {**parsed, "ticker": ticker, "requested_model_id": MODEL,
                          "returned_model_id": raw.get("model"), "request_id": metadata.get("request_id"),
                          "evidence_tokens": inputs["evidence_packet_tokens_estimate"],
                          "input_tokens": usage.get("input_tokens"), "output_tokens": usage.get("output_tokens"),
                          "total_tokens": usage.get("total_tokens"), "runtime_seconds": runtime,
                          "actual_cost_usd": cost, "web_search_calls": 0,
                          "evidence_ids": sorted({eid for group in parsed.get("claims", {}).values()
                                                   for claim in group for eid in claim.get("evidence_ids", [])}),
                          "new_information_vs_phase2_3": parsed.get("claims", {}).get("NEW_INFORMATION", []),
                          "evidence_limitations": parsed.get("evidence_limitations", [])}
                event_rows = db.execute("""SELECT event_json FROM evidence_events_v2
                    WHERE ticker=? AND evidence_asof=?""", (ticker, inputs["evidence_asof"])).fetchall()
                events = [json.loads(row[0]) for row in event_rows]
                evaluator = evaluate(ticker, result, {e["duplicate_group"]: e.get("event_type", "UNKNOWN")
                    for e in events}, inputs["allowed_evidence_ids"])
                esc_cfg = load_tier_config(environ={})
                esc_evidence = {"confidence": inputs["prompt"]["phase4a_evidence"].get("confidence", "UNKNOWN"),
                    "material_catalyst": any(e.get("materiality") in {"MATERIAL", "HIGH"} and
                        e.get("event_type") not in {"RISK_FACTORS", "LIQUIDITY", "DEBT", "LEGAL_PROCEEDINGS",
                        "MATERIAL_COMMITMENT", "SHARE_ISSUANCE", "SHARE_OFFERING", "FINANCING",
                        "RESTATEMENT", "AUDITOR_CHANGE", "ACCOUNTING_ISSUE", "REGULATORY_EVENT"} for e in events),
                    "material_risk": any(e.get("materiality") in {"MATERIAL", "HIGH"} and e.get("event_type") in
                        {"RISK_FACTORS", "LIQUIDITY", "DEBT", "LEGAL_PROCEEDINGS", "MATERIAL_COMMITMENT",
                         "SHARE_ISSUANCE", "SHARE_OFFERING", "FINANCING", "RESTATEMENT", "AUDITOR_CHANGE",
                         "ACCOUNTING_ISSUE", "REGULATORY_EVENT"} for e in events),
                    "conflict": bool(parsed.get("evidence_conflicts")),
                    "unresolved_conflict": bool(parsed.get("evidence_conflicts")),
                    "regulatory_risk": any(e.get("event_type") in {"LEGAL_PROCEEDINGS", "REGULATORY_EVENT"} for e in events),
                    "accounting_risk": any(e.get("event_type") in {"RESTATEMENT", "AUDITOR_CHANGE", "ACCOUNTING_ISSUE"} for e in events),
                    "material_event_count": sum(e.get("materiality") in {"MATERIAL", "HIGH"} for e in events),
                    "events": events}
                escalation_result = escalation(inputs["candidate_score"], _initial_for_escalation(parsed),
                                                 esc_evidence, esc_cfg)
                result["evaluator"] = evaluator
                result["escalation"] = escalation_result
            except Exception as exc:
                result = {"ticker": ticker, "research_status": "PARSE_FAILED",
                          "error_class": type(exc).__name__, "error": str(exc),
                          "requested_model_id": MODEL}
            with db:
                db.execute("""UPDATE phase4_ab_attempts SET status=?,runtime_seconds=?,result_json=?,
                    evaluator_json=?,error_category=?,finished_at=? WHERE run_id=? AND ticker=?""",
                    (result["research_status"], result.get("runtime_seconds"), compact(result),
                     compact(result.get("evaluator", {})), result.get("error"),
                     __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
                     PILOT_ID, ticker))
            report["results"].append(result)
            report["usage"] = {"calls": len(report["results"]),
                "input_tokens": sum(r.get("input_tokens", 0) or 0 for r in report["results"]),
                "output_tokens": sum(r.get("output_tokens", 0) or 0 for r in report["results"]),
                "cost_usd": round(sum(r.get("actual_cost_usd", 0) or 0 for r in report["results"]), 8)}
            write_report(report)
        completed = {r["ticker"]: r["evaluator"] for r in report["results"]
                     if r.get("research_status") == "COMPLETE" and r.get("evaluator")}
        report["average_evaluator"] = final_decision(completed) if len(completed) == 3 else None
        report["recommendation"] = (report["average_evaluator"]["recommendation"].replace(
            "LUNA_INITIAL_CANDIDATE", "GPT6_LUNA_INITIAL_CANDIDATE") if report["average_evaluator"]
            else "GPT6_LUNA_INITIAL_CANDIDATE_FAIL")
        report["status"] = "COMPLETE" if len(completed) == 3 else "PARTIAL"
        return report
    finally:
        with db:
            db.execute("UPDATE phase4_ab_runs SET status='CLOSED',closed_at=? WHERE id=?",
                       (__import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(), PILOT_ID))
        db.commit()
        report["status"] = "COMPLETE_CLOSED" if report.get("status") == "COMPLETE" else "CLOSED_PARTIAL"
        write_report(report)


def _payload(raw):
    text = "".join(part.get("text", "") for item in raw.get("output", [])
                   if item.get("type") == "message" for part in item.get("content", [])
                   if part.get("type") == "output_text")
    return json.loads(text)


def _group_claims(claims):
    groups = {}
    for claim in claims or []:
        groups.setdefault(claim.get("kind", "UNKNOWN"), []).append(claim)
    return groups


def _escalation_for(ticker, parsed, candidate, events, packet_confidence):
    risk_types = {"RISK_FACTORS", "LIQUIDITY", "DEBT", "LEGAL_PROCEEDINGS", "MATERIAL_COMMITMENT",
                  "SHARE_ISSUANCE", "SHARE_OFFERING", "FINANCING", "RESTATEMENT", "AUDITOR_CHANGE",
                  "ACCOUNTING_ISSUE", "REGULATORY_EVENT"}
    conflicts = parsed.get("evidence_conflicts", [])
    evidence = {"confidence": packet_confidence,
        "material_catalyst": any(e.get("materiality") in {"MATERIAL", "HIGH"} and
                                  e.get("event_type") not in risk_types for e in events),
        "material_risk": any(e.get("materiality") in {"MATERIAL", "HIGH"} and
                              e.get("event_type") in risk_types for e in events),
        "conflict": bool(conflicts), "unresolved_conflict": bool(conflicts), "events": events,
        "material_event_count": sum(e.get("materiality") in {"MATERIAL", "HIGH"} for e in events),
        "regulatory_risk": any(e.get("event_type") in {"LEGAL_PROCEEDINGS", "REGULATORY_EVENT"} for e in events),
        "accounting_risk": any(e.get("event_type") in {"RESTATEMENT", "AUDITOR_CHANGE", "ACCOUNTING_ISSUE"}
                                for e in events)}
    return escalation(candidate, _initial_for_escalation(parsed), evidence, load_tier_config(environ={}))


def reconcile(db):
    """Re-evaluate only already archived responses. This function never calls the API."""
    report = json.loads(REPORT_PATH.read_text())
    cfg = json.loads((ROOT / "phase4_config.json").read_text())
    old_luna = json.loads((ROOT / "data" / "phase4_ab_luna_report.json").read_text())
    old_by_ticker = {r["ticker"]: r for r in old_luna["results"]}
    new_by_ticker = {}
    for ticker in TICKERS:
        case = db.execute("SELECT * FROM phase4_ab_cases WHERE run_id=? AND ticker=?",
                          (PILOT_ID, ticker)).fetchone()
        attempt = db.execute("SELECT * FROM phase4_ab_attempts WHERE run_id=? AND ticker=?",
                             (PILOT_ID, ticker)).fetchone()
        if not case or not attempt or not attempt["raw_response_saved"]:
            raise ValueError(f"{ticker}: archived API raw response is missing")
        raw = json.loads(attempt["raw_response_json"])
        payload = _payload(raw)
        requested = json.loads(case["luna_request_json"])["model"]
        returned = raw.get("model")
        inputs = cached_inputs(db, ticker, cfg)
        allowed = inputs["allowed_evidence_ids"]
        grouped = _group_claims(payload.get("claims"))
        payload_for_eval = {**payload, "claims": grouped}
        validated = True
        validation_error = None
        try:
            parsed = validate_response(raw, allowed, inputs["candidate_score"], cfg)
        except ValueError as exc:
            validated = False
            validation_error = str(exc)
            parsed = {"research_status": payload.get("research_status", "VALIDATION_FAILED"),
                      "research_score": None, "catalyst_score": None, "thesis_confidence": payload.get("thesis_confidence", "LOW"),
                      "claims": grouped, "evidence_conflicts": payload.get("evidence_conflicts", []),
                      "evidence_limitations": payload.get("evidence_limitations", []),
                      "score_evidence_ids": [x for x in payload.get("score_evidence_ids", []) if x in allowed],
                      "catalyst_evidence_ids": [x for x in payload.get("catalyst_evidence_ids", []) if x in allowed]}
        parsed_for_eval = {**parsed, "claims": grouped, "research_status": payload.get("research_status"),
                           "score_evidence_ids": parsed.get("score_evidence_ids", payload.get("score_evidence_ids", [])),
                           "catalyst_evidence_ids": parsed.get("catalyst_evidence_ids", payload.get("catalyst_evidence_ids", []))}
        event_rows = db.execute("""SELECT event_json FROM evidence_events_v2
            WHERE ticker=? AND evidence_asof=?""", (ticker, case["evidence_asof"])).fetchall()
        events = [json.loads(row[0]) for row in event_rows]
        event_types = {e["duplicate_group"]: e.get("event_type", "UNKNOWN") for e in events}
        evaluator = evaluate(ticker, parsed_for_eval, event_types, allowed)
        escalation_result = _escalation_for(ticker, parsed_for_eval, inputs["candidate_score"], events,
            inputs["prompt"]["phase4a_evidence"].get("confidence", "UNKNOWN"))
        details = raw.get("usage", {})
        start = __import__("datetime").datetime.fromisoformat(attempt["request_started_at"].replace("Z", "+00:00"))
        finish = __import__("datetime").datetime.fromisoformat(attempt["finished_at"].replace("Z", "+00:00"))
        runtime = attempt["runtime_seconds"] or round((finish - start).total_seconds(), 2)
        cited_ids = {eid for claims in grouped.values() for claim in claims for eid in claim.get("evidence_ids", [])}
        top_ids = (payload.get("score_evidence_ids", []) + payload.get("catalyst_evidence_ids", []))
        handled_ids = cited_ids | (set(top_ids) & allowed)
        result = {"ticker": ticker, "requested_model_id": requested, "returned_model_id": returned,
            "request_id": attempt["request_id"], "research_status": payload.get("research_status"),
            "validation_status": "VALID" if validated else "REJECTED",
            "validation_error": validation_error,
            "model_research_status": payload.get("research_status"),
            "research_score": parsed.get("research_score"), "raw_research_score": payload.get("research_score"),
            "catalyst_score": parsed.get("catalyst_score"), "raw_catalyst_score": payload.get("catalyst_score"),
            "thesis_confidence": parsed.get("thesis_confidence", payload.get("thesis_confidence")),
            "claims": grouped, "evidence_ids": sorted(handled_ids),
            "new_information_vs_phase2_3": grouped.get("NEW_INFORMATION", []),
            "evidence_limitations": payload.get("evidence_limitations", []),
            "evidence_conflicts": payload.get("evidence_conflicts", []),
            "input_tokens": details.get("input_tokens"), "output_tokens": details.get("output_tokens"),
            "total_tokens": details.get("total_tokens"), "runtime_seconds": runtime,
            "actual_cost_usd": attempt["local_cost"], "web_search_calls": 0,
            "evaluator": evaluator, "escalation": escalation_result,
            "major_hallucination": evaluator["major_hallucination"]}
        result["research_status"] = payload.get("research_status") if validated else "VALIDATION_FAILED"
        new_by_ticker[ticker] = result
        with db:
            db.execute("""UPDATE phase4_ab_attempts SET status=?,runtime_seconds=?,result_json=?,
                evaluator_json=?,error_category=? WHERE run_id=? AND ticker=?""",
                (result["research_status"], runtime, compact(result), compact(evaluator),
                 validation_error, PILOT_ID, ticker))

    metrics = ("material_catalyst_recall", "material_risk_recall", "evidence_grounding",
               "citation_accuracy", "unsupported_claim_rate", "uncertainty_handling", "thesis_quality",
               "initial_model_suitability", "major_hallucination")
    comparison = {}
    sol_eval = old_luna["sol_baseline_evaluators"]
    for ticker in TICKERS:
        old = old_by_ticker[ticker]
        new = new_by_ticker[ticker]
        sol_row = db.execute("""SELECT result_json,input_tokens,output_tokens,local_cost_usd,runtime_seconds
            FROM evidence_reasoner_results WHERE run_id=? AND ticker=?""", (SOL_RUNS[ticker], ticker)).fetchone()
        sol_result = json.loads(sol_row["result_json"])
        inp = cached_inputs(db, ticker, cfg)
        ev_rows = db.execute("SELECT event_json FROM evidence_events_v2 WHERE ticker=? AND evidence_asof=?",
                             (ticker, inp["evidence_asof"])).fetchall()
        events = [json.loads(x[0]) for x in ev_rows]
        sol_esc = _escalation_for(ticker, sol_result, inp["candidate_score"], events,
                                  inp["prompt"]["phase4a_evidence"].get("confidence", "UNKNOWN"))
        old_parsed = {**old, "claims": old.get("claims", {}), "research_status": old.get("research_status")}
        old_esc = _escalation_for(ticker, old_parsed, inp["candidate_score"], events,
                                  inp["prompt"]["phase4a_evidence"].get("confidence", "UNKNOWN"))
        comparison[ticker] = {
            "GPT-5.6 Luna": {**{k:old["evaluator"].get(k) for k in metrics},
                "research_score":old.get("research_score"),"catalyst_score":old.get("catalyst_score"),
                "confidence":old.get("thesis_confidence"),"input_tokens":old.get("input_tokens"),
                "output_tokens":old.get("output_tokens"),"cost_usd":old.get("local_cost_usd"),
                "runtime_seconds":old.get("runtime_seconds"),"escalation":old_esc},
            "GPT-6 Luna": {**{k:new["evaluator"].get(k) for k in metrics},
                "research_score":new.get("research_score"),"raw_research_score":new.get("raw_research_score"),
                "catalyst_score":new.get("catalyst_score"),"raw_catalyst_score":new.get("raw_catalyst_score"),
                "confidence":new.get("thesis_confidence"),"input_tokens":new.get("input_tokens"),
                "output_tokens":new.get("output_tokens"),"cost_usd":new.get("actual_cost_usd"),
                "runtime_seconds":new.get("runtime_seconds"),"escalation":new["escalation"],
                "validation_status":new["validation_status"]},
            "GPT-6 Sol": {**{k:sol_eval[ticker].get(k) for k in metrics},
                "research_score":sol_result.get("research_score"),"catalyst_score":sol_result.get("catalyst_score"),
                "confidence":sol_result.get("thesis_confidence"),"input_tokens":sol_row["input_tokens"],
                "output_tokens":sol_row["output_tokens"],"cost_usd":sol_row["local_cost_usd"],
                "runtime_seconds":sol_row["runtime_seconds"],"escalation":sol_esc}}
    complete_eval = {t:r["evaluator"] for t,r in new_by_ticker.items()}
    decision = final_decision(complete_eval)
    report.update({"results":[new_by_ticker[t] for t in TICKERS], "comparisons":comparison,
        "average_evaluator":decision,
        "recommendation":decision["recommendation"].replace("LUNA_INITIAL_CANDIDATE", "GPT6_LUNA_INITIAL_CANDIDATE"),
        "status":"COMPLETE_CLOSED",
        "usage":{"calls":len(new_by_ticker),
            "input_tokens":sum(r.get("input_tokens",0) or 0 for r in new_by_ticker.values()),
            "output_tokens":sum(r.get("output_tokens",0) or 0 for r in new_by_ticker.values()),
            "cost_usd":round(sum(r.get("actual_cost_usd",0) or 0 for r in new_by_ticker.values()),8)}})
    old_gap_count = sum(bool(v["GPT-5.6 Luna"]["escalation"]["uncovered_material_event_ids"])
                        for v in comparison.values())
    new_gap_count = sum(bool(v["GPT-6 Luna"]["escalation"]["uncovered_material_event_ids"])
                        for v in comparison.values())
    old_conflict_count = sum("EVIDENCE_CONFLICT_UNRESOLVED" in v["GPT-5.6 Luna"]["escalation"]["hard_triggers"]
                             for v in comparison.values())
    new_conflict_count = sum("EVIDENCE_CONFLICT_UNRESOLVED" in v["GPT-6 Luna"]["escalation"]["hard_triggers"]
                             for v in comparison.values())
    report["coverage_comparison"] = {"material_event_gap_tickers_56_luna":old_gap_count,
        "material_event_gap_tickers_gpt6_luna":new_gap_count,
        "material_event_gap_ids_56_luna":sum(len(v["GPT-5.6 Luna"]["escalation"]["uncovered_material_event_ids"])
                                              for v in comparison.values()),
        "material_event_gap_ids_gpt6_luna":sum(len(v["GPT-6 Luna"]["escalation"]["uncovered_material_event_ids"])
                                                 for v in comparison.values()),
        "unresolved_conflict_tickers_56_luna":old_conflict_count,
        "unresolved_conflict_tickers_gpt6_luna":new_conflict_count}
    report["usage"]["total_tokens"] = report["usage"]["input_tokens"] + report["usage"]["output_tokens"]
    write_report(report)
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--reconcile", action="store_true", help="offline-only parse/evaluate saved raw responses")
    args = parser.parse_args()
    db = sqlite3.connect(ROOT / "data" / "research_data.db")
    db.row_factory = sqlite3.Row
    if args.reconcile:
        report = reconcile(db)
        print(json.dumps({"status":report["status"],"usage":report.get("usage"),
                          "recommendation":report.get("recommendation"),
                          "coverage_comparison":report.get("coverage_comparison")},indent=2))
        return
    cfg, cases, forecast = prepare(db)
    if not args.execute:
        print(json.dumps({"status": "PREFLIGHT_OK", "requested_model_id": MODEL,
                          "web_search": "OFF", "allowed_tickers": TICKERS,
                          "budget": {"max_calls": MAX_CALLS, "max_input_tokens": MAX_INPUT,
                                     "max_output_tokens": MAX_OUTPUT, "max_cost_usd": MAX_COST},
                          "forecast": forecast}, indent=2))
        return
    report = run(db, load_key(ROOT / ".env"))
    print(json.dumps({"status": report["status"], "usage": report.get("usage"),
                      "recommendation": report.get("recommendation")}, indent=2))


if __name__ == "__main__":
    main()

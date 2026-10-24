#!/usr/bin/env python3
"""Execute only the approved Day 1 GPT-5.6 Luna Initial plan."""
from __future__ import annotations

import json
import sqlite3
import time
import urllib.error
from datetime import datetime, timezone

from day0_1_research import SCHEMA, configuration, cost, evidence_for_escalation
from phase4_engine import compact
from phase4_run import api_research, load_key
from phase4_tiers import budget_transition, escalation, init_db as init_tiers, reserve
from phase4b_evidence import cached_inputs, request_body, validate_response
from quant_screen import ROOT
from shadow_day1_research import CONFIG, DB, FUNDAMENTAL_DB, PLAN, build_plan, digest
from shadow_day1_state import connect, row as state_row

REPORT = ROOT / "data" / "shadow_day1_initial_report.json"


def now():
    return datetime.now(timezone.utc).isoformat()


def local_tier_budget(frozen, limits):
    # Enable only this in-memory, one-run budget. Never change the frozen file.
    return {**frozen, "enabled": True,
            "max_initial_calls": limits["max_daily_initial_calls"],
            "max_daily_initial_ai_cost": limits["max_daily_initial_cost_usd"],
            "max_deep_calls": 0, "max_daily_deep_ai_cost": 0,
            "max_daily_total_ai_cost": limits["max_daily_initial_cost_usd"]}


def token_capacity(db, day, limits, predicted_input, predicted_output):
    rows = db.execute("""SELECT state,estimated_input_tokens,estimated_output_tokens,
        actual_input_tokens,actual_output_tokens FROM tier_budget_ledger
        WHERE usage_day=? AND tier='INITIAL'""", (day,)).fetchall()
    used_input = sum(r[3] if r[3] is not None else r[1] for r in rows
                     if r[0] not in {"BUDGET_BLOCKED", "FAILED_NO_REQUEST"})
    used_output = sum(r[4] if r[4] is not None else r[2] for r in rows
                      if r[0] not in {"BUDGET_BLOCKED", "FAILED_NO_REQUEST"})
    return (used_input + predicted_input <= limits["max_daily_initial_input_tokens"] and
            used_output + predicted_output <= limits["max_daily_initial_output_tokens"])


def classify(case, attempt):
    if case["required_action"] == "RESEARCH_DEFERRED_BUDGET":
        return "RESEARCH_DEFERRED_BUDGET", None, None
    if case["cache_status"] == "VALID_CACHE":
        esc = case.get("prior_escalation")
        if esc and esc.get("decision") == "ESCALATE_SOL":
            return "DEEP_RESEARCH_PENDING", None, esc
        tier = "DEEP" if case["prior_source"] == "SOL_DB" else "INITIAL"
        return "FINAL_RESEARCH_AVAILABLE", tier, esc
    if case["cache_status"] == "INSUFFICIENT_EVIDENCE":
        esc = case.get("prior_escalation")
        return ("DEEP_RESEARCH_PENDING" if esc and esc.get("decision") == "ESCALATE_SOL"
                else "INSUFFICIENT_EVIDENCE"), None, esc
    if not attempt:
        return "RESEARCH_FAILED", None, None
    status, parsed, esc = attempt["state"], (json.loads(attempt["parsed_result_json"])
                                             if attempt["parsed_result_json"] else None), (
        json.loads(attempt["escalation_json"]) if attempt["escalation_json"] else None)
    if esc and esc["decision"] == "ESCALATE_SOL":
        return "DEEP_RESEARCH_PENDING", None, esc
    if status == "COMPLETE" and parsed:
        return "FINAL_RESEARCH_AVAILABLE", "INITIAL", esc
    if status == "INSUFFICIENT_EVIDENCE":
        return "INSUFFICIENT_EVIDENCE", None, esc
    return "RESEARCH_FAILED", None, esc


def current_report(db, plan):
    attempts = {r["ticker"]: dict(r) for r in db.execute(
        "SELECT * FROM day01_attempts WHERE run_id=?", (plan["run_id"],))}
    cases = []
    for case in plan["cases"]:
        attempt = attempts.get(case["ticker"])
        status, tier, esc = classify(case, attempt)
        parsed = (json.loads(attempt["parsed_result_json"]) if attempt and attempt["parsed_result_json"]
                  else case.get("prior_result") if status == "FINAL_RESEARCH_AVAILABLE" else None)
        cases.append({"ticker": case["ticker"], "priority": case["priority"],
                      "cache_status": case["cache_status"], "required_action": case["required_action"],
                      "final_status": status, "final_research_tier": tier,
                      "candidate_score": case["candidate_score"],
                      "evidence_coverage": case["evidence_coverage"],
                      "research_score": parsed.get("research_score") if parsed else None,
                      "catalyst_score": parsed.get("catalyst_score") if parsed else None,
                      "conviction_score": parsed.get("conviction_score") if parsed else None,
                      "research_completed_at": (attempt["finished_at"] if attempt else
                                                case.get("prior_completed_at")),
                      "escalation": esc,
                      "input_tokens": attempt["input_tokens"] if attempt else 0,
                      "output_tokens": attempt["output_tokens"] if attempt else 0,
                      "cost_usd": attempt["local_cost_usd"] if attempt else 0,
                      "error_class": attempt["error_class"] if attempt else None})
    return {"run_id": plan["run_id"], "source_research_run": plan["source_research_run"],
            "model": plan["model"], "web_search": "OFF", "cases": cases,
            "cached_count": plan["cached_count"],
            "luna_calls": len(attempts),
            "input_tokens": sum(a["input_tokens"] or 0 for a in attempts.values()),
            "output_tokens": sum(a["output_tokens"] or 0 for a in attempts.values()),
            "confirmed_cost_usd": round(sum(a["local_cost_usd"] or 0 for a in attempts.values()), 8),
            "unknown_cost_reservations_usd": round(sum(a["estimated_cost"] for a in attempts.values()
                if a["local_cost_usd"] is None), 8),
            "deep_pending": sum(c["final_status"] == "DEEP_RESEARCH_PENDING" for c in cases),
            "final_available": sum(c["final_status"] == "FINAL_RESEARCH_AVAILABLE" for c in cases),
            "sol_calls": 0}


def save_report(db, plan):
    report = current_report(db, plan)
    temporary = REPORT.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    temporary.replace(REPORT)
    return report


def run_one(db, plan, case, cfg, frozen, limits, key, names):
    ticker = case["ticker"]
    inputs = cached_inputs(db, ticker, cfg, allowed_tickers=names, citation_mode="short")
    if inputs["source_run_id"] != plan["source_research_run"]:
        raise ValueError("RESEARCH_SOURCE_RUN_MISMATCH")
    if digest(inputs["prompt"]) != case["input_hash"]:
        raise ValueError(f"{ticker}: FROZEN_RESEARCH_INPUT_CHANGED")
    body = request_body(inputs, cfg)
    if body["model"] != "gpt-5.6-luna" or body.get("tools") or body.get("max_tool_calls"):
        raise ValueError("INITIAL_MODEL_OR_SEARCH_BLOCK")
    day = datetime.now(timezone.utc).date().isoformat()
    if not token_capacity(db, day, limits, case["estimated_input"], case["estimated_output"]):
        return "RESEARCH_DEFERRED_BUDGET"
    budget = local_tier_budget(frozen, limits)
    reserved = reserve(db, day, plan["run_id"], ticker, "INITIAL",
                       case["estimated_input"], case["estimated_output"],
                       case["estimated_cost"], budget)
    if reserved != "RESERVED":
        return "RESEARCH_DEFERRED_BUDGET"
    with db:
        db.execute("""INSERT INTO day01_attempts(run_id,ticker,state,request_started_at,
            estimated_input,estimated_output,estimated_cost,requested_model)
            VALUES(?,?,?,?,?,?,?,?)""", (plan["run_id"], ticker, "IN_FLIGHT", now(),
            case["estimated_input"], case["estimated_output"], case["estimated_cost"],
            "gpt-5.6-luna"))
    budget_transition(db, plan["run_id"], ticker, "INITIAL", "IN_FLIGHT")
    try:
        raw, metadata = api_research(body, key)
    except Exception as exc:
        budget_transition(db, plan["run_id"], ticker, "INITIAL", "FAILED_UNKNOWN")
        with db:
            db.execute("""UPDATE day01_attempts SET state='FAILED_UNKNOWN',error_class=?,
                finished_at=? WHERE run_id=? AND ticker=?""",
                (type(exc).__name__, now(), plan["run_id"], ticker))
        if isinstance(exc, urllib.error.HTTPError):
            exc.close()
        raise RuntimeError(f"{ticker}: FAILED_UNKNOWN; no automatic retry") from None
    # Commit the paid response before reading usage or parsing model text.
    with db:
        db.execute("""UPDATE day01_attempts SET raw_response_json=?,raw_response_saved=1,
            returned_model=?,request_id=? WHERE run_id=? AND ticker=?""",
            (compact(raw), raw.get("model"), metadata.get("request_id"), plan["run_id"], ticker))
    usage = raw.get("usage") or {}
    used_in, used_out = usage.get("input_tokens"), usage.get("output_tokens")
    actual_cost = (cost(used_in, used_out, usage.get("input_tokens_details"))
                   if used_in is not None and used_out is not None else None)
    with db:
        db.execute("""UPDATE day01_attempts SET input_tokens=?,output_tokens=?,local_cost_usd=?
            WHERE run_id=? AND ticker=?""", (used_in, used_out, actual_cost, plan["run_id"], ticker))
    if actual_cost is None:
        with db:
            db.execute("""UPDATE day01_attempts SET state='FAILED_UNKNOWN',
                error_class='USAGE_UNKNOWN',finished_at=? WHERE run_id=? AND ticker=?""",
                (now(), plan["run_id"], ticker))
        budget_transition(db, plan["run_id"], ticker, "INITIAL", "FAILED_UNKNOWN",
                          request_id=metadata.get("request_id"))
        raise RuntimeError(f"{ticker}: USAGE_UNKNOWN; raw response preserved")
    budget_transition(db, plan["run_id"], ticker, "INITIAL", "COMPLETED",
                      input_tokens=used_in, output_tokens=used_out, cost_usd=actual_cost,
                      request_id=metadata.get("request_id"))
    parsed = esc = None
    state, error = "PARSE_FAILED", None
    try:
        if raw.get("model") != "gpt-5.6-luna":
            raise ValueError("RETURNED_MODEL_MISMATCH")
        parsed = validate_response(raw, inputs["allowed_evidence_ids"],
                                   inputs["candidate_score"], cfg)
        state = parsed["research_status"]
        initial = {**parsed, "status": state, "confidence": parsed["thesis_confidence"]}
        if state == "COMPLETE":
            initial["handled_evidence_ids"] = sorted(set(parsed["score_evidence_ids"] +
                                                         parsed["catalyst_evidence_ids"]))
        esc = escalation(inputs["candidate_score"], initial,
                         evidence_for_escalation(inputs), frozen)
    except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        error = type(exc).__name__
    with db:
        db.execute("""UPDATE day01_attempts SET state=?,parsed_result_json=?,escalation_json=?,
            error_class=?,finished_at=? WHERE run_id=? AND ticker=?""",
            (state, compact(parsed) if parsed else None, compact(esc) if esc else None,
             error, now(), plan["run_id"], ticker))
        if parsed:
            db.execute("""INSERT INTO tier_research_results
                (run_id,ticker,tier,model,status,research_score,catalyst_score,thesis,
                 confidence,evidence_hash,input_hash,input_tokens,output_tokens,cost_usd,
                 completed_at,response_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (plan["run_id"], ticker, "INITIAL", "gpt-5.6-luna", state,
                 parsed.get("research_score"), parsed.get("catalyst_score"),
                 parsed.get("score_rationale"), parsed.get("thesis_confidence"),
                 case["evidence_hash"], case["input_hash"], used_in, used_out,
                 actual_cost, now(), compact(parsed)))
        if esc:
            db.execute("""INSERT INTO tier_escalations VALUES(?,?,?,?,?,?)""",
                       (plan["run_id"], ticker, esc["score"], esc["decision"],
                        compact(esc["reasons"]), now()))
    return state


def execute():
    with connect() as state:
        current = state_row(state)
        if current["state"] != "RUNNING" or current["resume_count"] != 1:
            raise RuntimeError("SHADOW_DAY1_MANUAL_RESUME_REQUIRED")
    cfg, frozen = configuration()
    limits = json.loads(CONFIG.read_text())
    saved = json.loads(PLAN.read_text())
    if saved["limits_hash"] != digest(limits):
        raise ValueError("DAY1_LIMITS_CHANGED_AFTER_PLAN")
    with sqlite3.connect(DB) as db, sqlite3.connect(FUNDAMENTAL_DB) as fundamental:
        db.row_factory = fundamental.row_factory = sqlite3.Row
        live = build_plan(db, fundamental, cfg, frozen, limits)
        if live != saved:
            raise ValueError("DAY1_RESEARCH_PLAN_CHANGED; no API call")
        key = load_key(ROOT / ".env")
        if not key:
            raise ValueError("OPENAI_API_KEY_MISSING")
        db.executescript(SCHEMA)
        init_tiers(db)
        if db.execute("SELECT 1 FROM day01_runs WHERE id=?", (saved["run_id"],)).fetchone():
            raise ValueError("DAY1_INITIAL_ALREADY_STARTED; no automatic retry")
        with db:
            db.execute("INSERT INTO day01_runs VALUES(?,?,?,?,?,?,?,?,NULL)",
                       (saved["run_id"], saved["source_research_run"], saved["model"],
                        "ACTIVE", saved["max_calls"], saved["max_cost_usd"],
                        saved["plan_hash"], now()))
            for case in saved["cases"]:
                db.execute("""INSERT INTO day01_states VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (saved["run_id"], case["ticker"], case["cache_status"], case["reason"],
                     case["prior_completed_at"], case["evidence_asof"], None,
                     case["candidate_score"], int(case["material_evidence_change"]),
                     case["evidence_coverage"], case["evidence_confidence"],
                     case["evidence_tokens"], "NOT_RUN", "NOT_RUN"))
        names = [case["ticker"] for case in saved["cases"]]
        for case in saved["cases"]:
            if case["required_action"] != "INITIAL_RESEARCH":
                continue
            status = run_one(db, saved, case, cfg, frozen, limits, key, names)
            if status == "RESEARCH_DEFERRED_BUDGET":
                case["required_action"] = status
            print(f"INITIAL {case['ticker']} {status}", flush=True)
            save_report(db, saved)
        report = save_report(db, saved)
        with db:
            db.execute("UPDATE day01_runs SET status='CLOSED',closed_at=? WHERE id=?",
                       (now(), saved["run_id"]))
            for case in report["cases"]:
                db.execute("""UPDATE day01_states SET research_status=?,final_status=?
                    WHERE run_id=? AND ticker=?""",
                    (case["required_action"], case["final_status"], saved["run_id"], case["ticker"]))
                tier = case["final_research_tier"] or "UNAVAILABLE"
                db.execute("""INSERT INTO tier_final_results
                    (run_id,ticker,final_research_tier,status,source_tier,final_research_score,
                     final_catalyst_score,final_thesis,final_confidence,decided_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (saved["run_id"], case["ticker"], tier, case["final_status"],
                     case["final_research_tier"], case["research_score"], case["catalyst_score"],
                     None, None, now()))
        return report


if __name__ == "__main__":
    result = execute()
    print(json.dumps({k: v for k, v in result.items() if k != "cases"}, indent=2))

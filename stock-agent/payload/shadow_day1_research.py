#!/usr/bin/env python3
"""One-session, cache-aware Initial research. Plan is offline by default."""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from datetime import datetime, timezone

from day0_1_research import configuration, evidence_for_escalation
from phase4_engine import compact
from phase4b_evidence import cached_inputs, estimate, request_body
from quant_screen import ROOT

DB = ROOT / "data" / "research_data.db"
FUNDAMENTAL_DB = ROOT / "data" / "fundamental_data.db"
PLAN = ROOT / "data" / "shadow_day1_research_plan.json"
CONFIG = ROOT / "shadow_day1_research_config.json"


def digest(value):
    return hashlib.sha256(compact(value).encode()).hexdigest()


def material_hash(db, ticker, asof):
    row = db.execute("""SELECT packet_json FROM evidence_packets_v2 WHERE ticker=?
        AND evidence_asof=? ORDER BY created_at DESC LIMIT 1""", (ticker, asof)).fetchone()
    if not row:
        return None
    packet = json.loads(row[0])
    events = [e for e in packet["recent_material_events"] + packet["other_events"]
              if e["materiality"] in {"MATERIAL", "HIGH"}]
    return digest([(e["duplicate_group"], e["event_type"], e.get("verified_facts")) for e in events])


def latest_prior(db, ticker, current_run_id):
    options = []
    row = db.execute("""SELECT a.run_id,a.state,a.finished_at,a.parsed_result_json,a.escalation_json,
        a.requested_model,a.returned_model,r.source_research_run,s.evidence_as_of,
        s.current_candidate_score FROM day01_attempts a
        JOIN day01_runs r ON r.id=a.run_id
        LEFT JOIN day01_states s ON s.run_id=a.run_id AND s.ticker=a.ticker
        WHERE a.ticker=? AND a.run_id<>? ORDER BY a.finished_at DESC LIMIT 1""",
        (ticker, current_run_id)).fetchone()
    if row and row[2] and row[5] == "gpt-5.6-luna" and \
            (row[6] == "gpt-5.6-luna" or row[1] == "FAILED_UNKNOWN"):
        options.append({"source": "LUNA_DB", "status": row[1], "completed_at": row[2],
                        "result": json.loads(row[3]) if row[3] else None,
                        "escalation": json.loads(row[4]) if row[4] else None,
                        "research_run_id": row[7], "evidence_asof": row[8],
                        "candidate_score": row[9]})
    row = db.execute("""SELECT run_id,research_status,created_at,result_json,
        evidence_asof,candidate_score FROM evidence_reasoner_results
        WHERE ticker=? AND research_status='COMPLETE' ORDER BY created_at DESC LIMIT 1""",
        (ticker,)).fetchone()
    if row:
        archived = db.execute("""SELECT response_json FROM research_response_archive
            WHERE run_id=? AND ticker=? LIMIT 1""", (row[0], ticker)).fetchone()
        if archived and json.loads(archived[0]).get("model") == "gpt-6-sol":
            options.append({"source": "SOL_DB", "status": row[1], "completed_at": row[2],
                            "result": json.loads(row[3]), "research_run_id": row[0],
                            "evidence_asof": row[4], "candidate_score": row[5],
                            "escalation": None})
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='tier_research_results'").fetchone():
        row = db.execute("""SELECT t.run_id,t.status,t.completed_at,t.response_json,t.evidence_hash,
            s.evidence_as_of,s.current_candidate_score,r.source_research_run,e.score,e.decision,e.reasons_json
            FROM tier_research_results t
            LEFT JOIN day01_states s ON s.run_id=t.run_id AND s.ticker=t.ticker
            LEFT JOIN day01_runs r ON r.id=t.run_id
            LEFT JOIN tier_escalations e ON e.run_id=t.run_id AND e.ticker=t.ticker
            WHERE t.ticker=? AND t.tier='INITIAL'
            ORDER BY completed_at DESC LIMIT 1""", (ticker,)).fetchone()
        if row:
            esc = ({"score": row[8], "decision": row[9], "reasons": json.loads(row[10])}
                   if row[9] else None)
            options.append({"source": "TIER_DB", "status": row[1], "completed_at": row[2],
                            "result": json.loads(row[3]), "research_run_id": row[7],
                            "evidence_asof": row[5], "candidate_score": row[6],
                            "escalation": esc})
    return max(options, key=lambda item: item["completed_at"]) if options else None


def prior_rank(fundamental, research, prior, ticker):
    if not prior or prior["research_run_id"] is None:
        return None
    run = research.execute("SELECT fundamental_run_id FROM research_runs WHERE id=?",
                           (prior["research_run_id"],)).fetchone()
    if not run:
        return None
    row = fundamental.execute("SELECT candidate_rank FROM fundamental_scores WHERE run_id=? AND ticker=?",
                              (run[0], ticker)).fetchone()
    return row[0] if row else None


def build_plan(research, fundamental, cfg, frozen, limits, at=None, day="2026-09-28"):
    at = at or datetime.now(timezone.utc)
    if frozen["architecture_status"] != "FROZEN_V1" or frozen["deep_research_model"] != "gpt-6-sol":
        raise ValueError("FROZEN_PHASE4_CONFIG_CHANGED")
    if (limits["initial_model"] != frozen["initial_research_model"] or
            limits["web_search_enabled"] or limits["max_daily_initial_calls"] > 10 or
            limits["max_daily_initial_cost_usd"] > 0.05):
        raise ValueError("SHADOW_RESEARCH_LIMITS_INVALID")
    run = research.execute("""SELECT id,fundamental_run_id,candidate_scan_date FROM research_runs
        WHERE candidate_scan_date=? ORDER BY id DESC LIMIT 1""", (day,)).fetchone()
    if not run or run[1] != fundamental.execute("""SELECT id FROM fundamental_runs
        WHERE scan_date=? ORDER BY id DESC LIMIT 1""", (day,)).fetchone()[0]:
        raise ValueError("DAY1_PHASE3_INPUT_MISMATCH")
    tickers = [r[0] for r in research.execute("""SELECT ticker FROM research_priority
        WHERE run_id=? AND priority_rank<=15 ORDER BY priority_rank""", (run[0],))]
    if len(tickers) != 15 or len(set(tickers)) != 15:
        raise ValueError("DAY1_TOP15_INCOMPLETE")
    cases = []
    for priority, ticker in enumerate(tickers, 1):
        candidate = fundamental.execute("""SELECT candidate_score,candidate_rank
            FROM fundamental_scores WHERE run_id=? AND ticker=?""", (run[1], ticker)).fetchone()
        if not candidate:
            raise ValueError("DAY1_PHASE3_CANDIDATE_MISSING")
        try:
            inputs = cached_inputs(research, ticker, cfg, allowed_tickers=tickers, citation_mode="short")
        except ValueError as exc:
            if not str(exc).startswith("CACHE_INTEGRITY_FAILED:"):
                raise
            # Invalid cache is a local no-call result, never a usable Limited tier.
            row = research.execute("""SELECT evidence_asof FROM evidence_packets_v2
                WHERE ticker=? ORDER BY evidence_asof DESC LIMIT 1""", (ticker,)).fetchone()
            cases.append({"priority": priority, "ticker": ticker,
                "cache_status": "RESEARCH_FAILED_CACHE", "reason": str(exc),
                "evidence_status": "CACHE_INTEGRITY_FAILED", "evidence_asof": row[0] if row else None,
                "evidence_tokens": None, "evidence_confidence": "INSUFFICIENT",
                "evidence_coverage": None, "candidate_score": candidate[0],
                "candidate_rank": candidate[1], "candidate_score_change": None,
                "candidate_rank_change": None, "material_evidence_change": False,
                "prior_status": None, "prior_source": None, "prior_completed_at": None,
                "prior_result": None, "prior_escalation": None,
                "evidence_hash": None, "input_hash": None,
                "estimated_input": 0, "estimated_output": 0, "estimated_cost": 0.0})
            continue
        if inputs["source_run_id"] != run[0] or inputs["candidate_scan_date"] != day:
            raise ValueError("RESEARCH_SOURCE_RUN_MISMATCH")
        evidence = inputs["prompt"]["phase4a_evidence"]
        evidence_at = datetime.fromisoformat(inputs["evidence_asof"].replace("Z", "+00:00"))
        age_hours = (at - evidence_at).total_seconds() / 3600
        prior = latest_prior(research, ticker, limits["run_id"])
        score_delta = (inputs["candidate_score"] - prior["candidate_score"]
                       if prior and prior["candidate_score"] is not None else None)
        current_rank = candidate[1]
        old_rank = prior_rank(fundamental, research, prior, ticker)
        rank_change = old_rank - current_rank if old_rank is not None else None
        changed = bool(prior and prior["evidence_asof"] and
                       material_hash(research, ticker, prior["evidence_asof"]) !=
                       material_hash(research, ticker, inputs["evidence_asof"]))
        events = evidence["recent_material_events"] + evidence["other_events"]
        official = any(any(s.get("source_type", "").startswith("SEC") for s in e["sources"])
                       for e in events)
        unresolved = research.execute("""SELECT 1 FROM tier_budget_ledger WHERE ticker=?
            AND tier='INITIAL' AND state IN ('RESERVED','IN_FLIGHT','FAILED_UNKNOWN') LIMIT 1""",
            (ticker,)).fetchone()
        if unresolved or (prior and prior["status"] == "FAILED_UNKNOWN"):
            status, reason = "RESEARCH_BLOCKED_UNKNOWN", "prior request cost/outcome unknown; manual reconciliation required"
        elif (age_hours > limits["evidence_max_age_hours"] or
                inputs["evidence_policy"]["status"] == "EVIDENCE_PACKET_OVERSIZE" or
                evidence["coverage_pct"] < limits["minimum_evidence_coverage_pct"] or
                evidence["coverage"].get("SEC_CONTENT") != "AVAILABLE" or not official):
            status, reason = "INSUFFICIENT_EVIDENCE", "stale, oversized or insufficient official evidence"
        elif prior and prior["status"] == "COMPLETE" and not changed and \
                score_delta is not None and abs(score_delta) < frozen["candidate_score_refresh_threshold"] and \
                (rank_change is None or abs(rank_change) < limits["candidate_rank_refresh_threshold"]):
            status, reason = "VALID_CACHE", "valid database research; no material evidence or candidate change"
        elif prior and prior["status"] == "INSUFFICIENT_EVIDENCE" and not changed and \
                score_delta is not None and abs(score_delta) < frozen["candidate_score_refresh_threshold"] and \
                (rank_change is None or abs(rank_change) < limits["candidate_rank_refresh_threshold"]):
            status, reason = "INSUFFICIENT_EVIDENCE", "prior Initial insufficient; no material change"
        elif prior and prior["status"] == "COMPLETE":
            status, reason = "INITIAL_REFRESH_REQUIRED", "material evidence or candidate change"
        else:
            status, reason = "FULL_RESEARCH_REQUIRED", "no valid completed research"
        case = {"priority": priority, "ticker": ticker, "cache_status": status,
                "reason": reason, "evidence_status": inputs["evidence_policy"]["status"],
                "evidence_asof": inputs["evidence_asof"], "evidence_tokens": inputs["evidence_packet_tokens_estimate"],
                "evidence_confidence": evidence["confidence"], "evidence_coverage": evidence["coverage_pct"],
                "candidate_score": inputs["candidate_score"], "candidate_rank": current_rank,
                "candidate_score_change": round(score_delta, 4) if score_delta is not None else None,
                "candidate_rank_change": rank_change, "material_evidence_change": changed,
                "prior_status": prior["status"] if prior else None,
                "prior_source": prior["source"] if prior else None,
                "prior_completed_at": prior["completed_at"] if prior else None,
                "prior_result": prior["result"] if status == "VALID_CACHE" else None,
                "prior_escalation": prior["escalation"] if prior else None,
                "evidence_hash": material_hash(research, ticker, inputs["evidence_asof"]),
                "input_hash": digest(inputs["prompt"])}
        if status in {"INITIAL_REFRESH_REQUIRED", "FULL_RESEARCH_REQUIRED"}:
            body = request_body(inputs, cfg)
            if body["model"] != limits["initial_model"] or body.get("tools"):
                raise ValueError("MODEL_OR_WEB_SEARCH_SAFETY_BLOCK")
            prediction = estimate(body, cfg)
            case.update(estimated_input=prediction["estimated_input_tokens"],
                        estimated_output=prediction["estimated_output_tokens"],
                        estimated_cost=prediction["estimated_total_cost_usd"])
        cases.append(case)
    calls = input_total = output_total = 0
    cost_total = 0.0
    for case in cases:
        if case["cache_status"] not in {"INITIAL_REFRESH_REQUIRED", "FULL_RESEARCH_REQUIRED"}:
            case["required_action"] = "CACHED" if case["cache_status"] == "VALID_CACHE" else "NO_CALL"
            continue
        i, o, cost = case["estimated_input"], case["estimated_output"], case["estimated_cost"]
        if (i > limits["max_input_tokens_per_stock"] or cost > limits["max_cost_per_stock_usd"] or
                calls + 1 > limits["max_daily_initial_calls"] or
                input_total + i > limits["max_daily_initial_input_tokens"] or
                output_total + o > limits["max_daily_initial_output_tokens"] or
                cost_total + cost > limits["max_daily_initial_cost_usd"]):
            case["required_action"] = "RESEARCH_DEFERRED_BUDGET"
        else:
            case["required_action"] = "INITIAL_RESEARCH"
            calls += 1; input_total += i; output_total += o; cost_total += cost
    plan = {"run_id": limits["run_id"], "source_research_run": run[0],
            "limits_hash": digest(limits),
            "candidate_scan_date": run[2], "model": limits["initial_model"],
            "web_search": "OFF", "max_calls": limits["max_daily_initial_calls"],
            "max_cost_usd": limits["max_daily_initial_cost_usd"],
            "estimated_calls": calls, "estimated_input_tokens": input_total,
            "estimated_output_tokens": output_total, "estimated_cost_usd": round(cost_total, 6),
            "cached_count": sum(c["cache_status"] == "VALID_CACHE" for c in cases),
            "refresh_count": sum(c["cache_status"] == "INITIAL_REFRESH_REQUIRED" for c in cases),
            "full_count": sum(c["cache_status"] == "FULL_RESEARCH_REQUIRED" for c in cases),
            "insufficient_count": sum(c["cache_status"] == "INSUFFICIENT_EVIDENCE" for c in cases),
            "failed_cache_count": sum(c["cache_status"] == "RESEARCH_FAILED_CACHE" for c in cases),
            "deferred_count": sum(c["required_action"] == "RESEARCH_DEFERRED_BUDGET" for c in cases),
            "cases": cases}
    plan["plan_hash"] = digest(plan)
    return plan


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    cfg, frozen = configuration()
    limits = json.loads(CONFIG.read_text())
    with sqlite3.connect(DB) as research, sqlite3.connect(FUNDAMENTAL_DB) as fundamental:
        research.row_factory = sqlite3.Row
        fundamental.row_factory = sqlite3.Row
        plan = build_plan(research, fundamental, cfg, frozen, limits)
    if args.execute:
        raise SystemExit("PAID_EXECUTION_NOT_IMPLEMENTED")
    PLAN.write_text(json.dumps(plan, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps({k: v for k, v in plan.items() if k != "cases"}, indent=2))
    print("CASES", [(c["ticker"], c["cache_status"], c["required_action"],
                    c.get("estimated_input"), c.get("estimated_cost")) for c in plan["cases"]])


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Offline grounding audit, short-ID storage, and conservative Deep triage."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from day0_1_research import (DB, PLAN as DAY01_PLAN, REPORT as DAY01_REPORT, RUN,
                            evidence_for_escalation, preflight, result_report)
from day0_1_validate import main as refresh_entry_report
from phase4_engine import compact
from phase4_grounding import validate_id
from phase4_tiers import escalation
from phase4b_evidence import cached_inputs, estimate, request_body, validate_response
from quant_screen import ROOT

OUTPUT = ROOT / "data" / "deep_research_plan_day0_2.json"
MODEL = "gpt-6-sol"
PENDING = ("GIC", "JILL", "LEGH", "SMCI", "KEYS", "HBB", "CRCT", "TWIN")
SCHEMA = """
CREATE TABLE IF NOT EXISTS day02_grounding_audit(
 ticker TEXT NOT NULL, invalid_id TEXT NOT NULL, validation_reason TEXT NOT NULL,
 closest_real_id TEXT, alternative_ids_json TEXT NOT NULL, repaired INTEGER NOT NULL,
 audited_at TEXT NOT NULL, PRIMARY KEY(ticker,invalid_id));
CREATE TABLE IF NOT EXISTS day02_citation_aliases(
 ticker TEXT NOT NULL, evidence_as_of TEXT NOT NULL, source_hash TEXT NOT NULL,
 short_id TEXT NOT NULL, canonical_id TEXT NOT NULL,
 PRIMARY KEY(ticker,evidence_as_of,source_hash,short_id));
CREATE TABLE IF NOT EXISTS day02_deep_executions(
 run_id TEXT NOT NULL, ticker TEXT NOT NULL, model TEXT NOT NULL,
 status TEXT NOT NULL, completed_at TEXT NOT NULL, PRIMARY KEY(run_id,ticker));
CREATE TABLE IF NOT EXISTS day02_deep_metrics(
 run_id TEXT PRIMARY KEY, initial_count INTEGER NOT NULL, deep_pending_count INTEGER NOT NULL,
 deep_executed_count INTEGER NOT NULL, escalation_rate_pct REAL NOT NULL,
 high_escalation_rate INTEGER NOT NULL, recorded_at TEXT NOT NULL);
"""


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def output_payload(raw):
    text = "".join(part.get("text", "") for item in raw.get("output", [])
                   if item.get("type") == "message" for part in item.get("content", [])
                   if part.get("type") == "output_text")
    return json.loads(text)


def audit_hhh(db, cfg, tickers):
    row = db.execute("SELECT raw_response_json FROM day01_attempts WHERE run_id=? AND ticker='HHH'",
                     (RUN,)).fetchone()
    if not row or not row[0]:
        raise ValueError("HHH_RAW_RESPONSE_MISSING")
    raw = json.loads(row[0])
    inputs = cached_inputs(db, "HHH", cfg, allowed_tickers=tickers)
    packet = inputs["prompt"]["phase4a_evidence"]
    events = packet["recent_material_events"] + packet["other_events"]
    allowed = inputs["allowed_evidence_ids"]
    payload = output_payload(raw)
    cited = [id_ for claim in payload["claims"] for id_ in claim["evidence_ids"]]
    cited += payload["score_evidence_ids"] + payload["catalyst_evidence_ids"]
    invalid = sorted(set(cited) - allowed)
    audits = []
    for id_ in invalid:
        check = validate_id(id_, allowed)
        # Descriptive source inspection only; never use semantic proximity as a mapping.
        facts = [(event["duplicate_group"], " ".join(event.get("verified_facts", []))) for event in events]
        acquisition = [event_id for event_id, text in facts
                       if "June 4, 2026" in text and "Vantage" in text]
        preferred = next((event_id for event_id in acquisition if ":RISK_FACTORS:" in event_id), None)
        closest = preferred or (acquisition[0] if acquisition else None)
        audits.append({"invalid_id": id_, "validation_reason": check["reason"],
                       "canonical_id": check["canonical_id"], "closest_real_id": closest,
                       "alternative_ids": [x for x in acquisition if x != closest],
                       "canonicalization_rule": "strict date/type/accession syntax only"})
    repairable = bool(invalid) and all(a["validation_reason"] == "NORMALIZABLE_MATCH" for a in audits)
    if repairable:
        mapping = {a["invalid_id"]: a["canonical_id"] for a in audits}
        for claim in payload["claims"]:
            claim["evidence_ids"] = [mapping.get(x, x) for x in claim["evidence_ids"]]
        for field in ("score_evidence_ids", "catalyst_evidence_ids"):
            payload[field] = [mapping.get(x, x) for x in payload[field]]
        fixed = json.loads(compact(raw))
        for item in fixed["output"]:
            if item.get("type") == "message":
                for part in item.get("content", []):
                    if part.get("type") == "output_text":
                        part["text"] = compact(payload)
        parsed = validate_response(fixed, allowed, inputs["candidate_score"], cfg)
        tiers = json.loads((ROOT / "phase4_tiers_config.json").read_text())
        initial = {**parsed, "status": parsed["research_status"],
                   "confidence": parsed["thesis_confidence"]}
        if parsed["research_status"] == "COMPLETE":
            initial["handled_evidence_ids"] = sorted(set(parsed["score_evidence_ids"] +
                                                         parsed["catalyst_evidence_ids"]))
        decision = escalation(inputs["candidate_score"], initial,
                              evidence_for_escalation(inputs), tiers)
        with db:
            db.execute("""UPDATE day01_attempts SET state=?,parsed_result_json=?,escalation_json=?,
                error_class=NULL WHERE run_id=? AND ticker='HHH'""",
                (parsed["research_status"], compact(parsed), compact(decision), RUN))
    else:
        with db:
            db.execute("""UPDATE day01_attempts SET state='RESEARCH_FAILED_GROUNDING',
                error_class='UNSUPPORTED_EVIDENCE_ID' WHERE run_id=? AND ticker='HHH'""", (RUN,))
    with db:
        for audit in audits:
            db.execute("""INSERT OR REPLACE INTO day02_grounding_audit VALUES(?,?,?,?,?,?,?)""",
                       ("HHH", audit["invalid_id"], audit["validation_reason"],
                        audit["closest_real_id"], compact(audit["alternative_ids"]),
                        int(repairable), utc_now()))
    return {"ticker": "HHH", "raw_response_preserved": True, "invalid_ids": audits,
            "deterministic_repair": repairable,
            "research_status": "REPARSED_FROM_RAW" if repairable else "RESEARCH_FAILED_GROUNDING",
            "new_api_calls": 0}


def score_case(candidate, quant, fundamental, rank, sector_count, evidence, parsed, escalation):
    events = evidence["recent_material_events"] + evidence["other_events"]
    high = [e for e in events if e["materiality"] == "HIGH"]
    material = [e for e in events if e["materiality"] == "MATERIAL"]
    high_with_facts = sum(bool(e.get("verified_facts")) and
                          e.get("extraction_confidence") != "LOW" for e in high)
    conflicts = parsed.get("evidence_conflicts", [])
    limitations = " ".join(parsed.get("evidence_limitations", [])).lower()
    critical_gap = any(term in limitations for term in
                       ("does not include", "does not provide", "underlying", "unavailable"))
    resolvable_conflict = bool(conflicts) and not any(term in " ".join(conflicts).lower()
                                                         for term in ("without enough context", "does not provide",
                                                                      "unavailable", "not supplied"))
    confidence = evidence["confidence"]
    components = {
        "candidate": round(.18 * candidate, 2),
        "quant": round(.08 * quant, 2),
        "fundamental": round(.08 * fundamental, 2),
        "materiality": 20 if material else min(12, 4 * len(high)),
        "coverage": round(.10 * evidence["coverage_pct"], 2),
        "evidence_confidence": {"HIGH": 8, "MEDIUM": 5, "LOW": 1}.get(confidence, 0),
        "initial_failure_reason": 8 if resolvable_conflict else 2 if critical_gap else 5,
        "escalation": min(10, 2 * len(escalation.get("hard_triggers", [])) +
                          escalation.get("score", 0) / 20),
        "candidate_rank": round(5 * max(0, 31 - rank) / 30, 2),
        "potential_portfolio_relevance": round(5 / max(1, sector_count), 2),
    }
    score = round(sum(components.values()), 2)
    # A stronger model cannot invent missing filings or resolve a contradiction
    # that the compressed packet itself does not explain.
    p1_ready = (score >= 68 and evidence["coverage_pct"] >= 60 and
                confidence in {"MEDIUM", "HIGH"} and
                (bool(material) or (resolvable_conflict and high_with_facts >= 2)) and
                not critical_gap)
    if p1_ready:
        group, reason = "P1_DEEP_NOW", "high-priority facts support a bounded Deep synthesis"
    elif (score >= 45 and evidence["coverage_pct"] >= 60 and rank <= 20 and
          len(high) >= 2 and high_with_facts >= 1):
        group, reason = "P2_DEEP_LATER", "some high-value evidence; refresh missing context before Sol"
    else:
        group, reason = "P3_KEEP_PENDING", "weak factual basis or lower portfolio priority; wait for evidence"
    return score, components, group, reason, {"material_events": len(material),
            "high_events": len(high), "high_events_with_usable_facts": high_with_facts,
            "evidence_conflicts": len(conflicts), "critical_fact_gap": critical_gap,
            "resolvable_conflict": resolvable_conflict}


def main():
    cfg = json.loads((ROOT / "phase4_config.json").read_text())
    if cfg["paid_ai_enabled"] or cfg["web_search_escalation_enabled"]:
        raise ValueError("PAID_RESEARCH_MUST_REMAIN_DISABLED")
    base_plan = json.loads(DAY01_PLAN.read_text())
    with sqlite3.connect(DB) as db, \
         sqlite3.connect(f"file:{ROOT / 'data' / 'fundamental_data.db'}?mode=ro", uri=True) as fundamental:
        db.row_factory = sqlite3.Row
        fundamental.row_factory = sqlite3.Row
        db.executescript(SCHEMA)
        tickers = [case["ticker"] for case in base_plan["cases"]]
        if len(tickers) != 15 or set(PENDING) - set(tickers):
            raise ValueError("FROZEN_TOP15_CHANGED")
        audit = audit_hhh(db, cfg, tickers)
        for case in base_plan["cases"]:
            inputs = cached_inputs(db, case["ticker"], cfg, allowed_tickers=tickers,
                                   citation_mode="short")
            if inputs["evidence_asof"] != case["evidence_as_of"]:
                raise ValueError("FROZEN_EVIDENCE_CHANGED")
            with db:
                for short, canonical in inputs["citation_aliases"].items():
                    db.execute("""INSERT OR REPLACE INTO day02_citation_aliases VALUES(?,?,?,?,?)""",
                               (case["ticker"], case["evidence_as_of"], case["evidence_hash"],
                                short, canonical))
        current = result_report(db, preflight(db))
        with db:
            db.executemany("""UPDATE day01_states SET research_status=?,final_status=?
                WHERE run_id=? AND ticker=?""",
                [(c["research_status"], c["final_status"], RUN, c["ticker"])
                 for c in current["cases"]])
        DAY01_REPORT.write_text(json.dumps(current, ensure_ascii=False, indent=2) + "\n")
        pending = [c for c in current["cases"] if c["final_status"] == "DEEP_RESEARCH_PENDING"]
        if set(PENDING) - {c["ticker"] for c in pending} or (
                not audit["deterministic_repair"] and len(pending) != len(PENDING)):
            raise ValueError("DEEP_PENDING_SET_CHANGED")
        sector_counts = {}
        for ticker in tickers:
            packet = json.loads(db.execute("SELECT packet_json FROM research_priority WHERE run_id=9 AND ticker=?",
                                           (ticker,)).fetchone()[0])
            sector = packet["sector"] or "UNKNOWN"
            sector_counts[sector] = sector_counts.get(sector, 0) + 1
        rows = []
        for case in pending:
            ticker = case["ticker"]
            packet = json.loads(db.execute("SELECT packet_json FROM research_priority WHERE run_id=9 AND ticker=?",
                                           (ticker,)).fetchone()[0])
            scores = fundamental.execute("""SELECT candidate_rank,quant_score,fundamental_score
                FROM fundamental_scores WHERE run_id=4 AND ticker=?""", (ticker,)).fetchone()
            if not scores:
                raise ValueError(f"{ticker}: FROZEN_CANDIDATE_MISSING")
            evidence = json.loads(db.execute("""SELECT packet_json FROM evidence_packets_v2
                WHERE ticker=? AND evidence_asof=? ORDER BY created_at DESC LIMIT 1""",
                (ticker, case["evidence_as_of"])).fetchone()[0])
            parsed = json.loads(db.execute("SELECT parsed_result_json FROM day01_attempts WHERE run_id=? AND ticker=?",
                                           (RUN, ticker)).fetchone()[0])
            score, components, group, reason, factors = score_case(
                case["current_candidate_score"], scores["quant_score"], scores["fundamental_score"],
                scores["candidate_rank"], sector_counts[packet["sector"] or "UNKNOWN"], evidence,
                parsed, case["escalation"] or {})
            row = {"ticker": ticker, "deep_priority_score": score, "group": group,
                   "reason": reason, "components": components, "factors": factors,
                   "candidate_rank": scores["candidate_rank"],
                   "candidate_score": case["current_candidate_score"],
                   "quant_score": scores["quant_score"],
                   "fundamental_score": scores["fundamental_score"],
                   "sector": packet["sector"], "evidence_coverage": evidence["coverage_pct"],
                   "evidence_confidence": evidence["confidence"],
                   "initial_research_failure_reason": parsed.get("evidence_limitations", []),
                   "escalation_reasons": (case["escalation"] or {}).get("reasons", [])}
            if group == "P1_DEEP_NOW":
                sol_cfg = {**cfg, "model": MODEL, "model_input_usd_per_million": 2.0,
                           "model_output_usd_per_million": 10.0}
                inputs = cached_inputs(db, ticker, sol_cfg, allowed_tickers=tickers,
                                       citation_mode="short")
                forecast = estimate(request_body(inputs, sol_cfg), sol_cfg)
                row.update({"estimated_input": forecast["estimated_input_tokens"],
                            "estimated_output": forecast["estimated_output_tokens"],
                            "estimated_sol_cost_usd": round((forecast["estimated_input_tokens"] * 2.5 +
                                                              forecast["estimated_output_tokens"] * 10) / 1_000_000, 6),
                            "web_search": "OFF"})
            rows.append(row)
        rows.sort(key=lambda x: (-x["deep_priority_score"], x["ticker"]))
        selected = [row for row in rows if row["group"] == "P1_DEEP_NOW"]
        if len(selected) > 3:
            for row in selected[3:]:
                row["group"] = "P2_DEEP_LATER"
                row["reason"] = "P1 maximum of three; not authorized for immediate Sol"
                for field in ("estimated_input", "estimated_output", "estimated_sol_cost_usd", "web_search"):
                    row.pop(field, None)
            selected = selected[:3]
        initial = db.execute("SELECT COUNT(*) FROM day01_attempts WHERE run_id=?", (RUN,)).fetchone()[0]
        executed = db.execute("""SELECT COUNT(*) FROM day02_deep_executions
            WHERE run_id=? AND status='COMPLETE'""", (RUN,)).fetchone()[0]
        metrics = {"initial_research_count": initial, "deep_pending_count": len(pending),
                   "deep_executed_count": executed,
                   "deep_escalation_rate_pct": round(100 * executed / initial, 2) if initial else 0,
                   "pending_to_initial_pct": round(100 * len(pending) / initial, 2) if initial else 0,
                   "high_escalation_rate": bool(initial and executed / initial > .35),
                   "high_pending_pressure": bool(initial and len(pending) / initial > .35)}
        with db:
            db.execute("""INSERT OR REPLACE INTO day02_deep_metrics VALUES(?,?,?,?,?,?,?)""",
                       (RUN, initial, len(pending), executed, metrics["deep_escalation_rate_pct"],
                        int(metrics["high_escalation_rate"]), utc_now()))
        output = {"status": "DAY0_2_PLAN_READY", "generated_at": utc_now(),
                  "source_run": RUN, "market_data_as_of": "2026-09-25",
                  "grounding_audit": audit,
                  "citation_schema": {"format": "E1/E2 per frozen packet",
                      "mapping_table": "day02_citation_aliases", "canonical_ids_preserved": True},
                  "deep_priority": rows,
                  "groups": {name: [r["ticker"] for r in rows if r["group"] == name]
                             for name in ("P1_DEEP_NOW", "P2_DEEP_LATER", "P3_KEEP_PENDING")},
                  "p1_estimated_calls": len(selected),
                  "p1_estimated_input_tokens": sum(r["estimated_input"] for r in selected),
                  "p1_estimated_output_tokens": sum(r["estimated_output"] for r in selected),
                  "p1_estimated_sol_cost_usd": round(sum(r["estimated_sol_cost_usd"] for r in selected), 6),
                  "cost_basis": "GPT-6 Sol Standard, conservative cache-write input rate $2.50/M; output $10/M",
                  "metrics": metrics, "new_ai_calls": 0, "new_api_cost_usd": 0,
                  "sol_executed": False, "shadow_scheduler_started": False}
    OUTPUT.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n")
    refresh_entry_report()
    print(json.dumps({"status": output["status"], "grounding": audit["research_status"],
                      "groups": output["groups"], "p1_estimated_sol_cost_usd":
                      output["p1_estimated_sol_cost_usd"], "metrics": metrics,
                      "new_ai_calls": 0}, indent=2))


if __name__ == "__main__":
    main()

"""Offline, opt-in two-tier Phase 4 policy. This module has no API client."""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
HORIZONS = (1, 5, 10, 20, 60)


def now():
    return datetime.now(timezone.utc).isoformat()


def load_config(path=ROOT / "phase4_tiers_config.json", environ=None):
    cfg = json.loads(Path(path).read_text())
    env = os.environ if environ is None else environ
    for key in ("INITIAL_RESEARCH_MODEL", "DEEP_RESEARCH_MODEL"):
        if key in env:
            cfg[key.lower()] = env[key].strip() or None
    return cfg


def init_db(db):
    db.executescript((ROOT / "phase4_tiers_schema.sql").read_text())


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode()).hexdigest()


def _handled_evidence_ids(initial):
    handled = set(initial.get("handled_evidence_ids", ()))
    claims = initial.get("claims", {}) or {}
    items = claims if isinstance(claims, list) else [
        claim for group in claims.values() for claim in (group or [])]
    for claim in items:
        handled.update(claim.get("evidence_ids", ()))
    return handled


def _evidence_events(evidence):
    events = list(evidence.get("events", ()))
    packet = evidence.get("packet", {}) or {}
    events.extend(packet.get("recent_material_events", ()))
    events.extend(packet.get("other_events", ()))
    return events


_FORWARD_MODALITY = re.compile(r"\b(propos(?:e|ed|al)|plan(?:s|ned)?|expect(?:s|ed)?|"
                               r"could|would|subject to|not yet|beginning in|target(?:s|ed)?)\b", re.I)
_REALIZED_ASSERTION = re.compile(r"\b(already deployed|has deployed|deployed .*into|completed deployment|"
                                 r"has generated|generated .*revenue|produced .*revenue|"
                                 r"acquisition closed|deal closed|has closed)\b", re.I)
_NEGATED_REALIZATION = re.compile(r"\b(not yet|has not|have not|not completed|not realized|"
                                  r"no realized|not established|does not establish|do not establish|"
                                  r"doesn't establish|don't establish|unverified)\b", re.I)


def _factual_misreads(events, initial):
    claims = initial.get("claims", {}) or {}
    items = claims if isinstance(claims, list) else [
        claim for group in claims.values() for claim in (group or [])]
    misreads = set()
    for event in events:
        event_id = event.get("duplicate_group") or event.get("event_id")
        if not event_id or event.get("materiality", "").upper() not in {"MATERIAL", "HIGH"}:
            continue
        evidence_text = " ".join([str(event.get("headline", "")), str(event.get("title", "")),
                                  *[str(x) for x in event.get("verified_facts", ())]])
        if not _FORWARD_MODALITY.search(evidence_text):
            continue
        for claim in items:
            if event_id not in claim.get("evidence_ids", ()):
                continue
            claim_text = str(claim.get("text", ""))
            if (_REALIZED_ASSERTION.search(claim_text) and
                    not _NEGATED_REALIZATION.search(claim_text)):
                misreads.add(event_id)
    return misreads


def escalation(candidate_score, initial, evidence, cfg, *, source_tier="INITIAL"):
    """All inputs are point-in-time summaries; signal magnitudes are in [0,1]."""
    if source_tier != "INITIAL":
        raise ValueError("escalation evaluation is only valid for INITIAL research")
    if initial is None or initial.get("status") not in {"COMPLETE", "INSUFFICIENT_EVIDENCE"}:
        raise ValueError("INITIAL_UNAVAILABLE is not an automatic Sol escalation")
    if candidate_score is None or not 0 <= candidate_score <= 100:
        raise ValueError("valid 0-100 candidate score required")
    for score in (initial.get("research_score"), initial.get("catalyst_score")):
        if score is not None and not 0 <= score <= 100:
            raise ValueError("research/catalyst scores must be 0-100 when present")
    research = initial.get("research_score")
    catalyst = initial.get("catalyst_score")
    confidence = initial.get("confidence", "UNKNOWN").upper()
    evidence_confidence = evidence.get("confidence", "UNKNOWN").upper()
    signals = {
        "candidate_strength": max(0, (candidate_score - 70) / 30),
        # Missing scores on an insufficient/rejected response add no numeric points;
        # the dedicated hard triggers handle its material-evidence implications.
        "initial_research_strength": max(0, (research - 75) / 25) if research is not None else 0,
        "catalyst_strength": max(0, (catalyst - 70) / 30) if catalyst is not None else 0,
        "low_thesis_confidence": 1 if confidence in {"LOW", "UNKNOWN"} else 0,
        "low_evidence_confidence": 1 if evidence_confidence in {"LOW", "UNKNOWN"} else 0,
        "material_event": bool(evidence.get("material_catalyst")),
        "high_risk_event": bool(evidence.get("material_risk")),
        "evidence_conflict": bool(evidence.get("conflict")),
        "accounting_risk": bool(evidence.get("accounting_risk")),
        "regulatory_risk": bool(evidence.get("regulatory_risk")),
        "earnings_imminent": bool(evidence.get("earnings_imminent")),
        "score_disagreement": min(1, abs(candidate_score - research) / 30) if research is not None else 0,
        "evidence_complexity": min(1, max(0, evidence.get("material_event_count", 0) - 2) / 8),
        "research_uncertainty": bool(initial.get("uncertainty")),
    }
    weights = cfg["escalation_weights"]
    if set(weights) != set(signals) or any(v < 0 for v in weights.values()) or sum(weights.values()) <= 0:
        raise ValueError("invalid escalation weights")
    score = round(100 * sum(weights[k] * float(v) for k, v in signals.items()) / sum(weights.values()), 2)

    # Hard safety triggers supplement (but never alter) the numeric score.
    handled_ids = _handled_evidence_ids(initial)
    material_ids = set(evidence.get("material_event_ids", ()))
    risk_ids = set(evidence.get("high_risk_event_ids", ()))
    events = _evidence_events(evidence)
    risk_types = {"RISK_FACTORS", "LIQUIDITY", "DEBT", "LEGAL_PROCEEDINGS",
                  "MATERIAL_COMMITMENT", "SHARE_ISSUANCE", "SHARE_OFFERING",
                  "FINANCING", "RESTATEMENT", "AUDITOR_CHANGE", "ACCOUNTING_ISSUE",
                  "REGULATORY_EVENT", "FRAUD_INVESTIGATION", "ACCOUNTING_INVESTIGATION"}
    accounting_restatement = bool(evidence.get("accounting_restatement"))
    auditor_issue = bool(evidence.get("auditor_issue"))
    material_regulatory_event = bool(evidence.get("material_regulatory_event"))
    confirmed_investigation = bool(evidence.get("confirmed_fraud_or_accounting_investigation"))
    for event in events:
        event_id = event.get("duplicate_group") or event.get("event_id")
        if not event_id:
            continue
        materiality = str(event.get("materiality", "")).upper()
        event_type = str(event.get("event_type", "")).upper()
        if materiality in {"MATERIAL", "HIGH"}:
            material_ids.add(event_id)
            if event_type in risk_types:
                risk_ids.add(event_id)
        if materiality in {"MATERIAL", "HIGH"} and event_type == "RESTATEMENT":
            accounting_restatement = True
        if materiality in {"MATERIAL", "HIGH"} and event_type == "AUDITOR_CHANGE":
            auditor_issue = True
        if materiality in {"MATERIAL", "HIGH"} and event_type == "REGULATORY_EVENT":
            material_regulatory_event = True
        if event_type in {"FRAUD_INVESTIGATION", "ACCOUNTING_INVESTIGATION"} and event.get("confirmed"):
            confirmed_investigation = True
    uncovered_material = sorted(material_ids - handled_ids)
    uncovered_risk = sorted(risk_ids - handled_ids)
    hard_triggers = []
    if uncovered_material:
        hard_triggers.append("MATERIAL_EVENT_COVERAGE_GAP")
    if uncovered_risk:
        hard_triggers.append("HIGH_RISK_COVERAGE_GAP")
    if (evidence.get("unresolved_conflict") or evidence.get("unresolved_conflicts") or
            initial.get("unresolved_conflicts")):
        hard_triggers.append("EVIDENCE_CONFLICT_UNRESOLVED")
    if accounting_restatement:
        hard_triggers.append("ACCOUNTING_RESTATEMENT")
    if auditor_issue:
        hard_triggers.append("AUDITOR_ISSUE")
    if material_regulatory_event:
        hard_triggers.append("MATERIAL_REGULATORY_EVENT")
    if confirmed_investigation:
        hard_triggers.append("CONFIRMED_FRAUD_ACCOUNTING_INVESTIGATION")
    if (initial.get("confidence", "UNKNOWN").upper() == "LOW" and
            candidate_score > cfg["hard_escalation_candidate_threshold"]):
        hard_triggers.append("LOW_CONFIDENCE_HIGH_CANDIDATE")
    if (initial.get("research_status") == "INSUFFICIENT_EVIDENCE" and material_ids):
        hard_triggers.append("INSUFFICIENT_WITH_MATERIAL_EVENT")
    misread_ids = sorted((set(evidence.get("misread_material_event_ids", ())) |
                          _factual_misreads(events, initial)) & material_ids)
    if misread_ids:
        hard_triggers.append("MATERIAL_EVENT_FACTUAL_MISREAD")

    reasons = [k for k, v in signals.items() if v >= 0.5] + hard_triggers
    decision = ("ESCALATE_SOL" if score >= cfg["escalation_threshold"] or hard_triggers
                else "NO_ESCALATION")
    return {"score": score, "decision": decision, "reasons": reasons,
            "signals": signals, "hard_triggers": hard_triggers,
            "uncovered_material_event_ids": uncovered_material,
            "uncovered_high_risk_event_ids": uncovered_risk,
            "misread_material_event_ids": misread_ids}


def deep_limitation_reasons(deep, evidence):
    """Return unresolved evidence limitations; this is descriptive, never escalation."""
    handled = _handled_evidence_ids(deep)
    material_ids = set(evidence.get("material_event_ids", ()))
    risk_ids = set(evidence.get("high_risk_event_ids", ()))
    for event in _evidence_events(evidence):
        event_id = event.get("duplicate_group") or event.get("event_id")
        if not event_id:
            continue
        materiality = str(event.get("materiality", "")).upper()
        event_type = str(event.get("event_type", "")).upper()
        if materiality in {"MATERIAL", "HIGH"}:
            material_ids.add(event_id)
        if materiality in {"MATERIAL", "HIGH"} and event_type in {
                "RISK_FACTORS", "LIQUIDITY", "DEBT", "LEGAL_PROCEEDINGS",
                "MATERIAL_COMMITMENT", "SHARE_ISSUANCE", "SHARE_OFFERING", "FINANCING",
                "RESTATEMENT", "AUDITOR_CHANGE", "ACCOUNTING_ISSUE", "REGULATORY_EVENT",
                "FRAUD_INVESTIGATION", "ACCOUNTING_INVESTIGATION"}:
            risk_ids.add(event_id)
    reasons = []
    if material_ids - handled:
        reasons.append("MATERIAL_EVENT_COVERAGE_GAP")
    if risk_ids - handled:
        reasons.append("HIGH_RISK_COVERAGE_GAP")
    if (evidence.get("unresolved_conflict") or evidence.get("unresolved_conflicts") or
            deep.get("unresolved_conflicts")):
        reasons.append("EVIDENCE_CONFLICT_UNRESOLVED")
    if deep.get("research_status") == "INSUFFICIENT_EVIDENCE" or deep.get("insufficient_evidence"):
        reasons.append("INSUFFICIENT_EVIDENCE")
    if deep.get("high_risk_uncertainty"):
        reasons.append("HIGH_RISK_UNCERTAINTY")
    return sorted(set(reasons))


def research_cache_decision(existing, *, material_new_evidence, candidate_score_change,
                            threshold=5):
    """No paid call for valid unchanged research; otherwise request an Initial refresh."""
    if not existing or existing.get("status") != "COMPLETE":
        return "INITIAL_RESEARCH"
    if not material_new_evidence and candidate_score_change is not None and abs(candidate_score_change) < threshold:
        return "CACHED_RESEARCH"
    return "INITIAL_REFRESH"


def save_result(db, run_id, ticker, tier, result, *, evidence=None):
    """Insert-only tier result; caller must archive the validated complete model response."""
    if tier not in {"INITIAL", "DEEP"} or result.get("status") != "COMPLETE":
        raise ValueError("only complete INITIAL/DEEP results can be stored")
    required = ("model", "research_score", "catalyst_score", "thesis", "confidence",
                "evidence_hash", "input_hash", "input_tokens", "output_tokens", "cost_usd",
                "completed_at", "raw_response")
    if any(result.get(k) is None for k in required):
        raise ValueError("complete result and raw response required")
    if tier == "DEEP":
        if not isinstance(evidence, dict):
            raise ValueError("frozen evidence summary required to record DEEP limitations")
        initial = db.execute("""SELECT 1 FROM tier_research_results
            WHERE run_id=? AND ticker=? AND tier='INITIAL'""", (run_id, ticker)).fetchone()
        decision = db.execute("""SELECT decision FROM tier_escalations
            WHERE run_id=? AND ticker=?""", (run_id, ticker)).fetchone()
        if not initial or not decision or decision[0] != "ESCALATE_SOL":
            raise ValueError("DEEP requires preserved INITIAL and deterministic escalation")
    with db:
        db.execute("""INSERT INTO tier_research_results VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                   (run_id, ticker, tier, result["model"], result["status"],
                    result["research_score"], result["catalyst_score"], result["thesis"],
                    result["confidence"], result["evidence_hash"], result["input_hash"],
                    result["input_tokens"], result["output_tokens"], result["cost_usd"],
                    result["completed_at"], json.dumps(result["raw_response"], sort_keys=True)))
    if tier == "DEEP":
        limitations = {**result, **result.get("parsed_result", {})}
        save_deep_limitations(db, run_id, ticker, limitations, evidence)


def save_escalation(db, run_id, ticker, decision):
    with db:
        db.execute("INSERT INTO tier_escalations VALUES(?,?,?,?,?,?)",
                   (run_id, ticker, decision["score"], decision["decision"],
                   json.dumps(decision["reasons"]), now()))


def save_deep_limitations(db, run_id, ticker, deep, evidence):
    reasons = deep_limitation_reasons(deep, evidence)
    if reasons:
        with db:
            db.execute("INSERT INTO tier_deep_limitations VALUES(?,?,?,?,?)",
                       (run_id, ticker, "DEEP_RESEARCH_LIMITATION",
                        json.dumps(reasons), now()))
    return reasons


def select_final(db, run_id, ticker, deep_failed=False):
    rows = {r[0]: r for r in db.execute("""SELECT tier,status,research_score,catalyst_score,thesis,confidence
        FROM tier_research_results WHERE run_id=? AND ticker=?""", (run_id, ticker))}
    policy = db.execute("SELECT decision FROM tier_escalations WHERE run_id=? AND ticker=?",
                        (run_id, ticker)).fetchone()
    if "INITIAL" not in rows:
        if "DEEP" in rows:
            raise ValueError("deep result without initial requires explicit exception policy")
        tier, source, row, status = "UNAVAILABLE", None, None, "UNAVAILABLE"
    elif policy is None:
        raise ValueError("escalation decision required")
    elif policy[0] == "NO_ESCALATION":
        tier, source, row, status = "INITIAL", "INITIAL", rows["INITIAL"], "COMPLETE"
    elif "DEEP" in rows:
        tier, source, row, status = "DEEP", "DEEP", rows["DEEP"], "COMPLETE"
    elif deep_failed:
        tier, source, row, status = "INITIAL_FALLBACK", "INITIAL", rows["INITIAL"], "COMPLETE"
    else:
        raise ValueError("deep pending; do not finalize before outcome")
    with db:
        db.execute("INSERT INTO tier_final_results VALUES(?,?,?,?,?,?,?,?,?,?)",
                   (run_id, ticker, tier, status, source, *(row[2:6] if row else (None,) * 4), now()))
    return tier


def tier_summary(db, run_id, ticker):
    rows = {r[0].lower(): r for r in db.execute("""SELECT tier,model,research_score,catalyst_score,
        thesis,confidence,input_tokens,output_tokens,cost_usd FROM tier_research_results
        WHERE run_id=? AND ticker=?""", (run_id, ticker))}
    final = db.execute("SELECT * FROM tier_final_results WHERE run_id=? AND ticker=?",
                       (run_id, ticker)).fetchone()
    report = {}
    for tier in ("initial", "deep"):
        row = rows.get(tier)
        for idx, field in enumerate(("model", "research_score", "catalyst_score", "thesis",
                                      "confidence", "input_tokens", "output_tokens", "cost_usd"), 1):
            report[f"{tier}_{field}"] = row[idx] if row else None
        report[f"{tier}_tokens"] = (row[6] + row[7]) if row else None
        report[f"{tier}_cost"] = row[8] if row else None
    report.update({"final_research_tier": final[2] if final else None,
                   "final_research_score": final[5] if final else None,
                   "final_catalyst_score": final[6] if final else None,
                   "final_thesis": final[7] if final else None,
                   "final_confidence": final[8] if final else None})
    report["total_phase4_cost"] = round(sum(report[f"{tier}_cost"] or 0
                                             for tier in ("initial", "deep")), 6)
    limitation = db.execute("SELECT limitation_code,reasons_json FROM tier_deep_limitations "
                            "WHERE run_id=? AND ticker=?", (run_id, ticker)).fetchone()
    report["deep_limitation_code"] = limitation[0] if limitation else None
    report["deep_limitation_reasons"] = json.loads(limitation[1]) if limitation else []
    return report


def cost_report(db, day, cfg):
    rows = db.execute("""SELECT tier,state,estimated_input_tokens,estimated_output_tokens,
        estimated_cost_usd,actual_input_tokens,actual_output_tokens,confirmed_cost_usd
        FROM tier_budget_ledger WHERE usage_day=?""", (day,)).fetchall()
    out = {}
    for tier in ("INITIAL", "DEEP"):
        selected = [r for r in rows if r[0] == tier]
        active = [r for r in selected if r[1] not in {"BUDGET_BLOCKED", "FAILED_NO_REQUEST", "CONFIRMED_NO_CHARGE"}]
        confirmed = sum(r[7] or 0 for r in selected if r[1] in {"COMPLETED", "CONFIRMED_BILLED"})
        reserved = sum(r[4] for r in selected if r[1] in {"RESERVED", "IN_FLIGHT"})
        unknown = sum(r[4] for r in selected if r[1] == "FAILED_UNKNOWN")
        out[tier.lower()] = {"calls": len(active),
                           "input_tokens": sum(r[5] or 0 for r in selected if r[1] == "COMPLETED"),
                           "output_tokens": sum(r[6] or 0 for r in selected if r[1] == "COMPLETED"),
                           "confirmed_cost": round(confirmed, 6), "reserved_cost": round(reserved, 6),
                           "unknown_cost": round(unknown, 6),
                           "safe_used": confirmed + reserved + unknown}
    total = sum(out[t]["safe_used"] for t in ("initial", "deep"))
    cap = cfg["max_daily_total_ai_cost"]
    out["total_confirmed_cost"] = round(sum(out[t]["confirmed_cost"] for t in ("initial", "deep")), 6)
    out["total_unknown_cost"] = round(sum(out[t]["unknown_cost"] for t in ("initial", "deep")), 6)
    out["total_reserved_cost"] = round(sum(out[t]["reserved_cost"] for t in ("initial", "deep")), 6)
    out["total_safe_used"] = round(total, 6)
    out["daily_budget_remaining"] = None if cap is None else round(max(0, cap - total), 6)
    return out


def reserve(db, day, run_id, ticker, tier, estimated_input, estimated_output, estimated_cost, cfg):
    if tier not in {"INITIAL", "DEEP"} or min(estimated_input, estimated_output, estimated_cost) < 0:
        raise ValueError("invalid reservation")
    report = cost_report(db, day, cfg)
    unresolved = db.execute("""SELECT 1 FROM tier_budget_ledger WHERE usage_day=? AND ticker=?
        AND tier=? AND state IN ('RESERVED','IN_FLIGHT','FAILED_UNKNOWN') LIMIT 1""",
        (day, ticker, tier)).fetchone()
    label = tier.lower()
    limits = (cfg[f"max_{label}_calls"], cfg[f"max_daily_{label}_ai_cost"],
              cfg["max_daily_total_ai_cost"])
    allowed = (not unresolved and cfg["enabled"] and cfg[f"{label}_research_model"]
               and all(v is not None for v in limits)
               and report[label]["calls"] < limits[0]
               and report[label]["safe_used"] + estimated_cost <= limits[1]
               and report["total_safe_used"] + estimated_cost <= limits[2])
    with db:
        db.execute("""INSERT INTO tier_budget_ledger(usage_day,run_id,ticker,tier,state,
            estimated_input_tokens,estimated_output_tokens,estimated_cost_usd,created_at)
            VALUES(?,?,?,?,?,?,?,?,?)""", (day, run_id, ticker, tier,
            "RESERVED" if allowed else "BUDGET_BLOCKED", estimated_input,
            estimated_output, estimated_cost, now()))
    return "RESERVED" if allowed else "BUDGET_BLOCKED"


def budget_transition(db, run_id, ticker, tier, state, *, input_tokens=None,
                      output_tokens=None, cost_usd=None, request_id=None, evidence=None):
    row = db.execute("SELECT state,request_id FROM tier_budget_ledger WHERE run_id=? AND ticker=? AND tier=?",
                     (run_id, ticker, tier)).fetchone()
    transitions = {"RESERVED": {"IN_FLIGHT", "FAILED_NO_REQUEST"},
                   "IN_FLIGHT": {"COMPLETED", "FAILED_UNKNOWN", "FAILED_NO_REQUEST"},
                   "FAILED_UNKNOWN": {"CONFIRMED_BILLED", "CONFIRMED_NO_CHARGE"}}
    if not row or state not in transitions.get(row[0], set()):
        raise ValueError("invalid budget transition")
    if state == "COMPLETED" and (input_tokens is None or output_tokens is None or cost_usd is None):
        raise ValueError("completed usage and cost required")
    if state == "CONFIRMED_BILLED" and cost_usd is None:
        raise ValueError("reconciled billed amount required")
    if state == "CONFIRMED_NO_CHARGE" and cost_usd != 0:
        raise ValueError("verified zero charge required")
    if state in {"CONFIRMED_BILLED", "CONFIRMED_NO_CHARGE"}:
        if not isinstance(evidence, dict) or not evidence.get("reference") or not evidence.get("verified_at"):
            raise ValueError("traceable reconciliation evidence required")
        if evidence.get("source") not in {"provider_request_record", "manual_verified"}:
            raise ValueError("request-level or manually verified evidence required")
        if row[1] and evidence.get("request_id") != row[1]:
            raise ValueError("request ID mismatch")
    with db:
        db.execute("""UPDATE tier_budget_ledger SET state=?,actual_input_tokens=?,
            actual_output_tokens=?,confirmed_cost_usd=?,request_id=COALESCE(?,request_id),reconciliation_json=?
            WHERE run_id=? AND ticker=? AND tier=?""",
            (state, input_tokens, output_tokens, cost_usd, request_id,
             json.dumps(evidence, sort_keys=True) if evidence else None, run_id, ticker, tier))


def validate_ab_case(baseline, trial):
    """Requires exact immutable inputs, no search, different model. No API request."""
    fields = ("ticker", "evidence_hash", "quant_summary_hash", "fundamental_summary_hash",
              "task_hash", "output_schema_hash", "input_hash")
    if any(not baseline.get(k) or baseline.get(k) != trial.get(k) for k in fields):
        raise ValueError("A/B inputs differ or are unarchived")
    if baseline.get("web_search_enabled") is not False or trial.get("web_search_enabled") is not False:
        raise ValueError("A/B Web Search must be OFF")
    if not baseline.get("model") or not trial.get("model") or baseline["model"] == trial["model"]:
        raise ValueError("model must be the only variable")
    for case in (baseline, trial):
        body = case.get("request_body")
        if not isinstance(body, dict) or body.get("model") != case["model"]:
            raise ValueError("archived request body required")
    base_body = {k: v for k, v in baseline["request_body"].items() if k != "model"}
    trial_body = {k: v for k, v in trial["request_body"].items() if k != "model"}
    if base_body != trial_body or fingerprint(base_body) != baseline["input_hash"]:
        raise ValueError("A/B request body differs beyond model")
    return True


def evaluate_ab(response, expected_catalyst_ids, expected_risk_ids, valid_evidence_ids,
                *, evidence_coverage=None):
    """Deterministic ID/claim checks; semantic quality remains human-reviewed."""
    claims = response.get("claims", [])
    cited = [eid for claim in claims for eid in claim.get("evidence_ids", [])]
    detected_catalysts = {eid for claim in claims if claim.get("kind") == "KEY_CATALYST"
                          for eid in claim.get("evidence_ids", [])}
    detected_risks = {eid for claim in claims if claim.get("kind") == "KEY_RISK"
                      for eid in claim.get("evidence_ids", [])}
    recall = lambda found, expected: None if not expected else len(found & set(expected)) / len(expected)
    unsupported = sum(not claim.get("evidence_ids") or
                      not set(claim["evidence_ids"]) <= set(valid_evidence_ids) for claim in claims)
    confidence = str(response.get("thesis_confidence", "")).upper()
    return {"material_catalyst_recall": recall(detected_catalysts, expected_catalyst_ids),
            "material_risk_recall": recall(detected_risks, expected_risk_ids),
            "evidence_grounding": None if not claims else (len(claims) - unsupported) / len(claims),
            "evidence_citation_accuracy": None if not cited else
                sum(eid in valid_evidence_ids for eid in cited) / len(cited),
            "unsupported_claim_rate": None if not claims else unsupported / len(claims),
            "thesis_consistency": "HUMAN_REVIEW_REQUIRED",
            "uncertainty_handling": (None if evidence_coverage is None else
                confidence != "HIGH" or evidence_coverage >= 0.8),
            "bull_bear_thesis_quality": "HUMAN_REVIEW_REQUIRED"}


def compare_ab(baseline_metrics, trial_metrics):
    def ratio(key):
        denom = baseline_metrics[key]
        return trial_metrics[key] / denom if denom else None
    return {"cost_ratio": ratio("cost_usd"), "latency_ratio": ratio("latency_seconds"),
            "baseline": baseline_metrics, "trial": trial_metrics}


def register_forward(market_db, research_db, run_id, ticker, tier, completed_at):
    """Anchor to first common close on/after research date, never an earlier scan close."""
    if tier not in {"INITIAL", "DEEP", "FINAL"}:
        raise ValueError("invalid forward tier")
    if tier == "FINAL":
        result = research_db.execute("""SELECT final_research_score,source_tier FROM tier_final_results
            WHERE run_id=? AND ticker=?""", (run_id, ticker)).fetchone()
        source = result[1] if result else None
    else:
        result = research_db.execute("""SELECT research_score,completed_at FROM tier_research_results
            WHERE run_id=? AND ticker=? AND tier=?""", (run_id, ticker, tier)).fetchone()
        source = tier
    if not result or result[0] is None:
        return False
    actual_completed = (research_db.execute("""SELECT completed_at FROM tier_research_results
        WHERE run_id=? AND ticker=? AND tier=?""", (run_id, ticker, source)).fetchone() or (None,))[0]
    if completed_at != actual_completed:
        raise ValueError("forward anchor must use stored research completion time")
    completed = datetime.fromisoformat(completed_at.replace("Z", "+00:00"))
    if completed.tzinfo is None:
        raise ValueError("timezone-aware completion time required")
    eastern = completed.astimezone(ZoneInfo("America/New_York"))
    research_day = eastern.date().isoformat()
    after_close = eastern.hour >= 16
    days = market_db.execute("""SELECT date FROM daily_bars WHERE ticker='SPY' AND date>=?
        ORDER BY date""", (research_day,)).fetchall()
    for (day,) in days:
        if after_close and day == research_day:
            continue
        bars = {row[0]: row[1] for row in market_db.execute("""SELECT ticker,close FROM daily_bars
            WHERE ticker IN (?,?,?) AND date=?""", (ticker, "SPY", "QQQ", day))}
        if len(bars) != len({ticker, "SPY", "QQQ"}) or any(v is None or v <= 0 for v in bars.values()):
            continue
        with research_db:
            research_db.execute("""INSERT INTO tier_forward_snapshots VALUES(?,?,?,?,?,?,?,?,?)""",
                (run_id, ticker, tier, completed_at, day, result[0], bars[ticker], bars["SPY"], bars["QQQ"]))
        return True
    return False


def update_forward(market_db, research_db, horizons=HORIZONS):
    """Only observed future trading sessions count; missing bars keep a horizon pending."""
    if any(h not in HORIZONS for h in horizons):
        raise ValueError("unsupported horizon")
    inserted = 0
    snapshots = research_db.execute("SELECT * FROM tier_forward_snapshots").fetchall()
    for snap in snapshots:
        run_id, ticker, tier, _, anchor, _, stock_base, spy_base, qqq_base = snap
        sessions = [r[0] for r in market_db.execute("""SELECT date FROM daily_bars
            WHERE ticker='SPY' AND date>? ORDER BY date LIMIT 60""", (anchor,))]
        for horizon in horizons:
            if len(sessions) < horizon or research_db.execute("""SELECT 1 FROM tier_forward_observations
                WHERE run_id=? AND ticker=? AND tier=? AND horizon=?""",
                (run_id, ticker, tier, horizon)).fetchone():
                continue
            day = sessions[horizon - 1]
            marks = ",".join("?" for _ in sessions[:horizon])
            rows = market_db.execute(f"""SELECT ticker,date,close FROM daily_bars
                WHERE ticker IN (?,?,?) AND date IN ({marks})""",
                (ticker, "SPY", "QQQ", *sessions[:horizon])).fetchall()
            by_symbol = {(r[0], r[1]): r[2] for r in rows if r[2] is not None and r[2] > 0}
            if any((symbol, date) not in by_symbol for symbol in {ticker, "SPY", "QQQ"}
                   for date in sessions[:horizon]):
                continue
            stock_return = by_symbol[ticker, day] / stock_base - 1
            spy_return = by_symbol["SPY", day] / spy_base - 1
            qqq_return = by_symbol["QQQ", day] / qqq_base - 1
            with research_db:
                research_db.execute("""INSERT INTO tier_forward_observations VALUES(?,?,?,?,?,?,?,?)""",
                    (run_id, ticker, tier, horizon, day, stock_return,
                     stock_return - spy_return, stock_return - qqq_return))
            inserted += 1
    return inserted

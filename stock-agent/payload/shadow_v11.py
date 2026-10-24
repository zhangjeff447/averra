#!/usr/bin/env python3
"""Offline Shadow V1.1 research eligibility and Entry quality policy."""
from __future__ import annotations

import json
import math
from pathlib import Path

from phase5_entry import aggregate, session_vwap
from quant_screen import ROOT

CONFIG = ROOT / "shadow_v11_config.json"
FATAL_TYPES = {"RESTATEMENT", "AUDITOR_CHANGE", "ACCOUNTING_ISSUE",
               "REGULATORY_EVENT", "FRAUD_INVESTIGATION",
               "ACCOUNTING_INVESTIGATION"}
RISK_TYPES = {"RISK_FACTORS", "LIQUIDITY", "DEBT", "LEGAL_PROCEEDINGS",
              "MATERIAL_COMMITMENT", "SHARE_ISSUANCE", "SHARE_OFFERING",
              "FINANCING", *FATAL_TYPES}


def load_config(path=CONFIG):
    cfg = json.loads(Path(path).read_text())
    if not (0 < cfg["limited_research_penalty"] < 1 and
            0 < cfg["limited_max_conviction"] < 100 and
            0 < cfg["limited_position_size_fraction_of_full"] < 1 and
            cfg["limited_buy_tomorrow_threshold_add"] > 0 and
            0 < cfg["minimum_limited_evidence_coverage_pct"] <= 100):
        raise ValueError("INVALID_SHADOW_V11_POLICY")
    return cfg


def classify_research(candidate, evidence, initial, escalation, cfg=None):
    """No model calls: fail closed on material gaps; never invent Research Score."""
    cfg = cfg or load_config()
    reasons = []
    quant = candidate.get("quant_score")
    fundamental = candidate.get("fundamental_score")
    if any(v is None or not isinstance(v, (int, float)) or not math.isfinite(v)
           for v in (quant, fundamental, candidate.get("candidate_score"))):
        return _result("RESEARCH_FAILED", ["INVALID_QUANT_OR_FUNDAMENTAL"], None, None)
    status = initial.get("research_status")
    if candidate.get("grounding_failure") and status != "COMPLETE":
        return _result("RESEARCH_FAILED", ["UNRESOLVED_GROUNDING_FAILURE"], None, None)
    if initial.get("error_class") in {"PARSE_FAILED", "GROUNDING_FAILURE", "NO_MATCH"}:
        return _result("RESEARCH_FAILED", ["INITIAL_RESULT_INVALID"], None, None)
    if candidate.get("confirmed_material_negative_override"):
        reasons.append("CONFIRMED_MATERIAL_NEGATIVE_OVERRIDE")
    events = evidence.get("recent_material_events", []) + evidence.get("other_events", [])
    handled = set(initial.get("handled_evidence_ids") or [])
    for event in events:
        materiality = event.get("materiality", "").upper()
        kind = event.get("event_type", "").upper()
        event_id = event.get("duplicate_group") or event.get("event_id")
        unresolved = event_id not in handled
        if unresolved and materiality == "MATERIAL":
            reasons.append("MATERIAL_EVENT_UNRESOLVED")
        if unresolved and materiality in {"HIGH", "MATERIAL"} and kind in RISK_TYPES:
            reasons.append("HIGH_OR_MATERIAL_RISK_UNRESOLVED")
        if unresolved and materiality in {"HIGH", "MATERIAL"} and kind in FATAL_TYPES:
            reasons.append(kind + "_UNRESOLVED")
        if kind == "REGULATORY_EVENT" and materiality in {"HIGH", "MATERIAL"}:
            reasons.append("MATERIAL_REGULATORY_EVENT")
    if initial.get("evidence_conflicts"):
        reasons.append("EVIDENCE_CONFLICT_UNRESOLVED")
    hard = set((escalation or {}).get("hard_triggers", []))
    for trigger in hard & {"ACCOUNTING_RESTATEMENT", "AUDITOR_ISSUE",
                           "MATERIAL_REGULATORY_EVENT", "CONFIRMED_FRAUD_ACCOUNTING_INVESTIGATION",
                           "EVIDENCE_CONFLICT_UNRESOLVED", "HIGH_RISK_COVERAGE_GAP"}:
        reasons.append(trigger)
    if any("MISREAD" in trigger or "GROUNDING" in trigger for trigger in hard):
        reasons.append("MATERIAL_FACTUAL_OR_GROUNDING_ERROR")
    if reasons:
        return _result("DEEP_REQUIRED", sorted(set(reasons)), None, None)
    if status == "COMPLETE" and (escalation or {}).get("decision") != "ESCALATE_SOL":
        return _result("FINAL_RESEARCH_AVAILABLE", ["VALID_FULL_RESEARCH"],
                       initial.get("research_score"), initial.get("conviction_score"))
    coverage = evidence.get("coverage_pct")
    confidence = evidence.get("confidence")
    if (coverage is None or coverage < cfg["minimum_limited_evidence_coverage_pct"] or
            confidence not in {"MEDIUM", "HIGH"} or
            evidence.get("coverage", {}).get("SEC_CONTENT") != "AVAILABLE" or
            status not in {"COMPLETE", "INSUFFICIENT_EVIDENCE"}):
        return _result("RESEARCH_FAILED", ["LIMITED_RESEARCH_MINIMUM_NOT_MET"], None, None)
    effective = min(cfg["limited_max_conviction"],
                    candidate["candidate_score"] * cfg["limited_research_penalty"])
    return _result("RESEARCH_LIMITED", ["NON_FATAL_EVIDENCE_GAP",
                                        "LIMITED_RESEARCH_CONFIDENCE"],
                   None, round(effective, 2))


def _result(state, reasons, research_score, conviction):
    return {"eligibility": state,
            "research_quality_tier": ("FULL_RESEARCH" if state == "FINAL_RESEARCH_AVAILABLE" else
                                      "LIMITED_RESEARCH" if state == "RESEARCH_LIMITED" else None),
            "research_score": research_score, "effective_conviction": conviction,
            "reasons": reasons, "entry_observation_allowed": state in
            {"FINAL_RESEARCH_AVAILABLE", "RESEARCH_LIMITED"}}


def entry_data_quality(daily, intraday, asof, entry_cfg, policy=None):
    policy = policy or load_config()
    counts = {name: len(bars) for name, bars in
              (("daily", daily), ("5m", intraday),
               ("15m", aggregate(intraday, "15m")),
               ("1h", aggregate(intraday, "1h")))}
    requirements = {"daily": entry_cfg["minimum_bars"]["daily"],
                    "5m": max(entry_cfg["minimum_bars"]["5m"],
                              policy["minimum_current_5m_bars"]),
                    "15m": max(entry_cfg["minimum_bars"]["15m"],
                               policy["required_full_15m_bars"]),
                    "1h": max(entry_cfg["minimum_bars"]["1h"],
                              policy["required_full_1h_bars"])}
    problems = [name.upper() + "_BARS_INSUFFICIENT" for name in requirements
                if counts[name] < requirements[name]]
    current = [b for b in intraday if b["date"] == asof]
    if len(current) < policy["minimum_current_5m_bars"] or not current or current[-1]["minute"] != 955:
        problems.append("CURRENT_5M_SESSION_INCOMPLETE")
    if any(not bars or bars[-1]["date"] != asof for bars in
           (daily, intraday, aggregate(intraday, "15m"), aggregate(intraday, "1h"))):
        problems.append("STALE_BARS")
    if not current or not all(math.isfinite(float(b["volume"])) and b["volume"] >= 0 for b in current):
        problems.append("VOLUME_INVALID")
    if session_vwap(intraday, asof) is None:
        problems.append("VWAP_UNAVAILABLE")
    return {"status": "READY" if not problems else "ENTRY_DATA_INSUFFICIENT",
            "reasons": sorted(set(problems)), "bar_counts": counts}


def apply_limited_entry(result, eligibility, entry_cfg, policy=None):
    policy = policy or load_config()
    if eligibility["eligibility"] != "RESEARCH_LIMITED":
        return result
    result = {**result, "research_quality_tier": "LIMITED_RESEARCH",
              "research_confidence": "LIMITED_RESEARCH_CONFIDENCE"}
    threshold = entry_cfg["thresholds"]["entry_buy_tomorrow"] + \
        policy["limited_buy_tomorrow_threshold_add"]
    if result["entry_state"] == "BUY_TOMORROW_CANDIDATE" and \
            (result.get("entry_score") is None or result["entry_score"] < threshold):
        result["entry_state"] = "WATCH"
        result["why_wait"] = [*result.get("why_wait", []),
                              "LIMITED_RESEARCH_CONFIDENCE: stricter BUY threshold"]
    return result

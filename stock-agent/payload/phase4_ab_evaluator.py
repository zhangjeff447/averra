"""Deterministic, evidence-ID based rubric for the frozen Luna/Sol comparison."""
from __future__ import annotations

import re

CATALYSTS = {
    "META": [{"data_center_venture": {"META:2026-07-28:STRATEGIC_PARTNERSHIP:meta-announces-new-strategic-venture-with-blackrock-to-develop-data-center-in-el"}}],
    "CDNA": [
        {"Eurobio_Naveris_transactions": {"CDNA:2026-07-07:ACQUISITION:0001217234-26-000038"}},
        {"earnings_guidance": {"CDNA:2026-07-30:EARNINGS"}},
        {"filing_amendment": {"CDNA:2026-09-16:FILING_AMENDMENT:0001217234-26-000052"}},
    ],
    "AMD": [
        {"earnings_guidance": {"AMD:2026-08-04:EARNINGS"}},
        {"Microsoft": {"AMD:2026-07-20:STRATEGIC_PARTNERSHIP:microsoft-to-deploy-next-gen-amd-instinct-and-amd-epyc-processors-as-the-compani"}},
        {"Anthropic": {"AMD:2026-07-22:STRATEGIC_PARTNERSHIP:amd-and-anthropic-announce-strategic-partnership-to-deploy-up-to-2-gigawatts-of-"}},
        {"Taalas": {"AMD:2026-08-06:ACQUISITION:amd-acquires-taalas-to-advance-compute-solutions-for-rapidly-growing-ai-inferenc"}},
    ],
}
RISKS = {
    "META": [{"execution_and_returns": {
        "META:2026-07-28:STRATEGIC_PARTNERSHIP:meta-announces-new-strategic-venture-with-blackrock-to-develop-data-center-in-el",
        "META:2026-07-30:RISK_FACTORS:0001628280-26-050705"}}],
    "CDNA": [
        {"acquisition_integration": {"CDNA:2026-07-07:ACQUISITION:0001217234-26-000038"}},
        {"cashflow_and_profitability": {"CDNA:2026-07-30:LIQUIDITY:0001217234-26-000046",
                                          "CDNA:2026-07-30:RISK_FACTORS:0001217234-26-000046"}},
    ],
    "AMD": [
        {"deployment_and_delivery": {
            "AMD:2026-07-20:STRATEGIC_PARTNERSHIP:microsoft-to-deploy-next-gen-amd-instinct-and-amd-epyc-processors-as-the-compani",
            "AMD:2026-07-22:STRATEGIC_PARTNERSHIP:amd-and-anthropic-announce-strategic-partnership-to-deploy-up-to-2-gigawatts-of-"}},
        {"commitments_and_financing": {"AMD:2026-08-05:MATERIAL_COMMITMENT:0000002488-26-000123",
                                         "AMD:2026-08-05:DEBT:0000002488-26-000123",
                                         "AMD:2026-08-17:MAJOR_CONTRACT:0001193125-26-354029"}},
        {"demand_and_guidance": {"AMD:2026-08-04:EARNINGS"}},
    ],
}
WEIGHTS = {"material_catalyst_recall": .25, "material_risk_recall": .25,
           "evidence_grounding": .20, "citation_accuracy": .10,
           "uncertainty_handling": .10, "thesis_quality": .10}
CORE = ("BULL_CASE", "BEAR_CASE", "KEY_CATALYST", "KEY_RISK", "THESIS_INVALIDATION")
QUALIFIERS = re.compile(r"\b(announced|proposed|planned|expects?|expected|guidance|could|may|would|"
                        r"not yet|not established|subject to|prospective|beginning in|projected)\b", re.I)
REALIZED_ASSERTIONS = re.compile(r"\b(already deployed|has deployed|completed deployment|generated .*revenue|"
                                 r"produced .*revenue|has generated|is generating .*revenue|"
                                 r"acquisition closed|deal closed|has closed)\b", re.I)
SPECIFIC_TRIGGER = re.compile(r"\b(if|failure|fails?|miss(?:es|ed)?|below|does not|did not|unless|"
                              r"falls?|not achieve|not begin|not reach)\b", re.I)
FORWARD_ONLY_IDS = {
    "META:2026-07-28:STRATEGIC_PARTNERSHIP:meta-announces-new-strategic-venture-with-blackrock-to-develop-data-center-in-el",
    "AMD:2026-07-22:STRATEGIC_PARTNERSHIP:amd-and-anthropic-announce-strategic-partnership-to-deploy-up-to-2-gigawatts-of-",
    "AMD:2026-08-06:ACQUISITION:amd-acquires-taalas-to-advance-compute-solutions-for-rapidly-growing-ai-inferenc",
}
REALIZATION_SENSITIVE_IDS = FORWARD_ONLY_IDS


def _claims(result):
    claims = result.get("claims", {}) or {}
    if isinstance(claims, list):
        grouped = {}
        for claim in claims:
            grouped.setdefault(claim.get("kind"), []).append(claim)
        claims = grouped
    return claims


def _recall(groups, detected):
    if not groups:
        return 100.0
    return round(100 * sum(bool(set.union(*(set(v) for v in group.values())) & detected)
                           for group in groups) / len(groups), 2)


def _kind_relevant(kind, event_type):
    if kind == "KEY_CATALYST":
        return event_type in {"EARNINGS", "STRATEGIC_PARTNERSHIP", "ACQUISITION", "FILING_AMENDMENT",
                              "MAJOR_CONTRACT", "GUIDANCE", "OFFICIAL_RELEASE", "EARNINGS_SCHEDULE"}
    if kind in {"BEAR_CASE", "KEY_RISK"}:
        return event_type in {"RISK_FACTORS", "LIQUIDITY", "DEBT", "MATERIAL_COMMITMENT", "LEGAL_PROCEEDINGS",
                              "SHARE_ISSUANCE", "ACQUISITION", "EARNINGS", "STRATEGIC_PARTNERSHIP",
                              "MAJOR_CONTRACT", "FILING_AMENDMENT"}
    if kind == "BULL_CASE":
        return event_type in {"EARNINGS", "LIQUIDITY", "ACQUISITION", "STRATEGIC_PARTNERSHIP",
                              "MAJOR_CONTRACT", "OFFICIAL_RELEASE", "GUIDANCE"}
    if kind == "THESIS_INVALIDATION":
        return event_type in {"RISK_FACTORS", "LIQUIDITY", "DEBT", "MATERIAL_COMMITMENT", "LEGAL_PROCEEDINGS",
                              "EARNINGS", "STRATEGIC_PARTNERSHIP", "MAJOR_CONTRACT", "ACQUISITION"}
    return kind == "NEW_INFORMATION"


def evaluate(ticker, result, event_types, valid_ids):
    """Scores observable properties; semantic evidence entailment gets separate explicit flags."""
    claims = _claims(result)
    all_claims = [(kind, claim) for kind, items in claims.items() for claim in items]
    important = [(kind, claim) for kind, claim in all_claims if kind in CORE or kind == "NEW_INFORMATION"]
    cited = [(kind, claim, eid) for kind, claim in important for eid in claim.get("evidence_ids", [])]
    valid = [(kind, claim, eid) for kind, claim, eid in cited if eid in valid_ids]
    # A material fact can be recognized in the bull case, thesis trigger, or new-info
    # section; it need not be mechanically repeated under KEY_CATALYST.
    catalyst_kinds = {"KEY_CATALYST", "NEW_INFORMATION", "BULL_CASE", "THESIS_INVALIDATION"}
    catalyst_ids = {eid for kind in catalyst_kinds for claim in claims.get(kind, [])
                    for eid in claim.get("evidence_ids", [])}
    # Thesis invalidation is an explicit risk statement. A catalyst claim can also
    # establish a risk where it explicitly states the outcome/return is unrealized.
    risk_ids = {eid for kind in ("KEY_RISK", "BEAR_CASE", "THESIS_INVALIDATION")
                for claim in claims.get(kind, []) for eid in claim.get("evidence_ids", [])}
    for claim in claims.get("KEY_CATALYST", []):
        if re.search(r"\b(risk|uncertain|unverified|no realized|not yet|execution|could|may)\b",
                     claim.get("text", ""), re.I):
            risk_ids.update(claim.get("evidence_ids", []))
    catalyst = _recall(CATALYSTS[ticker], catalyst_ids)
    risk = _recall(RISKS[ticker], risk_ids)
    grounding = 100.0 if not important else round(100 * sum(bool(claim.get("evidence_ids")) and
        set(claim.get("evidence_ids", ())) <= valid_ids for _, claim in important) / len(important), 2)
    relevant = sum(_kind_relevant(kind, event_types.get(eid, "UNKNOWN")) for kind, _, eid in valid)
    citation_accuracy = 100.0 if not cited else round(100 * relevant / len(cited), 2)

    hallucinations = []
    forward_claims = []
    properly_qualified = 0
    for kind, claim in important:
        text = claim.get("text", "")
        ids = set(claim.get("evidence_ids", []))
        sensitive = ids & REALIZATION_SENSITIVE_IDS
        if ids & FORWARD_ONLY_IDS:
            forward_claims.append(claim)
            if QUALIFIERS.search(text):
                properly_qualified += 1
        if sensitive and REALIZED_ASSERTIONS.search(text) and not QUALIFIERS.search(text):
            hallucinations.append({"kind": kind, "text": text,
                                   "evidence_ids": sorted(sensitive),
                                   "reason": "realized outcome asserted against forward-looking evidence"})
    invalid_citations = [(kind, eid) for kind, _, eid in cited if eid not in valid_ids]
    for kind, eid in invalid_citations:
        hallucinations.append({"kind": kind, "evidence_ids": [eid], "reason": "citation ID absent from frozen packet"})
    unsupported_claims = sum(not claim.get("evidence_ids") or
                             any(eid not in valid_ids for eid in claim.get("evidence_ids", []))
                             for _, claim in important) + len(hallucinations)
    unsupported_rate = 0.0 if not important else round(100 * min(len(important), unsupported_claims) / len(important), 2)
    uncertainty = (100.0 if not forward_claims else
                   round(100 * properly_qualified / len(forward_claims), 2))

    present = {kind for kind, items in claims.items() if items}
    required_ok = all(kind in present for kind in CORE)
    specific = any(SPECIFIC_TRIGGER.search(c.get("text", "")) for c in claims.get("THESIS_INVALIDATION", []))
    thesis = round(100 * (sum(kind in present for kind in CORE) / len(CORE) * .7 +
                          (0.2 if required_ok and catalyst > 0 and risk > 0 else 0) +
                          (0.1 if specific else 0)), 2)
    scores = {"material_catalyst_recall": catalyst, "material_risk_recall": risk,
              "evidence_grounding": grounding, "citation_accuracy": citation_accuracy,
              "uncertainty_handling": uncertainty, "thesis_quality": thesis,
              "unsupported_claim_rate": unsupported_rate}
    suitability = round(sum(scores[key] * weight for key, weight in WEIGHTS.items()), 2)
    return {**scores, "initial_model_suitability": suitability,
            "major_hallucination": bool(hallucinations), "hallucinations": hallucinations,
            "unsupported_claims": unsupported_claims, "important_claims": len(important),
            "new_information_claims": claims.get("NEW_INFORMATION", []),
            "rubric_note": "ID coverage and explicit modality rules are deterministic; claim-level entailment is not fully machine-verifiable."}


def final_decision(per_ticker):
    avg = round(sum(x["initial_model_suitability"] for x in per_ticker.values()) / len(per_ticker), 2)
    catalyst = round(sum(x["material_catalyst_recall"] for x in per_ticker.values()) / len(per_ticker), 2)
    risk = round(sum(x["material_risk_recall"] for x in per_ticker.values()) / len(per_ticker), 2)
    grounding = round(sum(x["evidence_grounding"] for x in per_ticker.values()) / len(per_ticker), 2)
    passed = (avg >= 80 and catalyst >= 80 and risk >= 80 and grounding >= 90 and
              all(not x["major_hallucination"] for x in per_ticker.values()))
    return {"average_suitability": avg, "average_catalyst_recall": catalyst,
            "average_risk_recall": risk, "average_grounding": grounding,
            "major_hallucination_free": all(not x["major_hallucination"] for x in per_ticker.values()),
            "recommendation": "LUNA_INITIAL_CANDIDATE_PASS" if passed else "LUNA_INITIAL_CANDIDATE_FAIL"}

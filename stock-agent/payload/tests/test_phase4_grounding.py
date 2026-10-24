import json
import unittest

from day0_1_telegram import validate_summary
from day0_2_plan import score_case
from phase4_grounding import short_citation_packet, validate_id
from phase4b_evidence import request_body, validate_response


class GroundingTests(unittest.TestCase):
    def test_strict_id_reasons(self):
        canonical = {"HHH:2026-08-05:RISK_FACTORS:0001628280-26-053324"}
        self.assertEqual(validate_id(next(iter(canonical)), canonical)["reason"], "EXACT_MATCH")
        self.assertEqual(validate_id("hhh:2026/08/05:risk-factors:000162828026053324",
                                     canonical)["reason"], "NORMALIZABLE_MATCH")
        self.assertEqual(validate_id("HHH:2026-06-04:ACQUISITION", canonical)["reason"], "NO_MATCH")
        ambiguous = {"T:2026-08-05:EARNINGS:0001-26-000001",
                     "T:2026-08-05:EARNINGS:000126000001"}
        self.assertEqual(validate_id("T:20260805:EARNINGS:0001-26-000001",
                                     ambiguous)["reason"], "AMBIGUOUS_MATCH")

    def test_short_ids_stay_in_prompt_and_canonicalize_on_parse(self):
        canonical = "TEST:2026-08-05:EARNINGS"
        evidence = {"recent_material_events": [{"duplicate_group": canonical, "event_type": "EARNINGS",
                    "materiality": "HIGH", "verified_facts": ["Results were reported"], "sources": []}],
                    "other_events": [], "recent_earnings": [canonical], "guidance": [],
                    "material_sec_events": [], "key_risk_evidence": []}
        aliased, mapping = short_citation_packet(evidence)
        self.assertEqual(mapping, {"E1": canonical})
        self.assertEqual(aliased["recent_earnings"], ["E1"])
        self.assertEqual(evidence["recent_material_events"][0]["duplicate_group"], canonical)
        cfg = {"model": "gpt-6-sol", "max_output_tokens_per_stock": 1200,
               "conviction_weights": {"candidate": .7, "research": .3}}
        body = request_body({"prompt": {"phase4a_evidence": aliased}, "citation_mode": "short"}, cfg)
        self.assertNotIn(canonical, body["input"])
        self.assertIn("E1", body["input"])
        claims = [{"kind": kind, "text": "Supported", "evidence_ids": ["E1"]} for kind in
                  ("BULL_CASE", "BEAR_CASE", "KEY_CATALYST", "KEY_RISK", "THESIS_INVALIDATION")]
        payload = {"research_status": "COMPLETE", "research_score": 70, "catalyst_score": 60,
                   "thesis_confidence": "MEDIUM", "score_rationale": "Supported",
                   "score_evidence_ids": ["E1"], "catalyst_evidence_ids": ["E1"], "claims": claims,
                   "evidence_conflicts": [], "evidence_limitations": []}
        raw = {"status": "completed", "output": [{"type": "message", "content":
               [{"type": "output_text", "text": json.dumps(payload)}]}]}
        parsed = validate_response(raw, mapping, 75, cfg)
        self.assertEqual(parsed["score_evidence_ids"], [canonical])
        self.assertEqual(parsed["claims"]["KEY_CATALYST"][0]["evidence_ids"], [canonical])
        payload["claims"][0]["evidence_ids"] = ["E9"]
        raw["output"][0]["content"][0]["text"] = json.dumps(payload)
        with self.assertRaises(ValueError):
            validate_response(raw, mapping, 75, cfg)

    def test_missing_fact_context_cannot_force_p1(self):
        evidence = {"confidence": "MEDIUM", "coverage_pct": 80,
                    "recent_material_events": [{"materiality": "HIGH", "event_type": "EARNINGS",
                        "verified_facts": ["release announced"], "extraction_confidence": "HIGH"}],
                    "other_events": []}
        score, _, group, _, _ = score_case(90, 90, 90, 1, 1, evidence,
            {"evidence_limitations": ["Underlying results are unavailable"],
             "evidence_conflicts": []}, {"hard_triggers": ["MATERIAL_EVENT_COVERAGE_GAP"],
                                           "score": 70})
        self.assertLessEqual(score, 100)
        self.assertNotEqual(group, "P1_DEEP_NOW")

    def test_telegram_summary_rejects_misclassified_deep(self):
        cases = [{"ticker": f"T{i}", "final_status": "FINAL_RESEARCH_AVAILABLE",
                  "research_status": "COMPLETE", "escalation": None} for i in range(15)]
        cases[0].update(final_status="INSUFFICIENT_EVIDENCE",
                        research_status="INSUFFICIENT_EVIDENCE",
                        escalation={"decision": "ESCALATE_SOL"})
        report = {"research_cases": cases, "research_counts": {
            "FINAL_RESEARCH_AVAILABLE": 14, "DEEP_RESEARCH_PENDING": 0,
            "INSUFFICIENT_EVIDENCE": 1, "RESEARCH_FAILED": 0}}
        with self.assertRaisesRegex(ValueError, "DEEP_PENDING_MISCLASSIFIED"):
            validate_summary(report, "")


if __name__ == "__main__":
    unittest.main()

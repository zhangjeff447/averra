import json
import unittest
from pathlib import Path

from phase4b_evidence import (CLAIM_KINDS, apply_packet_policy, estimate, request_body,
                              validate_response)

CFG = json.loads((Path(__file__).resolve().parents[1] / "phase4_config.json").read_text())
EVIDENCE_ID = "TEST:2026-09-20:EARNINGS"


def raw_response(payload):
    return {"status": "completed", "output": [{"type": "message", "content": [
        {"type": "output_text", "text": json.dumps(payload)}]}]}


def valid_payload():
    return {"research_status": "COMPLETE", "research_score": 75, "catalyst_score": 80,
            "thesis_confidence": "MEDIUM", "score_rationale": "Official earnings evidence matters.",
            "score_evidence_ids": [EVIDENCE_ID], "catalyst_evidence_ids": [EVIDENCE_ID],
            "claims": [{"kind": kind, "text": kind.lower(), "evidence_ids": [EVIDENCE_ID]}
                       for kind in CLAIM_KINDS],
            "evidence_conflicts": [], "evidence_limitations": ["No analyst consensus source"]}


class Phase4BEvidenceTests(unittest.TestCase):
    def test_request_is_evidence_only_and_search_cost_zero(self):
        inputs = {"prompt": {"ticker": "TEST", "phase4a_evidence": {"events": []}}}
        body = request_body(inputs, CFG)
        prediction = estimate(body, CFG)
        self.assertNotIn("tools", body)
        self.assertEqual(prediction["estimated_search_cost_usd"], 0)
        self.assertEqual(body["text"]["format"]["type"], "json_schema")

    def test_scores_are_evidence_supported_and_conviction_local(self):
        parsed = validate_response(raw_response(valid_payload()), {EVIDENCE_ID}, 70, CFG)
        self.assertEqual(parsed["research_status"], "COMPLETE")
        self.assertEqual(parsed["conviction_score"], 71.5)
        self.assertEqual(parsed["claims"]["KEY_CATALYST"][0]["evidence_ids"], [EVIDENCE_ID])

    def test_unsupported_evidence_id_rejected(self):
        payload = valid_payload()
        payload["claims"][0]["evidence_ids"] = ["INVENTED"]
        with self.assertRaises(ValueError):
            validate_response(raw_response(payload), {EVIDENCE_ID}, 70, CFG)

    def test_incomplete_and_web_search_rejected(self):
        response = raw_response(valid_payload())
        response["status"] = "incomplete"
        with self.assertRaises(ValueError):
            validate_response(response, {EVIDENCE_ID}, 70, CFG)
        response["status"] = "completed"
        response["output"].append({"type": "web_search_call"})
        with self.assertRaises(ValueError):
            validate_response(response, {EVIDENCE_ID}, 70, CFG)

    def test_insufficient_evidence_has_no_score(self):
        payload = valid_payload()
        payload["research_status"] = "INSUFFICIENT_EVIDENCE"
        payload["claims"] = []
        parsed = validate_response(raw_response(payload), {EVIDENCE_ID}, 70, CFG)
        self.assertIsNone(parsed["research_score"])
        self.assertIsNone(parsed["conviction_score"])

    def test_extra_ticker_is_not_permitted_in_cache_loader(self):
        from phase4b_evidence import cached_inputs
        with self.assertRaises(ValueError):
            cached_inputs(None, "NVDA", CFG)

    def test_packet_policy_removes_low_event_but_preserves_material(self):
        material = {"duplicate_group": "TEST:material", "materiality": "MATERIAL",
                    "event_type": "ACQUISITION", "date": "2026-09-20", "filing_date": "2026-09-20",
                    "accepted_at": "2026-09-20T10:00:00Z", "accession": "123",
                    "verified_facts": ["Important " + "x" * 200],
                    "sources": [{"url": "https://www.sec.gov/a", "retrieved_at": "2026-09-20T11:00:00Z"}]}
        low = {**material, "duplicate_group": "TEST:low", "materiality": "LOW",
               "event_type": "OFFICIAL_RELEASE", "verified_facts": ["ordinary " + "y" * 1000]}
        packet = {"ticker": "TEST", "evidence_asof": "2026-09-21T00:00:00Z",
                  "coverage": {}, "coverage_pct": 100, "confidence": "HIGH",
                  "recent_material_events": [material], "other_events": [low],
                  "recent_earnings": [], "guidance": [], "material_sec_events": ["TEST:material"],
                  "key_risk_evidence": [], "unresolved": [], "omitted_low_event_count": 0}
        cfg = {**CFG, "target_evidence_packet_tokens": 200,
               "soft_max_evidence_packet_tokens": 300, "hard_max_evidence_packet_tokens": 400}
        output, policy = apply_packet_policy(packet, {}, cfg)
        self.assertEqual([e["duplicate_group"] for e in output["recent_material_events"]], ["TEST:material"])
        self.assertEqual(policy["removed_low_event_ids"], ["TEST:low"])
        self.assertNotIn("retrieved_at", output["recent_material_events"][0]["sources"][0])
        self.assertIn("retrieved_at", material["sources"][0])

    def test_oversize_material_evidence_is_not_truncated(self):
        material = {"duplicate_group": "TEST:material", "materiality": "MATERIAL",
                    "event_type": "RESTATEMENT", "date": "2026-09-20",
                    "verified_facts": ["z" * 4000, "z" * 4000], "sources": []}
        packet = {"ticker": "TEST", "evidence_asof": "2026-09-21T00:00:00Z",
                  "coverage": {}, "coverage_pct": 100, "confidence": "HIGH",
                  "recent_material_events": [material], "other_events": [],
                  "recent_earnings": [], "guidance": [], "material_sec_events": ["TEST:material"],
                  "key_risk_evidence": ["TEST:material"], "unresolved": [],
                  "omitted_low_event_count": 0}
        cfg = {**CFG, "target_evidence_packet_tokens": 100,
               "soft_max_evidence_packet_tokens": 200, "hard_max_evidence_packet_tokens": 300}
        output, policy = apply_packet_policy(packet, {}, cfg)
        self.assertEqual(policy["status"], "EVIDENCE_PACKET_OVERSIZE")
        self.assertEqual(output["recent_material_events"][0]["verified_facts"], ["z" * 4000])


if __name__ == "__main__":
    unittest.main()

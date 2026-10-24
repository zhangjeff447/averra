import json
import sqlite3
import unittest
from pathlib import Path

from phase4_tiers import (budget_transition, compare_ab, cost_report, escalation, evaluate_ab,
                          fingerprint, init_db, load_config, register_forward, reserve, save_escalation,
                          research_cache_decision, save_result, select_final, tier_summary,
                          update_forward, validate_ab_case)


class TierTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        init_db(self.db)
        self.cfg = load_config(environ={})
        self.candidate = 82
        self.initial = {"status": "COMPLETE", "research_score": 78,
                        "catalyst_score": 74, "confidence": "HIGH"}
        self.evidence = {"confidence": "HIGH"}

    def tearDown(self):
        self.db.close()

    def result(self, model="cheap-test", score=78):
        return {"status": "COMPLETE", "model": model, "research_score": score,
                "catalyst_score": 74, "thesis": "Evidence-grounded thesis",
                "confidence": "HIGH", "evidence_hash": "e", "input_hash": "i",
                "input_tokens": 1000, "output_tokens": 200, "cost_usd": 0.001,
                "completed_at": "2026-09-28T15:00:00+00:00", "raw_response": {"output": "stored"}}

    def test_defaults_are_off_and_model_not_selected(self):
        self.assertFalse(self.cfg["enabled"])
        self.assertEqual(self.cfg["initial_research_model"], "gpt-5.6-luna")
        self.assertEqual(self.cfg["deep_research_model"], "gpt-6-sol")
        self.assertEqual(self.cfg["architecture_status"], "FROZEN_V1")
        self.assertFalse(self.cfg["web_search_enabled"])
        self.assertEqual(self.cfg["deep_research_model"], "gpt-6-sol")
        self.assertEqual(load_config(environ={"INITIAL_RESEARCH_MODEL": "cheap-test"})["initial_research_model"], "cheap-test")

    def test_initial_only_and_no_escalation(self):
        decision = escalation(self.candidate, self.initial, self.evidence, self.cfg)
        self.assertEqual(decision["decision"], "NO_ESCALATION")
        save_result(self.db, "r", "AMD", "INITIAL", self.result())
        save_escalation(self.db, "r", "AMD", decision)
        self.assertEqual(select_final(self.db, "r", "AMD"), "INITIAL")
        summary = tier_summary(self.db, "r", "AMD")
        self.assertEqual(summary["final_research_score"], summary["initial_research_score"])
        self.assertIsNone(summary["deep_model"])

    def test_escalation_and_preservation(self):
        evidence = {"confidence": "LOW", "material_catalyst": True, "material_risk": True,
                    "conflict": True, "accounting_risk": True, "regulatory_risk": True}
        decision = escalation(95, self.initial, evidence, self.cfg)
        self.assertEqual(decision["decision"], "ESCALATE_SOL")
        save_result(self.db, "r", "AMD", "INITIAL", self.result())
        save_escalation(self.db, "r", "AMD", decision)
        with self.assertRaises(ValueError):
            select_final(self.db, "r", "AMD")
        save_result(self.db, "r", "AMD", "DEEP", self.result("gpt-6-sol", 88),
                    evidence={"confidence": "HIGH"})
        self.assertEqual(select_final(self.db, "r", "AMD"), "DEEP")
        summary = tier_summary(self.db, "r", "AMD")
        self.assertEqual((summary["initial_research_score"], summary["deep_research_score"],
                          summary["final_research_score"]), (78, 88, 88))
        self.assertEqual(summary["final_research_tier"], "DEEP")
        self.assertAlmostEqual(summary["total_phase4_cost"], .002)
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute("UPDATE tier_research_results SET research_score=0")

    def test_all_escalation_factors_have_positive_effect(self):
        baseline = escalation(82, self.initial, self.evidence, self.cfg)["score"]
        cases = [({"material_catalyst": True}, None), ({"material_risk": True}, None),
                 ({"conflict": True}, None), ({"accounting_risk": True}, None),
                 ({"regulatory_risk": True}, None), ({"earnings_imminent": True}, None),
                 ({"material_event_count": 10}, None), ({}, {"confidence": "LOW"}),
                 ({}, {"uncertainty": True})]
        for additions, initial_additions in cases:
            with self.subTest(additions=additions, initial=initial_additions):
                initial = {**self.initial, **(initial_additions or {})}
                self.assertGreater(escalation(82, initial, {**self.evidence, **additions},
                                              self.cfg)["score"], baseline)
        self.assertGreater(escalation(30, self.initial, self.evidence, self.cfg)["score"], baseline)
        self.assertGreater(escalation(82, {**self.initial, "confidence": "LOW"},
                                      self.evidence, self.cfg)["score"], baseline)

    def test_material_and_high_risk_coverage_gaps_hard_escalate(self):
        initial = {**self.initial, "claims": {"KEY_CATALYST": [{"evidence_ids": ["covered"]}]}}
        evidence = {**self.evidence, "material_event_ids": ["covered", "missed"],
                    "high_risk_event_ids": ["risk-missed"]}
        result = escalation(82, initial, evidence, self.cfg)
        self.assertEqual(result["score"] < self.cfg["escalation_threshold"], True)
        self.assertEqual(result["decision"], "ESCALATE_SOL")
        self.assertEqual(result["hard_triggers"], ["MATERIAL_EVENT_COVERAGE_GAP", "HIGH_RISK_COVERAGE_GAP"])
        self.assertEqual(result["uncovered_material_event_ids"], ["missed"])
        self.assertEqual(result["uncovered_high_risk_event_ids"], ["risk-missed"])

    def test_covered_material_evidence_does_not_create_gap(self):
        initial = {**self.initial, "claims": {"BULL_CASE": [{"evidence_ids": ["event-1", "risk-1"]}]}}
        evidence = {**self.evidence, "material_event_ids": ["event-1"],
                    "high_risk_event_ids": ["risk-1"]}
        result = escalation(82, initial, evidence, self.cfg)
        self.assertEqual(result["hard_triggers"], [])
        self.assertEqual(result["decision"], "NO_ESCALATION")

    def test_packet_event_ids_drive_coverage_and_issue_triggers(self):
        initial = {**self.initial, "handled_evidence_ids": ["covered"]}
        evidence = {**self.evidence, "packet": {"recent_material_events": [
            {"duplicate_group": "covered", "materiality": "HIGH", "event_type": "EARNINGS"},
            {"duplicate_group": "missed", "materiality": "MATERIAL", "event_type": "ACQUISITION"},
            {"duplicate_group": "risk", "materiality": "HIGH", "event_type": "RESTATEMENT"},
        ]}}
        result = escalation(82, initial, evidence, self.cfg)
        self.assertEqual(result["decision"], "ESCALATE_SOL")
        self.assertEqual(result["uncovered_material_event_ids"], ["missed", "risk"])
        self.assertEqual(result["uncovered_high_risk_event_ids"], ["risk"])
        self.assertIn("ACCOUNTING_RESTATEMENT", result["hard_triggers"])
        self.assertNotIn("accounting_restatement", evidence)

    def test_proposed_event_claimed_as_completed_hard_escalates(self):
        initial = {**self.initial, "claims": {"KEY_CATALYST": [{
            "evidence_ids": ["event-4"], "text": "The company has deployed the product and generated revenue."}]}}
        evidence = {**self.evidence, "packet": {"recent_material_events": [{
            "duplicate_group": "event-4", "materiality": "HIGH", "event_type": "STRATEGIC_PARTNERSHIP",
            "verified_facts": ["The company plans to deploy the product beginning next year."]}]}}
        result = escalation(82, initial, evidence, self.cfg)
        self.assertEqual(result["decision"], "ESCALATE_SOL")
        self.assertEqual(result["hard_triggers"], ["MATERIAL_EVENT_FACTUAL_MISREAD"])
        self.assertEqual(result["misread_material_event_ids"], ["event-4"])

    def test_insufficient_initial_without_scores_can_still_escalate(self):
        initial = {"status": "COMPLETE", "research_status": "INSUFFICIENT_EVIDENCE",
                   "research_score": None, "catalyst_score": None,
                   "confidence": "LOW", "claims": {}}
        result = escalation(79, initial, {**self.evidence, "material_event_ids": ["event-6"]}, self.cfg)
        self.assertEqual(result["decision"], "ESCALATE_SOL")
        self.assertEqual(result["score"], 9.1)
        self.assertIn("MATERIAL_EVENT_COVERAGE_GAP", result["hard_triggers"])
        self.assertIn("INSUFFICIENT_WITH_MATERIAL_EVENT", result["hard_triggers"])

    def test_forward_status_with_explicit_not_yet_is_not_misread(self):
        initial = {**self.initial, "claims": {"KEY_CATALYST": [{
            "evidence_ids": ["event-5"], "text": "The announced deployment has not yet generated revenue."}]}}
        evidence = {**self.evidence, "packet": {"recent_material_events": [{
            "duplicate_group": "event-5", "materiality": "HIGH", "event_type": "STRATEGIC_PARTNERSHIP",
            "verified_facts": ["The company plans to deploy the product."]}]}}
        result = escalation(82, initial, evidence, self.cfg)
        self.assertEqual(result["hard_triggers"], [])

    def test_forward_status_with_does_not_establish_phrase_is_not_misread(self):
        initial = {**self.initial, "claims": {"KEY_CATALYST": [{
            "evidence_ids": ["event-7"], "text": "The excerpt does not establish completed deployment."}]}}
        evidence = {**self.evidence, "packet": {"recent_material_events": [{
            "duplicate_group": "event-7", "materiality": "HIGH", "event_type": "STRATEGIC_PARTNERSHIP",
            "verified_facts": ["The company plans deployment next year."]}]}}
        result = escalation(82, initial, evidence, self.cfg)
        self.assertEqual(result["hard_triggers"], [])

    def test_each_noncoverage_hard_trigger(self):
        cases = [
            ({"unresolved_conflict": True}, self.initial, "EVIDENCE_CONFLICT_UNRESOLVED"),
            ({"accounting_restatement": True}, self.initial, "ACCOUNTING_RESTATEMENT"),
            ({"auditor_issue": True}, self.initial, "AUDITOR_ISSUE"),
            ({"material_regulatory_event": True}, self.initial, "MATERIAL_REGULATORY_EVENT"),
            ({"confirmed_fraud_or_accounting_investigation": True}, self.initial,
             "CONFIRMED_FRAUD_ACCOUNTING_INVESTIGATION"),
            ({}, {**self.initial, "confidence": "LOW"}, "LOW_CONFIDENCE_HIGH_CANDIDATE"),
            ({"material_event_ids": ["event-2"]},
             {**self.initial, "research_status": "INSUFFICIENT_EVIDENCE"},
             "INSUFFICIENT_WITH_MATERIAL_EVENT"),
            ({"material_event_ids": ["event-3"], "misread_material_event_ids": ["event-3"]},
             self.initial, "MATERIAL_EVENT_FACTUAL_MISREAD"),
        ]
        for evidence_additions, initial, expected in cases:
            with self.subTest(trigger=expected):
                result = escalation(90, initial, {**self.evidence, **evidence_additions}, self.cfg)
                self.assertEqual(result["decision"], "ESCALATE_SOL")
                self.assertIn(expected, result["hard_triggers"])

    def test_initial_failure_and_deep_fallback(self):
        with self.assertRaises(ValueError):
            escalation(90, None, self.evidence, self.cfg)
        self.assertEqual(select_final(self.db, "missing", "AMD"), "UNAVAILABLE")
        decision = {"score": 95, "decision": "ESCALATE_SOL", "reasons": ["material_risk"]}
        save_result(self.db, "r", "AMD", "INITIAL", self.result())
        save_escalation(self.db, "r", "AMD", decision)
        self.assertEqual(select_final(self.db, "r", "AMD", deep_failed=True), "INITIAL_FALLBACK")
        self.assertEqual(tier_summary(self.db, "r", "AMD")["final_research_score"], 78)

    def test_deep_limitations_are_recorded_without_resolving_back_to_sol(self):
        save_result(self.db, "r", "AMD", "INITIAL", self.result())
        save_escalation(self.db, "r", "AMD", {"score": 60, "decision": "ESCALATE_SOL", "reasons": []})
        deep = self.result("gpt-6-sol", 88)
        deep.update({"high_risk_uncertainty": True, "research_status": "INSUFFICIENT_EVIDENCE",
                     "evidence_summary": {"material_event_ids": ["evt"]}})
        save_result(self.db, "r", "AMD", "DEEP", deep,
                    evidence={"material_event_ids": ["evt"]})
        self.assertEqual(select_final(self.db, "r", "AMD"), "DEEP")
        summary = tier_summary(self.db, "r", "AMD")
        self.assertEqual(summary["deep_limitation_code"], "DEEP_RESEARCH_LIMITATION")
        self.assertEqual(summary["deep_limitation_reasons"],
                         ["HIGH_RISK_UNCERTAINTY", "INSUFFICIENT_EVIDENCE", "MATERIAL_EVENT_COVERAGE_GAP"])
        with self.assertRaises(ValueError):
            escalation(90, self.initial, self.evidence, self.cfg, source_tier="DEEP")

    def test_cache_or_initial_refresh_policy(self):
        existing = {"status": "COMPLETE"}
        self.assertEqual(research_cache_decision(existing, material_new_evidence=False,
                                                 candidate_score_change=2, threshold=5), "CACHED_RESEARCH")
        self.assertEqual(research_cache_decision(existing, material_new_evidence=True,
                                                 candidate_score_change=0, threshold=5), "INITIAL_REFRESH")
        self.assertEqual(research_cache_decision(existing, material_new_evidence=False,
                                                 candidate_score_change=5, threshold=5), "INITIAL_REFRESH")
        self.assertEqual(research_cache_decision(None, material_new_evidence=False,
                                                 candidate_score_change=None), "INITIAL_RESEARCH")

    def test_deep_requires_initial_and_escalation(self):
        with self.assertRaises(ValueError):
            save_result(self.db, "r", "AMD", "DEEP", self.result("gpt-6-sol"))
        save_result(self.db, "r", "AMD", "INITIAL", self.result())
        with self.assertRaises(ValueError):
            save_result(self.db, "r", "AMD", "DEEP", self.result("gpt-6-sol"))

    def test_budget_blocks_and_separate_accounting(self):
        day = "2026-09-28"
        self.assertEqual(reserve(self.db, day, "disabled", "AMD", "INITIAL", 1000, 200, .01,
                                 self.cfg), "BUDGET_BLOCKED")
        cfg = {**self.cfg, "enabled": True, "initial_research_model": "cheap-test",
               "max_initial_calls": 2, "max_deep_calls": 1,
               "max_daily_initial_ai_cost": .02, "max_daily_deep_ai_cost": .08,
               "max_daily_total_ai_cost": .10}
        self.assertEqual(reserve(self.db, day, "r", "AMD", "INITIAL", 1000, 200, .01, cfg), "RESERVED")
        budget_transition(self.db, "r", "AMD", "INITIAL", "IN_FLIGHT")
        budget_transition(self.db, "r", "AMD", "INITIAL", "COMPLETED",
                          input_tokens=1100, output_tokens=250, cost_usd=.012)
        self.assertEqual(reserve(self.db, day, "r", "AMD", "DEEP", 5000, 800, .07, cfg), "RESERVED")
        report = cost_report(self.db, day, cfg)
        self.assertEqual((report["initial"]["input_tokens"], report["initial"]["output_tokens"],
                          report["deep"]["input_tokens"]), (1100, 250, 0))
        self.assertAlmostEqual(report["initial"]["confirmed_cost"], .012)
        self.assertAlmostEqual(report["deep"]["reserved_cost"], .07)
        self.assertAlmostEqual(report["daily_budget_remaining"], .018)
        self.assertEqual(reserve(self.db, day, "r2", "META", "DEEP", 5000, 800, .02, cfg), "BUDGET_BLOCKED")
        self.assertEqual(reserve(self.db, day, "r3", "META", "INITIAL", 1000, 200, .015, cfg), "BUDGET_BLOCKED")
        budget_transition(self.db, "r", "AMD", "DEEP", "IN_FLIGHT")
        budget_transition(self.db, "r", "AMD", "DEEP", "FAILED_UNKNOWN")
        self.assertAlmostEqual(cost_report(self.db, day, cfg)["deep"]["unknown_cost"], .07)
        self.assertEqual(reserve(self.db, day, "retry", "AMD", "DEEP", 100, 20, .001, cfg), "BUDGET_BLOCKED")
        with self.assertRaises(ValueError):
            budget_transition(self.db, "r", "AMD", "DEEP", "IN_FLIGHT")
        with self.assertRaises(ValueError):
            budget_transition(self.db, "r", "AMD", "DEEP", "CONFIRMED_NO_CHARGE", cost_usd=0)
        budget_transition(self.db, "r", "AMD", "DEEP", "CONFIRMED_BILLED", cost_usd=.063,
                          evidence={"source": "manual_verified", "reference": "test-evidence",
                                    "verified_at": "2026-09-29T00:00:00Z"})
        reconciled = cost_report(self.db, day, cfg)
        self.assertAlmostEqual(reconciled["deep"]["confirmed_cost"], .063)
        self.assertEqual(reconciled["deep"]["unknown_cost"], 0)

    def test_ab_strict_model_only_and_quality(self):
        body = {"input": "same", "text": {"format": "json_schema"}, "temperature": 0}
        base = {k: "same" for k in ("ticker", "evidence_hash", "quant_summary_hash",
                                   "fundamental_summary_hash", "task_hash", "output_schema_hash")}
        base.update({"model": "gpt-6-sol", "web_search_enabled": False,
                     "input_hash": fingerprint(body), "request_body": {**body, "model": "gpt-6-sol"}})
        cheap = {**base, "model": "cheap-test", "request_body": {**body, "model": "cheap-test"}}
        self.assertTrue(validate_ab_case(base, cheap))
        for change in ({"evidence_hash": "other"}, {"task_hash": "other"},
                       {"web_search_enabled": True}, {"model": "gpt-6-sol"},
                       {"request_body": {**body, "model": "cheap-test", "temperature": 1}}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate_ab_case(base, {**cheap, **change})
        result = evaluate_ab({"claims": [
            {"kind": "KEY_CATALYST", "evidence_ids": ["C"]},
            {"kind": "KEY_RISK", "evidence_ids": ["R"]},
            {"kind": "KEY_RISK", "evidence_ids": ["FAKE"]}],
            "thesis_confidence": "LOW"}, ["C"], ["R"], {"C", "R"}, evidence_coverage=.5)
        self.assertEqual(result["material_catalyst_recall"], 1)
        self.assertEqual(result["material_risk_recall"], 1)
        self.assertAlmostEqual(result["unsupported_claim_rate"], 1/3)
        self.assertEqual(result["thesis_consistency"], "HUMAN_REVIEW_REQUIRED")
        self.assertTrue(result["uncertainty_handling"])
        self.assertAlmostEqual(compare_ab({"cost_usd": .1, "latency_seconds": 10},
                                          {"cost_usd": .02, "latency_seconds": 5})["cost_ratio"], .2)

    def test_forward_separate_tiers_and_no_lookahead(self):
        market = sqlite3.connect(":memory:")
        market.execute("CREATE TABLE daily_bars(ticker TEXT,date TEXT,close REAL)")
        for day, stock in [("2026-09-28", 100), ("2026-09-29", 110),
                           ("2026-09-30", 121), ("2026-10-01", 125)]:
            market.executemany("INSERT INTO daily_bars VALUES(?,?,?)",
                               [("AMD", day, stock), ("SPY", day, 100), ("QQQ", day, 100)])
        initial = self.result()
        initial["completed_at"] = "2026-09-28T21:00:00+00:00"  # 17:00 ET: Monday close predates research.
        save_result(self.db, "r", "AMD", "INITIAL", initial)
        save_escalation(self.db, "r", "AMD", {"score": 90, "decision": "ESCALATE_SOL", "reasons": []})
        deep = self.result("gpt-6-sol", 88)
        deep["completed_at"] = "2026-09-29T21:00:00+00:00"
        save_result(self.db, "r", "AMD", "DEEP", deep, evidence={"confidence": "HIGH"})
        select_final(self.db, "r", "AMD")
        self.assertTrue(register_forward(market, self.db, "r", "AMD", "INITIAL", initial["completed_at"]))
        self.assertTrue(register_forward(market, self.db, "r", "AMD", "DEEP", deep["completed_at"]))
        self.assertTrue(register_forward(market, self.db, "r", "AMD", "FINAL", deep["completed_at"]))
        anchors = self.db.execute("SELECT tier,anchor_date FROM tier_forward_snapshots ORDER BY tier").fetchall()
        self.assertEqual(anchors, [("DEEP", "2026-09-30"), ("FINAL", "2026-09-30"),
                                   ("INITIAL", "2026-09-29")])
        self.assertEqual(update_forward(market, self.db, horizons=(1,)), 3)
        returns = self.db.execute("SELECT tier,stock_return FROM tier_forward_observations ORDER BY tier").fetchall()
        self.assertAlmostEqual(returns[2][1], .1)
        self.assertEqual(update_forward(market, self.db, horizons=(1,)), 0)
        market.close()


if __name__ == "__main__":
    unittest.main()

import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from shadow_day1_research import CONFIG, build_plan
from shadow_daily_pipeline import research_eligibility


class ResearchCacheIsolationTests(unittest.TestCase):
    def setUp(self):
        self.research = sqlite3.connect(":memory:")
        self.fundamental = sqlite3.connect(":memory:")
        self.addCleanup(self.research.close)
        self.addCleanup(self.fundamental.close)
        self.research.executescript("""
            CREATE TABLE research_runs(id INTEGER,fundamental_run_id INTEGER,candidate_scan_date TEXT);
            CREATE TABLE research_priority(run_id INTEGER,ticker TEXT,priority_rank INTEGER);
            CREATE TABLE evidence_packets_v2(ticker TEXT,evidence_asof TEXT);
            CREATE TABLE tier_budget_ledger(ticker TEXT,tier TEXT,state TEXT);
            INSERT INTO research_runs VALUES(7,3,'2026-09-30');
        """)
        self.fundamental.executescript("""
            CREATE TABLE fundamental_runs(id INTEGER,scan_date TEXT);
            CREATE TABLE fundamental_scores(run_id INTEGER,ticker TEXT,candidate_score REAL,candidate_rank INTEGER);
            INSERT INTO fundamental_runs VALUES(3,'2026-09-30');
        """)
        self.names = [f"TEST{i}" for i in range(15)]
        for i, ticker in enumerate(self.names, 1):
            self.research.execute("INSERT INTO research_priority VALUES(7,?,?)", (ticker, i))
            self.fundamental.execute("INSERT INTO fundamental_scores VALUES(3,?,80,?)", (ticker, i))
        self.limits = json.loads(CONFIG.read_text())
        self.frozen = {"architecture_status": "FROZEN_V1", "deep_research_model": "gpt-6-sol",
                       "initial_research_model": "gpt-5.6-luna", "candidate_score_refresh_threshold": 5}
        self.inputs = {"source_run_id": 7, "candidate_scan_date": "2026-09-30",
                       "evidence_asof": "2026-09-30T20:00:00Z", "candidate_score": 80,
                       "evidence_policy": {"status": "READY"}, "evidence_packet_tokens_estimate": 100,
                       "prompt": {"phase4a_evidence": {"confidence": "HIGH", "coverage_pct": 100,
                           "coverage": {"SEC_CONTENT": "AVAILABLE"}, "other_events": [],
                           "recent_material_events": [{"sources": [{"source_type": "SEC_CONTENT"}]}]}}}
        self.prior = {"status": "COMPLETE", "candidate_score": 80,
                      "evidence_asof": "2026-09-28T20:00:00Z", "source": "LUNA_DB",
                      "completed_at": "2026-09-28T21:00:00Z", "result": {"research_score": 75},
                      "escalation": None}

    def plan(self, inputs):
        with patch("shadow_day1_research.cached_inputs", side_effect=inputs), \
             patch("shadow_day1_research.latest_prior", return_value=self.prior), \
             patch("shadow_day1_research.prior_rank", return_value=None), \
             patch("shadow_day1_research.material_hash", return_value="same"):
            return build_plan(self.research, self.fundamental, {}, self.frozen, self.limits,
                              at=datetime(2026, 9, 30, 22, tzinfo=timezone.utc), day="2026-09-30")

    def test_one_invalid_cache_preserves_fourteen_valid_research_cases(self):
        def inputs(db, ticker, *args, **kwargs):
            if ticker == "TEST0":
                raise ValueError("CACHE_INTEGRITY_FAILED: event/packet mismatch for TEST0")
            return self.inputs

        plan = self.plan(inputs)
        failed = plan["cases"][0]
        self.assertEqual(len(plan["cases"]), 15)
        self.assertEqual(plan["cached_count"], 14)
        self.assertEqual(plan["failed_cache_count"], 1)
        self.assertEqual((failed["cache_status"], failed["required_action"]),
                         ("RESEARCH_FAILED_CACHE", "NO_CALL"))
        self.assertEqual((failed["candidate_rank"], failed["candidate_score"]), (1, 80))
        self.assertIsNone(failed["prior_result"])
        self.assertIsNone(failed["evidence_asof"])
        self.assertEqual((plan["estimated_calls"], plan["estimated_cost_usd"]), (0, 0))

    def test_network_and_configuration_exceptions_remain_fatal(self):
        for failure in (RuntimeError("NETWORK_ERROR"), ValueError("MODEL_SAFETY_BLOCK")):
            with self.subTest(failure=str(failure)), self.assertRaises(type(failure)):
                self.plan(failure)

    def test_source_run_mismatch_remains_fatal(self):
        with self.assertRaisesRegex(ValueError, "RESEARCH_SOURCE_RUN_MISMATCH"):
            self.plan(lambda *a, **k: {**self.inputs, "source_run_id": 8})

    def test_unresolved_ledger_still_blocks_research(self):
        self.research.execute("INSERT INTO tier_budget_ledger VALUES('TEST0','INITIAL','FAILED_UNKNOWN')")
        plan = self.plan(lambda *a, **k: self.inputs)
        self.assertEqual(plan["cases"][0]["cache_status"], "RESEARCH_BLOCKED_UNKNOWN")
        self.assertEqual(plan["cases"][0]["required_action"], "NO_CALL")

    def test_missing_packet_explicit_failure_cannot_become_limited_or_entry_eligible(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "data").mkdir()
            sqlite3.connect(root / "data" / "research_data.db").close()
            (root / "research_plan.json").write_text(json.dumps({"run_id": "TEST", "cases": [
                {"ticker": "TEST0", "cache_status": "RESEARCH_FAILED_CACHE",
                 "reason": "CACHE_INTEGRITY_FAILED: missing evidence", "candidate_rank": 1,
                 "evidence_asof": None}]}))
            report = {"cases": [{"ticker": "TEST0", "candidate_score": 99,
                                 "research_score": 99, "conviction_score": 99}]}
            with patch("shadow_daily_pipeline.ROOT", root), \
                 patch("shadow_daily_pipeline.paths", return_value=root):
                result = research_eligibility("2026-09-30", report, {})
            case = result["cases"][0]
            self.assertEqual(case["eligibility"], "RESEARCH_FAILED")
            self.assertFalse(case["entry_observation_allowed"])
            self.assertIsNone(case["research_score"])
            self.assertIsNone(case["effective_conviction"])
            self.assertEqual(result["deep_pending"], 0)


if __name__ == "__main__":
    unittest.main()

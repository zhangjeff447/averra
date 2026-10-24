import json
import socket
import sqlite3
import ssl
import unittest
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from phase4_budget import (budget_report, classify_network_error, mark_stage, reconcile, transition)
from phase4_evidence import (collect_cached, deduplicate, needs_web_escalation,
                             normalize_event, reasoner_packet)
from phase4_run import init_db, pilot, research_request, reserve_tokens


CFG = {**json.loads((Path(__file__).resolve().parents[1] / "phase4_config.json").read_text()),
       "paid_ai_enabled": True}  # Test-only override; API is mocked.
URL = "https://www.sec.gov/Archives/edgar/data/123/release.htm"
DAY = "2026-09-27"


def estimate(cost=0.02):
    return {"estimated_input_tokens": 2000, "estimated_output_tokens": 500,
            "estimated_model_cost_usd": cost, "estimated_search_cost_usd": 0,
            "estimated_total_cost_usd": cost}


def packet():
    return {"ticker": "TEST", "company": "Test Co", "research_cutoff": DAY + "T20:00:00+00:00",
            "quant": {"score": 80, "rank": 10},
            "fundamental": {"candidate_score": 82, "score": 78, "flags": [], "confidence": "HIGH"},
            "recent_filings": [], "previous_thesis": None,
            "evidence_records": [{"url": URL, "date": DAY, "event_type": "GUIDANCE", "headline": "Guidance raised",
                                  "summary": "Company raised full-year guidance", "source_type": "SEC",
                                  "materiality": 5, "content_verified": True}]}


class BudgetReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        init_db(self.db)

    def tearDown(self):
        self.db.close()

    def reserve(self, ticker="TEST", cost=0.02):
        return reserve_tokens(self.db, CFG, DAY, 1, ticker, estimate(cost))

    def test_state_transitions_and_unknown_cost(self):
        ledger = self.reserve()
        self.assertEqual(budget_report(self.db, DAY, CFG)["reserved_cost_usd"], 0.02)
        transition(self.db, ledger, "IN_FLIGHT", request_started_at=DAY + "T12:00:00Z")
        transition(self.db, ledger, "FAILED_UNKNOWN", error_category="UNKNOWN_NETWORK_ERROR")
        report = budget_report(self.db, DAY, CFG)
        self.assertEqual((report["confirmed_cost_usd"], report["pending_unknown_cost_usd"],
                          report["reserved_cost_usd"]), (0, 0.02, 0))
        self.assertAlmostEqual(report["available_safe_budget_usd"], 0.33)
        with self.assertRaises(ValueError):
            transition(self.db, ledger, "IN_FLIGHT")

    @patch("phase4_run.api_research")
    def test_paid_ai_hold_blocks_before_any_request(self, mock_api):
        chosen = [{"ticker": "TEST", "candidate_score": 82,
                   "refresh_mode": "FULL_RESEARCH", "packet": packet()}]
        with self.assertRaisesRegex(RuntimeError, "PAID_AI_DISABLED"):
            pilot(self.db, 1, chosen, {**CFG, "paid_ai_enabled": False}, "unused-test-key")
        mock_api.assert_not_called()
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM research_token_ledger").fetchone()[0], 0)

    @patch("phase4_run.api_research")
    def test_budget_block_is_terminal_and_not_a_paid_call(self, mock_api):
        chosen = [{"ticker": "TEST", "candidate_score": 82,
                   "refresh_mode": "FULL_RESEARCH", "packet": packet()}]
        result = pilot(self.db, 1, chosen, {**CFG, "max_cost_per_stock_usd": 0.0001}, "unused-test-key")
        self.assertEqual(result[0]["status"], "BUDGET_BLOCKED")
        report = budget_report(self.db, datetime.now(timezone.utc).date().isoformat(), CFG)
        self.assertEqual(report["state_counts"]["BUDGET_BLOCKED"], 1)
        self.assertEqual(report["ai_calls"], 0)
        self.assertEqual(report["reserved_cost_usd"], 0)
        mock_api.assert_not_called()

    def test_completed_cost_and_reservation_release(self):
        complete = self.reserve("DONE")
        transition(self.db, complete, "IN_FLIGHT")
        transition(self.db, complete, "COMPLETED", usage_received=1, raw_response_saved=1)
        self.db.execute("UPDATE research_token_ledger SET actual_input=1000,actual_output=100 WHERE id=?", (complete,))
        self.db.execute("UPDATE research_usage_details SET actual_total_cost_usd=0.003 WHERE ledger_id=?", (complete,))
        released = self.reserve("DNS")
        transition(self.db, released, "FAILED_NO_REQUEST", error_category="DNS_ERROR")
        report = budget_report(self.db, DAY, CFG)
        self.assertEqual(report["confirmed_cost_usd"], 0.003)
        self.assertEqual(report["reserved_cost_usd"], 0)
        self.assertEqual(report["pending_unknown_cost_usd"], 0)
        self.assertEqual(report["budget_input_tokens"], 1000)

    def test_reconciliation_requires_traceable_evidence(self):
        ledger = self.reserve()
        transition(self.db, ledger, "IN_FLIGHT", request_id="req_123")
        transition(self.db, ledger, "FAILED_UNKNOWN")
        with self.assertRaises(ValueError):
            reconcile(self.db, ledger, "CONFIRMED_NO_CHARGE", {"source": "aggregate_dashboard", "cost_usd": 0})
        evidence = {"source": "provider_request_record", "request_id": "req_123", "reference": "record-1",
                    "verified_at": DAY + "T21:00:00Z", "cost_usd": 0}
        reconcile(self.db, ledger, "CONFIRMED_NO_CHARGE", evidence)
        self.assertEqual(budget_report(self.db, DAY, CFG)["pending_unknown_cost_usd"], 0)
        self.assertAlmostEqual(budget_report(self.db, DAY, CFG)["available_safe_budget_usd"], 0.35)

    def test_reconciled_billed_cost(self):
        ledger = self.reserve()
        transition(self.db, ledger, "IN_FLIGHT")
        transition(self.db, ledger, "FAILED_UNKNOWN")
        reconcile(self.db, ledger, "CONFIRMED_BILLED",
                  {"source": "manual_verified", "reference": "invoice-line-1",
                   "verified_at": DAY + "T21:00:00Z", "cost_usd": 0.011})
        report = budget_report(self.db, DAY, CFG)
        self.assertEqual((report["confirmed_cost_usd"], report["pending_unknown_cost_usd"]), (0.011, 0))

    def test_provider_reconciliation_cannot_match_missing_request_id(self):
        ledger = self.reserve()
        transition(self.db, ledger, "IN_FLIGHT")
        transition(self.db, ledger, "FAILED_UNKNOWN")
        with self.assertRaisesRegex(ValueError, "request ID"):
            reconcile(self.db, ledger, "CONFIRMED_NO_CHARGE",
                      {"source": "provider_request_record", "reference": "bucket-1",
                       "verified_at": DAY + "T21:00:00Z", "cost_usd": 0})
        self.assertEqual(budget_report(self.db, DAY, CFG)["pending_unknown_cost_usd"], 0.02)

    def test_network_error_categories_and_no_unknown_retry(self):
        cases = [(urllib.error.URLError(socket.gaierror(8, "DNS")), "DNS_ERROR", "FAILED_NO_REQUEST"),
                 (TimeoutError(), "API_TIMEOUT", "FAILED_UNKNOWN"),
                 (ConnectionResetError(), "CONNECTION_RESET", "FAILED_UNKNOWN"),
                 (ssl.SSLError("TLS"), "TLS_ERROR", "FAILED_UNKNOWN"),
                 (urllib.error.HTTPError(URL, 429, "rate", {}, None), "HTTP_429", "FAILED_UNKNOWN"),
                 (urllib.error.HTTPError(URL, 503, "server", {}, None), "HTTP_5XX", "FAILED_UNKNOWN")]
        for error, kind, state in cases:
            with self.subTest(kind=kind):
                self.assertEqual(classify_network_error(error), (kind, state))
        self.assertEqual(classify_network_error(TimeoutError(), phase="connect")[0], "CONNECT_TIMEOUT")
        self.assertEqual(classify_network_error(TimeoutError(), response_started=True)[0], "READ_TIMEOUT")
        self.assertEqual(classify_network_error(ConnectionResetError(), response_started=True)[0],
                         "STREAM_INTERRUPTED")

    @patch("phase4_run.api_research", side_effect=urllib.error.URLError(OSError("network")))
    def test_reasoner_failure_retains_evidence_and_stops(self, mock_api):
        chosen = [{"ticker": "TEST", "candidate_score": 82, "refresh_mode": "FULL_RESEARCH", "packet": packet()},
                  {"ticker": "NEXT", "candidate_score": 70, "refresh_mode": "FULL_RESEARCH", "packet": packet()}]
        result = pilot(self.db, 1, chosen, CFG, "unused-test-key")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["reason"], "UNKNOWN_NETWORK_ERROR")
        self.assertEqual(self.db.execute("SELECT state FROM research_request_state").fetchone()[0], "FAILED_UNKNOWN")
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM research_evidence_cache").fetchone()[0], 1)
        self.assertEqual(collect_cached(self.db, packet(), CFG, cache_only=True)["events"][0]["event_type"],
                         "GUIDANCE")
        mock_api.assert_called_once()


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        init_db(self.db)

    def tearDown(self):
        self.db.close()

    def test_dedup_official_priority_and_materiality(self):
        stamp = DAY + "T21:00:00Z"
        base = {"event_type": "GUIDANCE", "date": DAY, "headline": "Guidance raised",
                "summary": "Guidance", "materiality": 5, "duplicate_group": "same"}
        news = normalize_event({**base, "url": "https://www.reuters.com/news", "source_type": "TRUSTED_NEWS"},
                               "TEST", stamp, stamp)
        official = normalize_event({**base, "url": URL, "source_type": "SEC"}, "TEST", stamp, stamp)
        minor = normalize_event({**base, "url": URL + "2", "source_type": "SEC", "materiality": 1,
                                 "duplicate_group": "minor"}, "TEST", stamp, stamp)
        events = deduplicate([news, minor, official])
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0]["source_type"], "SEC")
        self.assertEqual(len(events[0]["sources"]), 2)
        self.assertEqual(events[0]["materiality"], 5)

    def test_evidence_cache_skips_second_fetch(self):
        p = packet()
        p["evidence_records"] = []
        cfg = {**CFG, "evidence_feeds": {"TEST": [{"url": "https://investor.example.com/rss",
                                                   "source_type": "COMPANY_IR"}]},
               "verified_ir_domains": {"TEST": ["investor.example.com"]}}
        calls = []
        def fetch(url, kind):
            calls.append(url)
            return [{"url": "https://investor.example.com/release", "date": DAY,
                     "title": "Guidance raised", "summary": "Full year guidance raised", "materiality": 5}]
        first = collect_cached(self.db, p, cfg, fetcher=fetch)
        second = collect_cached(self.db, p, cfg, fetcher=fetch)
        self.assertEqual(len(calls), 1)
        self.assertEqual(first, second)
        self.assertEqual(first["status"], "SUFFICIENT")

    def test_packet_compression_preserves_material_events(self):
        evidence = {"status": "SUFFICIENT", "events": []}
        for i in range(30):
            evidence["events"].append({"materiality": 5 if i == 0 else 1,
                                       "short_factual_summary": "x" * 500, "event_type": str(i)})
        result = reasoner_packet(packet(), evidence, {**CFG, "phase4b_target_input_tokens": 500})
        self.assertEqual(result["events"][0]["event_type"], "0")
        self.assertLess(len(result["events"]), 30)
        self.assertLessEqual(len(json.dumps(result).encode()), 2000)

    def test_web_search_requires_insufficiency_or_conflict_and_opt_in(self):
        sufficient = {"status": "SUFFICIENT", "events": [{"conflict": False}]}
        self.assertFalse(needs_web_escalation(sufficient))
        self.assertTrue(needs_web_escalation({"status": "INSUFFICIENT", "events": []}))
        self.assertTrue(needs_web_escalation({"status": "SUFFICIENT", "events": [{"conflict": True}]}))
        self.assertNotIn("tools", research_request(packet(), "FULL_RESEARCH", CFG, sufficient))
        cfg = {**CFG, "web_search_escalation_enabled": True}
        body = research_request(packet(), "FULL_RESEARCH", cfg, {"status": "INSUFFICIENT", "events": []})
        self.assertEqual(body["max_tool_calls"], 1)
        self.assertEqual(body["tools"][0]["search_context_size"], "low")


if __name__ == "__main__":
    unittest.main()

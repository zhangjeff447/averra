import json
import sqlite3
import tempfile
import unittest
import zlib
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from phase4_engine import (apply_verified_override, deduplicate_events, earnings_context,
                           filings_for_ticker, refresh_mode, score_research, token_report, trusted_url)
from phase4_run import (estimate_request, init_db, load_key, pilot, pilot_selection,
                        research_request, reserve_tokens, token_breakdown)


CFG = {**json.loads((Path(__file__).resolve().parents[1] / "phase4_config.json").read_text()),
       "paid_ai_enabled": True}  # Mocked API calls only.
SEC_URL = "https://www.sec.gov/Archives/edgar/data/123/filing/"
REUTERS_URL = "https://www.reuters.com/business/test-story/"


def packet():
    return {"ticker": "TEST", "company": "Test Company", "research_cutoff": "2026-09-25T20:00:00+00:00",
            "quant": {"rvol": 2, "rs_spy_20d": 0.05},
            "fundamental": {"candidate_score": 80, "confidence": "HIGH", "flags": []},
            "recent_filings": [{"url": SEC_URL, "date": "2026-09-20", "form": "8-K",
                                "items": "2.02", "accession": "0000000123-26-000001"}],
            "evidence_records": [{"url": SEC_URL, "date": "2026-09-20", "source_type": "SEC",
                                  "event_type": "GUIDANCE_RAISE", "headline": "Guidance raised",
                                  "summary": "Company raised guidance", "materiality": 5,
                                  "content_verified": True}], "earnings": {"status": "NORMAL"},
            "previous_thesis": None}


def event(direction="POSITIVE", kind="GUIDANCE_RAISE", day="2026-09-20", url=SEC_URL):
    return {"event_type": kind, "date": day, "summary": "Company changed full-year guidance",
            "direction": direction, "materiality": "HIGH", "fundamental_impact": 75,
            "duration": 75, "sources": [{"url": url, "date": day, "source_type": "SEC"}]}


def response(events):
    return {"events": events, "dimensions": {"news_sentiment": 70, "earnings_guidance": 75,
            "business_momentum": 70, "competitive_position": 65, "risk_events": 60},
            "thesis": "Evidence-led thesis", "bull_case": "Improving guidance",
            "bear_case": "Execution risk", "key_catalyst": "Guidance",
            "key_risk": "Reversal", "what_changes_thesis": "Two weak quarters"}


def api_response(input_tokens=5000, output_tokens=500, searched=False):
    output = ([{"type": "web_search_call", "action": {"sources": [{"url": SEC_URL}]}}] if searched else [])
    output.append({"type": "message", "content": [{"type": "output_text",
                   "text": json.dumps(response([event()]))}]})
    return {"status": "completed", "output": output,
            "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens,
                      "input_tokens_details": {"cached_tokens": 0}}}


def budget_estimate(input_tokens, output_tokens=1200, total_cost=0.05):
    return {"estimated_input_tokens": input_tokens, "estimated_output_tokens": output_tokens,
            "estimated_model_cost_usd": total_cost - 0.01,
            "estimated_search_cost_usd": 0.01, "estimated_total_cost_usd": total_cost}


class ResearchScoringTests(unittest.TestCase):
    def scored(self, events, urls=None):
        return score_research(response(events), packet(), set(urls or [SEC_URL]), CFG)

    def test_positive_and_negative_catalysts(self):
        positive = self.scored([event()])
        negative = self.scored([event("NEGATIVE", "GUIDANCE_CUT")])
        self.assertEqual(positive["status"], "COMPLETE")
        self.assertLess(negative["catalyst_score"], positive["catalyst_score"])

    def test_neutral_news(self):
        result = self.scored([event("NEUTRAL", "PRODUCT_LAUNCH")])
        self.assertEqual(result["status"], "COMPLETE")

    def test_material_event_types(self):
        for kind in ("GUIDANCE_RAISE", "GUIDANCE_CUT", "EARNINGS_BEAT", "EARNINGS_MISS",
                     "MAJOR_CONTRACT", "PRODUCT_LAUNCH", "REGULATORY_APPROVAL", "LITIGATION",
                     "MANAGEMENT_CHANGE", "SHARE_OFFERING", "BUYBACK", "M_AND_A", "CONFIRMED_FRAUD",
                     "BANKRUPTCY_FILING"):
            with self.subTest(kind=kind):
                result = self.scored([event(kind=kind)])
                self.assertEqual(result["status"], "COMPLETE")
                self.assertNotEqual(result["label"], "RESEARCH_BLOCK")

    def test_duplicate_news_one_catalyst_multiple_sources(self):
        second = event(url=REUTERS_URL)
        result = self.scored([event(), second], [SEC_URL, REUTERS_URL])
        self.assertEqual(len(result["events"]), 1)
        self.assertEqual(len(result["sources"]), 2)

    def test_conflicting_sources_reduce_confidence(self):
        result = self.scored([event(), event("NEGATIVE")])
        self.assertIn("SOURCE_CONFLICT", result["flags"])
        self.assertEqual(result["confidence"], "LOW")

    def test_future_news_and_future_source_excluded(self):
        future = event(day="2026-09-26")
        self.assertEqual(self.scored([future])["status"], "UNAVAILABLE")
        later_source = event()
        later_source["sources"][0]["date"] = "2026-09-26"
        self.assertEqual(self.scored([later_source])["status"], "UNAVAILABLE")

    def test_missing_and_low_quality_source(self):
        self.assertEqual(score_research(response([event()]), packet(), set(), CFG)["status"], "UNAVAILABLE")
        self.assertEqual(self.scored([event(url="https://reddit.com/post")])["status"], "UNAVAILABLE")
        self.assertFalse(trusted_url("https://sec.gov.evil.example/file", CFG))

    def test_unconsulted_reuters_source_rejected(self):
        self.assertEqual(self.scored([event(url=REUTERS_URL)])["status"], "UNAVAILABLE")

    def test_conviction_uses_config_and_keeps_candidate_immutable(self):
        p = packet()
        original = p["fundamental"]["candidate_score"]
        result = score_research(response([event()]), p, {SEC_URL}, CFG)
        expected = round(.7 * original + .3 * result["research_score"], 4)
        self.assertEqual(result["conviction_score"], expected)
        self.assertEqual(p["fundamental"]["candidate_score"], original)

    def test_no_valid_events_no_fabricated_score(self):
        result = self.scored([])
        self.assertEqual(result["status"], "UNAVAILABLE")
        self.assertNotIn("conviction_score", result)

    def test_earnings_context(self):
        files = [{"form": "8-K", "items": "2.02", "date": "2026-09-23"}]
        result = earnings_context(files, datetime(2026, 9, 25, tzinfo=timezone.utc))
        self.assertEqual(result["status"], "POST_EARNINGS")
        self.assertIsNone(result["next_earnings_date"])

    def test_earnings_imminent_reliable_date(self):
        result = earnings_context([], datetime(2026, 9, 25, tzinfo=timezone.utc),
                                  {"date": "2026-09-29", "quality": "CONFIRMED", "source": SEC_URL})
        self.assertEqual(result["status"], "EARNINGS_IMMINENT")
        self.assertEqual(result["trading_days_until_earnings"], 2)

    def test_refresh_modes(self):
        p = packet()
        p["news_check_status"] = "VERIFIED_NO_CHANGE"
        self.assertEqual(refresh_mode(p, None, CFG), "FULL_RESEARCH")
        previous = {"candidate_score": 80, "researched_at": "2026-09-24T00:00:00+00:00"}
        self.assertEqual(refresh_mode(p, previous, CFG), "CACHED_RESEARCH")
        p["fundamental"]["candidate_score"] = 83
        self.assertEqual(refresh_mode(p, previous, CFG), "LIGHT_REFRESH")
        p["fundamental"]["candidate_score"] = 86
        self.assertEqual(refresh_mode(p, previous, CFG), "FULL_RESEARCH")
        p["fundamental"]["candidate_score"] = 80
        p["recent_filings"] = [{"date": "2026-09-25"}]
        self.assertEqual(refresh_mode(p, previous, CFG), "FULL_RESEARCH")

    def test_unverified_news_prevents_free_cache(self):
        p = packet()
        previous = {"candidate_score": 80, "researched_at": "2026-09-24T00:00:00+00:00"}
        self.assertEqual(refresh_mode(p, previous, CFG), "LIGHT_REFRESH")

    def test_pilot_selection_high_middle_risk(self):
        items = [{"ticker": str(i), "candidate_rank": i,
                  "packet": {"fundamental": {"flags": ["X", "Y"] if i == 3 else []}}} for i in range(1, 16)]
        self.assertEqual([x["ticker"] for x in pilot_selection(items)], ["1", "15", "3"])


class TokenAndOverrideTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        init_db(self.db)

    def tearDown(self):
        self.db.close()

    def test_later_key_assignment_overrides_empty_template(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text("OPENAI_API_KEY=\nOPENAI_API_KEY=test-value\n")
            with patch.dict("os.environ", {"OPENAI_API_KEY": ""}):
                self.assertEqual(load_key(path), "test-value")

    def test_budget_and_ledger(self):
        reserve_tokens(self.db, CFG, "2026-09-25", 1, "TEST", budget_estimate(3000))
        report = token_report(self.db, "2026-09-25", CFG)
        self.assertEqual(report["ai_calls"], 1)
        self.assertEqual(report["input_tokens"], 0)
        self.assertEqual(report["budget_input_tokens"], 3000)
        self.assertEqual(report["reserved_cost_usd"], 0.05)
        with self.assertRaisesRegex(RuntimeError, "hard limit"):
            reserve_tokens(self.db, CFG, "2026-09-25", 1, "OTHER", budget_estimate(16001))

    def test_daily_budget_exceeded(self):
        limited = {**CFG, "max_daily_ai_calls": 1}
        reserve_tokens(self.db, limited, "2026-09-25", 1, "TEST", budget_estimate(100))
        with self.assertRaisesRegex(RuntimeError, "daily"):
            reserve_tokens(self.db, limited, "2026-09-25", 1, "OTHER", budget_estimate(100))

    def test_daily_input_output_and_dollar_budget_block_before_call(self):
        for limit in ({"max_daily_input_tokens": 1000}, {"max_daily_output_tokens": 100},
                      {"max_daily_cost_usd": 0.001}, {"max_cost_per_stock_usd": 0.001}):
            with self.subTest(limit=limit), self.assertRaisesRegex(RuntimeError, "BUDGET_BLOCKED"):
                reserve_tokens(self.db, {**CFG, **limit}, "2026-09-25", 1, "TEST",
                               budget_estimate(3000))
        self.assertEqual(token_report(self.db, "2026-09-25", CFG)["ai_calls"], 0)

    def test_legacy_pilot_usage_remains_accounted_without_invented_scores(self):
        day = "2026-09-25"
        for ticker, input_tokens, output_tokens in (("CDNA", 13638, 772),
                                                      ("AMD", 13585, 834),
                                                      ("META", 13604, 631)):
            self.db.execute("""INSERT INTO research_token_ledger(usage_day,run_id,ticker,
                reserved_input,reserved_output,actual_input,actual_output,status,created_at)
                VALUES(?,?,?,?,?,?,?,?,?)""", (day, 5, ticker, 5000, 1200,
                    input_tokens, output_tokens, "provider exceeded per-stock token ceiling", day))
        report = token_report(self.db, day, CFG)
        self.assertEqual((report["input_tokens"], report["output_tokens"]), (40827, 2237))
        self.assertEqual(report["stocks_researched"], 0)
        self.assertAlmostEqual(report["confirmed_cost_usd"], 0.104024)
        self.assertAlmostEqual(report["pending_unknown_cost_usd"], 0.03)

    @patch("phase4_run.api_research")
    def test_existing_daily_dollar_spend_blocks_next_request(self, mock_api):
        day = datetime.now(timezone.utc).date().isoformat()
        self.db.execute("""INSERT INTO research_token_ledger(usage_day,run_id,ticker,
            reserved_input,reserved_output,actual_input,actual_output,status,created_at)
            VALUES(?,?,?,?,?,?,?,?,?)""", (day, 5, "PRIOR", 5000, 1200, 13638, 772,
                                              "UNAVAILABLE", day))
        chosen = [{"ticker": "TEST", "candidate_score": 80, "refresh_mode": "FULL_RESEARCH",
                   "packet": packet()}]
        result = pilot(self.db, 1, chosen, {**CFG, "max_daily_cost_usd": 0.05}, "test-key")
        self.assertEqual(result[0]["status"], "BUDGET_BLOCKED")
        mock_api.assert_not_called()

    def test_search_context_estimation_is_explicit(self):
        body = research_request(packet(), "FULL_RESEARCH", CFG)
        estimate = estimate_request(packet(), body, CFG)
        self.assertNotIn("tools", body)
        self.assertEqual(estimate["estimated_search_cost_usd"], 0)
        self.assertLess(estimate["estimated_input_tokens"], CFG["hard_input_tokens_per_stock"])
        breakdown = token_breakdown(estimate, 13638, api_response(13638))
        self.assertGreater(breakdown["unattributed_search_or_other_context_tokens_estimate"], 10000)
        self.assertIsNone(breakdown["web_search_returned_context_tokens_actual"])

    def test_hard_risk_requires_separate_verification(self):
        base = {"status": "COMPLETE", "label": "NORMAL_CANDIDATE", "flags": []}
        cutoff = datetime(2026, 9, 25, 20, tzinfo=timezone.utc)
        self.assertEqual(apply_verified_override(self.db, "TEST", cutoff, base, CFG)["label"], "NORMAL_CANDIDATE")
        self.db.execute("INSERT INTO research_overrides VALUES(?,?,?,?,?,?)",
                        ("TEST", "BANKRUPTCY_FILING", "2026-09-20", SEC_URL,
                         "2026-09-24T12:00:00+00:00", "Manually confirmed SEC filing"))
        blocked = apply_verified_override(self.db, "TEST", cutoff, base, CFG)
        self.assertEqual(blocked["label"], "RESEARCH_BLOCK")
        self.assertEqual(blocked["override"]["source_url"], SEC_URL)

    def test_untrusted_override_does_not_block(self):
        self.db.execute("INSERT INTO research_overrides VALUES(?,?,?,?,?,?)",
                        ("TEST", "CONFIRMED_FRAUD", "2026-09-20", "https://reddit.com/x",
                         "2026-09-24T12:00:00+00:00", "Unknown source"))
        base = {"status": "COMPLETE", "label": "WATCH", "flags": []}
        self.assertEqual(apply_verified_override(self.db, "TEST", datetime(2026, 9, 25,
            tzinfo=timezone.utc), base, CFG)["label"], "WATCH")

    def test_point_in_time_filing_filter(self):
        market = sqlite3.connect(":memory:")
        market.execute("CREATE TABLE securities(ticker TEXT,cik TEXT)")
        market.execute("INSERT INTO securities VALUES('TEST','123')")
        payload = {"filings": {"recent": {
            "form": ["8-K", "8-K", "8-K"],
            "filingDate": ["2026-09-20", "2026-09-26", "2026-09-25"],
            "accessionNumber": ["0000000123-26-000001", "0000000123-26-000002", "0000000123-26-000003"],
            "acceptanceDateTime": ["2026-09-20T12:00:00+00:00", "2026-09-26T12:00:00+00:00",
                                   "2026-09-25T21:00:00+00:00"],
            "items": ["2.02", "1.01", "5.02"]}}}
        self.db.execute("""CREATE TABLE sec_cache(cik TEXT,endpoint TEXT,payload BLOB,cache_status TEXT)""")
        self.db.execute("INSERT INTO sec_cache VALUES(?,?,?,?)", ("0000000123", "submissions",
                        zlib.compress(json.dumps(payload).encode()), "FRESH"))
        items = filings_for_ticker(self.db, market, "TEST", datetime(2026, 9, 25,
            20, tzinfo=timezone.utc), CFG)
        self.assertEqual([x["date"] for x in items], ["2026-09-20"])
        market.close()

    @patch("phase4_run.api_research")
    def test_unknown_failure_stops_pilot_without_retry(self, mock_api):
        mock_api.side_effect = [RuntimeError("unavailable"), api_response(), api_response()]
        selected = [{"ticker": name, "candidate_score": 80, "refresh_mode": "FULL_RESEARCH",
                     "packet": packet()} for name in ("A", "B", "C")]
        result = pilot(self.db, 1, selected, CFG, "test-key")
        self.assertEqual([x["status"] for x in result], ["UNAVAILABLE"])
        self.assertEqual(token_report(self.db, datetime.now(timezone.utc).date().isoformat(), CFG)["ai_calls"], 1)
        self.assertEqual(self.db.execute("SELECT state FROM research_request_state").fetchone()[0], "FAILED_UNKNOWN")
        mock_api.assert_called_once()

    @patch("phase4_run.api_research")
    def test_below_target_preserves_response_and_accounts_cost(self, mock_api):
        mock_api.return_value = api_response(1000, 500)
        chosen = [{"ticker": "TEST", "candidate_score": 80, "refresh_mode": "FULL_RESEARCH",
                   "packet": packet()}]
        result = pilot(self.db, 1, chosen, CFG, "test-key")[0]
        self.assertEqual(result["status"], "COMPLETE")
        self.assertNotIn("TOKEN_BUDGET_OVERRUN", result["flags"])
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM research_response_archive").fetchone()[0], 1)
        detail = self.db.execute("SELECT * FROM research_usage_details").fetchone()
        self.assertAlmostEqual(detail["actual_total_cost_usd"], 0.007)
        self.assertEqual(detail["web_search_calls"], 0)
        self.assertAlmostEqual(token_report(self.db, datetime.now(timezone.utc).date().isoformat(), CFG)
                               ["confirmed_cost_usd"], 0.007)

    @patch("phase4_run.api_research")
    def test_above_target_below_hard_keeps_score(self, mock_api):
        mock_api.return_value = api_response(9000, 800)
        chosen = [{"ticker": "TEST", "candidate_score": 80, "refresh_mode": "FULL_RESEARCH",
                   "packet": packet()}]
        result = pilot(self.db, 1, chosen, CFG, "test-key")[0]
        self.assertEqual(result["status"], "COMPLETE")
        self.assertIn("TOKEN_BUDGET_OVERRUN", result["flags"])
        self.assertEqual(self.db.execute("SELECT status FROM research_results").fetchone()[0], "COMPLETE")

    @patch("phase4_run.api_research")
    def test_above_hard_keeps_score_and_stops_next_call(self, mock_api):
        mock_api.return_value = api_response(17000, 800)
        chosen = [{"ticker": name, "candidate_score": 80, "refresh_mode": "FULL_RESEARCH",
                   "packet": packet()} for name in ("A", "B")]
        result = pilot(self.db, 1, chosen, CFG, "test-key")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["status"], "COMPLETE")
        self.assertIn("HARD_INPUT_OVERRUN", result[0]["flags"])
        mock_api.assert_called_once()

    @patch("phase4_run.api_research")
    def test_pre_call_cost_block_makes_no_request(self, mock_api):
        chosen = [{"ticker": "TEST", "candidate_score": 80, "refresh_mode": "FULL_RESEARCH",
                   "packet": packet()}]
        result = pilot(self.db, 1, chosen, {**CFG, "max_cost_per_stock_usd": 0.001}, "test-key")
        self.assertEqual(result[0]["status"], "BUDGET_BLOCKED")
        self.assertEqual(self.db.execute("SELECT status FROM research_results").fetchone()[0], "BUDGET_BLOCKED")
        mock_api.assert_not_called()

    @patch("phase4_run.api_research")
    def test_valid_api_response_without_verified_sources_is_archived_without_score(self, mock_api):
        raw = api_response()
        raw["output"][0]["content"][0]["text"] = json.dumps(response([event(url=REUTERS_URL)]))
        mock_api.return_value = raw
        chosen = [{"ticker": "TEST", "candidate_score": 80, "refresh_mode": "FULL_RESEARCH",
                   "packet": packet()}]
        result = pilot(self.db, 1, chosen, CFG, "test-key")[0]
        self.assertEqual(result["status"], "UNAVAILABLE")
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM research_response_archive").fetchone()[0], 1)
        self.assertEqual(token_report(self.db, datetime.now(timezone.utc).date().isoformat(), CFG)
                         ["input_tokens"], 5000)


if __name__ == "__main__":
    unittest.main()

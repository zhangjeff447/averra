import sqlite3
import unittest
from datetime import datetime, timedelta, timezone

from phase5_entry import aggregate, cache_intraday, init_db
from shadow_day1_entry import sync_5m
from shadow_day1_initial import classify, token_capacity
from shadow_day1_research import latest_prior


class FakeAlpaca:
    def __init__(self):
        self.requests = 0

    def bars(self, symbols, timeframe, start, end):
        self.requests += 1
        base = datetime(2026, 9, 28, 13, 30, tzinfo=timezone.utc)
        return {symbols[0]: [{"t": (base + timedelta(minutes=5 * i)).isoformat().replace("+00:00", "Z"),
                              "o": 10, "h": 11, "l": 9, "c": 10.5, "v": 100, "vw": 10.2}
                             for i in range(78)]}


class ShadowDay1PipelineTests(unittest.TestCase):
    def test_alpaca_5m_cache_and_local_aggregation(self):
        db = sqlite3.connect(":memory:")
        init_db(db)
        client = FakeAlpaca()
        rows, status = sync_5m(db, client, "TEST")
        self.assertEqual((rows, status, client.requests), (78, "DOWNLOADED", 1))
        self.assertEqual(db.execute("SELECT COUNT(*) FROM entry_intraday_bars WHERE source='ALPACA_SIP'").fetchone()[0], 78)
        bars = cache_intraday(db, "TEST", "2026-09-28")
        self.assertEqual((len(aggregate(bars, "15m")), len(aggregate(bars, "1h"))), (26, 7))
        db.close()

    def test_token_budget_uses_actual_completed_usage(self):
        db = sqlite3.connect(":memory:")
        db.execute("""CREATE TABLE tier_budget_ledger(usage_day TEXT,tier TEXT,run_id TEXT,
            state TEXT,estimated_input_tokens INTEGER,estimated_output_tokens INTEGER,
            actual_input_tokens INTEGER,actual_output_tokens INTEGER)""")
        limits = {"run_id": "SHADOW_DAY1_20260928_INITIAL",
                  "max_daily_initial_input_tokens": 80000,
                  "max_daily_initial_output_tokens": 18000}
        db.execute("INSERT INTO tier_budget_ledger VALUES(?,?,?,?,?,?,?,?)",
                   ("2026-09-29", "INITIAL", limits["run_id"], "COMPLETED", 5000, 1200, 76000, 1000))
        self.assertFalse(token_capacity(db, "2026-09-29", limits, 5000, 1200))
        self.assertTrue(token_capacity(db, "2026-09-29", limits, 3000, 1200))
        db.execute("INSERT INTO tier_budget_ledger VALUES(?,?,?,?,?,?,?,?)",
                   ("2026-09-29", "INITIAL", "SHADOW_DAY2_20260929_INITIAL",
                    "RESERVED", 2000, 500, None, None))
        self.assertFalse(token_capacity(db, "2026-09-29", limits, 3000, 1200))
        db.close()

    def test_cached_and_deferred_never_look_like_final_research(self):
        self.assertEqual(classify({"required_action": "RESEARCH_DEFERRED_BUDGET"}, None)[0],
                         "RESEARCH_DEFERRED_BUDGET")
        cached = {"required_action": "CACHED", "cache_status": "VALID_CACHE",
                  "prior_source": "SOL_DB", "prior_escalation": None}
        self.assertEqual(classify(cached, None)[:2], ("FINAL_RESEARCH_AVAILABLE", "DEEP"))
        cached["prior_escalation"] = {"decision": "ESCALATE_SOL"}
        self.assertEqual(classify(cached, None)[0], "DEEP_RESEARCH_PENDING")

    def test_unknown_initial_request_remains_visible_to_future_cache_check(self):
        db = sqlite3.connect(":memory:")
        db.execute("CREATE TABLE day01_runs(id TEXT,source_research_run INTEGER)")
        db.execute("CREATE TABLE day01_states(run_id TEXT,ticker TEXT,evidence_as_of TEXT,current_candidate_score REAL)")
        db.execute("""CREATE TABLE day01_attempts(run_id TEXT,ticker TEXT,state TEXT,finished_at TEXT,
            parsed_result_json TEXT,escalation_json TEXT,requested_model TEXT,returned_model TEXT)""")
        db.execute("""CREATE TABLE evidence_reasoner_results(run_id TEXT,ticker TEXT,research_status TEXT,
            created_at TEXT,result_json TEXT,evidence_asof TEXT,candidate_score REAL)""")
        db.execute("CREATE TABLE research_response_archive(run_id TEXT,ticker TEXT,response_json TEXT)")
        db.execute("INSERT INTO day01_runs VALUES('OLD',1)")
        db.execute("INSERT INTO day01_states VALUES('OLD','TEST','2026-09-28T20:00:00Z',80)")
        db.execute("""INSERT INTO day01_attempts VALUES('OLD','TEST','FAILED_UNKNOWN',
            '2026-09-28T21:00:00Z',NULL,NULL,'gpt-5.6-luna',NULL)""")
        self.assertEqual(latest_prior(db, "TEST", "NEW")["status"], "FAILED_UNKNOWN")
        db.close()


if __name__ == "__main__":
    unittest.main()

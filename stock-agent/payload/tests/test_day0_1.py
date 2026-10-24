import unittest
import sqlite3
import json
from datetime import datetime
from zoneinfo import ZoneInfo

from day0_1_research import RUN, SCHEMA, cost, result_report
from day0_1_validate import entry_status


class Day01SafetyTests(unittest.TestCase):
    def test_cache_write_cost_uses_provider_detail(self):
        self.assertEqual(cost(1000, 100, {"cache_write_tokens": 900, "cached_tokens": 0}),
                         0.000365)

    def test_invalid_provider_usage_rejected(self):
        with self.assertRaises(ValueError):
            cost(100, 10, {"cache_write_tokens": 101})

    def test_research_after_close_never_backdates_entry(self):
        now = datetime(2026, 9, 27, 12, tzinfo=ZoneInfo("America/New_York"))
        self.assertEqual(entry_status("FINAL_RESEARCH_AVAILABLE", "2026-09-25", "2026-09-25",
                                      "2026-09-27T20:00:00+00:00", now),
                         ("ENTRY_DATA_STALE", "WAIT_FOR_NEXT_MARKET_DATA"))

    def test_missing_research_or_bar_never_signals(self):
        now = datetime(2026, 9, 27, 12, tzinfo=ZoneInfo("America/New_York"))
        self.assertEqual(entry_status("DEEP_RESEARCH_PENDING", "2026-09-25", "2026-09-25",
                                      None, now)[0], "ENTRY_NOT_AVAILABLE")
        self.assertEqual(entry_status("FINAL_RESEARCH_AVAILABLE", "2026-09-24", "2026-09-25",
                                      "2026-09-24T18:00:00+00:00", now)[0], "ENTRY_DATA_STALE")

    def test_insufficient_initial_with_hard_trigger_is_deep_pending(self):
        db = sqlite3.connect(":memory:")
        db.row_factory = sqlite3.Row
        db.executescript(SCHEMA)
        db.execute("""INSERT INTO day01_attempts(run_id,ticker,state,request_started_at,
            estimated_input,estimated_output,estimated_cost,requested_model,escalation_json)
            VALUES(?,?,?,?,?,?,?,?,?)""", (RUN, "TEST", "INSUFFICIENT_EVIDENCE", "2026-09-28T00:00:00+00:00",
                  100, 100, .001, "gpt-5.6-luna", json.dumps({"decision": "ESCALATE_SOL"})))
        report = result_report(db, {"cases": [{"ticker": "TEST", "cache_status": "FULL_RESEARCH_REQUIRED"}]})
        self.assertEqual(report["cases"][0]["final_status"], "DEEP_RESEARCH_PENDING")
        db.close()


if __name__ == "__main__":
    unittest.main()

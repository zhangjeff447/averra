import sqlite3
import unittest

from phase4b_one_time import (COST_ALLOWANCE, INPUT_ALLOWANCE, MAX_CALLS,
                              OUTPUT_ALLOWANCE, PILOT_ID, SCHEMA, permitted, usage)


class OneTimePilotTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.executescript("CREATE TABLE research_runs(id INTEGER PRIMARY KEY);" + SCHEMA)
        self.db.execute("INSERT INTO one_time_pilot VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (PILOT_ID, "ACTIVE", "gpt-6-sol", MAX_CALLS, INPUT_ALLOWANCE,
                         OUTPUT_ALLOWANCE, COST_ALLOWANCE, 1, "2026-09-27T00:00:00Z", None))
        self.prediction = {"estimated_input_tokens": 9000, "estimated_output_tokens": 1200,
                           "estimated_total_cost_usd": .04}

    def tearDown(self):
        self.db.close()

    def insert(self, ticker, status, actual_input=None, actual_output=None, actual_cost=None):
        self.db.execute("""INSERT INTO one_time_pilot_attempts VALUES(
            ?,?,7,'MANUAL_RETRY_AUTHORIZED',?,9000,1200,.04,?,?,?,'2026-09-27T00:00:00Z',NULL)""",
            (PILOT_ID, ticker, status, actual_input, actual_output, actual_cost))

    def test_two_calls_only_and_ticker_scope(self):
        self.assertTrue(permitted(usage(self.db), self.prediction))
        self.insert("CDNA", "COMPLETE", 9000, 800, .03)
        self.assertTrue(permitted(usage(self.db), self.prediction))
        self.insert("AMD", "FAILED_UNKNOWN")
        self.assertEqual(usage(self.db)["calls_used"], 2)
        self.assertFalse(permitted(usage(self.db), self.prediction))
        with self.assertRaises(sqlite3.IntegrityError):
            self.insert("META", "IN_FLIGHT")
        with self.assertRaises(sqlite3.IntegrityError):
            self.insert("CDNA", "IN_FLIGHT")

    def test_unknown_cost_is_reserved_not_zero(self):
        self.insert("CDNA", "FAILED_UNKNOWN")
        state = usage(self.db)
        self.assertEqual(state["input_allowance_used"], 9000)
        self.assertEqual(state["output_allowance_used"], 1200)
        self.assertEqual(state["cost_allowance_used"], .04)

    def test_actual_usage_blocks_second_when_over_remaining(self):
        self.insert("CDNA", "COMPLETE", 14000, 1100, .07)
        self.assertFalse(permitted(usage(self.db), self.prediction))

    def test_no_request_does_not_consume_allowance(self):
        self.insert("CDNA", "FAILED_NO_REQUEST")
        self.assertEqual(usage(self.db)["calls_used"], 0)
        self.assertEqual(usage(self.db)["cost_allowance_used"], 0)


if __name__ == "__main__":
    unittest.main()

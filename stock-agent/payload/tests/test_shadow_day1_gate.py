import sqlite3
import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from shadow_day1_gate import BENCHMARKS, DAY, VOLATILITY, check


class ShadowDay1GateTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.executescript("""CREATE TABLE daily_bars(ticker TEXT,date TEXT);
            CREATE TABLE volatility_daily(symbol TEXT,date TEXT);
            CREATE TABLE market_sync_runs(id INTEGER PRIMARY KEY,asof TEXT,status TEXT);
            CREATE TABLE market_sync_days(date TEXT);""")
        self.after_close = datetime(2026, 9, 28, 17, 0, tzinfo=ZoneInfo("America/New_York"))

    def tearDown(self):
        self.db.close()

    def populate(self):
        self.db.executemany("INSERT INTO daily_bars VALUES(?,?)", [(x, DAY) for x in BENCHMARKS])
        self.db.executemany("INSERT INTO volatility_daily VALUES(?,?)", [(x, DAY) for x in VOLATILITY])
        self.db.execute("INSERT INTO market_sync_runs(asof,status) VALUES(?,?)", (DAY, "COMPLETE"))
        self.db.execute("INSERT INTO market_sync_days VALUES(?)", (DAY,))

    def test_old_bars_block_decision_and_research(self):
        result = check(self.db, self.after_close)
        self.assertEqual(result["status"], "SHADOW_DAY1_WAITING_FOR_DATA")
        self.assertFalse(result["entry_decision_allowed"])
        self.assertFalse(result["ai_research_allowed"])

    def test_complete_data_before_close_still_waits(self):
        self.populate()
        result = check(self.db, datetime(2026, 9, 28, 15, 59, tzinfo=ZoneInfo("America/New_York")))
        self.assertEqual(result["status"], "SHADOW_DAY1_WAITING_FOR_DATA")

    def test_missing_volatility_blocks(self):
        self.populate()
        self.db.execute("DELETE FROM volatility_daily WHERE symbol='VXN'")
        self.assertIn("VXN_DAILY_STALE:None", check(self.db, self.after_close)["missing"])

    def test_complete_after_close_ready(self):
        self.populate()
        result = check(self.db, self.after_close)
        self.assertEqual(result["status"], "SHADOW_DAY1_DATA_READY")
        self.assertTrue(result["entry_decision_allowed"])

    def test_late_data_next_morning_still_counts_as_day1(self):
        self.populate()
        later = datetime(2026, 9, 29, 8, 0, tzinfo=ZoneInfo("America/New_York"))
        self.assertEqual(check(self.db, later)["status"], "SHADOW_DAY1_DATA_READY")

    def test_alpaca_source_must_be_consistent(self):
        self.db.executescript("""DROP TABLE daily_bars; DROP TABLE market_sync_runs;
            DROP TABLE market_sync_days;
            CREATE TABLE daily_bars(ticker TEXT,date TEXT,source TEXT);
            CREATE TABLE market_sync_runs(id INTEGER PRIMARY KEY,asof TEXT,status TEXT,source TEXT);
            CREATE TABLE market_sync_days(date TEXT,source TEXT);""")
        self.db.executemany("INSERT INTO daily_bars VALUES(?,?,?)",
                            [(x, DAY, "ALPACA_SIP") for x in BENCHMARKS])
        self.db.executemany("INSERT INTO volatility_daily VALUES(?,?)", [(x, DAY) for x in VOLATILITY])
        self.db.execute("INSERT INTO market_sync_runs(asof,status,source) VALUES(?,?,?)",
                        (DAY, "COMPLETE", "ALPACA_SIP"))
        self.db.execute("INSERT INTO market_sync_days VALUES(?,?)", (DAY, "ALPACA_SIP"))
        self.assertEqual(check(self.db, self.after_close)["status"], "SHADOW_DAY1_DATA_READY")
        self.db.execute("UPDATE daily_bars SET source='MASSIVE' WHERE ticker='SPY'")
        self.assertIn("SPY_SOURCE_NOT_ALPACA_SIP", check(self.db, self.after_close)["missing"])


if __name__ == "__main__":
    unittest.main()

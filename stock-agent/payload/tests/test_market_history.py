import sqlite3
import tempfile
import unittest
from pathlib import Path

from market_history import check_recent_history, repair_recent_history
from quant_screen import init_db
from shadow_day1_gate import BENCHMARKS


class MarketHistoryTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / "market.db"
        self.db = sqlite3.connect(self.path)
        self.addCleanup(self.db.close)
        init_db(self.db)
        self.symbols = (*BENCHMARKS, "AAPL", "AMD")
        self.db.executemany("""INSERT INTO securities
            (ticker,asset_type,country,exchange,is_etf,is_otc,is_preferred,is_warrant,is_active)
            VALUES(?,'COMMON_STOCK','US','NASDAQ',0,0,0,0,1)""",
                            [(ticker,) for ticker in self.symbols])
        for day in ("2026-09-25", "2026-09-28", "2026-09-29", "2026-09-30"):
            self.add_day(self.db, day)

    def add_day(self, db, day):
        db.executemany("""INSERT OR REPLACE INTO daily_bars
            (ticker,date,open,high,low,close,volume,source)
            VALUES(?,?,10,11,9,10,100,'MASSIVE')""",
                       [(ticker, day) for ticker in self.symbols])
        db.commit()

    def test_current_complete_does_not_mask_a_globally_missing_prior_session(self):
        self.db.execute("DELETE FROM daily_bars WHERE date='2026-09-29'")
        self.db.commit()
        report = check_recent_history(self.db, "2026-09-30", lookback_sessions=3)
        self.assertEqual(report["status"], "HISTORY_GAP")
        self.assertEqual(report["missing_sessions"], ["2026-09-29"])
        self.assertEqual(report["sessions"][-1]["reasons"], [])

    def test_mixed_sources_and_weekend_are_not_false_gaps(self):
        self.db.execute("UPDATE daily_bars SET source='ALPACA_SIP' WHERE date='2026-09-30'")
        report = check_recent_history(self.db, "2026-09-30", lookback_sessions=3)
        self.assertEqual(report["status"], "HISTORY_READY")
        self.assertEqual(report["required_sessions"],
                         ["2026-09-28", "2026-09-29", "2026-09-30"])

    def test_holiday_not_required(self):
        for day in ("2026-09-03", "2026-09-04", "2026-09-08"):
            self.add_day(self.db, day)
        report = check_recent_history(self.db, "2026-09-08", lookback_sessions=2)
        self.assertEqual(report["status"], "HISTORY_READY")
        self.assertNotIn("2026-09-07", report["required_sessions"])

    def test_benchmark_gap_and_invalid_bar_fail_gate(self):
        self.db.execute("UPDATE daily_bars SET close=0 WHERE ticker='SPY' AND date='2026-09-29'")
        report = check_recent_history(self.db, "2026-09-30", lookback_sessions=3)
        day = report["sessions"][1]
        self.assertIn("SPY", day["missing_benchmarks"])
        self.assertIn("SPY", day["invalid_symbols"])

    def test_partial_whole_market_day_fails_coverage(self):
        self.db.execute("DELETE FROM daily_bars WHERE ticker='AAPL' AND date='2026-09-29'")
        report = check_recent_history(self.db, "2026-09-30", lookback_sessions=3)
        self.assertIn("INCOMPLETE_MARKET_SESSION", report["sessions"][1]["reasons"])

    def test_repair_only_missing_session_and_preserve_existing_sources(self):
        self.db.execute("DELETE FROM daily_bars WHERE date='2026-09-29'")
        self.db.commit()
        calls = []

        def fake_sync(day, path, *, preserve_existing):
            calls.append((day, preserve_existing))
            with sqlite3.connect(path) as db:
                self.add_day(db, day)
            return {"requests": 1, "rows": len(self.symbols)}

        report = repair_recent_history("2026-09-30", self.path, lookback_sessions=3,
                                       sync_fn=fake_sync)
        self.assertEqual(calls, [("2026-09-29", True)])
        self.assertEqual(report["status"], "HISTORY_READY")
        self.assertEqual(report["requests"], 1)
        self.assertEqual(report["rows"], len(self.symbols))

    def test_large_gap_is_blocked_before_any_request(self):
        self.db.execute("DELETE FROM daily_bars WHERE date>='2026-09-28'")
        self.db.commit()
        calls = []
        with self.assertRaisesRegex(RuntimeError, "HISTORY_REPAIR_BLOCKED"):
            repair_recent_history("2026-09-30", self.path, lookback_sessions=3,
                                  max_repair_sessions=2, sync_fn=lambda *a, **k: calls.append(a))
        self.assertFalse(calls)

    def test_no_baseline_is_blocked_before_any_request(self):
        self.db.execute("DELETE FROM daily_bars")
        self.db.commit()
        calls = []
        with self.assertRaisesRegex(RuntimeError, "HISTORY_REPAIR_BLOCKED"):
            repair_recent_history("2026-09-30", self.path, lookback_sessions=3,
                                  sync_fn=lambda *a, **k: calls.append(a))
        self.assertFalse(calls)

    def test_failed_repair_stays_blocked(self):
        self.db.execute("DELETE FROM daily_bars WHERE date='2026-09-29'")
        self.db.commit()
        with self.assertRaisesRegex(RuntimeError, "HISTORY_REPAIR_INCOMPLETE"):
            repair_recent_history("2026-09-30", self.path, lookback_sessions=3,
                                  sync_fn=lambda *a, **k: {"requests": 1, "rows": 0})


if __name__ == "__main__":
    unittest.main()

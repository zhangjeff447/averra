"""Offline forward attribution: missing sessions and adjustments never fake returns."""
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import shadow_daily_pipeline as pipeline


class ShadowForwardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "data").mkdir()
        (self.root / "phase26_config.json").write_text(json.dumps({
            "corporate_actions": {"jump_threshold": .45}}))
        self.results = self.root / "data" / "shadow_daily_results.db"
        self.market = sqlite3.connect(self.root / "data" / "market_data.db")
        self.market.executescript("""CREATE TABLE daily_bars (
            ticker TEXT,date TEXT,close REAL,high REAL,low REAL,source TEXT,
            PRIMARY KEY(ticker,date));
            CREATE TABLE corporate_actions(ticker TEXT,action_date TEXT,action_type TEXT);""")
        self.db = sqlite3.connect(self.results)
        self.db.executescript((pipeline.ROOT / "shadow_daily_results.sql").read_text())
        self.day1_schema = (pipeline.ROOT / "shadow_day1_results.sql").read_text()
        self.patches = [patch.object(pipeline, "ROOT", self.root),
                        patch.object(pipeline, "RESULTS_DB", self.results)]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.market.close()
        self.db.close()
        self.temp.cleanup()

    def bars(self, day, *, stock=100, skip=(), source="ALPACA_SIP"):
        with self.market:
            self.market.executemany("INSERT INTO daily_bars VALUES(?,?,?,?,?,?)",
                [(ticker, day, price, price * 1.01, price * .99, source)
                 for ticker, price in (("ABC", stock), ("SPY", 200), ("QQQ", 300))
                 if ticker not in skip])

    def snapshot(self, day="2026-09-28", horizon=1, status="PENDING", end=None):
        with self.db:
            self.db.execute("INSERT INTO snapshots VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (day, "ABC", 1, 90, 90, 90, "FINAL_RESEARCH_AVAILABLE", "FULL_RESEARCH",
                 80, 85, 80, "WATCH", 100, "ALPACA_SIP", day + "T21:00:00Z"))
            self.db.execute("""INSERT INTO forward(trading_date,ticker,horizon,status,end_date,
                return_pct) VALUES(?,?,?,?,?,?)""", (day, "ABC", horizon, status, end,
                                                   777 if status == "COMPLETE" else None))

    def outcome(self):
        return self.db.execute("SELECT status,end_date,return_pct FROM forward").fetchone()

    def reasons(self, result):
        return [reason for item in result["quality_issues"] for reason in item["reasons"]]

    def test_mature_horizon_written_once(self):
        self.snapshot()
        self.bars("2026-09-28")
        self.bars("2026-09-29", stock=110)
        self.assertEqual(pipeline.advance_forward("2026-09-29")["daily"], 1)
        self.assertEqual(self.outcome(), ("COMPLETE", "2026-09-29", 10))
        self.assertEqual(pipeline.advance_forward("2026-09-29")["daily"], 0)

    def test_missing_whole_session_cannot_shift_one_day_horizon(self):
        self.snapshot()
        self.bars("2026-09-28")
        self.bars("2026-09-30", stock=110)
        result = pipeline.advance_forward("2026-09-30")
        self.assertEqual(self.outcome(), ("PENDING", None, None))
        self.assertIn("MISSING_SESSION_BAR:SPY", self.reasons(result))
        self.assertEqual(result["quality_issues"][0]["expected_end_date"], "2026-09-29")

    def test_missing_qqq_stays_pending_without_crash(self):
        self.snapshot()
        self.bars("2026-09-28")
        self.bars("2026-09-29", skip=("QQQ",))
        self.assertIn("MISSING_SESSION_BAR:QQQ", self.reasons(pipeline.advance_forward("2026-09-29")))
        self.assertEqual(self.outcome()[0], "PENDING")

    def test_exchange_holiday_is_not_counted_as_missing_session(self):
        self.snapshot("2026-11-25")
        self.bars("2026-11-25")
        self.bars("2026-11-27", stock=102)
        self.assertEqual(pipeline.advance_forward("2026-11-27")["daily"], 1)
        self.assertEqual(self.outcome(), ("COMPLETE", "2026-11-27", 2))

    def test_known_split_requires_verified_adjustment_basis(self):
        self.snapshot()
        self.bars("2026-09-28")
        self.bars("2026-09-29")
        with self.market:
            self.market.execute("INSERT INTO corporate_actions VALUES('ABC','2026-09-29','forward_split')")
        result = pipeline.advance_forward("2026-09-29")
        self.assertIn("CORPORATE_ACTION_ADJUSTMENT_UNVERIFIED:ABC", self.reasons(result))
        self.assertEqual(self.outcome()[0], "PENDING")

    def test_mixed_sources_do_not_assume_matching_adjustments(self):
        self.snapshot()
        self.bars("2026-09-28")
        self.bars("2026-09-29", source="MASSIVE")
        result = pipeline.advance_forward("2026-09-29")
        self.assertIn("MIXED_SOURCE_ADJUSTMENT_UNVERIFIED:ABC", self.reasons(result))
        self.assertEqual(self.outcome()[0], "PENDING")

    def test_unknown_split_like_jump_is_not_scored_as_loss(self):
        self.snapshot()
        self.bars("2026-09-28")
        self.bars("2026-09-29", stock=50)
        result = pipeline.advance_forward("2026-09-29")
        self.assertIn("PRICE_DISCONTINUITY_UNRESOLVED:ABC", self.reasons(result))
        self.assertEqual(self.outcome()[0], "PENDING")

    def test_nonfinite_price_is_rejected(self):
        self.snapshot()
        self.bars("2026-09-28")
        self.bars("2026-09-29", stock=float("inf"))
        self.assertIn("INVALID_PRICE:ABC", self.reasons(pipeline.advance_forward("2026-09-29")))
        self.assertEqual(self.outcome()[0], "PENDING")

    def test_old_incorrect_complete_horizon_flagged_without_overwrite(self):
        self.snapshot(status="COMPLETE", end="2026-09-30")
        self.bars("2026-09-28")
        self.bars("2026-09-29", stock=110)
        result = pipeline.advance_forward("2026-09-30")
        self.assertIn("HORIZON_CALENDAR_MISMATCH", self.reasons(result))
        self.assertTrue(result["quality_issues"][0]["completed_immutable"])
        self.assertEqual(self.outcome(), ("COMPLETE", "2026-09-30", 777))

    def test_mutated_anchor_cannot_rewrite_frozen_return_basis(self):
        self.snapshot()
        self.bars("2026-09-28", stock=50)
        self.bars("2026-09-29", stock=55)
        result = pipeline.advance_forward("2026-09-29")
        self.assertIn("FROZEN_ANCHOR_PRICE_CHANGED:ABC", self.reasons(result))
        self.assertEqual(self.outcome()[0], "PENDING")

    def test_calendar_correction_preserves_original_and_uses_true_one_day(self):
        self.snapshot(status="COMPLETE", end="2026-09-30")
        self.bars("2026-09-28")
        self.bars("2026-09-29", stock=104)
        self.bars("2026-09-30", stock=130)
        first = pipeline.audit_and_correct_forward("2026-09-30")
        self.assertEqual(first["corrections_written"], 1)
        self.assertEqual(self.outcome(), ("COMPLETE", "2026-09-30", 777))
        self.assertEqual(self.db.execute("SELECT status,end_date,return_pct FROM effective_forward").fetchone(),
                         ("COMPLETE", "2026-09-29", 4))
        original, revised = self.db.execute("SELECT original_json,revised_json FROM forward_corrections").fetchone()
        self.assertEqual(json.loads(original)["return_pct"], 777)
        self.assertEqual(json.loads(revised)["return_pct"], 4)
        self.assertEqual(pipeline.audit_and_correct_forward("2026-09-30")["unchanged"], 1)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM forward_corrections").fetchone()[0], 1)

    def test_missing_true_horizon_is_excluded_without_fabricating_correction(self):
        self.snapshot(status="COMPLETE", end="2026-09-30")
        self.bars("2026-09-28")
        self.bars("2026-09-30", stock=130)
        result = pipeline.audit_and_correct_forward("2026-09-30")
        self.assertEqual((result["corrections_written"], result["invalid_excluded"]), (0, 1))
        self.assertEqual(self.db.execute("SELECT status,end_date,return_pct FROM effective_forward").fetchone(),
                         ("INVALID_EXCLUDED", None, None))
        self.assertIsNone(self.db.execute("SELECT revised_json FROM forward_corrections").fetchone()[0])
        self.assertEqual(self.outcome(), ("COMPLETE", "2026-09-30", 777))
        self.bars("2026-09-29", stock=103)
        self.assertEqual(pipeline.audit_and_correct_forward("2026-09-30")["corrections_written"], 1)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM forward_corrections").fetchone()[0], 2)
        self.assertEqual(self.db.execute("SELECT status,end_date,return_pct FROM effective_forward").fetchone(),
                         ("COMPLETE", "2026-09-29", 3))

    def test_correct_complete_observation_gets_no_correction(self):
        self.snapshot(status="COMPLETE", end="2026-09-29")
        self.bars("2026-09-28")
        self.bars("2026-09-29", stock=103)
        self.assertEqual(pipeline.audit_and_correct_forward("2026-09-30")["corrections_written"], 0)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM forward_corrections").fetchone()[0], 0)
        self.assertEqual(self.db.execute("SELECT return_pct FROM effective_forward").fetchone()[0], 777)

    def test_audit_correction_does_not_fill_unrelated_pending_horizon(self):
        self.snapshot()
        self.bars("2026-09-28")
        self.bars("2026-09-29", stock=102)
        pipeline.audit_and_correct_forward("2026-09-29")
        self.assertEqual(self.outcome(), ("PENDING", None, None))

    def test_day1_uses_its_frozen_benchmark_anchors(self):
        path = self.root / "data" / "shadow_day1_results.db"
        with closing(sqlite3.connect(path)) as db, db:
            db.executescript(self.day1_schema)
            db.execute("INSERT INTO shadow_day1_snapshots VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("DAY1", "ABC", "2026-09-28", "ALPACA_SIP", 1, 90, 90, 90,
                 "FINAL_RESEARCH_AVAILABLE", 80, 85, "WATCH", 80, "{}", 100, 200, 300,
                 "2026-09-28T21:00:00Z"))
            db.execute("INSERT INTO shadow_day1_forward(run_id,ticker,horizon,status) VALUES('DAY1','ABC',1,'PENDING')")
        self.bars("2026-09-28")
        self.bars("2026-09-29", stock=103)
        self.assertEqual(pipeline.advance_forward("2026-09-29")["day1"], 1)
        with closing(sqlite3.connect(path)) as db:
            self.assertEqual(db.execute("SELECT status,return_pct FROM shadow_day1_forward").fetchone(),
                             ("COMPLETE", 3))
            with db:
                db.execute("UPDATE shadow_day1_forward SET end_date='2026-09-30',return_pct=999")
        self.assertEqual(pipeline.audit_and_correct_forward("2026-09-30")["corrections_written"], 1)
        with closing(sqlite3.connect(path)) as db:
            self.assertEqual(db.execute("SELECT run_id,end_date,return_pct FROM effective_forward").fetchone(),
                             ("DAY1", "2026-09-29", 3))
            self.assertEqual(db.execute("SELECT return_pct FROM shadow_day1_forward").fetchone()[0], 999)


if __name__ == "__main__":
    unittest.main()

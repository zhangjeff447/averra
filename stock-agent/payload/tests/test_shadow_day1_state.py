import json
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from threading import Barrier
from zoneinfo import ZoneInfo

from shadow_day1_gate import BENCHMARKS, DAY, VOLATILITY
from shadow_day1_state import (KEY, REQUIRED_PHASES, StateError, action, authorize_resume,
                               begin_pipeline, claim_telegram,
                               connect, fail_pipeline, initialize, pipeline_complete,
                               refresh_from_market, resume_pipeline, row, telegram_failed, telegram_sent)


class ShadowDay1StateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "shadow.db"
        self.db = connect(self.path)
        self.addCleanup(self.db.close)
        self.market = sqlite3.connect(":memory:")
        self.addCleanup(self.market.close)
        self.market.executescript("""CREATE TABLE daily_bars(ticker TEXT,date TEXT);
            CREATE TABLE volatility_daily(symbol TEXT,date TEXT);
            CREATE TABLE market_sync_runs(id INTEGER PRIMARY KEY,asof TEXT,status TEXT);
            CREATE TABLE market_sync_days(date TEXT);""")
        self.now = datetime(2026, 9, 28, 19, tzinfo=ZoneInfo("America/New_York"))

    def ready(self):
        self.market.executemany("INSERT INTO daily_bars VALUES(?,?)",
                                [(x, DAY) for x in BENCHMARKS])
        self.market.executemany("INSERT INTO volatility_daily VALUES(?,?)",
                                [(x, DAY) for x in VOLATILITY])
        self.market.execute("INSERT INTO market_sync_runs(asof,status) VALUES(?,?)",
                            (DAY, "COMPLETE"))
        self.market.execute("INSERT INTO market_sync_days VALUES(?)", (DAY,))
        self.market.commit()
        self.assertEqual(refresh_from_market(self.db, self.market, self.now)[0]["state"], "READY")

    def running(self):
        initialize(self.db)
        self.ready()
        return begin_pipeline(self.db, self.market, self.now)

    def complete_report(self):
        path = Path(self.temp.name) / "final.json"
        path.write_text(json.dumps({"trading_date": DAY, "market_data_as_of": DAY,
                                    "status": "PIPELINE_COMPLETE",
                                    "phase_statuses": {**{name: "COMPLETE" for name in REQUIRED_PHASES},
                                                       "telegram_summary": "READY"}}))
        return path

    def test_waiting_is_persistent_and_cannot_begin_on_old_data(self):
        self.assertEqual(initialize(self.db)["state"], "WAITING_FOR_DATA")
        self.assertEqual(initialize(self.db)["state"], "WAITING_FOR_DATA")
        self.assertEqual(refresh_from_market(self.db, self.market, self.now)[0]["state"],
                         "WAITING_FOR_DATA")
        with self.assertRaisesRegex(StateError, "WAITING_FOR_DATA"):
            begin_pipeline(self.db, self.market, self.now)
        with connect(self.path) as reopened:
            self.assertEqual(row(reopened)["state"], "WAITING_FOR_DATA")

    def test_ready_to_running_claim_is_atomic_across_connections(self):
        initialize(self.db)
        self.ready()
        barrier = Barrier(2)

        # SQLite market connection is thread-bound; use a disk copy for concurrent reads.
        market_path = Path(self.temp.name) / "market.db"
        with sqlite3.connect(market_path) as disk:
            self.market.backup(disk)

        def disk_claim():
            with connect(self.path) as db, sqlite3.connect(market_path) as market:
                barrier.wait()
                try:
                    return begin_pipeline(db, market, self.now)["run_id"]
                except StateError:
                    return "BLOCKED"

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: disk_claim(), range(2)))
        self.assertCountEqual(results, ["SHADOW_DAY1_20260928", "BLOCKED"])
        self.assertEqual(action(row(self.db)), "RUNNING_STOP")

    def test_pipeline_failure_is_terminal_without_automatic_retry(self):
        self.running()
        failed = fail_pipeline(self.db, "PHASE3", ValueError("missing SEC filing"))
        self.assertEqual(failed["state"], "FAILED")
        self.assertEqual(failed["failed_phase"], "PHASE3")
        self.assertEqual(failed["error_type"], "ValueError")
        self.assertIsNotNone(failed["failed_at"])
        self.assertEqual(action(failed), "FAILED_STOP")
        self.assertEqual(refresh_from_market(self.db, self.market, self.now)[0]["state"], "FAILED")
        with self.assertRaises(StateError):
            begin_pipeline(self.db, self.market, self.now)

    def test_one_manual_resume_requires_saved_phase3_and_cannot_repeat(self):
        self.running()
        fail_pipeline(self.db, "RESEARCH_PLANNING", RuntimeError("runner unavailable"))
        self.assertEqual(authorize_resume(self.db)["state"], "RESUME_AUTHORIZED")
        with self.assertRaises(StateError):
            authorize_resume(self.db)
        fundamental = sqlite3.connect(":memory:")
        self.addCleanup(fundamental.close)
        self.market.execute("CREATE TABLE quant_runs(id INTEGER,asof TEXT,data_mode TEXT)")
        self.market.execute("INSERT INTO quant_runs VALUES(7,?,?)", (DAY, "live"))
        fundamental.execute("CREATE TABLE fundamental_runs(id INTEGER,quant_run_id INTEGER,processed_count INTEGER,scan_date TEXT)")
        fundamental.execute("INSERT INTO fundamental_runs VALUES(1,7,100,?)", (DAY,))
        self.assertEqual(resume_pipeline(self.db, self.market, fundamental, self.now)["state"], "RUNNING")
        with self.assertRaises(StateError):
            resume_pipeline(self.db, self.market, fundamental, self.now)
        fail_pipeline(self.db, "RESEARCH_EXECUTION", RuntimeError("network uncertain"))
        with self.assertRaises(StateError):
            authorize_resume(self.db)

    def test_pipeline_complete_then_telegram_only_then_complete(self):
        self.running()
        pending = pipeline_complete(self.db, self.complete_report(),
                                    {"input_tokens": 0, "output_tokens": 0, "cost_usd": 0})
        self.assertEqual(pending["state"], "PIPELINE_COMPLETE_TELEGRAM_PENDING")
        self.assertEqual(action(pending), "TELEGRAM_ONLY")
        self.assertEqual(pending["pipeline_status"], "COMPLETE")
        self.assertIsNotNone(pending["result_sha256"])
        with connect(self.path) as reopened:
            self.assertEqual(action(row(reopened)), "TELEGRAM_ONLY")
        with self.assertRaises(StateError):
            begin_pipeline(self.db, self.market, self.now)
        self.assertEqual(claim_telegram(self.db), KEY)
        with self.assertRaises(StateError):
            claim_telegram(self.db)
        completed = telegram_sent(self.db, KEY, 12345)
        self.assertEqual(completed["state"], "COMPLETE")
        self.assertEqual(completed["telegram_message_id"], 12345)
        self.assertIsNotNone(completed["completed_at"])
        self.assertEqual(action(completed), "ALREADY_COMPLETE")
        with self.assertRaises(StateError):
            claim_telegram(self.db)
        with self.assertRaises(StateError):
            begin_pipeline(self.db, self.market, self.now)

    def test_definite_no_send_can_retry_telegram_only(self):
        self.running()
        pipeline_complete(self.db, self.complete_report(), {})
        key = claim_telegram(self.db)
        pending = telegram_failed(self.db, key, certain_no_send=True)
        self.assertEqual(action(pending), "TELEGRAM_ONLY")
        self.assertEqual(pending["pipeline_status"], "COMPLETE")
        self.assertEqual(claim_telegram(self.db), key)

    def test_unknown_send_never_auto_retries(self):
        self.running()
        pipeline_complete(self.db, self.complete_report(), {})
        key = claim_telegram(self.db)
        unknown = telegram_failed(self.db, key, certain_no_send=False)
        self.assertEqual(action(unknown), "TELEGRAM_RECONCILIATION_REQUIRED")
        with self.assertRaises(StateError):
            claim_telegram(self.db)

    def test_uncommitted_or_wrong_day_result_cannot_finish_pipeline(self):
        self.running()
        bad = Path(self.temp.name) / "bad.json"
        bad.write_text(json.dumps({"trading_date": "2026-09-25", "status": "PIPELINE_COMPLETE"}))
        with self.assertRaises(StateError):
            pipeline_complete(self.db, bad, {})
        self.assertEqual(row(self.db)["state"], "RUNNING")


if __name__ == "__main__":
    unittest.main()

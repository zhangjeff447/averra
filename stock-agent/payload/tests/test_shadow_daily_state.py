import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from shadow_daily_state import (action, begin, claim_telegram, connect, fail, mark_missed,
                                manual_recover_finalization, manual_recover_market_update,
                                initialize, pipeline_complete, ready, record, slot,
                                telegram_sent, telegram_uncertain)

ET = ZoneInfo("America/New_York")
DAY = "2026-09-29"
START = datetime(2026, 9, 29, 16, 35, tzinfo=ET)


class ShadowDailyStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "state.db"
        self.db = connect(self.path)
        initialize(self.db, DAY)

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def test_slots_and_missed_first_check(self):
        self.assertIsNone(slot(datetime(2026, 9, 29, 16, 19, tzinfo=ET)))
        self.assertEqual(slot(datetime(2026, 9, 29, 16, 20, tzinfo=ET))["due_slot_et"], "16:20")
        self.assertEqual(slot(datetime(2026, 9, 29, 16, 42, tzinfo=ET))["due_slot_et"], "16:35")
        self.assertEqual(slot(datetime(2026, 9, 29, 16, 51, tzinfo=ET))["due_slot_et"], "16:50")
        self.assertEqual(slot(datetime(2026, 9, 29, 17, 5, tzinfo=ET))["due_slot_et"], "17:05")
        self.assertIsNone(slot(datetime(2026, 9, 29, 17, 10, tzinfo=ET)))
        self.assertIsNone(slot(datetime(2026, 10, 3, 16, 20, tzinfo=ET)))

    def test_wait_then_ready_atomic_duplicate_trigger(self):
        self.assertEqual(ready(self.db, DAY, False)["state"], "WAITING_FOR_DATA")
        self.assertEqual(ready(self.db, DAY, True)["state"], "READY")
        other = connect(self.path)
        self.assertEqual(begin(self.db, DAY, START)["state"], "RUNNING")
        with self.assertRaisesRegex(ValueError, "RUNNING_STOP"):
            begin(other, DAY, START)
        other.close()

    def test_complete_one_message_and_no_reexecution(self):
        ready(self.db, DAY, True)
        begin(self.db, DAY, START)
        path = Path(self.temp.name) / "result.json"
        path.write_text(json.dumps({"status": "PIPELINE_COMPLETE", "trading_date": DAY,
            "market_data_as_of": DAY, "phase_statuses": {**{k: "COMPLETE" for k in
            ("market", "phase1", "phase2", "phase3", "research", "entry",
             "portfolio", "forward_tracking")}, "telegram_summary": "READY"}}))
        self.assertEqual(pipeline_complete(self.db, DAY, path, {"cost": 0})["state"],
                         "PIPELINE_COMPLETE_TELEGRAM_PENDING")
        key = claim_telegram(self.db, DAY, path)
        self.assertEqual(telegram_sent(self.db, DAY, key, 25)["state"], "COMPLETE")
        self.assertEqual(action(record(self.db, DAY)), "ALREADY_COMPLETE")
        with self.assertRaises(ValueError):
            claim_telegram(self.db, DAY, path)
        with self.assertRaises(ValueError):
            begin(self.db, DAY, START)

    def test_failed_stop_and_unknown_send_no_replay(self):
        ready(self.db, DAY, True)
        begin(self.db, DAY, START)
        fail(self.db, DAY, "RESEARCH", RuntimeError("offline test"))
        self.assertEqual(action(record(self.db, DAY)), "FAILED_STOP")
        with self.assertRaises(ValueError):
            begin(self.db, DAY, START)

    def test_unknown_telegram_requires_reconciliation(self):
        ready(self.db, DAY, True)
        begin(self.db, DAY, START)
        path = Path(self.temp.name) / "result.json"
        path.write_text(json.dumps({"status": "PIPELINE_COMPLETE", "trading_date": DAY,
            "market_data_as_of": DAY, "phase_statuses": {**{k: "COMPLETE" for k in
            ("market", "phase1", "phase2", "phase3", "research", "entry",
             "portfolio", "forward_tracking")}, "telegram_summary": "READY"}}))
        pipeline_complete(self.db, DAY, path, {})
        key = claim_telegram(self.db, DAY, path)
        self.assertEqual(action(telegram_uncertain(self.db, DAY, key)), "TELEGRAM_RECONCILE")
        with self.assertRaises(ValueError):
            claim_telegram(self.db, DAY, path)

    def test_cutoff_only_blocks_new_runs(self):
        late = datetime(2026, 9, 29, 17, 10, tzinfo=ET)
        self.assertEqual(mark_missed(self.db, DAY, late)["state"], "MISSED_DATA_WINDOW")
        with self.assertRaises(ValueError):
            begin(self.db, DAY, START)

    def test_started_before_cutoff_can_finish_after_cutoff(self):
        ready(self.db, DAY, True)
        self.assertEqual(begin(self.db, DAY, START)["state"], "RUNNING")
        late = datetime(2026, 9, 29, 17, 40, tzinfo=ET)
        self.assertEqual(mark_missed(self.db, DAY, late)["state"], "RUNNING")
        path = Path(self.temp.name) / "late.json"
        path.write_text(json.dumps({"status": "PIPELINE_COMPLETE", "trading_date": DAY,
            "market_data_as_of": DAY, "phase_statuses": {**{k: "COMPLETE" for k in
            ("market", "phase1", "phase2", "phase3", "research", "entry",
             "portfolio", "forward_tracking")}, "telegram_summary": "READY"}}))
        pipeline_complete(self.db, DAY, path, {})
        key = claim_telegram(self.db, DAY, path)
        self.assertEqual(telegram_sent(self.db, DAY, key, 31)["state"], "COMPLETE")

    def test_ready_cannot_start_after_cutoff(self):
        ready(self.db, DAY, True)
        self.assertEqual(begin(self.db, DAY, datetime(2026, 9, 29, 17, 11,
                                                     tzinfo=ET))["state"], "MISSED_DATA_WINDOW")

    def test_manual_market_update_recovery_is_audited_and_one_time(self):
        from shadow_daily_state import fail_before_run
        fail_before_run(self.db, DAY, "MARKET_UPDATE", RuntimeError("HTTP_403"))
        with self.assertRaisesRegex(ValueError, "DATA_NOT_READY"):
            manual_recover_market_update(self.db, DAY, gate_ready=False, at=START)
        self.assertEqual(manual_recover_market_update(self.db, DAY, gate_ready=True,
                                                      at=START)["state"], "READY")
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM shadow_daily_recovery_audit").fetchone()[0], 1)
        with self.assertRaisesRegex(ValueError, "NOT_SAFE"):
            manual_recover_market_update(self.db, DAY, gate_ready=True, at=START)

    def test_finalization_recovery_never_replays_unstarted_run(self):
        from shadow_daily_state import fail_before_run
        fail_before_run(self.db, DAY, "MARKET_UPDATE", RuntimeError("HTTP_403"))
        with self.assertRaisesRegex(ValueError, "NOT_SAFE"):
            manual_recover_finalization(self.db, DAY, artifacts_ready=True)

    def test_finalization_recovery_requires_saved_artifacts_and_is_audited(self):
        ready(self.db, DAY, True)
        begin(self.db, DAY, START)
        fail(self.db, DAY, "FINALIZE", ValueError("prior candidate missing"))
        with self.assertRaisesRegex(ValueError, "ARTIFACTS_INCOMPLETE"):
            manual_recover_finalization(self.db, DAY, artifacts_ready=False)
        self.assertEqual(manual_recover_finalization(self.db, DAY,
                                                     artifacts_ready=True)["state"], "RUNNING")
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM shadow_daily_recovery_audit").fetchone()[0], 1)
        with self.assertRaisesRegex(ValueError, "NOT_SAFE"):
            manual_recover_finalization(self.db, DAY, artifacts_ready=True)


if __name__ == "__main__":
    unittest.main()

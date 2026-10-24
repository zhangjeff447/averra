import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

from shadow_daily_scheduler import (alert_once, execution_lock, exit_status,
                                    monitored_tick, run_once)
from shadow_daily_state import connect, initialize, ready, begin, record
from shadow_schedule import authorized_day, shadow_day_number

ET = ZoneInfo('America/New_York')


class MonitorRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'state.db'
        self.notifier = Mock()

    def tearDown(self):
        self.temp.cleanup()

    def tick(self, at, runner):
        return monitored_tick(at, state_path=self.path, runner=runner, notifier=self.notifier)

    def test_ongoing_dates_weekends_holidays_and_dst(self):
        self.assertTrue(authorized_day('2026-10-05'))
        self.assertFalse(authorized_day('2026-10-03'))
        self.assertFalse(authorized_day('2026-12-25'))
        self.assertEqual(shadow_day_number('2026-10-05'), 6)
        from shadow_daily_state import slot
        self.assertEqual(slot(datetime.fromisoformat('2026-11-02T21:20:00+00:00'))['due_slot_et'], '16:20')
        self.assertIsNone(slot(datetime.fromisoformat('2026-11-02T20:20:00+00:00')))

    def test_once_per_slot_even_with_every_minute_wakes(self):
        runner = Mock(return_value={'status': 'WAITING_FOR_DATA', 'day': '2026-10-01'})
        self.assertEqual(self.tick(datetime(2026,10,1,16,20,tzinfo=ET), runner)['status'], 'WAITING_FOR_DATA')
        self.assertEqual(self.tick(datetime(2026,10,1,16,21,tzinfo=ET), runner)['status'], 'SLOT_ALREADY_CHECKED')
        self.tick(datetime(2026,10,1,16,35,tzinfo=ET), runner)
        self.assertEqual(runner.call_count, 2)

    def test_orphan_is_failed_without_research_replay(self):
        with connect(self.path) as db:
            initialize(db,'2026-10-01'); ready(db,'2026-10-01',True)
            begin(db,'2026-10-01',datetime(2026,10,1,16,20,tzinfo=ET))
        runner = Mock()
        result = self.tick(datetime(2026,10,1,17,40,tzinfo=ET),runner)
        self.assertEqual(result['status'],'FAILED_STOP')
        runner.assert_not_called()
        with connect(self.path) as db:
            self.assertEqual(record(db,'2026-10-01')['error_message'],'PROCESS_INTERRUPTED_MANUAL_REVIEW_REQUIRED')

    def test_failure_blocks_later_day_and_has_nonzero_exit(self):
        with connect(self.path) as db:
            initialize(db,'2026-10-01')
            db.execute("UPDATE shadow_daily_runs SET state='FAILED',failed_phase='RESEARCH'")
        runner = Mock()
        result = self.tick(datetime(2026,10,2,16,20,tzinfo=ET),runner)
        self.assertEqual(exit_status(result),1)
        runner.assert_not_called()

    def test_post_cutoff_no_new_pipeline(self):
        runner = Mock()
        self.assertEqual(self.tick(datetime(2026,10,1,17,11,tzinfo=ET),runner)['status'],'MISSED_DATA_WINDOW')
        runner.assert_not_called()

    def test_wake_next_day_records_missed_prior_session_without_catchup(self):
        with connect(self.path) as db:
            initialize(db,'2026-09-30')
            db.execute("UPDATE shadow_daily_runs SET state='COMPLETE'")
        runner=Mock()
        self.tick(datetime(2026,10,2,9,0,tzinfo=ET),runner)
        runner.assert_not_called()
        with connect(self.path) as db:
            self.assertEqual(record(db,'2026-10-01')['state'],'MISSED_DATA_WINDOW')

    def test_complete_no_network_or_duplicate_send(self):
        with connect(self.path) as db:
            initialize(db,'2026-10-01')
            db.execute("UPDATE shadow_daily_runs SET state='COMPLETE',telegram_message_id=25,telegram_status='SENT'")
        runner=Mock()
        self.assertEqual(self.tick(datetime(2026,10,1,16,35,tzinfo=ET),runner)['status'],'ALREADY_COMPLETE')
        runner.assert_not_called(); self.notifier.assert_not_called()

    def test_active_lock_cannot_be_stolen(self):
        path=Path(self.temp.name)/'lock'
        with execution_lock(path) as first:
            with execution_lock(path) as second:
                self.assertTrue(first); self.assertFalse(second)
        with execution_lock(path) as third:
            self.assertTrue(third)

    def test_alert_sent_once_and_unknown_not_retried(self):
        with connect(self.path) as db, patch('shadow_daily_scheduler.load_env'), patch.dict(
                'os.environ', {'STOCK_TELEGRAM_BOT_TOKEN':'test-only','STOCK_TELEGRAM_CHAT_ID':'test'}):
            initialize(db,'2026-10-01')
            with patch('shadow_daily_scheduler.request_json',return_value={'ok':True,'result':{'message_id':99}}) as send:
                alert_once(db,'2026-10-01','FAILED')
                alert_once(db,'2026-10-01','FAILED')
                self.assertEqual(send.call_count,1)
            with patch('shadow_daily_scheduler.request_json',side_effect=TimeoutError) as send:
                alert_once(db,'2026-10-01','TELEGRAM_RECONCILE')
                alert_once(db,'2026-10-01','TELEGRAM_RECONCILE')
                self.assertEqual(send.call_count,1)
                self.assertEqual(db.execute("SELECT status FROM shadow_monitor_alerts WHERE reason='TELEGRAM_RECONCILE'").fetchone()[0],'UNKNOWN')

    def test_gate_exception_is_durable_failed(self):
        def runner(at, **kw):
            return run_once(at, **kw, updater=lambda _: {}, gate=Mock(side_effect=ValueError('bad data')))
        out=self.tick(datetime(2026,10,1,16,20,tzinfo=ET),runner)
        self.assertEqual(out['status'],'FAILED')
        with connect(self.path) as db:
            self.assertEqual(record(db,'2026-10-01')['state'],'FAILED')


if __name__=='__main__': unittest.main()

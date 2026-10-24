import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from monitor_status import exit_status, next_data_check, notify_ready, service_status, snapshot, telegram_check


class MonitorStatusTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "data").mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def ready_report(self):
        with sqlite3.connect(self.root / "data" / "stock_agent.db"):
            pass
        return {"status": "MONITOR_READY", "next_expected_data_check_et": "2026-10-01T16:20:00-04:00", "tracked_holdings": 0}

    def test_ready_message_success_is_durable_and_never_resent(self):
        report = self.ready_report()
        checker, sender = Mock(return_value={"username": "AverraStockAgentBot"}), Mock(return_value=27)
        first = notify_ready(report, self.root, checker=checker, sender=sender)
        second = notify_ready(report, self.root, checker=checker, sender=sender)
        self.assertEqual(first["message_id"], 27)
        self.assertEqual(second["status"], "ALREADY_SENT")
        checker.assert_called_once()
        sender.assert_called_once()

    def test_unknown_ready_send_never_retries(self):
        report = self.ready_report()
        checker, sender = Mock(return_value={}), Mock(side_effect=TimeoutError("secret"))
        self.assertEqual(notify_ready(report, self.root, checker=checker, sender=sender)["status"], "UNKNOWN")
        self.assertEqual(notify_ready(report, self.root, checker=checker, sender=sender)["status"], "ALREADY_UNKNOWN")
        sender.assert_called_once()

    def test_unready_monitor_blocks_send_and_network(self):
        report = {**self.ready_report(), "status": "MONITOR_NOT_READY"}
        checker, sender = Mock(), Mock()
        with self.assertRaisesRegex(ValueError, "SEND_BLOCKED"):
            notify_ready(report, self.root, checker=checker, sender=sender)
        checker.assert_not_called()
        sender.assert_not_called()

    def test_identity_check_rejects_webhook_conflict(self):
        request = Mock(side_effect=[{"ok": True, "result": {"is_bot": True, "id": 2, "username": "AverraStockAgentBot"}},
                                    {"ok": True, "result": {"url": "https://example.invalid/webhook"}}])
        with patch.dict(os.environ, {"STOCK_TELEGRAM_TOKEN": "secret", "STOCK_TELEGRAM_CHAT_ID": "123"}):
            with self.assertRaisesRegex(ValueError, "WEBHOOK_CONFLICT"):
                telegram_check(self.root, requester=request)
        self.assertEqual(request.call_count, 2)

    def test_identity_report_never_contains_secret_and_validates_private_chat(self):
        request = Mock(side_effect=[{"ok": True, "result": {"is_bot": True, "id": 2, "username": "AverraStockAgentBot"}},
                                    {"ok": True, "result": {"url": ""}},
                                    {"ok": True, "result": {"id": 123, "type": "private"}}])
        with patch.dict(os.environ, {"STOCK_TELEGRAM_TOKEN": "secret", "STOCK_TELEGRAM_CHAT_ID": "123"}):
            result = telegram_check(self.root, requester=request)
        self.assertEqual(result["network_requests"], 3)
        self.assertNotIn("secret", json.dumps(result))

    def test_offline_missing_state_does_not_create_databases_or_call_network(self):
        (self.root / "shadow_v11_config.json").write_text(json.dumps({"max_daily_initial_calls": 10,
            "max_daily_initial_cost_usd": .05, "max_daily_initial_input_tokens": 80000,
            "max_daily_initial_output_tokens": 18000}))
        (self.root / "phase4_tiers_config.json").write_text(json.dumps({"initial_research_model": "gpt-5.6-luna",
            "deep_research_model": "gpt-6-sol", "web_search_enabled": False}))
        before = sorted(p.name for p in self.root.rglob("*"))
        with patch("monitor_status.request_json") as network:
            result = snapshot(self.root, service_reader=lambda label: {"label": label, "installed": False, "loaded": False})
            network.assert_not_called()
        self.assertEqual(before, sorted(p.name for p in self.root.rglob("*")))
        self.assertEqual(result["status"], "MONITOR_NOT_READY")

    def test_launchd_output_sanitized(self):
        result = Mock(returncode=0, stdout="\tstate = running\n\tpid = 123\n\tlast exit code = 0\n\tSECRET = sensitive\n")
        with patch("monitor_status.subprocess.run", return_value=result):
            status = service_status("com.averra.stock.commands", self.root)
        self.assertEqual(status["pid"], 123)
        self.assertNotIn("sensitive", json.dumps(status))

    def test_complete_day_next_check_is_next_market_session(self):
        at = datetime(2026, 9, 30, 21, 30, tzinfo=timezone.utc)
        self.assertEqual(next_data_check(at, {"2026-09-30": "COMPLETE"}), "2026-10-01T16:20:00-04:00")

    def test_unready_or_unknown_notification_is_nonzero_exit(self):
        self.assertEqual(exit_status({"status": "MONITOR_READY"}), 0)
        self.assertEqual(exit_status({"status": "MONITOR_NOT_READY"}), 1)
        self.assertEqual(exit_status({"status": "CHECK_FAILED"}), 1)
        for state in ("UNKNOWN", "ALREADY_UNKNOWN", "ALREADY_IN_FLIGHT"):
            self.assertEqual(exit_status({"status": "MONITOR_READY", "ready_notification": {"status": state}}), 1)
        for state in ("SENT", "ALREADY_SENT"):
            self.assertEqual(exit_status({"status": "MONITOR_READY", "ready_notification": {"status": state}}), 0)


if __name__ == "__main__":
    unittest.main()

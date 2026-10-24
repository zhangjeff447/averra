"""Offline tests for forwarding only confirmed automatic Stock Bot notices."""
import sqlite3
import tempfile
import unittest
from pathlib import Path

from telegram_mirror import activate, connect, deliver


class MirrorTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.root = Path(self.folder.name)
        (self.root / "data").mkdir()
        self.db = connect(self.root / "data" / "telegram_mirror.db")
        with sqlite3.connect(self.root / "data" / "shadow_daily_state.db") as source:
            source.execute("""CREATE TABLE shadow_daily_runs
              (trading_date TEXT,state TEXT,telegram_status TEXT,telegram_message_id INTEGER,
               completed_at TEXT)""")
            source.executemany("INSERT INTO shadow_daily_runs VALUES(?,?,?,?,?)", [
                ("2026-10-01", "COMPLETE", "SENT", 11, "2026-10-01T21:00:00+00:00"),
                ("2026-10-02", "COMPLETE", "SENT", 12, "2026-10-02T21:00:00+00:00"),
                ("2026-10-03", "FAILED", "UNKNOWN", 13, "2026-10-03T21:00:00+00:00")])
        with sqlite3.connect(self.root / "data" / "intraday_monitor.db") as source:
            source.execute("""CREATE TABLE intraday_alerts
              (id INTEGER,send_status TEXT,telegram_message_id INTEGER,sent_at TEXT)""")
            source.execute("INSERT INTO intraday_alerts VALUES(1,'SENT',14,'2026-10-02T20:00:00+00:00')")
        with sqlite3.connect(self.root / "data" / "stock_agent.db") as source:
            source.execute("CREATE TABLE telegram_command_receipts(message_id INTEGER,state TEXT)")
            source.execute("INSERT INTO telegram_command_receipts VALUES(99,'SENT')")

    def tearDown(self):
        self.db.close()
        self.folder.cleanup()

    def activate(self):
        activate(self.db, "123456789", activated_at="2026-10-02T19:00:00+00:00",
                 verifier=lambda _: {"id": 123456789, "type": "private"})

    def test_requires_exact_verified_private_chat(self):
        with self.assertRaisesRegex(ValueError, "TARGET_CHAT_NOT_VERIFIED"):
            activate(self.db, "123456789", verifier=lambda _: {"id": 1, "type": "private"})
        with self.assertRaisesRegex(ValueError, "TARGET_CHAT_NOT_VERIFIED"):
            activate(self.db, "123456789", verifier=lambda _: {"id": 123456789, "type": "group"})
        self.assertEqual(deliver(self.db, self.root, "OWNER", lambda *_: 1)["status"], "NOT_ACTIVE")

    def test_future_automatic_only_and_idempotent(self):
        self.activate()
        calls = []
        def forward(source, target, message_id):
            calls.append((source, target, message_id))
            return 100 + message_id
        self.assertEqual(deliver(self.db, self.root, "OWNER", forward),
                         {"status": "ACTIVE", "sent": 2, "unknown": 0})
        self.assertEqual({item[2] for item in calls}, {12, 14})
        self.assertNotIn(99, {item[2] for item in calls})
        self.assertEqual(deliver(self.db, self.root, "OWNER", forward)["sent"], 0)
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM mirror_receipts WHERE state='SENT'").fetchone()[0], 2)

    def test_uncertain_forward_is_never_retried(self):
        self.activate()
        calls = []
        def fail(*args):
            calls.append(args)
            raise TimeoutError()
        result = deliver(self.db, self.root, "OWNER", fail)
        self.assertEqual(result["unknown"], 2)
        self.assertEqual(deliver(self.db, self.root, "OWNER", fail)["unknown"], 0)
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM mirror_receipts WHERE state='UNKNOWN'").fetchone()[0], 2)


if __name__ == "__main__":
    unittest.main()

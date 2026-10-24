import hashlib
import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from portfolio import Portfolio
from stock_agent import (command, command_snapshot, process_telegram_update, serve,
                         shadow_candidates, telegram_send)


def update(number, text, chat="123"):
    return {"update_id": number, "message": {"chat": {"id": chat, "type": "private"},
            "from": {"id": chat}, "text": text}}


class TelegramCommandTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.book = Portfolio(self.root / "stock_agent.db")

    def tearDown(self):
        self.book.close()
        self.temp.cleanup()

    def receipt(self, number):
        return self.book.db.execute("SELECT * FROM telegram_command_receipts WHERE update_id=?", (number,)).fetchone()

    def test_redelivered_buy_and_reply_are_exactly_once_across_restart(self):
        sender = Mock(return_value=99)
        snap = {"stocks": [{"ticker": "AMD", "asset_type": "COMMON_STOCK", "country": "US", "exchange": "NASDAQ", "is_active": True}]}
        item = update(8, "/bought AMD")
        self.assertEqual(process_telegram_update(self.book, item, "secret", "123", sender=sender,
                                                  snapshotter=lambda *_: snap), 9)
        self.book.close()
        self.book = Portfolio(self.root / "stock_agent.db")
        process_telegram_update(self.book, item, "secret", "123", sender=sender,
                                snapshotter=lambda *_: snap)
        self.assertEqual(self.book.db.execute("SELECT COUNT(*) FROM holding_events").fetchone()[0], 1)
        sender.assert_called_once()
        self.assertEqual(self.receipt(8)["state"], "SENT")
        self.assertEqual(self.receipt(8)["message_id"], 99)

    def test_uncertain_send_does_not_reapply_or_resend_and_next_command_works(self):
        sender = Mock(side_effect=TimeoutError())
        item = update(1, "/bought AMD")
        snap = {"stocks": [{"ticker": "AMD", "asset_type": "COMMON_STOCK", "country": "US", "exchange": "NASDAQ", "is_active": True}]}
        process_telegram_update(self.book, item, "secret", "123", sender=sender, snapshotter=lambda *_: snap)
        process_telegram_update(self.book, item, "secret", "123", sender=sender, snapshotter=lambda *_: snap)
        sender.assert_called_once()
        self.assertEqual(self.receipt(1)["state"], "UNKNOWN")
        self.assertEqual(self.book.db.execute("SELECT COUNT(*) FROM holding_events").fetchone()[0], 1)
        followup = Mock(return_value=20)
        self.assertEqual(process_telegram_update(self.book, update(2, "/portfolio"), "secret", "123", sender=followup), 3)
        self.assertIn("AMD", followup.call_args.args[2])

    def test_process_interruption_is_unknown_not_retry(self):
        command(self.book, "/bought AMD", {"stocks": [{"ticker": "AMD", "asset_type": "COMMON_STOCK", "country": "US", "exchange": "NASDAQ", "is_active": True}]}, update_id=1)
        with self.book.db:
            self.book.db.execute("INSERT INTO telegram_command_receipts(update_id,state) VALUES(1,'IN_FLIGHT')")
        sender = Mock(return_value=10)
        process_telegram_update(self.book, update(1, "/bought AMD"), "secret", "123", sender=sender)
        sender.assert_not_called()
        self.assertEqual(self.book.db.execute("SELECT COUNT(*) FROM holding_events").fetchone()[0], 1)
        self.assertEqual(self.receipt(1)["state"], "UNKNOWN")

    def test_unauthorized_and_malformed_money_commands_do_not_mutate(self):
        sender = Mock(return_value=10)
        process_telegram_update(self.book, update(1, "/deposit 1000", "999"), "secret", "123", sender=sender)
        sender.assert_not_called()
        process_telegram_update(self.book, update(2, "/deposit banana"), "secret", "123", sender=sender)
        self.assertIn("retired", sender.call_args.args[2])
        self.assertEqual(self.book.cash_cents(), 0)

    def test_send_requires_successful_message_id(self):
        with patch("stock_agent.request_json", return_value={"ok": False}):
            with self.assertRaisesRegex(ValueError, "UNCONFIRMED"):
                telegram_send("secret", "123", "message")
        with patch("stock_agent.request_json", return_value={"ok": True, "result": {"message_id": 42}}) as request:
            self.assertEqual(telegram_send("secret", "123", "message"), 42)
            self.assertFalse(request.call_args.kwargs["retry"])

    def test_commands_only_cannot_enter_legacy_scans_even_if_env_enabled(self):
        clock = datetime(2026, 9, 30, 20, 5, tzinfo=timezone.utc)
        with patch.dict(os.environ, {"STOCK_TELEGRAM_TOKEN": "secret", "STOCK_TELEGRAM_CHAT_ID": "123", "STOCK_ALERTS_ENABLED": "true"}), \
                patch("stock_agent.ROOT", self.root), patch("stock_agent.datetime") as dt, \
                patch("stock_agent.request_json", return_value={"ok": True, "result": []}), \
                patch("stock_agent.phase6_scan") as scan, patch("stock_agent.ai_event_note") as ai, \
                patch("stock_agent.time.sleep", side_effect=KeyboardInterrupt):
            dt.now.return_value = clock
            with self.assertRaises(KeyboardInterrupt):
                serve(self.book, {"timezone": "UTC", "scan_times": ["20:05"]}, "file", commands_only=True)
            scan.assert_not_called()
            ai.assert_not_called()

    def make_market(self):
        path = self.root / "market_data.db"
        with sqlite3.connect(path) as db:
            db.execute("CREATE TABLE securities(ticker,asset_type,country,exchange,is_etf,is_otc,is_preferred,is_warrant,is_active,normalized_sector)")
            db.execute("INSERT INTO securities VALUES('AMD','COMMON_STOCK','US','NASDAQ',0,0,0,0,1,'Technology')")
            db.execute("INSERT INTO securities VALUES('SPY','ETF','US','NYSE',1,0,0,0,1,'Unknown')")
            db.execute("CREATE TABLE daily_bars(ticker,date,close)")
            db.execute("INSERT INTO daily_bars VALUES('AMD','2026-09-30',200)")
        return path

    def test_actual_confirmation_uses_validated_cached_security_without_network(self):
        self.make_market()
        with patch("stock_agent.request_json") as network:
            snap = command_snapshot(self.book, "/bought AMD")
            result = command(self.book, "/bought AMD", snap)
            self.assertIn("now tracked", result)
            self.assertEqual(self.book.cash_cents(), 0)
            self.assertEqual(self.book.db.execute("SELECT status FROM tracked_holdings WHERE ticker='AMD'").fetchone()[0], "HELD")
            self.assertEqual(command_snapshot(self.book, "/bought SPY"), {"stocks": []})
            network.assert_not_called()

    def test_stale_cached_close_cannot_invent_amount_only_fill(self):
        self.make_market()
        with patch("stock_agent.price_is_fresh", return_value=False):
            snap = command_snapshot(self.book, "/bought AMD")
        result = command(self.book, "/bought AMD 200", snap)
        self.assertIn("Entry price $200.00", result)
        self.assertEqual(self.book.cash_cents(), 0)
        self.assertEqual(len(self.book.positions()), 0)

    def test_status_and_candidates_use_completed_saved_report_and_integrity(self):
        path = self.root / "shadow_daily_state.db"
        report_path = self.root / "final.json"
        report = {"status": "PIPELINE_COMPLETE", "trading_date": "2026-09-30", "target_session": "2026-10-01",
                  "eligibility_cases": [{"ticker": "AMD", "candidate_rank": 1, "eligibility": "RESEARCH_LIMITED"}],
                  "entry_results": [{"ticker": "AMD", "entry_state": "WATCH", "entry_score": 60}],
                  "portfolio": {"allocations": [{"ticker": "AMD", "suggested_cents": 0}]}}
        raw = json.dumps(report).encode()
        report_path.write_bytes(raw)
        with sqlite3.connect(path) as db:
            db.execute("CREATE TABLE shadow_daily_runs(run_type,trading_date,state,market_data_as_of,telegram_status,telegram_message_id,result_path,result_sha256)")
            db.execute("INSERT INTO shadow_daily_runs VALUES('SHADOW_DAILY','2026-09-30','COMPLETE','2026-09-30','SENT',25,?,?)",
                       (str(report_path), hashlib.sha256(raw).hexdigest()))
        self.assertIn("COMPLETE", command(self.book, "/status", {"stocks": []}))
        self.assertIn("message ID 25", command(self.book, "/status", {"stocks": []}))
        self.assertIn("AMD: WATCH", command(self.book, "/candidates", {"stocks": []}))
        report_path.write_text("{}")
        with self.assertRaisesRegex(ValueError, "integrity"):
            shadow_candidates(path)


if __name__ == "__main__":
    unittest.main()

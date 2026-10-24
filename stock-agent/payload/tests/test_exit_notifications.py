import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from exit_notifications import PRIORITY, dispatch_alerts, plan_alert, position_summary_text
from holdings_monitor import confirm, held
from portfolio import Portfolio
from shadow_daily_scheduler import render

DAYS = ("2026-09-28", "2026-09-29", "2026-09-30", "2026-10-01")


def decision(state, score=20, *, reasons=None, drawdown=-1, profit="NORMAL_PROFIT",
             trend="INTACT", event="EARNINGS_NORMAL", thesis="INTACT_WITH_AVAILABLE_EVIDENCE"):
    return {"ticker": "MOCKB", "action": state, "reason": (reasons or [state])[0],
            "reasons": reasons or [state], "exit_score": score, "close_price": 110,
            "position": {"entry_price": 100, "current_price": 110, "return_since_entry_pct": 10,
                         "drawdown_from_peak_pct": drawdown, "profit_state": profit},
            "trend": {"state": trend}, "event": {"state": event},
            "thesis": {"state": thesis}, "profit_protection_zone": [106, 108],
            "trend_failure_zone": [100, 102]}


class ExitNotificationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.book = Portfolio(Path(self.temp.name) / "book.db")
        with self.book.atomic():
            confirm(self.book.db, "MOCKB", "BOUGHT", price="100")
        self.lifecycle = held(self.book.db)[0]["confirmed_at"]
        self.state_path = Path(self.temp.name) / "shadow.db"
        with sqlite3.connect(self.state_path) as db:
            db.execute("""CREATE TABLE shadow_daily_runs(trading_date TEXT PRIMARY KEY,
                state TEXT,telegram_status TEXT,telegram_message_id INTEGER)""")

    def tearDown(self):
        self.book.close()
        self.temp.cleanup()

    def record(self, day, result):
        with self.book.atomic():
            key = plan_alert(self.book.db, self.lifecycle, day, result)
            self.book.db.execute("""INSERT INTO exit_decisions
                (trading_date,ticker,exit_state,exit_score,exit_confidence,details_json,evaluated_at,lifecycle_id)
                VALUES(?,?,?,?,?,?,?,?)""",
                (day, "MOCKB", result["action"], result["exit_score"], "HIGH",
                 json.dumps(result), day + "T20:00:00Z", self.lifecycle))
        return key

    def sent(self, day):
        with sqlite3.connect(self.state_path) as db:
            db.execute("INSERT OR REPLACE INTO shadow_daily_runs VALUES(?,?,?,?)",
                       (day, "COMPLETE", "SENT", 100))
        return dispatch_alerts(self.book, self.state_path, "unused", "unused",
                               lambda *args: 101)

    def alerts(self):
        return [dict(row) for row in self.book.db.execute("SELECT * FROM exit_highlight_alerts ORDER BY trading_date")]

    def test_hold_repeat_suppressed_but_daily_summary_contains_position(self):
        self.assertIsNone(self.record(DAYS[0], decision("HOLD")))
        self.assertIsNone(self.record(DAYS[1], decision("HOLD")))
        self.assertEqual(self.alerts(), [])
        text = position_summary_text(decision("HOLD"))
        self.assertIn("Entry $100.00", text)
        self.assertIn("Return +10.00%", text)

    def test_hold_tight_first_then_repeat_then_materially_worse(self):
        self.record(DAYS[0], decision("HOLD"))
        self.assertIsNotNone(self.record(DAYS[1], decision("HOLD_TIGHT")))
        self.sent(DAYS[1])
        self.assertIsNone(self.record(DAYS[2], decision("HOLD_TIGHT")))
        self.assertIsNotNone(self.record(DAYS[3], decision("HOLD_TIGHT", drawdown=-4)))

    def test_hold_to_trim_repeat_and_reduce_exit(self):
        self.record(DAYS[0], decision("HOLD"))
        self.record(DAYS[1], decision("TRIM_CANDIDATE"))
        self.sent(DAYS[1])
        self.assertIsNone(self.record(DAYS[2], decision("TRIM_CANDIDATE")))
        self.assertIsNotNone(self.record(DAYS[3], decision("REDUCE_CANDIDATE")))
        self.assertEqual(self.alerts()[-1]["priority"], "HIGH")

    def test_reduce_to_exit_is_critical(self):
        self.record(DAYS[0], decision("REDUCE_CANDIDATE"))
        self.sent(DAYS[0])
        self.record(DAYS[1], decision("EXIT_CANDIDATE"))
        self.assertEqual(self.alerts()[-1]["priority"], "CRITICAL")

    def test_hold_to_thesis_review_is_high(self):
        self.record(DAYS[0], decision("HOLD"))
        self.record(DAYS[1], decision("THESIS_REVIEW"))
        self.assertEqual(self.alerts()[-1]["priority"], "HIGH")

    def test_trim_repeat_with_new_reason_highlights(self):
        self.record(DAYS[0], decision("TRIM_CANDIDATE", reasons=["OVERHEAT"]))
        self.sent(DAYS[0])
        self.assertIsNotNone(self.record(DAYS[1], decision("TRIM_CANDIDATE", reasons=["OVERHEAT", "VOLUME_CLIMAX"])))

    def test_exit_reminder_after_cooldown(self):
        self.record(DAYS[0], decision("EXIT_CANDIDATE"))
        self.sent(DAYS[0])
        self.assertIsNone(self.record(DAYS[1], decision("EXIT_CANDIDATE")))
        self.assertIsNotNone(self.record(DAYS[2], decision("EXIT_CANDIDATE")))
        self.assertEqual(self.alerts()[-1]["alert_kind"], "CRITICAL_REMINDER")

    def test_thesis_review_reminder_after_cooldown(self):
        self.record(DAYS[0], decision("THESIS_REVIEW"))
        self.sent(DAYS[0])
        self.assertIsNone(self.record(DAYS[1], decision("THESIS_REVIEW")))
        self.assertIsNone(self.record(DAYS[2], decision("THESIS_REVIEW")))
        self.assertIsNotNone(self.record(DAYS[3], decision("THESIS_REVIEW")))

    def test_reduce_to_hold_recovery_and_opt_out(self):
        self.record(DAYS[0], decision("REDUCE_CANDIDATE"))
        self.sent(DAYS[0])
        key = self.record(DAYS[1], decision("HOLD"))
        self.assertIsNotNone(key)
        self.assertEqual(self.alerts()[-1]["alert_kind"], "POSITION_STATUS_IMPROVED")
        self.assertEqual(self.alerts()[-1]["priority"], "INFO")
        self.sent(DAYS[1])
        disabled = {"notification": {"exit_reminder_trading_days": 2,
                    "thesis_review_reminder_trading_days": 3,
                    "hold_tight_drawdown_increase_pct": 2,
                    "trim_reduce_score_increase": 12,
                    "recovery_alert_enabled": False}}
        self.record(DAYS[2], decision("REDUCE_CANDIDATE"))
        self.sent(DAYS[2])
        with self.book.atomic():
            self.assertIsNone(plan_alert(self.book.db, self.lifecycle, DAYS[3], decision("HOLD"), disabled))

    def test_sold_cancels_pending_and_rebuy_has_new_lifecycle(self):
        self.record(DAYS[0], decision("EXIT_CANDIDATE"))
        with self.book.atomic():
            confirm(self.book.db, "MOCKB", "SOLD", price="110")
        self.assertEqual(self.alerts()[0]["status"], "CANCELLED")
        self.assertEqual(self.sent(DAYS[0]), 0)
        with self.book.atomic():
            confirm(self.book.db, "MOCKB", "BOUGHT", price="120")
        newer = held(self.book.db)[0]["confirmed_at"]
        self.assertNotEqual(newer, self.lifecycle)
        self.assertIsNone(plan_alert(self.book.db, newer, DAYS[1], decision("HOLD")))

    def test_duplicate_delivery_and_unknown_send_do_not_retry(self):
        self.record(DAYS[0], decision("EXIT_CANDIDATE"))
        self.assertEqual(self.sent(DAYS[0]), 1)
        self.assertEqual(self.sent(DAYS[0]), 0)
        self.assertEqual(self.alerts()[0]["message_id"], 101)
        self.record(DAYS[1], decision("THESIS_REVIEW"))
        with sqlite3.connect(self.state_path) as db:
            db.execute("INSERT INTO shadow_daily_runs VALUES(?,?,?,?)", (DAYS[1], "COMPLETE", "SENT", 102))
        calls = []
        def uncertain(*args):
            calls.append(1)
            raise TimeoutError("unknown delivery")
        dispatch_alerts(self.book, self.state_path, "unused", "unused", uncertain)
        dispatch_alerts(self.book, self.state_path, "unused", "unused", uncertain)
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.alerts()[-1]["status"], "UNKNOWN")

    def test_main_summary_must_be_confirmed_before_highlight(self):
        self.record(DAYS[0], decision("EXIT_CANDIDATE"))
        self.assertEqual(dispatch_alerts(self.book, self.state_path, "x", "y", lambda *a: 1), 0)
        self.assertEqual(self.alerts()[0]["status"], "PENDING")

    def test_two_positions_fit_one_daily_summary_without_hold_highlights(self):
        first = decision("HOLD")
        second = {**decision("HOLD_TIGHT"), "ticker": "META"}
        report = {"trading_date": DAYS[3], "status": "PIPELINE_COMPLETE",
                  "market_data_as_of": DAYS[3], "market_data_source": "ALPACA_SIP",
                  "sol_calls": 0, "no_trade_placed": True, "eligibility_counts": {},
                  "entry_counts": {"BUY_TOMORROW_CANDIDATE": 0},
                  "eligibility_cases": [], "entry_results": [], "shadow_day": 4,
                  "market_regime": "NORMAL", "target_session": "2026-10-02",
                  "portfolio": {"tracked_count": 2, "holding_decisions": [
                      {**first, "reason": position_summary_text(first)},
                      {**second, "reason": position_summary_text(second)}]},
                  "luna_calls": 0, "luna_input_tokens": 0,
                  "luna_output_tokens": 0, "luna_estimated_cost_usd": 0}
        text = render(report)
        self.assertEqual(text.count("⏸ HOLD"), 1)
        self.assertIn("MOCKB", text)
        self.assertIn("META | HOLD_TIGHT", text)
        self.assertIn("🔴 SELL / REDUCE（人工复核）\n无", text)

    def test_priority_map_and_transition_history(self):
        self.assertEqual([PRIORITY[k] for k in ("EXIT_CANDIDATE", "THESIS_REVIEW", "TRIM_CANDIDATE", "HOLD")],
                         ["CRITICAL", "HIGH", "MEDIUM", "INFO"])
        self.record(DAYS[0], decision("HOLD"))
        self.record(DAYS[1], decision("HOLD_TIGHT"))
        rows = self.book.db.execute("SELECT previous_state,exit_state FROM exit_state_transitions ORDER BY trading_date").fetchall()
        self.assertEqual([(row[0], row[1]) for row in rows], [(None, "HOLD"), ("HOLD", "HOLD_TIGHT")])


if __name__ == "__main__":
    unittest.main()

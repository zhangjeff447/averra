import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from shadow_validation import connect, tick

DAYS = ("2026-09-30", "2026-10-01", "2026-10-02", "2026-10-05", "2026-10-06")
PHASES = ("market", "phase1", "phase2", "phase3", "research", "entry", "portfolio", "forward_tracking")


class ShadowValidationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.state = root / "state.db"
        self.results = root / "results.db"
        self.stock = root / "stock.db"
        self.validation = root / "validation.db"
        with sqlite3.connect(self.state) as db:
            db.execute("""CREATE TABLE shadow_daily_runs(trading_date TEXT PRIMARY KEY,run_id TEXT,
                result_path TEXT,result_sha256 TEXT,market_data_as_of TEXT,state TEXT,
                telegram_status TEXT,telegram_message_id INTEGER)""")
        with sqlite3.connect(self.results) as db:
            db.executescript((Path(__file__).resolve().parents[1] / "shadow_daily_results.sql").read_text())
            db.execute("""CREATE VIEW effective_forward AS SELECT '' AS run_id,trading_date,ticker,horizon,
                status,end_date,return_pct,excess_spy_pct,excess_qqq_pct,mfe_pct,mae_pct,
                NULL AS correction_version,NULL AS correction_reason FROM forward""")
        self.messages = []

    def tearDown(self):
        self.temp.cleanup()

    def add_day(self, day, *, state="COMPLETE"):
        root = Path(self.temp.name)
        report = {"status": "PIPELINE_COMPLETE", "trading_date": day,
                  "market_data_as_of": day, "phase_statuses": {p: "COMPLETE" for p in PHASES},
                  "entry_counts": {"BUY_TOMORROW_CANDIDATE": 1, "WATCH": 1},
                  "eligibility_counts": {"FINAL_RESEARCH_AVAILABLE": 1, "RESEARCH_LIMITED": 1},
                  "eligibility_cases": [{"ticker": "AAA"}, {"ticker": "BBB"}],
                  "luna_calls": 1, "luna_input_tokens": 3000,
                  "luna_output_tokens": 500, "luna_estimated_cost_usd": .001, "sol_calls": 0}
        artifact = root / f"{day}.json"
        artifact.write_text(json.dumps(report))
        digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
        with sqlite3.connect(self.state) as db:
            db.execute("INSERT INTO shadow_daily_runs VALUES(?,?,?,?,?,?,?,?)",
                       (day, "SHADOW_DAILY_" + day.replace("-", ""), str(artifact), digest, day,
                        state, "SENT" if state == "COMPLETE" else
                        "PENDING" if state == "PIPELINE_COMPLETE_TELEGRAM_PENDING" else "NOT_READY",
                        1 if state == "COMPLETE" else None))
        with sqlite3.connect(self.results) as db:
            for ticker, rank, entry_state in (("AAA", 1, "BUY_TOMORROW_CANDIDATE"), ("BBB", 2, "WATCH")):
                db.execute("""INSERT INTO snapshots VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                           (day, ticker, rank, 80, 75, 78, "FINAL_RESEARCH_AVAILABLE", "FULL_RESEARCH",
                            80, 80, 81, entry_state, 100, "ALPACA_SIP", day + "T20:00:00Z"))
                for horizon in (1, 5):
                    matured = horizon == 1 and day != DAYS[-1]
                    db.execute("""INSERT INTO forward
                        (trading_date,ticker,horizon,status,end_date,return_pct,excess_spy_pct,
                         excess_qqq_pct,mfe_pct,mae_pct) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                        (day, ticker, horizon, "COMPLETE" if matured else "PENDING",
                         day if matured else None, 2 if matured else None, 1 if matured else None,
                         .5 if matured else None, 3 if matured else None, -1 if matured else None))

    def send(self, token, chat, message):
        self.messages.append(message)
        return 300

    def test_two_days_do_not_trigger_then_fifth_once(self):
        for day in DAYS[:2]:
            self.add_day(day)
        self.assertEqual(tick(self.state, self.results, self.stock, "x", "y", self.send,
                              self.validation), 2)
        self.assertEqual(self.messages, [])
        for day in DAYS[2:]:
            self.add_day(day)
        self.assertEqual(tick(self.state, self.results, self.stock, "x", "y", self.send,
                              self.validation), 5)
        self.assertEqual(len(self.messages), 1)
        self.assertIn("5-DAY SHADOW REVIEW", self.messages[0])
        self.assertEqual(tick(self.state, self.results, self.stock, "x", "y", self.send,
                              self.validation), 5)
        self.assertEqual(len(self.messages), 1)
        with connect(self.validation) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM completed_shadow_trading_days").fetchone()[0], 5)
            saved = db.execute("SELECT report_json,telegram_status,telegram_message_id FROM shadow_5d_reviews").fetchone()
            report = json.loads(saved[0])
            self.assertEqual((saved[1], saved[2]), ("SENT", 300))
        self.assertEqual(report["candidate_count"], 10)
        self.assertEqual(report["buy_tomorrow"]["1d"]["sample_size"], 4)
        self.assertEqual(report["watch"]["1d"]["sample_size"], 4)
        self.assertEqual(report["buy_tomorrow"]["5d"]["status"], "NOT_MATURED")
        self.assertEqual(report["candidate_top10"]["1d"]["sample_size"], 8)
        self.assertEqual(report["benchmarks_matched_1d"]["SPY"], 1)
        self.assertEqual(report["ai"]["luna_calls"], 5)
        self.assertEqual(report["exit_validation"], "EXIT ENGINE REAL VALIDATION NOT STARTED")
        self.assertTrue((Path(self.temp.name) / "shadow_reviews" /
                         "SHADOW_5D_2026-09-30_2026-10-06.json").is_file())

    def test_failed_day_is_not_counted(self):
        for day in DAYS[:4]:
            self.add_day(day)
        self.add_day(DAYS[4], state="FAILED")
        self.assertEqual(tick(self.state, self.results, self.stock, "x", "y", self.send,
                              self.validation), 4)
        self.assertEqual(self.messages, [])

    def test_unknown_review_delivery_is_not_retried(self):
        for day in DAYS:
            self.add_day(day)
        calls = []
        def unknown(*args):
            calls.append(1)
            raise TimeoutError("ambiguous")
        tick(self.state, self.results, self.stock, "x", "y", unknown, self.validation)
        tick(self.state, self.results, self.stock, "x", "y", unknown, self.validation)
        self.assertEqual(len(calls), 1)
        with connect(self.validation) as db:
            self.assertEqual(db.execute("SELECT telegram_status FROM shadow_5d_reviews").fetchone()[0], "UNKNOWN")

    def test_pipeline_complete_counts_but_daily_message_must_send_first(self):
        for day in DAYS[:4]:
            self.add_day(day)
        self.add_day(DAYS[4], state="PIPELINE_COMPLETE_TELEGRAM_PENDING")
        self.assertEqual(tick(self.state, self.results, self.stock, "x", "y", self.send,
                              self.validation), 5)
        self.assertEqual(self.messages, [])
        with sqlite3.connect(self.state) as db:
            db.execute("""UPDATE shadow_daily_runs SET state='COMPLETE',telegram_status='SENT',
                telegram_message_id=99 WHERE trading_date=?""", (DAYS[4],))
        tick(self.state, self.results, self.stock, "x", "y", self.send, self.validation)
        self.assertEqual(len(self.messages), 1)

    def test_entry_transitions_are_descriptive_only(self):
        for day in DAYS:
            self.add_day(day)
        with sqlite3.connect(self.results) as db:
            db.execute("UPDATE snapshots SET entry_state='WATCH' WHERE trading_date=? AND ticker='AAA'",
                       (DAYS[1],))
        tick(self.state, self.results, self.stock, "x", "y", self.send, self.validation)
        with connect(self.validation) as db:
            report = json.loads(db.execute("SELECT report_json FROM shadow_5d_reviews").fetchone()[0])
        self.assertEqual(report["entry_state_transitions"]["BUY_TOMORROW_TO_WATCH"], 1)
        self.assertEqual(report["entry_state_transitions"]["WATCH_TO_BUY_TOMORROW"], 1)
        self.assertFalse(report["strategy_parameters_changed"])


if __name__ == "__main__":
    unittest.main()

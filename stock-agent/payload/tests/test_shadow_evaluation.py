import json
import math
import sqlite3
import tempfile
import unittest
from pathlib import Path

from shadow_evaluation import evaluate, ranks, rank_ic, render, stats


class EvaluationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.path = self.root / "results.db"
        with sqlite3.connect(self.path) as db:
            db.executescript("""CREATE TABLE snapshots(trading_date TEXT,ticker TEXT,recorded_at TEXT,
                quant_score REAL,fundamental_score REAL,candidate_score REAL,research_score REAL,
                entry_score REAL,entry_state TEXT,research_quality_tier TEXT);
                CREATE TABLE outcomes(trading_date TEXT,ticker TEXT,horizon INTEGER,status TEXT,
                end_date TEXT,return_pct REAL,excess_spy_pct REAL,excess_qqq_pct REAL,mfe_pct REAL,mae_pct REAL);
                CREATE VIEW effective_forward AS SELECT * FROM outcomes;""")

    def tearDown(self):
        self.temporary.cleanup()

    def add(self, ticker="ABC", day="2026-10-01", status="COMPLETE", end="2026-10-02",
            result=2., recorded="2026-10-01T21:00:00+00:00"):
        with sqlite3.connect(self.path) as db:
            db.execute("INSERT INTO snapshots VALUES(?,?,?,?,?,?,?,?,?,?)",
                (day,ticker,recorded,80,75,79,70,81,"WATCH","FULL_RESEARCH"))
            db.execute("INSERT INTO outcomes VALUES(?,?,?,?,?,?,?,?,?,?)",
                (day,ticker,1,status,end,result,1.,.5,3.,-1.))

    def test_mature_observations_not_simulated_execution(self):
        self.add()
        report = evaluate(self.path,"2026-10-16",root=self.root)
        self.assertEqual(report["accepted_observations"],1)
        self.assertEqual(report["horizons"]["1"]["status"],"INSUFFICIENT_HISTORY")
        self.assertEqual(report["post_close_close_anchor_snapshots"],1)
        self.assertEqual(report["historical_manifest_dates"],[])
        self.assertEqual(report["ai_calls"],0)
        self.assertIn("NOT_EXECUTION_BACKTEST",report["report_type"])

    def test_pending_never_generates_a_return(self):
        self.add(status="PENDING",end=None,result=None)
        report = evaluate(self.path,"2026-10-16",root=self.root)
        self.assertEqual(report["accepted_observations"],0)
        self.assertEqual(report["horizons"]["1"]["pending"],1)
        self.assertIsNone(report["horizons"]["1"]["return_pct"]["mean"])

    def test_future_endpoint_cannot_enter_past_report(self):
        self.add(end="2026-10-20")
        report = evaluate(self.path,"2026-10-16",root=self.root)
        self.assertEqual(report["excluded"]["OUTCOME_TIME_INVALID_OR_AFTER_ASOF"],1)

    def test_snapshot_unavailable_by_asof_excluded(self):
        self.add(recorded="2026-10-18T21:00:00+00:00")
        report = evaluate(self.path,"2026-10-16",root=self.root)
        self.assertEqual(report["accepted_observations"],0)

    def test_quarantined_and_infinite_values_excluded(self):
        self.add(ticker="BAD",status="INVALID_EXCLUDED")
        self.add(ticker="INF",result=math.inf)
        report = evaluate(self.path,"2026-10-16",root=self.root)
        self.assertEqual(report["accepted_observations"],0)
        self.assertEqual(sum(report["excluded"].values()),2)

    def test_date_balanced_mean_is_not_stock_count_weighted(self):
        rows = []
        for day, values in [("2026-10-01",[10,10,10]),("2026-10-02",[-10])]:
            for value in values:
                rows.append({"trading_date":day,**dict.fromkeys(
                    ["return_pct","excess_spy_pct","excess_qqq_pct","mfe_pct","mae_pct"],value)})
        result = stats(rows,20)
        self.assertEqual(result["return_pct"]["mean"],5)
        self.assertEqual(result["return_pct"]["date_balanced_mean"],0)
        self.assertEqual(result["independent_scan_dates"],2)

    def test_tied_ranks_and_rank_ic(self):
        self.assertEqual(ranks([3,1,1]),[2,.5,.5])
        rows=[{"quant_score":x,"excess_spy_pct":x} for x in [1,2,3]]
        self.assertAlmostEqual(rank_ic(rows,"quant_score"),1)
        self.assertIsNone(rank_ic(rows[:2],"quant_score"))

    def test_report_is_read_only_and_html_escaped(self):
        self.add()
        before=self.path.read_bytes()
        report=evaluate(self.path,"2026-10-16",root=self.root)
        self.assertEqual(before,self.path.read_bytes())
        report["limitations"].append("<script>alert(1)</script>")
        page=render(report)
        self.assertNotIn("<script>",page)
        self.assertIn("&lt;script&gt;",page)
        json.dumps(report,allow_nan=False)


if __name__ == "__main__":
    unittest.main()

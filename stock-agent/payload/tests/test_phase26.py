"""Phase 2.6 regression scenarios, entirely offline and deterministic."""
import json
import sqlite3
import unittest

from phase26_actions import inspect_jumps
from phase26_diagnostics import redundancy, technical_attribution
from phase26_forward import register_scan, statistics_report, update
from phase26_sector import classify, sector_benchmark
from quant_ranking import component_score, load_config, validate_bars
from quant_screen import ROOT, init_db


PHASE = json.loads((ROOT / "phase26_config.json").read_text())
QUANT = load_config(ROOT / "quant_config.json")


class ActionTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        init_db(self.db)
        self.bars = [{"date": f"2026-09-{n:02}", "open": 100.0, "high": 105.0,
                      "low": 95.0, "close": 100.0, "volume": 1000} for n in range(1, 11)]

    def tearDown(self):
        self.db.close()

    def event(self, kind, ratio):
        self.db.execute("INSERT INTO corporate_actions VALUES(?,?,?,?,?,?)",
                        (kind, "ABC", "2026-09-06", kind, "MASSIVE",
                         json.dumps({"split_from": ratio[0], "split_to": ratio[1]})))

    def test_normal_stock(self):
        _, checks, allowed = inspect_jumps(self.db, "ABC", self.bars, PHASE["corporate_actions"])
        self.assertEqual((checks, allowed), ([], set()))

    def test_confirmed_split_recovered_without_source_mutation(self):
        self.event("forward_split", (1, 2))
        for bar in self.bars[5:]:
            bar.update(open=50, high=52, low=48, close=50, volume=2000)
        corrected, checks, _ = inspect_jumps(self.db, "ABC", self.bars, PHASE["corporate_actions"])
        self.assertEqual(checks[0]["corporate_action_type"], "CONFIRMED_SPLIT")
        self.assertEqual(checks[0]["validation_status"], "RECOVERED_IN_MEMORY")
        self.assertEqual(corrected[0]["close"], 50)
        self.assertEqual(corrected[0]["volume"], 2000)
        self.assertEqual(self.bars[0]["close"], 100)

    def test_reverse_split(self):
        self.event("reverse_split", (10, 1))
        for bar in self.bars[5:]:
            bar.update(open=1000, high=1050, low=950, close=1000, volume=100)
        corrected, checks, _ = inspect_jumps(self.db, "ABC", self.bars, PHASE["corporate_actions"])
        self.assertEqual(checks[0]["corporate_action_type"], "CONFIRMED_REVERSE_SPLIT")
        self.assertEqual(corrected[0]["close"], 1000)

    def test_extreme_real_move(self):
        self.bars[5].update(open=200, high=220, low=190, close=200, volume=3000)
        self.bars[6]["close"] = 200
        _, checks, allowed = inspect_jumps(self.db, "ABC", self.bars, PHASE["corporate_actions"])
        self.assertEqual(checks[0]["corporate_action_type"], "TRUE_PRICE_MOVE")
        self.assertIn("2026-09-06", allowed)

    def test_unexplained_anomaly(self):
        self.bars[5]["close"] = 500
        _, checks, _ = inspect_jumps(self.db, "ABC", self.bars, PHASE["corporate_actions"])
        self.assertEqual(checks[0]["corporate_action_type"], "UNEXPLAINED_ANOMALY")

    def test_insufficient_next_day(self):
        self.bars[-1]["close"] = 200
        _, checks, _ = inspect_jumps(self.db, "ABC", self.bars, PHASE["corporate_actions"])
        self.assertEqual(checks[-1]["corporate_action_type"], "INSUFFICIENT_DATA")

    def test_other_action(self):
        self.event("stock_dividend", (1, 1))
        self.bars[5]["close"] = 200
        _, checks, _ = inspect_jumps(self.db, "ABC", self.bars, PHASE["corporate_actions"])
        self.assertEqual(checks[0]["corporate_action_type"], "OTHER_CORPORATE_ACTION")


class SectorTests(unittest.TestCase):
    def test_high_confidence(self):
        self.assertEqual(classify("Technology", "Software", "GICS_VERIFIED", PHASE["sector_mapping"]),
                         ("Technology", "XLK", "HIGH"))

    def test_low_confidence(self):
        self.assertEqual(classify("Industrials", "Biotechnology: Laboratory Instruments", "NASDAQ_SCREENER", PHASE["sector_mapping"])[-1], "LOW")

    def test_unknown(self):
        self.assertEqual(classify(None, "", "NASDAQ_SCREENER", PHASE["sector_mapping"]), (None, None, "UNKNOWN"))

    def test_sector_rs_fallback(self):
        symbol, trace = sector_benchmark({"mapping_confidence": "LOW", "mapped_sector_etf": "XLI"}, PHASE["sector_mapping"])
        self.assertIsNone(symbol)
        self.assertEqual(trace["fallback_benchmark"], "SPY_QQQ")
        self.assertEqual(trace["sector_rs_weight"], 0)


class DiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.features = {name: rule.get("target", (rule["low"] + rule["high"]) / 2) if "low" in rule else rule["target"]
                         for name, rule in QUANT["features"]["technical"].items()}

    def test_rsi_overbought(self):
        self.features["rsi14"] = 85
        a = technical_attribution(self.features, QUANT["features"]["technical"], PHASE["technical_groups"])
        self.assertLess(a["group_deltas"]["RSI"], 0)

    def test_kdj_overbought(self):
        self.features["kdj_k"] = 100
        a = technical_attribution(self.features, QUANT["features"]["technical"], PHASE["technical_groups"])
        self.assertLess(a["group_deltas"]["KDJ"], 0)

    def test_macd_bullish(self):
        self.features["macd_above_zero"] = 1
        a = technical_attribution(self.features, QUANT["features"]["technical"], PHASE["technical_groups"])
        self.assertGreater(a["group_deltas"]["MACD"], 0)

    def test_contribution_sum(self):
        a = technical_attribution(self.features, QUANT["features"]["technical"], PHASE["technical_groups"])
        self.assertAlmostEqual(a["reconstructed_technical_score"],
                               component_score(self.features, QUANT["features"]["technical"]), places=4)

    def test_high_redundancy(self):
        rows = [{"return20": n, "return60": 2*n} for n in range(10)]
        result = redundancy(rows, PHASE["correlation"])
        self.assertEqual(result["flags"][0]["level"], "HIGH_REDUNDANCY")


class ForwardTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        init_db(self.db)
        self.db.execute("INSERT INTO quant_runs VALUES(1,'2026-09-01T22:00:00Z','2026-09-01','{}',1,1,0,0,'live')")
        self.db.execute("INSERT INTO quant_scores VALUES(1,'ABC',1,90,90,90,90,90,90,?, '{}')", (json.dumps({"close": 100}),))
        self.db.execute("INSERT INTO securities(ticker,asset_type,country,exchange,is_etf,is_otc,is_preferred,is_warrant,normalized_sector) VALUES('ABC','COMMON_STOCK','US','NYSE',0,0,0,0,'Technology')")
        for symbol, close in (("ABC", 100), ("SPY", 200), ("QQQ", 300)):
            self.add_bar(symbol, "2026-09-01", close)
        register_scan(self.db, 1, "NORMAL")

    def tearDown(self):
        self.db.close()

    def add_bar(self, symbol, day, close, high=None, low=None):
        self.db.execute("INSERT INTO daily_bars VALUES(?,?,?,?,?,?,?,NULL,NULL,NULL,NULL)",
                        (symbol, day, close, high or close*1.01, low or close*.99, close, 1000))

    def fill(self, count):
        for n in range(2, count+2):
            day = f"2026-09-{n:02}"
            self.add_bar("ABC", day, 100+n, 105+n, 95+n)
            self.add_bar("SPY", day, 200+n)
            self.add_bar("QQQ", day, 300+n)

    def obs(self, horizon):
        return self.db.execute("SELECT * FROM forward_observations WHERE horizon=?", (horizon,)).fetchone()

    def test_forward_1d(self):
        self.fill(1); update(self.db, PHASE["forward"])
        self.assertAlmostEqual(self.obs(1)["stock_return"], .02)

    def test_forward_5d(self):
        self.fill(5); update(self.db, PHASE["forward"])
        self.assertAlmostEqual(self.obs(5)["stock_return"], .06)

    def test_forward_20d(self):
        self.fill(20); update(self.db, PHASE["forward"])
        self.assertAlmostEqual(self.obs(20)["stock_return"], .21)

    def test_mfe(self):
        self.fill(5); update(self.db, PHASE["forward"])
        self.assertAlmostEqual(self.obs(5)["mfe"], .11)

    def test_mae(self):
        self.fill(5); update(self.db, PHASE["forward"])
        self.assertAlmostEqual(self.obs(5)["mae"], -.03)

    def test_spy_excess(self):
        self.fill(1); update(self.db, PHASE["forward"])
        self.assertAlmostEqual(self.obs(1)["excess_spy"], .02 - .01)

    def test_qqq_excess(self):
        self.fill(1); update(self.db, PHASE["forward"])
        self.assertAlmostEqual(self.obs(1)["excess_qqq"], .02 - 2/300)

    def test_no_lookahead(self):
        self.assertEqual(update(self.db, PHASE["forward"])["inserted"], 0)
        self.assertIsNone(self.obs(1))

    def test_incremental_update(self):
        self.fill(1)
        self.assertEqual(update(self.db, PHASE["forward"])["inserted"], 1)
        self.assertEqual(update(self.db, PHASE["forward"])["inserted"], 0)

    def test_historical_score_immutability(self):
        self.fill(5); update(self.db, PHASE["forward"])
        self.assertEqual(self.db.execute("SELECT quant_score FROM quant_scores").fetchone()[0], 90)
        self.assertEqual(self.db.execute("SELECT quant_score FROM forward_snapshots").fetchone()[0], 90)

    def test_single_symbol_failure_isolation(self):
        self.fill(5)
        self.db.execute("INSERT INTO quant_scores VALUES(1,'BAD',2,80,80,80,80,80,80,?, '{}')", (json.dumps({"close": 100}),))
        register_scan(self.db, 1)
        update(self.db, PHASE["forward"])
        self.assertIsNotNone(self.obs(5))
        self.assertEqual(statistics_report(self.db, PHASE["forward"])["pending"]["5"], 1)

    def test_same_date_rescan_latest_only(self):
        self.db.execute("INSERT INTO quant_runs VALUES(2,'2026-09-02T22:00:00Z','2026-09-01','{}',1,1,0,0,'live')")
        self.db.execute("INSERT INTO quant_scores VALUES(2,'ABC',1,80,80,80,80,80,80,?, '{}')", (json.dumps({"close": 100}),))
        register_scan(self.db, 2)
        stats = statistics_report(self.db, PHASE["forward"])
        self.assertEqual(stats["snapshots"], 2)
        self.assertEqual(stats["effective_unique_date_ticker_snapshots"], 1)
        self.assertEqual(stats["pending"]["5"], 1)


if __name__ == "__main__":
    unittest.main()

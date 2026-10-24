import json
import math
import sqlite3
import sys
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mock_data import bars
from quant_ranking import component_score, load_config, relative_features, scan
from quant_screen import ROOT, init_db


class QuantRankingTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = sqlite3.connect(Path(self.temp.name) / "market.db")
        self.db.row_factory = sqlite3.Row
        init_db(self.db)
        self.cfg = load_config(ROOT / "quant_config.json")
        self.series = {}
        for symbol, start, trend, phase in (("SPY", 400, 0.0005, 0), ("QQQ", 350, 0.0007, 0.3),
                                            ("XLK", 180, 0.0008, 0.5)):
            self.series[symbol] = bars(start, trend, phase)
        for n in range(12):
            symbol = f"MOCKQ{n + 1:02d}"
            self.series[symbol] = bars(30 + n * 8, 0.0006 + n * 0.00012, n * 0.4)
            self.db.execute("INSERT INTO securities(ticker,asset_type,country,exchange,is_etf,is_otc,is_preferred,is_warrant,sector_etf) VALUES(?,?,?,?,?,?,?,?,?)",
                            (symbol, "COMMON_STOCK", "US", "NASDAQ", 0, 0, 0, 0, "XLK"))
        for symbol, series in self.series.items():
            self.db.executemany("INSERT INTO daily_bars(ticker,date,open,high,low,close,volume) VALUES(?,?,?,?,?,?,?)",
                                [(symbol, b["date"], b["open"], b["high"], b["low"], b["close"], b["volume"])
                                 for b in series])
        self.db.commit()
        self.asof = self.series["SPY"][-1]["date"]

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def test_twelve_stock_scores_and_all_raw_features_persist_without_network(self):
        with patch("urllib.request.urlopen", side_effect=AssertionError("network called")):
            result = scan(self.db, self.asof, self.cfg, data_mode="mock")
        self.assertEqual((result["eligible_count"], result["scored_count"], result["failed_count"]), (12, 12, 0))
        self.assertEqual(len(result["top"]), 12)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM quant_scores").fetchone()[0], 12)
        row = result["top"][0]
        self.assertTrue(0 <= row["quant_score"] <= 100)
        self.assertTrue(all(0 <= row[f"{name}_score"] <= 100 for name in self.cfg["component_weights"]))
        for name in ("macd_line", "macd_signal", "macd_hist", "macd_hist_slope", "macd_hist_acceleration",
                     "rsi14", "kdj_k", "kdj_d", "kdj_j", "ema9", "ema21", "sma20", "sma50", "sma200",
                     "adx14", "bollinger_upper", "bollinger_lower", "atr14", "atr_pct", "realized_vol20",
                     "max_drawdown120", "downside_vol60", "avg_abs_gap60", "rs_spy_1d", "rs_qqq_60d",
                     "rs_sector_20d", "rvol", "volume_trend", "breakout_volume"):
            self.assertIn(name, row["features"])
        for rules in self.cfg["features"].values():
            self.assertTrue(set(rules).issubset(row["features"]))
        saved = self.db.execute("SELECT features_json FROM quant_scores WHERE run_id=? AND ticker=?",
                                (result["run_id"], row["ticker"])).fetchone()[0]
        self.assertEqual(json.loads(saved)["macd_hist"], row["features"]["macd_hist"])

    def test_relative_strength_detects_outperformance_and_underperformance(self):
        def series(last):
            return [{"date": f"D{i}", "close": 100} for i in range(60)] + [{"date": "D60", "close": last}]
        params = self.cfg["feature_parameters"]
        weak = relative_features(series(102), {"SPY": series(103), "QQQ": series(100)}, None, params)
        strong = relative_features(series(101), {"SPY": series(98.5), "QQQ": series(100)}, None, params)
        self.assertLess(weak["rs_spy_1d"], 0)
        self.assertGreater(strong["rs_spy_1d"], 0)
        self.assertIsNone(weak["rs_sector_20d"])

    def test_lower_risk_produces_higher_risk_score(self):
        rules = self.cfg["features"]["risk"]
        calm = {name: 0.02 for name in rules}
        risky = {name: 0.2 for name in rules}
        calm["max_drawdown120"], risky["max_drawdown120"] = -0.05, -0.5
        calm["abnormal_volatility"], risky["abnormal_volatility"] = 0.9, 1.8
        self.assertGreater(component_score(calm, rules), component_score(risky, rules))

    def test_memory_uses_prior_dates_and_no_future_bar(self):
        dates = [bar["date"] for bar in self.series["SPY"][-6:]]
        first = scan(self.db, dates[0], self.cfg, data_mode="historical")
        ticker = first["top"][0]["ticker"]
        initial_score = next(row["quant_score"] for row in first["top"] if row["ticker"] == ticker)
        self.db.execute("UPDATE daily_bars SET close=close*1.2,high=high*1.2 WHERE ticker=? AND date=?",
                        (ticker, dates[-1]))
        self.db.commit()
        repeated = scan(self.db, dates[0], self.cfg, data_mode="historical")
        self.assertEqual(initial_score, next(row["quant_score"] for row in repeated["top"] if row["ticker"] == ticker))
        for day in dates[1:]:
            result = scan(self.db, day, self.cfg, data_mode="historical")
        item = next(row for row in result["top"] if row["ticker"] != ticker)
        self.assertIsNotNone(item["memory"]["rank_change_1d"])
        self.assertIsNotNone(item["memory"]["rank_change_5d"])
        self.assertIsNotNone(item["memory"]["score_change_5d"])
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM quant_runs").fetchone()[0], 7)

    def test_bad_stocks_do_not_stop_scan_and_feature_can_be_disabled(self):
        self.db.execute("DELETE FROM daily_bars WHERE ticker='MOCKQ01' AND date=?", (self.asof,))
        self.db.execute("UPDATE daily_bars SET close=close*2,high=high*2 WHERE ticker='MOCKQ02' AND date=?", (self.asof,))
        self.db.commit()
        result = scan(self.db, self.asof, self.cfg, data_mode="mock")
        self.assertEqual((result["scored_count"], result["failed_count"]), (10, 2))
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM quant_failures WHERE run_id=?", (result["run_id"],)).fetchone()[0], 2)
        rules = self.cfg["features"]["momentum"]
        example = {name: 0.1 for name in rules}
        before = component_score(example, rules)
        modified = deepcopy(rules)
        modified["return20"]["enabled"] = False
        modified["return20"]["weight"] = 0
        self.assertNotEqual(before, component_score(example, modified))

    def test_quality_failures_are_isolated_and_recorded(self):
        self.db.execute("DELETE FROM daily_bars WHERE ticker='MOCKQ01' AND date IN (SELECT date FROM daily_bars WHERE ticker='MOCKQ01' ORDER BY date LIMIT 20)")
        self.db.execute("DELETE FROM daily_bars WHERE ticker='MOCKQ02' AND date IN (SELECT date FROM daily_bars WHERE ticker='MOCKQ02' ORDER BY date DESC LIMIT 8 OFFSET 10)")
        self.db.execute("UPDATE daily_bars SET close=?, high=? WHERE ticker='MOCKQ03' AND date=?",
                        (float("inf"), float("inf"), self.asof))
        self.db.execute("UPDATE daily_bars SET high=2000000 WHERE ticker='MOCKQ04' AND date=?", (self.asof,))
        self.db.execute("DELETE FROM daily_bars WHERE ticker='MOCKQ05' AND date=?", (self.asof,))
        self.db.commit()
        cfg = deepcopy(self.cfg)
        cfg["quality"]["maximum_gap_days"] = 7
        result = scan(self.db, self.asof, cfg, data_mode="mock")
        self.assertEqual((result["scored_count"], result["failed_count"]), (7, 5))
        self.assertIn("insufficient_history", result["failure_reasons"])
        self.assertIn("missing_bars", result["failure_reasons"])
        self.assertIn("nan_inf_or_invalid_ohlcv", result["failure_reasons"])
        self.assertIn("extreme_or_invalid_price", result["failure_reasons"])
        self.assertIn("stale_market_data", result["failure_reasons"])

    def test_missing_sector_data_is_null_but_spy_qqq_are_required(self):
        self.db.execute("DELETE FROM daily_bars WHERE ticker='XLK'")
        self.db.commit()
        result = scan(self.db, self.asof, self.cfg, data_mode="mock")
        self.assertEqual(result["scored_count"], 12)
        self.assertIsNone(result["top"][0]["features"]["rs_sector_20d"])
        self.db.execute("DELETE FROM daily_bars WHERE ticker='SPY' AND date=?", (self.asof,))
        self.db.commit()
        with self.assertRaisesRegex(ValueError, "stale_market_data"):
            scan(self.db, self.asof, self.cfg, data_mode="mock")


if __name__ == "__main__":
    unittest.main()

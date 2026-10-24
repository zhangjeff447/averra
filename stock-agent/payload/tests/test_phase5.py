import ast
import json
import sqlite3
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from phase5_entry import (aggregate, chase_risk, decide, event_entry_score, init_db,
                          load_config, opening_confirmation, premarket_gate,
                          research_reference, save_decision, session_vwap, trend,
                          research_available_by_close, select_entry_candidates, sync_symbol,
                          update_forward, validate_aggregate)

ET = ZoneInfo("America/New_York")


class EntryTests(unittest.TestCase):
    def setUp(self):
        self.cfg = load_config()
        self.quant = json.loads((Path(__file__).resolve().parents[1] / "quant_config.json").read_text())["feature_parameters"]
        self.daily = []
        first = date(2025, 9, 1)
        for i in range(310):
            day = first + timedelta(days=i)
            if day.weekday() >= 5:
                continue
            value = 50 + len(self.daily) * .2
            self.daily.append({"date": day.isoformat(), "open": value-.1,
                               "high": value+.4, "low": value-.5, "close": value,
                               "volume": 1000000})
        self.asof = self.daily[-1]["date"]
        self.intra = []
        for day in self.daily[-40:]:
            d = date.fromisoformat(day["date"])
            for j in range(78):
                stamp = datetime(d.year, d.month, d.day, 9, 30, tzinfo=ET) + timedelta(minutes=5*j)
                price = day["open"] + j*.002
                self.intra.append({"date": day["date"], "minute": 570+5*j,
                                   "ts_ms": int(stamp.timestamp()*1000), "open": price,
                                   "high": price+.1, "low": price-.1, "close": price+.02,
                                   "volume": 20000, "bar_vwap": price})
        self.daily[-1]["close"] = self.intra[-1]["close"]
        self.daily[-1]["high"] = self.intra[-1]["high"]
        self.daily[-1]["low"] = self.intra[-78]["low"]-.1
        self.research = {"conviction_score": 90}

    def result(self, **kwargs):
        return decide(self.daily, self.intra, {"SPY": -1, "QQQ": -.5}, 0,
                      self.research, "NORMAL", self.cfg, self.quant, **kwargs)

    def test_full_multiframe_and_raw_indicators(self):
        result = self.result()
        self.assertEqual(set(result["timeframes"]), {"daily", "1h", "15m", "5m"})
        for frame in result["timeframes"].values():
            for key in ("macd_line", "macd_signal", "macd_hist", "macd_hist_slope",
                        "kdj_k", "kdj_d", "kdj_j", "rsi14", "ema9", "ema21",
                        "sma20", "sma50", "sma200", "adx14", "bollinger_upper",
                        "bollinger_lower", "atr14", "atr_pct", "volume"):
                self.assertIn(key, frame)
                self.assertIsNotNone(frame[key])
        self.assertEqual(len(result["components"]), 9)
        self.assertGreater(result["features"]["relative_strength_pct"]["SPY"], 0)
        self.assertIn(result["entry_state"], {"BUY_TOMORROW_CANDIDATE", "WATCH", "REJECT_ENTRY"})

    def test_trend_macd_and_technical_direction(self):
        f = self.result()["timeframes"]["daily"]
        self.assertIn(trend(f, self.cfg), {"STRONG_UPTREND", "UPTREND"})
        bearish = {**f, "close": 10, "ema9": 12, "ema21": 13,
                   "sma50": 14, "sma200": 15, "macd_line": -2, "macd_signal": -1}
        self.assertIn(trend(bearish, self.cfg), {"STRONG_DOWNTREND", "DOWNTREND"})
        self.assertGreater(f["macd_line"], f["macd_signal"])
        self.assertLess(bearish["macd_line"], bearish["macd_signal"])

    def test_kdj_rsi_overheat_does_not_reject_stock(self):
        bars = self.intra
        for bar in bars[-20:]:
            bar["close"] += 0.05
            bar["high"] = max(bar["high"], bar["close"])
        result = self.result()
        self.assertNotEqual(result["entry_state"], "REJECT_STOCK")
        self.assertIn("kdj_k", result["timeframes"]["5m"])
        self.assertIn("rsi14", result["timeframes"]["5m"])

    def test_vwap_reclaim_rejection_and_volume(self):
        latest = self.intra[-78:]
        latest[-2]["close"] = latest[-2]["bar_vwap"]-.5
        latest[-1]["close"] = latest[-1]["bar_vwap"]+.5
        self.assertTrue(session_vwap(self.intra, self.asof)["reclaim"])
        latest[-2]["close"] = latest[-2]["bar_vwap"]+.5
        latest[-1]["close"] = latest[-1]["bar_vwap"]-.5
        self.assertTrue(session_vwap(self.intra, self.asof)["rejection"])
        self.assertGreater(self.result()["features"]["rvol"], 0)

    def test_gap_chase_and_material_event_confirmation(self):
        self.assertEqual(chase_risk(16, 0, 0, 0, self.cfg), "EXTREME")
        self.assertEqual(chase_risk(9, 0, 0, 0, self.cfg), "HIGH")
        event = {"materiality": "MATERIAL", "direction": "POSITIVE", "credible": True}
        self.assertEqual(event_entry_score(event, 3, 2, 2, True, self.cfg), 100)
        self.assertLess(event_entry_score(event, -3, 2, -2, False, self.cfg), 50)
        event["date"] = self.asof
        at_1300 = datetime.combine(date.fromisoformat(self.asof),
                                   datetime.strptime("13:00", "%H:%M").time(), tzinfo=ET)
        self.assertNotEqual(self.result(event=event, mode="EVENT", decision_time=at_1300)["entry_state"],
                            "BUY_TOMORROW_CANDIDATE")

    def test_buy_now_buy_tomorrow_watch_and_reject_entry(self):
        self.assertEqual(self.result()["entry_state"], "WATCH")
        self.daily[-1]["volume"] *= 2
        self.cfg["thresholds"].update(entry_buy_tomorrow=60, entry_buy_now=70)
        self.assertEqual(self.result()["entry_state"], "BUY_TOMORROW_CANDIDATE")
        for day in self.daily[-21:-1]:
            day["high"] = day["close"] + .05
        event = {"date": self.asof, "materiality": "MATERIAL", "direction": "POSITIVE", "credible": True}
        at_1300 = datetime.combine(date.fromisoformat(self.asof),
                                   datetime.strptime("13:00", "%H:%M").time(), tzinfo=ET)
        self.assertEqual(self.result(event=event, mode="EVENT", decision_time=at_1300)["entry_state"],
                         "BUY_NOW_CANDIDATE")
        self.cfg["thresholds"]["entry_watch"] = 100
        self.daily[-1]["close"] = self.daily[-1]["low"]
        self.assertEqual(self.result()["entry_state"], "REJECT_ENTRY")

    def test_support_resistance_and_low_volume_breakout(self):
        result = self.result()
        self.assertIn("support_bounce", result["features"]["support_resistance"])
        self.assertIn("resistance_rejection", result["features"]["support_resistance"])
        self.daily[-1]["volume"] = 100000
        thin = self.result()
        self.assertLess(thin["components"]["volume_confirmation"], result["components"]["volume_confirmation"])

    def test_risk_off_close_and_premarket_gates(self):
        result = self.result()
        self.assertLess(self.result().__getitem__("entry_score"), 101)
        self.assertNotEqual(self.result().__getitem__("entry_state"), "BUY_NOW_CANDIDATE")
        self.assertNotEqual(decide(self.daily, self.intra, {"SPY": -1, "QQQ": -.5}, 0,
                                   self.research, "EXTREME", self.cfg, self.quant)["entry_state"],
                            "BUY_TOMORROW_CANDIDATE")
        self.assertEqual(premarket_gate("BUY_TOMORROW_CANDIDATE", 13, "NORMAL",
                                        "CLEAR", "STRONG", self.cfg), "CANCEL_ENTRY")
        self.assertEqual(premarket_gate("BUY_TOMORROW_CANDIDATE", 2, "NORMAL",
                                        "UNKNOWN", "STRONG", self.cfg), "WAIT")
        self.assertEqual(premarket_gate("BUY_TOMORROW_CANDIDATE", 2, "NORMAL",
                                        "CLEAR", "STRONG", self.cfg), "APPROVED")
        self.assertEqual(opening_confirmation("APPROVED", 2, 100, 99, 99, 2, 1, self.cfg), "WAIT")
        self.assertEqual(opening_confirmation("APPROVED", 11, 100, 99, 99, 2, 1, self.cfg),
                         "ENTRY_CONFIRMED")

    def test_no_lookahead_forward_and_isolation(self):
        market = sqlite3.connect(":memory:")
        market.execute("CREATE TABLE daily_bars(ticker TEXT,date TEXT,open REAL,high REAL,low REAL,close REAL,volume REAL)")
        entry = sqlite3.connect(":memory:")
        init_db(entry)
        entry.execute("INSERT INTO entry_runs(asof,started_at,status,source,config_json) VALUES(?,?,?,?,?)",
                      (self.asof, "2026-01-01T00:00:00+00:00", "COMPLETE", "TEST", "{}"))
        result = self.result()
        result["signal_validity"] = "HISTORICAL_DIAGNOSTIC_ONLY"
        save_decision(entry, 1, "AMD", result, self.research)
        stored = entry.execute("SELECT timeframes_json FROM entry_decisions WHERE run_id=1 AND ticker='AMD'").fetchone()[0]
        self.assertIn("macd_hist_slope", json.loads(stored)["15m"])
        self.assertEqual(update_forward(entry, market, self.asof), 0)
        entry.close(); market.close()

    def test_research_must_precede_close_even_on_same_date(self):
        self.assertTrue(research_available_by_close({"created_at": "2026-09-25T19:00:00+00:00"},
                                                    "2026-09-25"))
        self.assertFalse(research_available_by_close({"created_at": "2026-09-25T21:00:00+00:00"},
                                                     "2026-09-25"))

    def test_event_mode_ignores_future_daily_close_and_stale_tape(self):
        event = {"date": self.asof, "materiality": "MATERIAL", "direction": "POSITIVE", "credible": True}
        day = date.fromisoformat(self.asof)
        at_1300 = datetime(day.year, day.month, day.day, 13, 0, tzinfo=ET)
        first = self.result(event=event, mode="EVENT", decision_time=at_1300)
        self.daily[-1]["close"] *= 1.1
        self.daily[-1]["high"] = self.daily[-1]["close"]
        second = self.result(event=event, mode="EVENT", decision_time=at_1300)
        self.assertEqual(first["entry_score"], second["entry_score"])
        self.assertEqual(first["entry_state"], second["entry_state"])
        at_1630 = datetime(day.year, day.month, day.day, 16, 30, tzinfo=ET)
        stale = self.result(event=event, mode="EVENT", decision_time=at_1630)
        self.assertNotEqual(stale["entry_state"], "BUY_NOW_CANDIDATE")
        self.assertIn("Event-mode market bar is stale or outside regular hours", stale["why_wait"])

    def test_incremental_cache_and_bad_bar(self):
        entry = sqlite3.connect(":memory:")
        init_db(entry)
        stamp = datetime(2026, 9, 25, 9, 30, tzinfo=ET)
        class Client:
            request_count = 0
            def get(self, path, params):
                self.request_count += 1
                return {"status": "OK", "results": [
                    {"t": int(stamp.timestamp()*1000), "o": 100, "h": 101,
                     "l": 99, "c": 100, "v": 1000, "vw": 100}]}
        client = Client()
        rows, status = sync_symbol(entry, client, "AMD", "2026-09-25", self.cfg)
        self.assertEqual((rows, status, client.request_count), (2, "READY", 2))
        self.assertEqual(sync_symbol(entry, client, "AMD", "2026-09-25", self.cfg), (0, "READY"))
        self.assertEqual(client.request_count, 2)
        with self.assertRaises(ValueError):
            validate_aggregate({"o": 10, "h": 9, "l": 8, "c": 10, "v": 10})
        entry.close()

    def test_selection_only_from_completed_final_research(self):
        rows = [{"ticker": "AMD", "research_status": "COMPLETE", "final_research_tier": "INITIAL",
                 "candidate_score": 80},
                {"ticker": "CDNA", "research_status": "COMPLETE", "final_research_tier": "DEEP",
                 "candidate_score": 75},
                {"ticker": "META", "research_status": "FAILED", "final_research_tier": "UNAVAILABLE",
                 "candidate_score": 99}]
        self.cfg["candidate_limit"] = 5
        self.assertEqual(select_entry_candidates(rows, {"META"}, {"AMD"}, self.cfg), ["AMD", "CDNA"])

    def test_forward_mfe_mae_only_after_anchor(self):
        market = sqlite3.connect(":memory:")
        market.execute("CREATE TABLE daily_bars(ticker TEXT,date TEXT,open REAL,high REAL,low REAL,close REAL,volume REAL)")
        market.executemany("INSERT INTO daily_bars VALUES(?,?,?,?,?,?,?)", [
            ("AMD", "2026-09-25", 100, 101, 99, 100, 1000),
            ("AMD", "2026-09-28", 105, 110, 95, 108, 1000)])
        entry = sqlite3.connect(":memory:")
        init_db(entry)
        entry.execute("INSERT INTO entry_runs(asof,started_at,status,source,config_json) VALUES(?,?,?,?,?)",
                      ("2026-09-25", "2026-09-25T21:00:00+00:00", "COMPLETE", "TEST", "{}"))
        save_decision(entry, 1, "AMD", {"entry_price_reference": 100, "entry_score": 70,
                      "entry_state": "WATCH", "signal_validity": "POINT_IN_TIME"}, {})
        self.assertEqual(update_forward(entry, market, "2026-09-25", horizons=(1,)), 0)
        self.assertEqual(update_forward(entry, market, "2026-09-28", horizons=(1,)), 1)
        row = entry.execute("SELECT return_pct,mfe_pct,mae_pct FROM entry_forward").fetchone()
        for actual, expected in zip(row, (8, 10, -5)):
            self.assertAlmostEqual(actual, expected)
        market.close(); entry.close()

    def test_no_llm_or_broker_calls_in_entry_module(self):
        tree = ast.parse((Path(__file__).resolve().parents[1] / "phase5_entry.py").read_text())
        imports = {node.module.split(".")[0] for node in ast.walk(tree)
                   if isinstance(node, ast.ImportFrom) and node.module}
        imports |= {alias.name.split(".")[0] for node in ast.walk(tree)
                    if isinstance(node, ast.Import) for alias in node.names}
        self.assertFalse(imports & {"openai", "robinhood", "telegram"})


if __name__ == "__main__":
    unittest.main()

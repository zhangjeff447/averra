import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from phase5_entry import init_db, save_decision
from price_plan import buy_plan
from shadow_daily_pipeline import observe_entry_references


THRESHOLDS = json.loads((Path(__file__).resolve().parents[1] / "phase5_config.json").read_text())["thresholds"]


class ReferenceZoneHistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.entry_path = Path(self.tmp.name) / "entry.db"
        self.market_path = Path(self.tmp.name) / "market.db"
        self.entry = sqlite3.connect(self.entry_path)
        init_db(self.entry)
        self.market = sqlite3.connect(self.market_path)
        self.market.execute("""CREATE TABLE daily_bars (
            ticker TEXT,date TEXT,open REAL,high REAL,low REAL,close REAL,
            source TEXT,quality_status TEXT)""")

    def tearDown(self):
        self.entry.close()
        self.market.close()
        self.tmp.cleanup()

    def test_frozen_plan_and_next_session_ohlc_are_saved_once(self):
        result = {"ticker": "AMD", "asof": "2026-10-01",
                  "target_session": "2026-10-02", "entry_state": "BUY_TOMORROW_CANDIDATE",
                  "entry_price_reference": 100.0, "entry_score": 83,
                  "timeframes": {"daily": {"atr14": 4.0}},
                  "features": {"support_resistance": {"support": 99.5, "resistance": 105.0}},
                  "chase_risk": "MEDIUM"}
        result["buy_plan"] = buy_plan(result, THRESHOLDS)
        save_decision(self.entry, 1, "AMD", result, {})
        saved = self.entry.execute("""SELECT generated_from_trading_date,target_trading_date,
            preferred_low,preferred_high,do_not_chase_above FROM entry_reference_plans""").fetchone()
        self.assertEqual(saved[:2], ("2026-10-01", "2026-10-02"))
        self.assertLessEqual(saved[2], saved[3])
        self.assertEqual(saved[3], saved[4])
        self.market.execute("INSERT INTO daily_bars VALUES(?,?,?,?,?,?,?,?)",
                            ("AMD", "2026-10-02", 103, 105, 99, 101, "ALPACA_SIP", "OK"))
        self.market.commit()
        self.assertEqual(observe_entry_references("2026-10-02", market_path=self.market_path,
                                                   entry_path=self.entry_path), 1)
        self.assertEqual(observe_entry_references("2026-10-02", market_path=self.market_path,
                                                   entry_path=self.entry_path), 0)
        row = self.entry.execute("""SELECT open,high,low,close,entry_zone_touched,
            do_not_chase_exceeded,open_reference_status FROM entry_reference_observations""").fetchone()
        self.assertEqual(row, (103, 105, 99, 101, 1, 1, "WAIT_FOR_REASSESSMENT"))

    def test_no_bar_means_no_fabricated_observation(self):
        self.assertEqual(observe_entry_references("2026-10-02", market_path=self.market_path,
                                                   entry_path=self.entry_path), 0)


if __name__ == "__main__":
    unittest.main()

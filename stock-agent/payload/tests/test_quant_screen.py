import csv
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mock_data import bars
from quant_screen import import_bars, import_master, main, screen


ROOT = Path(__file__).resolve().parents[1]
MASTER = ("ticker", "asset_type", "country", "exchange", "is_etf", "is_otc", "is_preferred", "is_warrant")
OHLCV = ("ticker", "date", "open", "high", "low", "close", "volume")


def write_csv(path, fields, rows):
    with path.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


class QuantScreenTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = sqlite3.connect(self.root / "market.db")
        self.db.row_factory = sqlite3.Row
        self.db.executescript((ROOT / "market_schema.sql").read_text())
        self.master = self.root / "master.csv"
        self.ohlcv = self.root / "bars.csv"
        base = {"asset_type": "COMMON_STOCK", "country": "US", "exchange": "NASDAQ",
                "is_etf": "0", "is_otc": "0", "is_preferred": "0", "is_warrant": "0"}
        self.securities = [{"ticker": "GOOD", **base}, {"ticker": "LOW", **base},
                           {"ticker": "FUND", **base, "asset_type": "ETF", "is_etf": "1"},
                           {"ticker": "PREF", **base, "is_preferred": "1"},
                           {"ticker": "WARR", **base, "is_warrant": "1"},
                           {"ticker": "OTC", **base, "exchange": "OTC", "is_otc": "1"},
                           {"ticker": "FOREIGN", **base, "country": "CA"},
                           {"ticker": "NOBARS", **base}]
        write_csv(self.master, MASTER, self.securities)
        self.good_bars = bars(50, 0.001, volume=1_000_000)
        rows = [{"ticker": "GOOD", **bar} for bar in self.good_bars]
        rows += [{"ticker": "LOW", **bar, "volume": 1} for bar in self.good_bars]
        rows += [{"ticker": "FUND", **bar} for bar in self.good_bars]
        write_csv(self.ohlcv, OHLCV, rows)
        self.asof = date.today().isoformat()

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def test_full_universe_filter_and_quant_screen_use_no_ai_or_network(self):
        with patch("urllib.request.urlopen", side_effect=AssertionError("network called")):
            self.assertEqual(import_master(self.db, self.master), 8)
            self.assertEqual(import_bars(self.db, self.ohlcv), 780)
            result = screen(self.db, self.asof)
        self.assertEqual(result["us_common_listed_count"], 3)
        self.assertEqual(result["screened_count"], 1)
        self.assertEqual([row["ticker"] for row in result["candidates"]], ["GOOD"])
        self.assertIn("quant_rank", result["candidates"][0])
        self.assertEqual(result["rejected"]["price_or_liquidity"], 1)
        self.assertEqual(result["rejected"]["missing_200_current_bars"], 1)
        self.assertEqual(sum(result["rejected"].values()) + result["screened_count"], result["master_count"])

    def test_daily_upsert_and_stale_bars_are_not_screened(self):
        import_master(self.db, self.master)
        import_bars(self.db, self.ohlcv)
        correction = [{"ticker": "GOOD", **self.good_bars[-1],
                       "close": self.good_bars[-1]["close"] + 1,
                       "high": self.good_bars[-1]["close"] + 2}]
        write_csv(self.ohlcv, OHLCV, correction)
        self.assertEqual(import_bars(self.db, self.ohlcv), 1)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM daily_bars WHERE ticker='GOOD'").fetchone()[0], 260)
        self.assertEqual(screen(self.db, self.asof)["candidates"][0]["close"], correction[0]["close"])
        self.assertEqual(screen(self.db, "2099-01-01")["screened_count"], 0)

    def test_invalid_bar_rolls_back_import(self):
        write_csv(self.ohlcv, OHLCV, [{"ticker": "GOOD", **self.good_bars[0]},
                                     {"ticker": "GOOD", **self.good_bars[1], "volume": -1}])
        with self.assertRaisesRegex(ValueError, "volume"):
            import_bars(self.db, self.ohlcv)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM daily_bars").fetchone()[0], 0)

    def test_missing_classification_is_rejected(self):
        row = dict(self.securities[0], is_etf="")
        write_csv(self.master, MASTER, [row])
        with self.assertRaisesRegex(ValueError, "classification flags"):
            import_master(self.db, self.master)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM securities").fetchone()[0], 0)

    def test_one_command_writes_top100_without_ai_even_if_key_exists(self):
        output = self.root / "top100.json"
        args = ["quant_screen.py", "--master", str(self.master), "--bars", str(self.ohlcv),
                "--asof", self.asof, "--db", str(self.root / "cli.db"), "--output", str(output)]
        with patch.object(sys, "argv", args), patch.dict(os.environ, {"OPENAI_API_KEY": "unused"}), \
             patch("urllib.request.urlopen", side_effect=AssertionError("network called")):
            main()
        result = json.loads(output.read_text())
        self.assertEqual(result["scope"], "supplied_master_only")
        self.assertEqual([item["ticker"] for item in result["candidates"]], ["GOOD"])


if __name__ == "__main__":
    unittest.main()

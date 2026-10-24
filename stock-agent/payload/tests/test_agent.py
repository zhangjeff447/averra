import os
import sys
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from analysis import allocation, score_stock, universe_ok
from mock_data import snapshot
from portfolio import Portfolio, money
from stock_agent import command, config, detect_events, scan


class AgentTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.book = Portfolio(Path(self.temp.name) / "test.db")
        self.snap = snapshot()

    def tearDown(self):
        self.book.close()
        self.temp.cleanup()

    def test_deposit_accumulates_and_suggestion_does_not_spend(self):
        self.assertEqual(self.book.deposit("2000"), 200000)
        self.assertEqual(self.book.deposit("125.50"), 212550)
        header, alerts = scan(self.book, self.snap, config(), "mock")
        self.assertIn("$", alerts[0])
        self.assertEqual(self.book.cash_cents(), 212550)
        self.assertEqual(len(self.book.positions()), 0)
        self.assertEqual([r["kind"] for r in self.book.history()], ["DEPOSIT", "DEPOSIT"])

    def test_confirmed_buy_sell_and_skip(self):
        scan(self.book, self.snap, config(), "mock")
        self.assertIn("skipped", command(self.book, "/skip MOCKB", self.snap))
        result = command(self.book, "/bought MOCKB", self.snap)
        self.assertIn("now tracked", result)
        self.assertIn("MOCKB", command(self.book, "/portfolio", self.snap))
        self.assertIn("removed from daily exit checks", command(self.book, "/sold MOCKB", self.snap))
        self.assertIn("Rejected", command(self.book, "/sold MOCKB", self.snap))
        self.assertEqual(self.book.cash_cents(), 0)

    def test_estimated_shares_are_labeled(self):
        self.assertIn("Entry price $100.00", command(self.book, "/bought MOCKB 100", self.snap))
        self.assertEqual(self.book.db.execute("SELECT COUNT(*) FROM holding_events").fetchone()[0], 1)

    def test_universe_excludes_non_common(self):
        stock = deepcopy(self.snap["stocks"][0])
        for change in ({"asset_type": "ETF"}, {"asset_type": "PREFERRED"},
                       {"is_warrant": True}, {"is_otc": True}, {"country": "CA"}):
            candidate = {**stock, **change}
            self.assertFalse(universe_ok(candidate))
            self.assertEqual(score_stock(candidate, self.snap)["action"], "REJECT")

    def test_no_trigger_means_no_ai_call(self):
        with patch("stock_agent.ai_event_note") as ai:
            self.assertEqual(detect_events(self.book, self.snap, config()), [])
            ai.assert_not_called()

    def test_event_deduplication_and_ai_budget(self):
        self.snap["stocks"][0]["events"] = [{"date": self.snap["asof"][:10], "type": "GUIDANCE_RAISE"}]
        cfg = config()
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test"}), patch("stock_agent.ai_event_note", return_value=("Evidence limited", True)) as ai:
            self.assertEqual(len(detect_events(self.book, self.snap, cfg)), 1)
            self.assertEqual(detect_events(self.book, self.snap, cfg), [])
            ai.assert_called_once()

    def test_sizing_honors_cash_and_single_name_cap(self):
        self.book.deposit("2000")
        scan(self.book, self.snap, config(), "mock")
        result = score_stock(self.snap["stocks"][1], self.snap)
        amount = allocation(result, self.snap["stocks"][1], self.book, self.snap, config())
        self.assertGreater(amount, 0)
        self.assertLessEqual(amount, 40000)
        self.book.buy("MOCKB", "1800", "Technology", "100")
        self.assertEqual(allocation(result, self.snap["stocks"][1], self.book, self.snap, config()), 0)

    def test_planned_sector_exposure_and_market_regime(self):
        self.book.deposit("2000")
        stock = self.snap["stocks"][1]
        result = score_stock(stock, self.snap)
        self.assertEqual(result["action"], "BUY NOW CANDIDATE")
        self.assertLessEqual(allocation(result, stock, self.book, self.snap, config(), {"MOCKA": 69000}), 1000)
        closed = deepcopy(self.snap)
        closed["session"] = "CLOSED"
        self.assertEqual(score_stock(closed["stocks"][1], closed)["action"], "BUY TOMORROW")
        stressed = deepcopy(self.snap)
        stressed["market"]["vix"] = 36
        self.assertEqual(score_stock(stressed["stocks"][1], stressed)["action"], "WATCH")

    def test_money_rejects_subcent_values(self):
        with self.assertRaises(ValueError):
            money("0.001")

    def test_start_explains_ledger_commands(self):
        self.assertIn("/bought TICKER", command(self.book, "/start", self.snap))


if __name__ == "__main__":
    unittest.main()

import sqlite3
import json
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

from holdings_monitor import confirm, evaluate, evaluate_all, held
from market_regime import previous_session
from mock_data import snapshot
from portfolio import Portfolio
from stock_agent import command


DAY = "2026-10-01"


def bars(prices, *, source="ALPACA_SIP"):
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE securities(ticker TEXT,mapped_sector_etf TEXT,mapping_confidence TEXT)")
    db.execute("INSERT INTO securities VALUES('MOCKB',NULL,'UNKNOWN')")
    db.execute("CREATE TABLE daily_bars(ticker TEXT,date TEXT,open REAL,close REAL,high REAL,low REAL,volume REAL,source TEXT,quality_status TEXT)")
    end = date.fromisoformat(DAY)
    days = []
    while len(days) < len(prices):
        days.append(end.isoformat())
        end = previous_session(end)
    db.executemany("INSERT INTO daily_bars VALUES(?,?,?,?,?,?,?,?,?)",
                   [("MOCKB", day, price, price, price + 1, price - 1, 100000, source, "OK")
                    for day, price in zip(reversed(days), prices)])
    return db


class HoldingsMonitorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.book = Portfolio(Path(self.tmp.name) / "stock.db")
        self.snap = snapshot()
        self.confirmed_at = datetime(2026, 9, 29, 19, tzinfo=timezone.utc).isoformat()

    def tearDown(self):
        self.book.close()
        self.tmp.cleanup()

    def test_ticker_only_confirmation_and_full_exit(self):
        self.assertIn("now tracked", command(self.book, "/bought MOCKB", self.snap, update_id=900))
        self.assertEqual([p["ticker"] for p in held(self.book.db)], ["MOCKB"])
        self.assertEqual(len(held(self.book.db)), 1)
        self.assertIn("MOCKB", command(self.book, "/portfolio", self.snap))
        self.assertIn("removed from daily exit checks", command(self.book, "/sold MOCKB", self.snap, update_id=901))
        self.assertEqual(held(self.book.db), [])
        self.assertEqual([r[0] for r in self.book.db.execute("SELECT kind FROM holding_events ORDER BY id")],
                         ["BOUGHT", "SOLD"])

    def test_money_commands_do_not_mutate_cash(self):
        self.assertIn("retired", command(self.book, "/deposit 2000", self.snap))
        self.assertIn("Entry price $200.00", command(self.book, "/bought MOCKB 200", self.snap))
        self.assertEqual(self.book.cash_cents(), 0)
        self.assertEqual(len(held(self.book.db)), 1)

    def test_confirmed_per_share_entry_exit_and_realized_return(self):
        self.assertIn("Entry price $100.00", command(self.book, "/bought MOCKB 100", self.snap))
        self.assertEqual(held(self.book.db)[0]["entry_price_source"], "USER_CONFIRMED_PRICE")
        self.assertIn("Exit price $125.00", command(self.book, "/sold MOCKB 125", self.snap))
        event = self.book.db.execute("SELECT price,metrics_json FROM holding_events WHERE kind='SOLD'").fetchone()
        self.assertEqual(event["price"], 125)
        self.assertEqual(json.loads(event["metrics_json"])["realized_return_pct"], 25)
        self.assertEqual(self.book.cash_cents(), 0)

    def test_unknown_entry_price_is_explicit(self):
        self.assertIn("Profit-protection features are limited", command(self.book, "/bought MOCKB", self.snap))
        self.assertEqual(held(self.book.db)[0]["entry_price_source"], "ENTRY_PRICE_UNKNOWN")
        self.assertIn("Exit price unknown", command(self.book, "/sold MOCKB", self.snap))

    def test_portfolio_shows_completed_exit_context(self):
        command(self.book, "/bought MOCKB 100", self.snap)
        reviewed = {"ticker": "MOCKB", "action": "HOLD_TIGHT", "exit_score": 20,
                    "exit_confidence": "HIGH", "reasons": ["PROFIT_OR_EVENT_MONITORING"],
                    "position": {"current_price": 115, "return_since_entry_pct": 15,
                                 "peak_return_pct": 18, "drawdown_from_peak_pct": -2,
                                 "profit_state": "PROFIT_PROTECTION", "atr": 3},
                    "trend": {"state": "INTACT", "support": 108, "ema21": 109},
                    "relative_strength": {"state": "STABLE"},
                    "thesis": {"state": "INTACT_WITH_AVAILABLE_EVIDENCE"},
                    "event": {"state": "EARNINGS_NORMAL", "upcoming_earnings": None},
                    "profit_protection_zone": [109, 110], "trend_failure_zone": [108, 109]}
        with self.book.atomic():
            self.book.db.execute("INSERT INTO exit_decisions VALUES(?,?,?,?,?,?,?,?)",
                                 ("2026-10-01", "MOCKB", "HOLD_TIGHT", 20, "HIGH",
                                  json.dumps(reviewed), "2026-10-01T20:00:00Z",
                                  held(self.book.db)[0]["confirmed_at"]))
        message = command(self.book, "/portfolio", self.snap)
        for expected in ("HOLD_TIGHT", "Peak return +18.0%", "Support 108", "EMA21 109",
                         "ATR 3", "Reference zones only. No order was placed."):
            self.assertIn(expected, message)

    def test_two_close_exit_and_persisted_decision(self):
        with self.book.atomic():
            confirm(self.book.db, "MOCKB", "BOUGHT")
            self.book.db.execute("UPDATE tracked_holdings SET confirmed_at=?,entry_date='2026-09-29' WHERE ticker='MOCKB'",
                                 (self.confirmed_at,))
        market = bars([100.0] * 210 + [80.0, 79.0])
        self.addCleanup(market.close)
        result = evaluate_all(self.book, market, DAY)
        self.assertEqual(result[0]["action"], "EXIT_CANDIDATE")
        self.assertEqual(evaluate_all(self.book, market, DAY), result)
        self.assertEqual(self.book.db.execute("SELECT COUNT(*) FROM holding_decisions").fetchone()[0], 1)
        saved = self.book.db.execute("""SELECT exit_state,trend_failure_low,trend_failure_high,current_price
            FROM position_reference_zones WHERE trading_date=? AND ticker='MOCKB'""", (DAY,)).fetchone()
        self.assertEqual(saved["exit_state"], "EXIT_CANDIDATE")
        self.assertLess(saved["trend_failure_low"], saved["trend_failure_high"])
        self.assertEqual(saved["current_price"], 79)
        market.execute("INSERT INTO daily_bars VALUES(?,?,?,?,?,?,?,?,?)",
                       ("MOCKB", "2026-10-02", 78, 78, 79, 77, 100000, "ALPACA_SIP", "OK"))
        evaluate_all(self.book, market, "2026-10-02")
        history = self.book.db.execute("""SELECT trading_date,current_price FROM position_reference_zones
            WHERE ticker='MOCKB' ORDER BY trading_date""").fetchall()
        self.assertEqual([(row[0], row[1]) for row in history], [(DAY, 79), ("2026-10-02", 78)])

    def test_missing_current_bar_does_not_invent_sell(self):
        market = bars([100.0] * 70)
        self.addCleanup(market.close)
        market.execute("DELETE FROM daily_bars WHERE date=?", (DAY,))
        decision = evaluate(market, "MOCKB", DAY, self.confirmed_at)
        self.assertEqual(decision["action"], "HOLD_DATA_INSUFFICIENT")

    def test_missing_intermediate_session_does_not_invent_sell(self):
        market = bars([100.0] * 68 + [80.0, 79.0])
        self.addCleanup(market.close)
        market.execute("DELETE FROM daily_bars WHERE date=?", (previous_session(date.fromisoformat(DAY)).isoformat(),))
        decision = evaluate(market, "MOCKB", DAY, self.confirmed_at)
        self.assertEqual(decision["action"], "HOLD_DATA_INSUFFICIENT")
        self.assertEqual(decision["reason"], "MISSING_DAILY_SESSION")

    def test_possible_split_does_not_invent_exit(self):
        market = bars([100.0] * 69 + [50.0])
        self.addCleanup(market.close)
        result = evaluate(market, "MOCKB", DAY, self.confirmed_at)
        self.assertEqual(result["action"], "HOLD_DATA_INSUFFICIENT")
        self.assertEqual(result["reason"], "PRICE_DISCONTINUITY_REVIEW")

    def test_no_same_session_exit(self):
        market = bars([100.0] * 68 + [80.0, 79.0])
        self.addCleanup(market.close)
        bought_after_close = datetime(2026, 10, 1, 21, tzinfo=timezone.utc).isoformat()
        self.assertEqual(evaluate(market, "MOCKB", DAY, bought_after_close)["action"],
                         "WAIT_NEXT_SESSION")

    def test_prebuy_close_cannot_trigger_sell(self):
        market = bars([100.0] * 68 + [80.0, 79.0])
        self.addCleanup(market.close)
        bought_on_previous_session = datetime(2026, 9, 30, 19, tzinfo=timezone.utc).isoformat()
        result = evaluate(market, "MOCKB", DAY, bought_on_previous_session)
        self.assertEqual(result["action"], "HOLD")
        self.assertEqual(result["reason"], "AWAITING_POST_BUY_CONFIRMATION_BARS")

    def test_strong_rally_prompts_trim_review_not_automatic_exit(self):
        market = bars([100.0 + i for i in range(70)])
        self.addCleanup(market.close)
        result = evaluate(market, "MOCKB", DAY, self.confirmed_at)
        self.assertEqual(result["action"], "TRIM_CANDIDATE")
        self.assertEqual(result["reason"], "RSI_ATR_OVEREXTENSION")
        self.assertGreater(result["sell_review_zone"][1], result["close_price"])

    def test_stable_holding_is_not_forced_to_sell(self):
        market = bars([100.0] * 70)
        self.addCleanup(market.close)
        result = evaluate(market, "MOCKB", DAY, self.confirmed_at)
        self.assertEqual(result["action"], "HOLD")
        self.assertEqual(result["sell_zone_basis"], "NEARBY_20D_RESISTANCE")

    def test_rally_pullback_prompts_gain_protection(self):
        market = bars([100.0] * 50 + [100.0 + i for i in range(18)] + [114.0, 112.0])
        self.addCleanup(market.close)
        result = evaluate(market, "MOCKB", DAY, self.confirmed_at)
        self.assertEqual(result["action"], "REDUCE_CANDIDATE")
        self.assertEqual(result["reason"], "RALLY_PULLBACK_BELOW_TRAIL")
        self.assertIsNotNone(result["protect_level"])


if __name__ == "__main__":
    unittest.main()

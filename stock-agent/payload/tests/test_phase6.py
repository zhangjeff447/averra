import json
import sqlite3
import sys
import tempfile
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from portfolio import Portfolio
from phase6_portfolio import allocate, correlation, format_candidate, load_config, monitor_position
from stock_agent import authorized_message, command, phase6_scan, request_json
from phase6_simulation import run as simulation_run
from phase6_bridge import from_entry_databases


def stamp(hours=0):
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()


def candidate(ticker, sector="Technology", conviction=90, entry=90, price=100, **extra):
    return {"ticker": ticker, "sector": sector, "asset_type": "COMMON_STOCK", "country": "US", "exchange": "NASDAQ",
            "conviction": conviction, "entry": entry,
            "risk_score": 80, "atr_pct": 3, "realized_vol_pct": 30, "drawdown_pct": 5,
            "chase_risk": "LOW", "regime": "NORMAL", "state": "BUY_TOMORROW_CANDIDATE",
            "price": price, "price_at": stamp(), **extra}


class Phase6Test(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "portfolio.db"
        self.book = Portfolio(self.path)
        self.snap = {"stocks": [{"ticker": "AMD", "sector": "Technology", "asset_type": "COMMON_STOCK", "country": "US", "exchange": "NASDAQ", "bars": [{"close": 100, "volume": 30000}] * 200}]}

    def tearDown(self):
        self.book.close()
        self.temp.cleanup()

    def test_zero_initial_and_multiple_deposits(self):
        self.assertEqual(self.book.cash_cents(), 0)
        self.book.deposit("2000")
        self.book.deposit("500")
        self.assertEqual(self.book.summary()["deposited_cents"], 250000)
        self.assertEqual([r["resulting_cash_cents"] for r in self.book.history()], [250000, 200000])

    def test_zero_capital_never_proposes_allocation(self):
        decision = allocate(self.book, [candidate("AMD", conviction=100, entry=100)], persist=False)[0]
        self.assertEqual(decision["suggested_cents"], 0)
        self.assertIn("NO_CAPITAL_CONFIGURED", decision["reasons"])

    def test_immutable_adjustment_and_negative_deposit(self):
        with self.assertRaises(ValueError):
            self.book.deposit("-2")
        self.book.deposit("100")
        self.book.adjust("-10", "Correction")
        self.assertEqual(self.book.cash_cents(), 9000)
        with self.assertRaises(sqlite3.IntegrityError):
            self.book.db.execute("UPDATE ledger SET amount_cents=1 WHERE id=1")
        self.assertEqual(self.book.cash_cents(), 9000)

    def test_fractional_buy_and_distinct_suggestion(self):
        self.book.deposit("1000")
        self.book.add_suggestion("AMD", "BUY_TOMORROW_CANDIDATE", 25000, "test")
        self.assertEqual(self.book.cash_cents(), 100000)
        shares, exact = self.book.buy("AMD", "200", "Technology", "162.35")
        self.assertTrue(exact)
        self.assertAlmostEqual(float(shares), 200 / 162.35)
        self.assertEqual(self.book.cash_cents(), 80000)
        self.assertEqual(self.book.history()[0]["price_source"], "USER_CONFIRMED_PRICE")

    def test_buy_cash_limit_and_transaction_rollback(self):
        self.book.deposit("100")
        with self.assertRaises(ValueError):
            self.book.buy("AMD", "101", "Technology", "100")
        self.assertEqual(len(self.book.positions()), 0)
        self.assertEqual(len(self.book.history()), 1)
        with patch.object(self.book, "_record", side_effect=sqlite3.OperationalError("disk")):
            with self.assertRaises(sqlite3.OperationalError):
                self.book.buy("AMD", "50", "Technology", "100")
        self.assertEqual(len(self.book.positions()), 0)
        self.assertEqual(self.book.cash_cents(), 10000)

    def test_partial_full_sell_and_realized_pl(self):
        self.book.deposit("1000")
        self.book.buy("AMD", "200", "Technology", "100")
        self.book.sell("AMD", "150", "150")
        state = self.book.summary()
        self.assertEqual(state["realized_pl_cents"], 5000)
        self.assertEqual(state["positions"][0]["cost_cents"], 10000)
        self.book.sell("AMD", "150", "150")
        self.assertEqual(len(self.book.positions()), 0)
        self.assertEqual(self.book.summary()["realized_pl_cents"], 10000)
        self.assertEqual(self.book.cash_cents(), 110000)

    def test_sell_over_position_and_invalid_ticker(self):
        self.book.deposit("1000")
        self.book.buy("AMD", "100", "Technology", "100")
        with self.assertRaises(ValueError):
            self.book.sell("AMD", "101", "100")
        with self.assertRaises(ValueError):
            self.book.buy("BAD!", "10", "Technology", "100")
        self.assertEqual(self.book.cash_cents(), 90000)

    def test_unrealized_and_stale(self):
        self.book.deposit("1000")
        self.book.buy("AMD", "200", "Technology", "100")
        self.book.quote("AMD", "120", stamp())
        self.assertEqual(self.book.summary()["unrealized_pl_cents"], 4000)
        self.book.quote("AMD", "130", stamp(100))
        self.assertIsNone(self.book.summary()["unrealized_pl_cents"])
        with self.assertRaisesRegex(ValueError, "PRICE_STALE"):
            self.book.buy("AMD", "50", "Technology")

    def test_no_price_estimate_without_fresh_quote(self):
        self.book.deposit("1000")
        self.book.quote("AMD", "100", stamp())
        self.book.buy("AMD", "50", "Technology")
        self.assertEqual(self.book.history()[0]["price_source"], "ESTIMATED_PRICE")

    def test_idempotent_deposit_and_buy(self):
        first = command(self.book, "/deposit 2000", self.snap, update_id=1)
        self.assertEqual(first, command(self.book, "/deposit 2000", self.snap, update_id=1))
        self.assertEqual(self.book.cash_cents(), 0)
        reply = command(self.book, "/bought AMD", self.snap, update_id=2)
        self.assertEqual(reply, command(self.book, "/bought AMD", self.snap, update_id=2))
        self.assertEqual(self.book.db.execute("SELECT COUNT(*) FROM holding_events").fetchone()[0], 1)

    def test_command_views_and_skip(self):
        command(self.book, "/bought AMD", self.snap)
        self.book.add_suggestion("AMD", "WATCH", 0, "wait")
        self.assertIn("skipped", command(self.book, "/skip AMD", self.snap))
        self.assertIn("retired", command(self.book, "/balance", self.snap))
        self.assertIn("AMD", command(self.book, "/portfolio", self.snap))
        self.assertIn("BOUGHT", command(self.book, "/history", self.snap))
        self.assertIn("MONITOR ONLY", command(self.book, "/status", self.snap))

    def test_authorized_chat(self):
        update = {"message": {"chat": {"id": 123, "type": "private"}, "from": {"id": 123}, "text": "/deposit 2000"}}
        self.assertTrue(authorized_message(update, "123"))
        self.assertFalse(authorized_message(update, "456"))
        update["message"]["chat"]["type"] = "group"
        self.assertFalse(authorized_message(update, "123"))
        self.assertEqual(self.book.cash_cents(), 0)

    def test_quote_quality_allocation(self):
        self.book.deposit("2000")
        c = candidate("AMD", price_at=stamp(100))
        # Replace the generated current timestamp with a stale one.
        d = allocate(self.book, [c], persist=False)[0]
        self.assertEqual(d["state"], "WAIT_FOR_FRESH_PRICE")
        self.assertIsNone(d["estimated_shares"])

    def test_stale_held_valuation_blocks_new_allocation(self):
        self.book.deposit("2000")
        self.book.buy("OLD", "100", "Utilities", "100")
        decision = allocate(self.book, [candidate("AMD")], persist=False)[0]
        self.assertEqual(decision["state"], "WAIT_FOR_FRESH_PRICE")
        self.assertIn("PORTFOLIO_PRICE_STALE", decision["reasons"])

    def test_cash_and_single_cap(self):
        self.book.deposit("2000")
        decisions = allocate(self.book, [candidate("AMD", conviction=100, entry=100)], persist=False)
        self.assertLessEqual(decisions[0]["suggested_cents"], 30000)
        self.assertGreaterEqual(200000 - decisions[0]["suggested_cents"], 40000)

    def test_cash_reserve_blocks_even_high_score(self):
        self.book.deposit("2000")
        self.book.buy("OLD", "1600", "Utilities", "100")
        self.book.quote("OLD", "100", stamp())
        decision = allocate(self.book, [candidate("AMD", conviction=100, entry=100)], persist=False)[0]
        self.assertEqual(decision["suggested_cents"], 0)
        self.assertEqual(decision["state"], "WATCH")

    def test_opportunity_reserve_blocks_lower_priority(self):
        self.book.deposit("2000")
        self.book.buy("OLD", "1400", "Utilities", "100")
        self.book.quote("OLD", "100", stamp())
        good = candidate("GOOD", conviction=75, entry=75)
        without_waiting = allocate(self.book, [good], persist=False)[0]
        waiting = candidate("BEST", sector="Healthcare", conviction=98, entry=98, state="WAIT")
        with_waiting = {d["ticker"]: d for d in allocate(self.book, [good, waiting], persist=False)}["GOOD"]
        self.assertGreater(without_waiting["suggested_cents"], 0)
        self.assertEqual(with_waiting["suggested_cents"], 0)

    def test_sector_and_correlation_caps(self):
        self.book.deposit("2000")
        returns = [((i % 7) - 3) / 100 for i in range(30)]
        items = [candidate("AMD", returns=returns), candidate("NVDA", returns=returns),
                 candidate("AVGO", returns=returns)]
        result = allocate(self.book, items, persist=False)
        self.assertLessEqual(sum(d["suggested_cents"] for d in result), 60000)
        self.assertEqual(correlation(returns, returns), 1)

    def test_cross_sector_correlation_cap(self):
        self.book.deposit("2000")
        returns = [((i % 7) - 3) / 100 for i in range(30)]
        items = [candidate("AAA", sector="Technology", conviction=100, entry=100, returns=returns),
                 candidate("BBB", sector="Healthcare", conviction=100, entry=100, returns=returns),
                 candidate("CCC", sector="Financials", conviction=100, entry=100, returns=returns)]
        result = allocate(self.book, items, persist=False)
        self.assertEqual([d["suggested_cents"] for d in result], [30000, 30000, 0])
        self.assertIn("CORRELATION_CAP", result[2]["reasons"])

    def test_multi_candidate_priority_and_opportunity_reserve(self):
        self.book.deposit("2000")
        waiting = candidate("BEST", sector="Healthcare", conviction=98, entry=98, state="WAIT")
        ready = candidate("GOOD", sector="Technology", conviction=75, entry=75)
        result = allocate(self.book, [ready, waiting], persist=False)
        self.assertEqual(result[0]["ticker"], "BEST")
        self.assertEqual(result[0]["suggested_cents"], 0)
        self.assertLessEqual(result[1]["suggested_cents"], 30000)

    def test_existing_position_add_and_hold(self):
        self.book.deposit("2000")
        self.book.buy("AMD", "100", "Technology", "100")
        self.book.quote("AMD", "100", stamp())
        result = allocate(self.book, [candidate("AMD")], persist=False)[0]
        self.assertEqual(result["state"], "ADD_CANDIDATE")
        self.assertLessEqual(result["suggested_cents"], 20000)
        hold = allocate(self.book, [candidate("AMD", state="WAIT", entry=90)], persist=False)[0]
        self.assertEqual(hold["state"], "HOLD")

    def test_reduce_and_exit(self):
        cfg = load_config()
        self.assertEqual(monitor_position({"material_negative_event": True}, {}, cfg), "EXIT_CANDIDATE")
        self.assertEqual(monitor_position({"research_deterioration": True}, {}, cfg), "REDUCE_CANDIDATE")

    def test_persistence_and_candidate_message(self):
        self.book.deposit("2000")
        decision = allocate(self.book, [candidate("AMD")], persist=True)[0]
        self.assertEqual(self.book.cash_cents(), 200000)
        self.assertIn("No trade was placed", format_candidate(decision, 200000, 200000))
        self.assertEqual(self.book.db.execute("SELECT COUNT(*) FROM allocation_decisions").fetchone()[0], 1)
        self.assertEqual(self.book.db.execute("SELECT COUNT(*) FROM suggested_portfolio_snapshots").fetchone()[0], 1)
        self.assertIn("No completed daily candidate report", command(self.book, "/candidates", self.snap))

    def test_phase6_file_demotes_buy_now(self):
        self.book.deposit("2000")
        path = Path(self.temp.name) / "candidate.json"
        path.write_text(json.dumps({"asof": stamp(), "candidates": [candidate("AMD", state="BUY_NOW_CANDIDATE")]}))
        self.assertIn("BUY_TOMORROW_CANDIDATE", phase6_scan(self.book, path)[0])

    def test_daily_snapshot_and_performance_not_fabricated(self):
        self.book.deposit("2000")
        sid = self.book.daily_snapshot(datetime.now(timezone.utc).date().isoformat(), 100, 200)
        self.assertEqual(self.book.performance(sid), [])
        self.assertEqual(self.book.db.execute("SELECT status FROM portfolio_snapshots WHERE id=?", (sid,)).fetchone()[0], "OK")

    def test_performance_with_external_deposit(self):
        self.book.deposit("1000")
        first = self.book.daily_snapshot("2026-08-01", 100, 200)
        self.book.deposit("500")
        second = self.book.daily_snapshot("2026-09-01", 110, 220)
        result = self.book.performance(second)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["horizon"], "1M")
        self.assertAlmostEqual(result[0]["return"], 0)
        self.assertAlmostEqual(result[0]["spy_return"], .1)
        self.assertEqual(self.book.db.execute("SELECT start_snapshot_id FROM portfolio_performance WHERE snapshot_id=?", (second,)).fetchone()[0], first)

    def test_legacy_ledger_migration_preserves_rows(self):
        old = Path(self.temp.name) / "legacy.db"
        db = sqlite3.connect(old)
        db.execute("""CREATE TABLE ledger (id INTEGER PRIMARY KEY,created_at TEXT NOT NULL,
            kind TEXT NOT NULL CHECK(kind IN ('DEPOSIT','BUY','SELL')),ticker TEXT,amount_cents INTEGER NOT NULL,
            shares TEXT,fill_price TEXT,price_source TEXT,note TEXT)""")
        db.execute("INSERT INTO ledger(created_at,kind,amount_cents) VALUES('2026-01-01','DEPOSIT',10000)")
        db.commit()
        db.close()
        migrated = Portfolio(old)
        try:
            self.assertEqual(migrated.cash_cents(), 10000)
            migrated.adjust("-10", "correct")
            self.assertEqual(migrated.cash_cents(), 9000)
            self.assertEqual(len(migrated.history()), 2)
        finally:
            migrated.close()

    def test_network_retry_schedule(self):
        error = urllib.error.URLError("offline")
        with patch("stock_agent.urllib.request.urlopen", side_effect=[error, error, error, error]) as urlopen, patch("stock_agent.time.sleep") as sleep:
            with self.assertRaises(urllib.error.URLError):
                request_json("https://invalid.local")
        self.assertEqual(urlopen.call_count, 4)
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [5, 15, 30])

    def test_simulation_isolated_from_other_database(self):
        self.book.deposit("2000")
        another = Portfolio(Path(self.temp.name) / "real.db")
        try:
            self.assertEqual(another.cash_cents(), 0)
        finally:
            another.close()

    def test_disposable_simulation_makes_no_api_calls(self):
        with patch("stock_agent.request_json") as network:
            result = simulation_run()
        network.assert_not_called()
        self.assertEqual(result["tracked_after_buy"], ["AMD"])
        self.assertEqual(result["tracked_after_sell"], 0)
        self.assertEqual(result["holding_events"], 2)
        self.assertEqual(result["api_calls"], 0)

    def test_readonly_phase5_bridge(self):
        entry_path = Path(self.temp.name) / "entry.db"
        market_path = Path(self.temp.name) / "market.db"
        e = sqlite3.connect(entry_path)
        e.execute("CREATE TABLE entry_runs(id INTEGER,asof TEXT,status TEXT)")
        e.execute("CREATE TABLE entry_decisions(run_id INTEGER,ticker TEXT,entry_price REAL,entry_score REAL,entry_state TEXT,chase_risk TEXT,regime TEXT,timeframes_json TEXT,research_reference_json TEXT)")
        e.execute("INSERT INTO entry_runs VALUES(1,'2026-09-25','COMPLETE')")
        e.execute("INSERT INTO entry_decisions VALUES(1,'AMD',100,80,'WAIT','LOW','NORMAL',?,?)",
                  (json.dumps({"daily": {"atr_pct": .03}}), json.dumps({"research_status": "COMPLETE", "sector": "Technology", "conviction_score": 90})))
        e.commit()
        e.close()
        m = sqlite3.connect(market_path)
        m.execute("CREATE TABLE quant_runs(id INTEGER,asof TEXT)")
        m.execute("CREATE TABLE quant_scores(run_id INTEGER,ticker TEXT,risk_score REAL,features_json TEXT)")
        m.execute("CREATE TABLE securities(ticker TEXT,asset_type TEXT,country TEXT,exchange TEXT,is_etf INTEGER,is_otc INTEGER,is_preferred INTEGER,is_warrant INTEGER)")
        m.execute("CREATE TABLE daily_bars(ticker TEXT,date TEXT,close REAL)")
        m.execute("INSERT INTO quant_runs VALUES(1,'2026-09-25')")
        m.execute("INSERT INTO quant_scores VALUES(1,'AMD',75,?)", (json.dumps({"realized_vol20": .4, "max_drawdown120": -.2}),))
        m.execute("INSERT INTO securities VALUES('AMD','COMMON_STOCK','US','NASDAQ',0,0,0,0)")
        m.execute("INSERT INTO daily_bars VALUES('AMD','2026-09-25',100)")
        m.commit()
        m.close()
        packet = from_entry_databases(entry_path, market_path)
        self.assertEqual(packet["candidates"][0]["atr_pct"], 3)
        self.assertEqual(packet["candidates"][0]["state"], "WAIT")
        self.assertEqual(packet["candidates"][0]["returns"], [])


if __name__ == "__main__":
    unittest.main()

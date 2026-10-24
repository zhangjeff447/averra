import unittest
from datetime import date, timedelta

from exit_engine import evaluate, relative_strength, thesis_assessment
from market_regime import market_session, previous_session
from price_plan import buy_plan, opening_rule
from phase5_entry import load_config as entry_config

DAY = "2026-10-01"


def series(prices, ticker="AMD", *, volumes=None):
    current = date.fromisoformat(DAY)
    dates = []
    for _ in prices:
        dates.append(current.isoformat())
        current = previous_session(current)
    dates.reverse()
    volumes = volumes or [100000] * len(prices)
    return [{"ticker": ticker, "date": day, "open": price, "high": price + 1,
             "low": price - 1, "close": price, "volume": volume,
             "source": "ALPACA_SIP" if index >= len(prices) - 4 else "MASSIVE",
             "quality_status": "OK" if index >= len(prices) - 4 else "VALID"}
            for index, (day, price, volume) in enumerate(zip(dates, prices, volumes))]


def review(prices, *, entry_price=100, entry_index=-25, events=None, thesis=True,
           spy=None, qqq=None, sector=None, volumes=None):
    bars = series(prices, volumes=volumes)
    benchmarks = {"SPY": series(spy or [100] * len(prices), "SPY"),
                  "QQQ": series(qqq or [100] * len(prices), "QQQ")}
    if sector is not None:
        benchmarks["XLK"] = series(sector, "XLK")
    position = {"ticker": "AMD", "entry_price": entry_price,
                "entry_date": bars[entry_index]["date"], "confirmed_at": bars[entry_index]["date"] + "T16:00:00-04:00"}
    context = {"original_thesis": {"claims": {"BULL_CASE": ["growth"]}} if thesis else None,
               "events": events or [], "sector_etf": "XLK" if sector is not None else None}
    return evaluate(bars, benchmarks, position, context, DAY)


class ExitEngineTests(unittest.TestCase):
    def test_profitable_rally_still_holds(self):
        prices = [100.0] * 210 + [100 + i * .45 for i in range(50)]
        result = review(prices, entry_price=110, entry_index=-28)
        self.assertIn(result["action"], {"HOLD", "HOLD_TIGHT"})
        self.assertGreater(result["position"]["return_since_entry_pct"], 0)
        self.assertIsNotNone(result["profit_protection_zone"])

    def test_profitable_pullback_uses_atr_trail(self):
        prices = [100.0] * 220 + [100 + i * .8 for i in range(37)] + [124, 121, 118]
        result = review(prices, entry_price=105, entry_index=-30)
        self.assertIn(result["action"], {"REDUCE_CANDIDATE", "EXIT_CANDIDATE"})
        self.assertGreater(result["position"]["peak_drawdown_atr_multiple"], 1)
        self.assertLess(result["sell_review_zone"][0], result["close_price"])
        self.assertGreater(result["sell_review_zone"][1], result["close_price"])
        self.assertIsNone(result["protect_level"])

    def test_parabolic_multi_factor_trim(self):
        prices = [100.0] * 240 + [100 + i * i * .2 for i in range(20)]
        volumes = [100000] * 259 + [350000]
        result = review(prices, entry_price=105, entry_index=-18, volumes=volumes)
        self.assertIn(result["action"], {"TRIM_CANDIDATE", "HOLD_TIGHT"})
        self.assertIn("PARABOLIC_MOVE", result["reasons"])

    def test_two_session_major_trend_breakdown(self):
        result = review([100.0] * 258 + [72.0, 70.0], entry_price=100)
        self.assertEqual(result["action"], "EXIT_CANDIDATE")
        self.assertEqual(result["trend"]["state"], "MAJOR_BREAKDOWN")

    def test_verified_material_thesis_breaks_but_news_does_not(self):
        event = {"id": "E1", "event_type": "GUIDANCE_CUT", "materiality": "MATERIAL",
                 "official": True, "verified": True, "thesis_conflict": True}
        result = review([100.0] * 260, events=[event])
        self.assertEqual(result["action"], "EXIT_CANDIDATE")
        self.assertEqual(result["thesis"]["evidence_ids"], ["E1"])
        uncertain = review([100.0] * 260, events=[{**event, "verified": False}])
        self.assertNotEqual(uncertain["action"], "EXIT_CANDIDATE")

    def test_thesis_damaged_review(self):
        event = {"id": "E2", "event_type": "MAJOR_DILUTION", "materiality": "HIGH",
                 "official": True, "verified": True, "thesis_conflict": True}
        self.assertEqual(review([100.0] * 260, events=[event])["action"], "THESIS_REVIEW")

    def test_relative_strength_requires_persistent_underperformance(self):
        prices = [100.0] * 239 + [100 - i * .04 for i in range(21)]
        benchmarks = [100.0] * 239 + [100 + i * .5 for i in range(21)]
        result = review(prices, spy=benchmarks, qqq=benchmarks, sector=benchmarks)
        self.assertEqual(result["relative_strength"]["state"], "RS_DETERIORATING")
        self.assertLess(result["relative_strength"]["windows"]["SPY"]["20d"], 0)
        single = review([100.0] * 259 + [99.0], spy=[100.0] * 259 + [102.0])
        self.assertNotEqual(single["relative_strength"]["state"], "RS_DETERIORATING")

    def test_flat_time_stop_requires_other_deterioration(self):
        prices = [100.0] * 239 + [100 - i * .04 for i in range(21)]
        benchmark = [100.0] * 239 + [100 + i * .5 for i in range(21)]
        result = review(prices, entry_price=100, entry_index=-20, spy=benchmark, qqq=benchmark)
        self.assertIn(result["action"], {"THESIS_REVIEW", "REDUCE_CANDIDATE"})
        self.assertIn("THESIS_STALLED", result["reasons"])
        self.assertEqual(review([100.0] * 260, entry_index=-20)["action"], "HOLD")

    def test_earnings_imminent_and_material_event_are_risk_not_auto_exit(self):
        future = date.fromisoformat(DAY)
        for _ in range(2):
            future += timedelta(days=1)
            while not market_session(future):
                future += timedelta(days=1)
        events = [{"id": "E3", "event_type": "EARNINGS_SCHEDULE", "event_date": future.isoformat(),
                   "official": True, "verified": True},
                  {"id": "E4", "event_type": "FDA_DECISION", "event_date": future.isoformat(),
                   "official": True, "verified": True}]
        result = review([100.0] * 260, entry_price=95, events=events)
        self.assertEqual(result["event"]["state"], "EARNINGS_IMMINENT")
        self.assertIn("UPCOMING_MATERIAL_EVENT", result["reasons"])
        self.assertNotEqual(result["action"], "EXIT_CANDIDATE")

    def test_rsi_alone_does_not_force_profit_sale(self):
        prices = [100.0] * 220 + [100 + i * .25 for i in range(40)]
        result = review(prices, entry_price=103, entry_index=-25)
        self.assertGreaterEqual(result["trend"]["adx"], 20)
        self.assertIn(result["action"], {"HOLD", "HOLD_TIGHT"})

    def test_recent_material_event_needs_post_entry_provenance(self):
        event = {"id": "E9", "event_type": "GUIDANCE", "materiality": "MATERIAL",
                 "official": True, "verified": True, "date": DAY}
        old = review([100.0] * 260, events=[{**event, "new_since_entry": False}])
        new = review([100.0] * 260, events=[{**event, "new_since_entry": True}])
        self.assertNotEqual(old["action"], "THESIS_REVIEW")
        self.assertEqual(new["action"], "THESIS_REVIEW")

    def test_stale_current_bar_blocks_exit(self):
        bars = series([100.0] * 258 + [70, 69])
        bars[-1]["source"] = "MASSIVE"
        position = {"ticker": "AMD", "entry_price": 100,
                    "entry_date": bars[-25]["date"], "confirmed_at": bars[-25]["date"] + "T16:00:00-04:00"}
        result = evaluate(bars, {}, position, {"original_thesis": None, "events": []}, DAY)
        self.assertEqual(result["action"], "HOLD_DATA_INSUFFICIENT")

    def test_unknown_entry_price_disables_profit_math(self):
        result = review([100.0] * 260, entry_price=None)
        self.assertIsNone(result["position"]["return_since_entry_pct"])
        self.assertIsNone(result["profit_protection_zone"])
        self.assertIn("ENTRY_PRICE_UNKNOWN_PROFIT_LIMITED", result["reasons"])

    def test_opening_gap_outside_zone_waits_or_cancels(self):
        entry = {"asof": DAY, "entry_state": "BUY_TOMORROW_CANDIDATE",
                 "entry_price_reference": 100, "timeframes": {"daily": {"atr14": 4}},
                 "features": {"support_resistance": {"support": 99}}}
        plan = buy_plan(entry, entry_config()["thresholds"])
        self.assertEqual(opening_rule(plan, 107), "WAIT_REASSESS")
        self.assertEqual(opening_rule(plan, 113), "CANCEL_CHASE")


if __name__ == "__main__":
    unittest.main()

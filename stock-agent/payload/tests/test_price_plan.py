import json
import unittest
from pathlib import Path

from price_plan import buy_plan, opening_assessment, opening_rule, reference_status


THRESHOLDS = json.loads((Path(__file__).resolve().parents[1] / "phase5_config.json").read_text())["thresholds"]


class PricePlanTests(unittest.TestCase):
    def entry(self):
        return {"asof": "2026-10-01", "entry_state": "BUY_TOMORROW_CANDIDATE",
                "target_session": "2026-10-02",
                "entry_price_reference": 100.0, "timeframes": {"daily": {"atr14": 4.0}},
                "features": {"support_resistance": {"support": 99.5}}}

    def test_buy_zone_and_opening_gap_safety(self):
        plan = buy_plan(self.entry(), THRESHOLDS)
        self.assertEqual((plan["buy_zone_low"], plan["buy_zone_high"]), (99.5, 101.0))
        self.assertEqual(plan["do_not_chase_above"], 101.0)
        self.assertEqual(plan["generated_from_trading_date"], "2026-10-01")
        self.assertEqual(plan["target_trading_date"], "2026-10-02")
        self.assertIsNotNone(plan["reference_generated_at"])
        self.assertEqual(plan["nearest_support"], 99.5)
        self.assertEqual(reference_status(plan, 101.01), "WAIT_FOR_REASSESSMENT")
        self.assertEqual(reference_status(plan, 100.5), "POST_CLOSE_REFERENCE_ONLY")
        self.assertEqual(opening_rule(plan, 100.5), "ZONE_ONLY_WAIT_FOR_CONFIRMATION")
        self.assertEqual(opening_rule(plan, 107.0), "WAIT_REASSESS")
        self.assertEqual(opening_rule(plan, 113.0), "CANCEL_CHASE")
        self.assertEqual(opening_rule(plan, 98.0), "WAIT_REASSESS")

    def test_non_buy_or_missing_atr_has_no_fabricated_zone(self):
        entry = self.entry()
        entry["entry_state"] = "WATCH"
        self.assertIsNone(buy_plan(entry, THRESHOLDS))
        entry["entry_state"] = "BUY_TOMORROW_CANDIDATE"
        del entry["timeframes"]["daily"]["atr14"]
        self.assertEqual(buy_plan(entry, THRESHOLDS)["status"], "PRICE_DATA_INSUFFICIENT")

    def test_gap_atr_distinguishes_low_from_extreme(self):
        entry = self.entry()
        entry["timeframes"]["daily"]["atr14"] = 1
        tight = buy_plan(entry, THRESHOLDS)
        self.assertEqual(opening_assessment(tight, 103)["gap_state"], "EXTREME_CHASE_RISK")
        entry["timeframes"]["daily"]["atr14"] = 10
        wide = buy_plan(entry, THRESHOLDS)
        self.assertEqual(opening_assessment(wide, 103)["gap_state"], "NORMAL_OPEN")
        self.assertEqual(opening_assessment(wide, 103)["decision"], "WAIT_FOR_REASSESSMENT")

    def test_opening_confirmation_needs_reliable_data(self):
        plan = buy_plan(self.entry(), THRESHOLDS)
        self.assertEqual(opening_assessment(plan, 100.5)["decision"], "WAIT_FOR_REASSESSMENT")
        observed = {"minutes_after_open": 12, "price": 100.5, "vwap": 100.2,
                    "opening_range_high": 100.4, "rvol": 1.5, "rs_spy_pct": 1}
        self.assertEqual(opening_assessment(plan, 100.5, observed)["decision"], "ENTRY_CONFIRMED")
        self.assertEqual(opening_assessment(plan, 100.5, {**observed, "material_negative_catalyst": True})["decision"], "WAIT")


if __name__ == "__main__":
    unittest.main()

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from shadow_daily_scheduler import render, run_once
from shadow_daily_state import (claim_telegram, connect, record,
                                telegram_sent)

ET = ZoneInfo("America/New_York")
DAY = "2026-09-29"


class DailySchedulerTests(unittest.TestCase):
    def test_buy_candidate_outside_top_five_is_named(self):
        cases = [{"ticker": f"T{i}", "candidate_rank": i, "eligibility": "RESEARCH_LIMITED",
                  "research_quality_tier": "LIMITED_RESEARCH"} for i in range(1, 6)]
        cases.append({"ticker": "LITE", "candidate_rank": 10, "eligibility": "RESEARCH_LIMITED",
                      "research_quality_tier": "LIMITED_RESEARCH", "effective_conviction": 71})
        report = {"trading_date": "2026-10-01", "status": "PIPELINE_COMPLETE",
                  "generated_at": "2026-10-01T20:23:54+00:00",
                  "market_data_as_of": "2026-10-01", "market_data_source": "ALPACA_SIP",
                  "sol_calls": 0, "no_trade_placed": True, "eligibility_counts": {},
                  "entry_counts": {"BUY_TOMORROW_CANDIDATE": 1, "WATCH": 5},
                  "eligibility_cases": cases, "entry_results": [
                      {"ticker": item["ticker"], "entry_state":
                       "BUY_TOMORROW_CANDIDATE" if item["ticker"] == "LITE" else "WATCH",
                       "entry_score": 81.5 if item["ticker"] == "LITE" else 70,
                       "buy_plan": {"status": "CONDITIONAL", "buy_zone_low": 100,
                                    "buy_zone_high": 102, "do_not_chase_above": 102, "wait_gap_pct": 5,
                                    "cancel_gap_pct": 12, "generated_from_trading_date": "2026-10-01",
                                    "target_trading_date": "2026-10-02",
                                    "nearest_support": 99, "nearest_resistance": 105,
                                    "chase_risk": "MEDIUM"} if item["ticker"] == "LITE" else None}
                      for item in cases], "shadow_day": 4, "market_regime": "NORMAL",
                  "target_session": "2026-10-02", "portfolio": {"tracked_count": 1,
                      "holding_decisions": [{"ticker": "AMD", "action": "EXIT_CANDIDATE",
                                             "reason": "CLOSE_BELOW_SMA50_CONFIRMED",
                                             "sell_review_zone": [50, 51]}]}, "luna_calls": 0,
                  "luna_input_tokens": 0, "luna_output_tokens": 0,
                  "luna_estimated_cost_usd": 0}
        message = render(report)
        self.assertIn("生成时间：2026-10-01 16:23 ET", message)
        self.assertIn("🟢 BUY TOMORROW（非实时买入）\nLITE", message)
        self.assertIn("Preferred Entry Zone：$100.00–$102.00", message)
        self.assertIn("Do Not Chase Above：$102.00", message)
        self.assertIn("Generated From：2026-10-01 Close | For：2026-10-02 Session", message)
        self.assertIn("Research LIMITED_RESEARCH", message)
        self.assertIn("Conviction 71 | Entry Score 81.5", message)
        self.assertIn("WAIT_FOR_REASSESSMENT（未实时复核）", message)
        self.assertIn("👀 WATCH", message)
        self.assertIn("⏸ HOLD / POSITION REVIEW\n无", message)
        self.assertIn("🔴 SELL / REDUCE（人工复核）\nAMD | 卖出候选", message)
        self.assertIn("Action Review Zone：$50.00–$51.00", message)
        self.assertIn("No trade was placed.", message)
        self.assertNotIn("BUY NOW", message)
        self.assertNotIn("Research:", message)
        self.assertNotIn("Top Candidates:", message)
        self.assertNotIn("allocation", message.lower())
        self.assertNotIn("Cash:", message)
        # An unconfirmed prior suggestion is not a holding. The close report
        # still evaluates fresh next-session BUY candidates with zero holdings.
        report["portfolio"] = {"tracked_count": 0, "holding_decisions": []}
        without_confirmed_buy = render(report)
        self.assertIn("🟢 BUY TOMORROW（非实时买入）\nLITE", without_confirmed_buy)
        self.assertIn("⏸ HOLD / POSITION REVIEW\n无", without_confirmed_buy)
        self.assertIn("🔴 SELL / REDUCE（人工复核）\n无", without_confirmed_buy)

    def test_p1_and_insufficient_reporting_has_no_fake_entry(self):
        report = {"trading_date": "2026-10-01", "status": "PIPELINE_COMPLETE",
                  "market_data_as_of": "2026-10-01", "market_data_source": "ALPACA_SIP",
                  "sol_calls": 0, "no_trade_placed": True, "eligibility_counts": {"DEEP_REQUIRED": 1},
                  "entry_counts": {"ENTRY_DATA_INSUFFICIENT": 1},
                  "eligibility_cases": [], "entry_results": [{"ticker": "MOCKB",
                      "entry_state": "ENTRY_DATA_INSUFFICIENT", "entry_score": None,
                      "why_wait": ["insufficient 5M history"]}], "shadow_day": 4,
                  "market_regime": "NORMAL", "target_session": "2026-10-02",
                  "portfolio": {"tracked_count": 0, "holding_decisions": []},
                  "deep_triage": {"rows": [{"ticker": "ABC", "group": "P1_DEEP_NOW",
                      "reason": "material event conflict", "evidence_coverage_pct": 80,
                      "estimated_sol_input_tokens": 6000, "estimated_sol_output_tokens": 1000,
                      "estimated_sol_cost_usd": .025}]},
                  "luna_calls": 1, "luna_input_tokens": 3000,
                  "luna_output_tokens": 500, "luna_estimated_cost_usd": .001}
        message = render(report)
        self.assertIn("🟢 BUY TOMORROW（非实时买入）\n无", message)
        self.assertIn("数据不足待观察：MOCKB", message)
        self.assertNotIn("MOCKB · 卖出", message)
        self.assertNotIn("DEEP RESEARCH", message)
        self.assertNotIn("Luna Cost", message)

    def test_hold_and_escalated_states_show_both_references(self):
        base = {"trading_date": "2026-10-01", "status": "PIPELINE_COMPLETE",
                "market_data_as_of": "2026-10-01", "market_data_source": "ALPACA_SIP",
                "sol_calls": 0, "no_trade_placed": True, "entry_counts": {}, "entry_results": [],
                "target_session": "2026-10-02", "portfolio": {"tracked_count": 0, "holding_decisions": []}}
        for state in ("HOLD", "HOLD_TIGHT", "TRIM_CANDIDATE", "REDUCE_CANDIDATE", "EXIT_CANDIDATE"):
            with self.subTest(state=state):
                row = {"ticker": "AMD", "action": state, "position": {"entry_price": 100,
                       "current_price": 110, "return_since_entry_pct": 10},
                       "profit_protection_zone": [105, 107], "trend_failure_zone": [97, 99],
                       "trend": {"state": "INTACT"}, "thesis": {"state": "INTACT_WITH_AVAILABLE_EVIDENCE"},
                       "exit_score": 20, "reasons": ["TREND_INTACT"]}
                base["portfolio"] = {"tracked_count": 1, "holding_decisions": [row]}
                message = render(base)
                self.assertIn("Profit Protection Reference：$105.00–$107.00", message)
                self.assertIn("Trend Failure Reference：$97.00–$99.00", message)
                self.assertIn("Reason：TREND_INTACT", message)

    def test_unknown_entry_price_does_not_fabricate_profit_zone(self):
        report = {"trading_date": "2026-10-01", "status": "PIPELINE_COMPLETE",
                  "market_data_as_of": "2026-10-01", "market_data_source": "ALPACA_SIP",
                  "sol_calls": 0, "no_trade_placed": True, "entry_counts": {}, "entry_results": [],
                  "target_session": "2026-10-02", "portfolio": {"tracked_count": 1,
                    "holding_decisions": [{"ticker": "AMD", "action": "HOLD",
                      "position": {"entry_price": None, "current_price": 110},
                      "profit_protection_zone": None, "trend_failure_zone": [97, 99]}]}}
        message = render(report)
        self.assertIn("UNAVAILABLE — ENTRY PRICE UNKNOWN", message)
        self.assertIn("Trend Failure Reference：$97.00–$99.00", message)

    def test_buy_plan_without_matching_target_date_is_blocked(self):
        report = {"trading_date": "2026-10-01", "status": "PIPELINE_COMPLETE",
                  "market_data_as_of": "2026-10-01", "market_data_source": "ALPACA_SIP",
                  "sol_calls": 0, "no_trade_placed": True,
                  "entry_counts": {"BUY_TOMORROW_CANDIDATE": 1},
                  "entry_results": [{"ticker": "AMD", "entry_state": "BUY_TOMORROW_CANDIDATE",
                     "buy_plan": {"status": "CONDITIONAL", "buy_zone_low": 100,
                                  "buy_zone_high": 102, "do_not_chase_above": 102,
                                  "generated_from_trading_date": "2026-10-01",
                                  "target_trading_date": "2026-10-01"}}],
                  "target_session": "2026-10-02", "portfolio": {"tracked_count": 0,
                      "holding_decisions": []}}
        with self.assertRaisesRegex(ValueError, "DAILY_BUY_REFERENCE_INVALID"):
            render(report)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "state.db"
        self.result = Path(self.temp.name) / "final.json"
        self.result.write_text(json.dumps({"status": "PIPELINE_COMPLETE", "trading_date": DAY,
            "market_data_as_of": DAY, "phase_statuses": {**{k: "COMPLETE" for k in
            ("market", "phase1", "phase2", "phase3", "research", "entry",
             "portfolio", "forward_tracking")}, "telegram_summary": "READY"}}))
        self.calls = {"updater": 0, "pipeline": 0, "telegram": 0}

    def tearDown(self):
        self.temp.cleanup()

    def updater(self, day):
        self.calls["updater"] += 1
        return {"status": "CACHED"}

    def pipeline(self, day, market):
        self.calls["pipeline"] += 1
        return self.result, {"luna_calls": 0, "luna_input_tokens": 0,
                             "luna_output_tokens": 0, "total_ai_cost_local_estimate_usd": 0}

    def sender(self, db, day, path):
        self.calls["telegram"] += 1
        key = claim_telegram(db, day, path)
        telegram_sent(db, day, key, 42)
        return 42

    def run_at(self, hour, minute, gate):
        return run_once(datetime(2026, 9, 29, hour, minute, tzinfo=ET),
                        state_path=self.db_path, updater=self.updater,
                        gate=gate, pipeline=self.pipeline, sender=self.sender)

    def test_wait_1620_retry_1635_then_one_complete(self):
        first = self.run_at(16, 20, lambda day, at: False)
        self.assertEqual(first["status"], "WAITING_FOR_DATA")
        self.assertEqual(self.calls["pipeline"], 0)
        second = self.run_at(16, 35, lambda day, at: True)
        self.assertEqual(second["status"], "COMPLETE")
        self.assertEqual(second["message_id"], 42)
        self.assertEqual(self.run_at(16, 50, lambda day, at: True)["status"], "ALREADY_COMPLETE")
        self.assertEqual(self.calls, {"updater": 2, "pipeline": 1, "telegram": 1})

    def test_pipeline_failure_stops_later_slots(self):
        def fail_pipeline(day, market):
            raise RuntimeError("offline test")
        first = run_once(datetime(2026, 9, 29, 16, 20, tzinfo=ET),
            state_path=self.db_path, updater=self.updater,
            gate=lambda day, at: True, pipeline=fail_pipeline, sender=self.sender)
        self.assertEqual(first["status"], "FAILED")
        self.assertEqual(self.run_at(16, 35, lambda day, at: True)["status"], "FAILED_STOP")
        self.assertEqual(self.calls["updater"], 1)
        self.assertEqual(self.calls["telegram"], 0)

    def test_outside_window_is_noop(self):
        self.assertEqual(self.run_at(16, 19, lambda day, at: True)["status"],
                         "OUTSIDE_APPROVED_SHADOW_SLOTS")
        self.assertFalse(self.db_path.exists())

    def test_cutoff_marks_waiting_without_market_or_pipeline(self):
        self.assertEqual(self.run_at(17, 5, lambda day, at: False)["status"],
                         "WAITING_FOR_DATA")
        self.assertEqual(self.run_at(17, 10, lambda day, at: True)["status"],
                         "MISSED_DATA_WINDOW")
        self.assertEqual(self.calls, {"updater": 1, "pipeline": 0, "telegram": 0})
        with connect(self.db_path) as db:
            self.assertEqual(record(db, DAY)["state"], "MISSED_DATA_WINDOW")

    def test_complete_remains_complete_after_cutoff(self):
        self.assertEqual(self.run_at(16, 20, lambda day, at: True)["status"], "COMPLETE")
        self.assertEqual(self.run_at(17, 10, lambda day, at: True)["status"], "ALREADY_COMPLETE")
        self.assertEqual(self.calls["telegram"], 1)


if __name__ == "__main__":
    unittest.main()

import unittest

from shadow_day1_telegram import render, validate


def sample():
    cases = [{"ticker": f"T{i}", "final_status": "FINAL_RESEARCH_AVAILABLE",
              "cache_status": "VALID_CACHE", "input_tokens": 0}
             for i in range(15)]
    cases[1]["final_status"] = "DEEP_RESEARCH_PENDING"
    cases[2]["final_status"] = "INSUFFICIENT_EVIDENCE"
    return {"status": "PIPELINE_COMPLETE", "trading_date": "2026-09-28",
            "market_data_as_of": "2026-09-28", "no_trade_placed": True,
            "sol_calls": 0, "portfolio": {"cash_usd": 0, "suggested_allocation_usd": 0},
            "research_cases": cases, "entry_results": [
                {"ticker": "T0", "entry_state": "WATCH"}],
            "phase_statuses": {"telegram_summary": "READY"},
            "market_regime": "NORMAL", "research": {"cached_count": 2,
            "luna_calls": 8, "deep_pending": 1},
            "candidate_changes": {"NEW_ENTRY": [], "DROPPED": [],
                                  "RANK_UP": {}, "RANK_DOWN": {}},
            "candidate_top30": [{"rank": 1, "ticker": "T0", "candidate_score": 80.0}]}


class Day1TelegramTests(unittest.TestCase):
    def test_valid_summary_does_not_present_deep_pending_as_final(self):
        message = render(sample())
        self.assertIn("Deep pending", message)
        self.assertIn("No trade was placed", message)
        self.assertNotIn("INSUFFICIENT_EVIDENCE | T1", message)

    def test_pending_cannot_have_final_entry(self):
        report = sample()
        report["entry_results"][0]["ticker"] = "T1"
        with self.assertRaisesRegex(ValueError, "ENTRY_WITHOUT_FINAL_RESEARCH"):
            validate(report)

    def test_live_or_funded_summary_rejected(self):
        report = sample()
        report["portfolio"]["cash_usd"] = 2000
        with self.assertRaisesRegex(ValueError, "INPUT_INVALID"):
            validate(report)


if __name__ == "__main__":
    unittest.main()

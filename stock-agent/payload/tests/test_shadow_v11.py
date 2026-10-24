import unittest
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from phase5_entry import load_config as entry_config
from phase6_portfolio import allocate
from shadow_daily_pipeline import forward_metrics
from shadow_v11 import apply_limited_entry, classify_research, entry_data_quality


def base():
    candidate = {"quant_score": 80, "fundamental_score": 76, "candidate_score": 82}
    evidence = {"coverage_pct": 60, "confidence": "MEDIUM",
                "coverage": {"SEC_CONTENT": "AVAILABLE"},
                "recent_material_events": [{"duplicate_group": "E1", "materiality": "HIGH",
                                            "event_type": "EARNINGS"}], "other_events": []}
    initial = {"research_status": "INSUFFICIENT_EVIDENCE", "handled_evidence_ids": [],
               "evidence_conflicts": []}
    escalation = {"decision": "ESCALATE_SOL", "hard_triggers": ["MATERIAL_EVENT_COVERAGE_GAP"]}
    return candidate, evidence, initial, escalation


class ShadowV11Tests(unittest.TestCase):
    def test_limited_nonfatal_evidence_and_penalty(self):
        result = classify_research(*base())
        self.assertEqual(result["eligibility"], "RESEARCH_LIMITED")
        self.assertIsNone(result["research_score"])
        self.assertEqual(result["effective_conviction"], 67.24)
        self.assertEqual(result["research_quality_tier"], "LIMITED_RESEARCH")

    def test_material_event_and_high_risk_require_deep(self):
        c, e, i, s = base()
        e["recent_material_events"][0]["materiality"] = "MATERIAL"
        self.assertEqual(classify_research(c, e, i, s)["eligibility"], "DEEP_REQUIRED")
        e["recent_material_events"][0].update(materiality="HIGH", event_type="FINANCING")
        self.assertEqual(classify_research(c, e, i, s)["eligibility"], "DEEP_REQUIRED")
        e["recent_material_events"][0].update(materiality="HIGH", event_type="EARNINGS")
        i["evidence_conflicts"] = ["Unresolved contradictory guidance"]
        self.assertEqual(classify_research(c, e, i, s)["eligibility"], "DEEP_REQUIRED")

    def test_grounding_history_stays_failed_until_valid_complete(self):
        c, e, i, s = base()
        c["grounding_failure"] = True
        self.assertEqual(classify_research(c, e, i, s)["eligibility"], "RESEARCH_FAILED")

    def test_full_research_remains_full(self):
        c, e, i, s = base()
        i.update(research_status="COMPLETE", handled_evidence_ids=["E1"],
                 research_score=75, conviction_score=78)
        s["decision"] = "NO_ESCALATION"
        result = classify_research(c, e, i, s)
        self.assertEqual(result["eligibility"], "FINAL_RESEARCH_AVAILABLE")
        self.assertEqual(result["research_score"], 75)

    def test_limited_buy_threshold_is_stricter(self):
        limited = classify_research(*base())
        cfg = entry_config()
        result = apply_limited_entry({"entry_state": "BUY_TOMORROW_CANDIDATE",
                                      "entry_score": 79, "why_wait": []}, limited, cfg)
        self.assertEqual(result["entry_state"], "WATCH")
        self.assertEqual(result["research_confidence"], "LIMITED_RESEARCH_CONFIDENCE")
        strong = apply_limited_entry({"entry_state": "BUY_TOMORROW_CANDIDATE",
                                      "entry_score": 81, "why_wait": []}, limited, cfg)
        self.assertEqual(strong["entry_state"], "BUY_TOMORROW_CANDIDATE")

    def test_entry_data_insufficient_has_no_score(self):
        cfg = entry_config()
        day = "2026-09-29"
        daily = [{"date": day, "volume": 100} for _ in range(200)]
        bars = [{"date": day, "minute": 570+i*5, "ts_ms": i*300000,
                 "open": 10, "high": 11, "low": 9, "close": 10, "volume": 100,
                 "bar_vwap": 10} for i in range(66)]
        result = entry_data_quality(daily, bars, day, cfg)
        self.assertEqual(result["status"], "ENTRY_DATA_INSUFFICIENT")
        self.assertIn("1H_BARS_INSUFFICIENT", result["reasons"])

    def test_full_intraday_quality(self):
        cfg = entry_config()
        day = "2026-09-29"
        daily = [{"date": day, "volume": 100} for _ in range(200)]
        bars = [{"date": day if d == 30 else f"2026-08-{d:02d}",
                 "minute": 570+i*5, "ts_ms": (d*78+i)*300000,
                 "open": 10, "high": 11, "low": 9, "close": 10,
                 "volume": 100, "bar_vwap": 10}
                for d in range(1, 31) for i in range(78)]
        self.assertEqual(entry_data_quality(daily, bars, day, cfg)["status"], "READY")

    def test_limited_position_cap(self):
        class Book:
            def summary(self):
                return {"portfolio_value_cents": 200000, "cash_cents": 200000,
                        "positions": [], "stale_tickers": []}
        base = {"ticker": "TEST", "state": "BUY_TOMORROW_CANDIDATE",
                "conviction": 85, "entry": 85, "risk_score": 80,
                "price": 100, "price_at": datetime.now(timezone.utc).isoformat(),
                "sector": "Technology", "industry": "Software", "regime": "NORMAL"}
        full = allocate(Book(), [base], persist=False)[0]
        limited = allocate(Book(), [{**base, "research_quality_tier": "LIMITED_RESEARCH",
                                     "position_size_cap_fraction": .5}], persist=False)[0]
        self.assertGreater(full["suggested_cents"], limited["suggested_cents"])
        self.assertIn("LIMITED_RESEARCH_POSITION_CAP", limited["reasons"])

    def test_forward_tiers_are_separate(self):
        db = sqlite3.connect(":memory:")
        db.executescript((Path(__file__).resolve().parents[1] / "shadow_daily_results.sql").read_text())
        db.executemany("""INSERT INTO forward(trading_date,ticker,horizon,research_quality_tier,status)
            VALUES('2026-09-29',?,1,?,'PENDING')""",
            [("FULL", "FULL_RESEARCH"), ("LIMITED", "LIMITED_RESEARCH")])
        self.assertEqual(db.execute("SELECT COUNT(DISTINCT research_quality_tier) FROM forward").fetchone()[0], 2)
        db.close()

    def test_forward_metrics_no_lookahead(self):
        result = forward_metrics(100, 200, 300,
            [{"close": 105, "high": 108, "low": 98}], 202, 306)
        self.assertEqual(result, (5.0, 4.0, 3.0, 8.0, -2.0))


if __name__ == "__main__":
    unittest.main()

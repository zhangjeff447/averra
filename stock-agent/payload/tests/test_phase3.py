"""Phase 3 SEC, point-in-time, scoring, cache and tracker checks (no network)."""
import json
import sqlite3
import unittest
import urllib.error
from datetime import datetime, timezone

from phase3_features import _ratio, build_features
from phase3_run import forward_link, init_db as init_fundamental_db, run as phase3_run
from phase3_scoring import normalize, peer_medians, quality_flags, score, trend
from phase3_sec import SECClient, SECError, availability_date, extract_facts, validate_identity
from quant_screen import ROOT, init_db as init_market_db


CFG = json.loads((ROOT / "phase3_config.json").read_text())
ENDS = ("2024-09-30", "2024-12-31", "2025-03-31", "2025-06-30",
        "2025-09-30", "2025-12-31", "2026-03-31", "2026-06-30")
STARTS = ("2024-07-01", "2024-10-01", "2025-01-01", "2025-04-01",
          "2025-07-01", "2025-10-01", "2026-01-01", "2026-04-01")


def fact(value, end, start=None, filed="2026-08-01", accession="a1", form="10-Q", concept="us-gaap:Revenues"):
    return {"value": value, "unit": "USD", "start": start, "end": end,
            "period": "QUARTERLY" if start else "INSTANT", "fy": 2026, "fp": "Q2",
            "filed": filed, "available_date": filed, "accepted_at": None,
            "accession": accession, "form": form, "concept": concept, "source": "SEC_COMPANYFACTS",
            "data_quality": "AS_FILED"}


def series(values):
    return [fact(value, end, start) for value, end, start in zip(values, ENDS, STARTS)]


def selected(revenues=None, ocf=None, capex=None, net=None, shares=None):
    out = {name: [] for name in CFG["aliases"]}
    out["revenue"] = series(revenues or [100, 105, 110, 115, 120, 130, 140, 150])
    out["operating_cash_flow"] = series(ocf or [20]*8)
    out["capital_expenditure"] = series(capex or [5]*8)
    out["net_income"] = series(net or [10]*8)
    out["operating_income"] = series([15]*8)
    out["gross_profit"] = series([50]*8)
    out["eps_diluted"] = series([1]*8)
    out["diluted_shares"] = series(shares or [100]*8)
    for name, value in (("cash", 100), ("debt_current", 10), ("debt_long_term", 20),
                        ("assets", 500), ("equity", 300), ("current_assets", 200),
                        ("current_liabilities", 100), ("inventory", 30), ("receivables", 40),
                        ("shares_outstanding", 100)):
        row = fact(value, "2026-06-30")
        row["unit"] = "shares" if name == "shares_outstanding" else "USD"
        out[name] = [row]
    return out


def item(raw=None, sector="Technology", confidence="MEDIUM", quant=80):
    raw = raw or build_features(selected(), "2026-09-24", 50, [], CFG)
    return {"ticker": "ABC", "sector": sector, "sector_confidence": confidence,
            "raw": raw, "quant_score": quant, "data_quality_status": "OK"}


class SECFilterTests(unittest.TestCase):
    def payload(self, entries):
        return {"cik": 1, "entityName": "ABC Corporation", "facts": {"us-gaap": {"Revenues": {"units": {"USD": entries}}}}}

    def entry(self, value, filed, accn="a1", form="10-Q", tag="Revenues"):
        return {"start": "2026-04-01", "end": "2026-06-30", "val": value,
                "filed": filed, "accn": accn, "form": form, "fy": 2026, "fp": "Q2"}

    def submissions(self, accn="a1", accepted="2026-08-01T12:00:00Z"):
        return {"cik": 1, "tickers": ["ABC"], "filings": {"recent": {
            "accessionNumber": [accn], "acceptanceDateTime": [accepted],
            "filingDate": ["2026-08-01"], "form": ["10-Q"]}}}

    def test_point_in_time_filter(self):
        facts = self.payload([self.entry(100, "2026-08-01")])
        self.assertEqual(extract_facts(facts, self.submissions(), "2026-09-24", CFG)["revenue"][-1]["value"], 100)

    def test_future_filing_excluded(self):
        facts = self.payload([self.entry(100, "2026-09-26")])
        self.assertFalse(extract_facts(facts, self.submissions("a1", "2026-09-26T12:00:00Z"), "2026-09-24", CFG)["revenue"])

    def test_amended_filing_restates_only_future_scan(self):
        facts = self.payload([self.entry(100, "2026-08-01"), self.entry(120, "2026-09-26", "a2", "10-Q/A")])
        submissions = self.submissions()
        submissions["filings"]["recent"]["accessionNumber"].append("a2")
        submissions["filings"]["recent"]["acceptanceDateTime"].append("2026-09-26T12:00:00Z")
        self.assertEqual(extract_facts(facts, submissions, "2026-09-24", CFG)["revenue"][-1]["value"], 100)
        self.assertEqual(extract_facts(facts, submissions, "2026-09-28", CFG)["revenue"][-1]["value"], 120)

    def test_duplicate_same_value(self):
        facts = self.payload([self.entry(100, "2026-08-01"), self.entry(100, "2026-08-01")])
        self.assertEqual(len(extract_facts(facts, self.submissions(), "2026-09-24", CFG)["revenue"]), 1)

    def test_conflicting_duplicate_rejected(self):
        facts = self.payload([self.entry(100, "2026-08-01"), self.entry(120, "2026-08-01")])
        with self.assertRaises(SECError):
            extract_facts(facts, self.submissions(), "2026-09-24", CFG)

    def test_different_xbrl_tag(self):
        facts = {"cik": 1, "facts": {"us-gaap": {"SalesRevenueNet": {"units": {"USD": [self.entry(99, "2026-08-01")]}}}}}
        self.assertEqual(extract_facts(facts, self.submissions(), "2026-09-24", CFG)["revenue"][-1]["value"], 99)

    def test_wrong_currency_unavailable(self):
        facts = {"cik": 1, "facts": {"us-gaap": {"Revenues": {"units": {"CAD": [self.entry(100, "2026-08-01")]}}}}}
        self.assertFalse(extract_facts(facts, self.submissions(), "2026-09-24", CFG)["revenue"])

    def test_after_close_accepted_next_day(self):
        self.assertEqual(availability_date("2026-09-24", "2026-09-24T21:30:00Z", "16:05"), "2026-09-25")

    def test_unknown_acceptance_conservative(self):
        self.assertEqual(availability_date("2026-09-25", None, "16:05"), "2026-09-28")

    def test_ticker_cik_mismatch(self):
        with self.assertRaises(SECError):
            validate_identity("1", "BAD", "ABC Corporation", self.submissions(),
                              {"cik": 1, "entityName": "ABC Corporation"}, CFG["quality"])

    def test_company_name_mismatch(self):
        with self.assertRaises(SECError):
            validate_identity("1", "ABC", "Unrelated Aerospace", self.submissions(),
                              {"cik": 1, "entityName": "ABC Corporation"}, CFG["quality"])


class FeatureTests(unittest.TestCase):
    def test_normal_profitable_company(self):
        v = item()["raw"]["values"]
        self.assertGreater(v["net_income_ttm"], 0)
        self.assertGreater(v["free_cash_flow_ttm"], 0)

    def test_high_growth(self):
        v = build_features(selected(revenues=[100,100,100,100,140,160,180,200]), "2026-09-24", 50, [], CFG)["values"]
        self.assertGreater(v["revenue_yoy"], .5)

    def test_revenue_acceleration(self):
        v = build_features(selected(revenues=[100,100,100,100,110,120,140,170]), "2026-09-24", 50, [], CFG)["values"]
        self.assertGreater(v["revenue_acceleration"], 0)

    def test_revenue_deceleration(self):
        v = build_features(selected(revenues=[100,100,100,100,170,140,120,110]), "2026-09-24", 50, [], CFG)["values"]
        self.assertLess(v["revenue_acceleration"], 0)

    def test_negative_earnings_no_pe(self):
        v = build_features(selected(net=[-10]*8), "2026-09-24", 50, [], CFG)["values"]
        self.assertIsNone(v["pe"])

    def test_negative_fcf_flag(self):
        v = build_features(selected(ocf=[2]*8, capex=[5]*8), "2026-09-24", 50, [], CFG)["values"]
        self.assertIn("NEGATIVE_FCF", quality_flags(v, CFG))
        self.assertIsNone(v["fcf_yield"])

    def test_net_cash(self):
        self.assertLess(item()["raw"]["values"]["net_debt"], 0)

    def test_high_debt(self):
        v = item()["raw"]["values"]
        v["debt_to_equity"] = 3
        self.assertIn("HIGH_DEBT", quality_flags(v, CFG))

    def test_share_dilution(self):
        v = build_features(selected(shares=[100]*4+[120]*4), "2026-09-24", 50, [], CFG)["values"]
        self.assertGreater(v["dilution_yoy"], .15)
        self.assertIn("DILUTION", quality_flags(v, CFG))

    def test_missing_quarter_no_fake_ttm(self):
        s = selected()
        s["revenue"].pop(-2)
        v = build_features(s, "2026-09-24", 50, [], CFG)["values"]
        self.assertIsNone(v["revenue_ttm"])

    def test_negative_denominator(self):
        self.assertIsNone(_ratio(100, -10))

    def test_missing_forward_pe_explicit(self):
        self.assertIsNone(item()["raw"]["values"]["forward_pe"])

    def test_split_adjusted_shares(self):
        s = selected()
        s["shares_outstanding"][0]["end"] = "2026-06-30"
        actions = [{"date": "2026-08-15", "type": "forward_split", "split_from": 1, "split_to": 2}]
        v = build_features(s, "2026-09-24", 50, actions, CFG)["values"]
        self.assertEqual(v["split_adjusted_shares_for_valuation"], 200)

    def test_missing_debt_not_assumed_zero(self):
        s = selected()
        s["debt_current"] = []
        v = build_features(s, "2026-09-24", 50, [], CFG)["values"]
        self.assertIsNone(v["total_debt"])


class ScoringTests(unittest.TestCase):
    def test_margin_expansion_trend(self):
        r = item()["raw"]
        r["quarterly"]["gross_profit"] = series([30,30,30,30,40,45,50,55])
        self.assertGreater(trend(r, CFG)[2]["gross_profit_margin"], 0)

    def test_margin_compression_flag(self):
        v = item()["raw"]["values"]
        v["margin_trend"] = -.1
        self.assertIn("MARGIN_COMPRESSION", quality_flags(v, CFG))

    def test_sector_normalization(self):
        items = [item() for _ in range(5)]
        peers = peer_medians(items, CFG)
        self.assertIn("gross_margin", peers["Technology"])
        scored = score(items[0], peers, CFG)
        self.assertEqual(scored["explanation"]["component_contributions"]["profitability"][0]["peer_scope"], "Technology")

    def test_low_sector_confidence_market_fallback(self):
        x = item(confidence="LOW")
        peers = peer_medians([x]*5, CFG)
        self.assertNotIn("Technology", peers)

    def test_financial_special_handling(self):
        x = item(sector="Financials")
        out = score(x, peer_medians([x]*5, CFG), CFG)
        self.assertTrue(out["explanation"]["financial_sector_special_handling"])
        self.assertNotIn("debt_to_equity", [v["feature"] for v in out["explanation"]["component_contributions"]["financial_health"]])

    def test_low_coverage(self):
        x = item(raw={"values": {}, "provenance": {}, "quarterly": {}, "annual": {}, "instant": {}})
        out = score(x, {}, CFG)
        self.assertEqual(out["confidence"], "INSUFFICIENT")
        self.assertIsNone(out["fundamental_score"])

    def test_confidence_adjustment(self):
        x = item()
        out = score(x, {}, CFG)
        self.assertLessEqual(out["effective_fundamental_weight"], CFG["candidate_weights"]["fundamental"])

    def test_candidate_score_formula(self):
        x = item()
        out = score(x, {}, CFG)
        expected = 80*out["effective_quant_weight"] + (out["fundamental_score"] or 0)*out["effective_fundamental_weight"]
        self.assertAlmostEqual(out["candidate_score"], expected, places=4)

    def test_hard_reject_not_equated_with_penalty(self):
        x = item()
        x["data_quality_status"] = "DATA_QUALITY_REJECT"
        out = score(x, {}, CFG)
        self.assertEqual(out["confidence"], "INSUFFICIENT")

    def test_earnings_cashflow_divergence(self):
        v = item()["raw"]["values"]
        v["operating_cash_flow_ttm"] = -1
        self.assertIn("EARNINGS_CASHFLOW_DIVERGENCE", quality_flags(v, CFG))

    def test_valuation_growth_adjustment(self):
        v = item()["raw"]["values"]
        self.assertIsNotNone(v["growth_adjusted_earnings_yield"])


class CacheAndTrackerTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        init_fundamental_db(self.db)

    def tearDown(self):
        self.db.close()

    def test_cache_hit_no_second_request(self):
        class Response:
            headers = {}
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self): return b'{"cik":1}'
        client = SECClient("Averra tests@example.com", CFG["source"], opener=lambda *a, **k: Response(),
                           sleeper=lambda _: None, now=lambda: datetime(2026, 9, 25, tzinfo=timezone.utc))
        client.cached(self.db, "0000000001", "submissions", "/submissions/CIK0000000001.json")
        client.cached(self.db, "0000000001", "submissions", "/submissions/CIK0000000001.json")
        self.assertEqual((client.api_requests, client.cache_hits), (1, 1))

    def test_incremental_companyfacts_no_new_filing(self):
        class Response:
            headers = {}
            def __init__(self, payload): self.payload = payload
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self): return json.dumps(self.payload).encode()
        paths = []
        def opener(request, timeout=30):
            paths.append(request.full_url)
            if "submissions" in request.full_url:
                return Response({"filings": {"recent": {"filingDate": ["2026-08-01"],
                    "form": ["10-Q"], "accessionNumber": ["a1"]}}})
            return Response({"cik": 1, "facts": {}})
        client = SECClient("Averra tests@example.com", CFG["source"], opener=opener,
                           sleeper=lambda _: None, now=lambda: datetime(2026, 9, 25, tzinfo=timezone.utc))
        client.company(self.db, "1"); client.company(self.db, "1")
        self.assertEqual(len(paths), 2)

    def test_missing_companyfacts_negative_cache(self):
        calls = []
        def opener(request, timeout=30):
            calls.append(request.full_url)
            raise urllib.error.HTTPError(request.full_url, 404, "missing", {}, None)
        client = SECClient("Averra tests@example.com", CFG["source"], opener=opener,
                           sleeper=lambda _: None, now=lambda: datetime(2026, 9, 25, tzinfo=timezone.utc))
        for _ in range(2):
            with self.assertRaises(SECError):
                client.cached(self.db, "0000000001", "companyfacts", "/api/xbrl/companyfacts/CIK0000000001.json")
        self.assertEqual(len(calls), 1)

    def test_forward_tracker_link(self):
        market = sqlite3.connect(":memory:")
        market.row_factory = sqlite3.Row
        init_market_db(market)
        market.execute("INSERT INTO forward_observations VALUES(1,'ABC',1,'2026-09-25',.1,.02,.03,.08,.07,NULL,NULL)")
        summary = forward_link(market, 1, [{"ticker": "ABC", "quant_score": 80,
            "fundamental_score": 70, "candidate_score": 76}], [1,5])
        self.assertEqual(summary["linked_observations"], 1)
        self.assertEqual(summary["pending_by_horizon"]["5"], 1)
        market.close()

    def test_historical_score_immutability_schema(self):
        self.db.execute("INSERT INTO fundamental_runs VALUES(1,1,'2026-09-24','2026-09-25','{}','TEST',0,0,1,0)")
        self.assertEqual(self.db.execute("SELECT scan_date FROM fundamental_runs WHERE id=1").fetchone()[0], "2026-09-24")

    def test_no_llm_import_in_phase3_modules(self):
        for filename in ("phase3_sec.py", "phase3_features.py", "phase3_scoring.py", "phase3_run.py"):
            code = (ROOT / filename).read_text().lower()
            self.assertNotIn("import openai", code)
            self.assertNotIn("responses.create", code)


class Phase3IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.market = sqlite3.connect(":memory:")
        self.market.row_factory = sqlite3.Row
        init_market_db(self.market)
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        init_fundamental_db(self.db)
        self.market.execute("INSERT INTO daily_bars(ticker,date,open,high,low,close,volume) VALUES('SPY','2026-09-24',1,1,1,1,1)")
        self.market.execute("INSERT INTO quant_runs VALUES(1,'2026-09-25','2026-09-24','{}',100,100,0,0,'live')")
        self.symbols = {}
        for n in range(100):
            symbol = f"T{n:03}"
            cik = str(n + 1).zfill(10)
            self.symbols[cik] = symbol
            self.market.execute("""INSERT INTO securities(ticker,asset_type,country,exchange,is_etf,is_otc,
                is_preferred,is_warrant,company_name,cik,normalized_sector,mapping_confidence)
                VALUES(?,'COMMON_STOCK','US','NYSE',0,0,0,0,?,?, 'Technology','MEDIUM')""",
                (symbol, f"{symbol} Corporation", cik))
            self.market.execute("INSERT INTO quant_scores VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (1, symbol, n+1, 90-n/10, 80, 80, 80, 80, 80, '{"close":50}', '{}'))

    def tearDown(self):
        self.market.close(); self.db.close()

    def test_top100_only_and_history_persisted(self):
        symbols = self.symbols
        class Fake:
            api_requests = 0
            cache_hits = 0
            def company(self, db, cik):
                symbol = symbols[str(int(cik)).zfill(10)]
                return {"cik": int(cik), "tickers": [symbol]}, {"cik": int(cik), "entityName": f"{symbol} Corporation", "facts": {}}
        result = phase3_run(self.market, self.db, CFG, Fake())
        self.assertEqual(result["quant_top100_count"], 100)
        self.assertEqual(len(result["candidate_top30"]), 30)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM fundamental_scores").fetchone()[0], 100)
        self.assertEqual(result["forward_tracker"]["linked_observations"], 0)

    def test_single_ticker_failure_isolated(self):
        symbols = self.symbols
        class Fake:
            api_requests = 0
            cache_hits = 0
            def company(self, db, cik):
                if int(cik) == 2:
                    raise SECError("MISSING_DATA", "companyfacts absent")
                symbol = symbols[str(int(cik)).zfill(10)]
                return {"cik": int(cik), "tickers": [symbol]}, {"cik": int(cik), "entityName": f"{symbol} Corporation", "facts": {}}
        result = phase3_run(self.market, self.db, CFG, Fake())
        self.assertEqual(result["validated_company_count"], 99)
        self.assertIn("T001", result["errors"])


if __name__ == "__main__":
    unittest.main()

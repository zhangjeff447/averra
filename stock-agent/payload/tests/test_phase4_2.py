import io
import json
import sqlite3
import unittest
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

from phase4_2_evidence import (DocumentClient, accepted_before, collect_ticker, coverage, document_text,
                               event, exhibit_links, extract_8k_items, extract_periodic, extract_release,
                               ir_feed_entries, ir_snapshot_eligible, merge_events, packet_for_evidence,
                               sec_url, select_filings,
                               verified_ir_config)

ROOT = Path(__file__).resolve().parents[1]
CFG = json.loads((ROOT / "phase4_2_config.json").read_text())
ASOF = "2026-09-27T02:00:00+00:00"
ACC = "0000000123-26-000001"
BASE = f"https://www.sec.gov/Archives/edgar/data/123/{ACC.replace('-', '')}/"
FILING = {"form": "8-K", "filing_date": "2026-09-20", "accepted_at": "2026-09-20T20:00:00Z",
          "accession": ACC, "primary_document": "filing.htm", "items": "2.02,9.01"}


class FakeResponse(io.BytesIO):
    def __init__(self, data, url):
        super().__init__(data)
        self.url = url
        self.headers = {"Content-Type": "text/html"}

    def geturl(self):
        return self.url


def submission(forms=("8-K",), days=("2026-09-20",), accepted=("2026-09-20T20:00:00Z",)):
    return {"cik": 123, "tickers": ["TEST"], "filings": {"recent": {
        "form": list(forms), "filingDate": list(days), "acceptanceDateTime": list(accepted),
        "accessionNumber": [ACC for _ in forms], "primaryDocument": ["filing.htm" for _ in forms],
        "items": ["2.02,9.01" for _ in forms]}}}


class Phase42ParsingTests(unittest.TestCase):
    def test_8k_item_extraction(self):
        text = document_text(b"<html><p>Item 2.02 Results of Operations</p><p>The company issued its results release.</p>"
                             b"<p>Item 4.02 Non-reliance</p><p>Prior financial statements should no longer be relied upon.</p></html>")
        events = extract_8k_items(text, FILING, BASE + "filing.htm", CFG)
        self.assertEqual([e["item"] for e in events], ["2.02", "4.02"])
        self.assertEqual(events[1]["event_type"], "RESTATEMENT")
        self.assertEqual(events[1]["materiality"], "MATERIAL")

    def test_8k_acquisition_auditor_executive_financing_offering(self):
        text = "\n".join(f"Item {code} {title}\nThe company announced a factual corporate transaction with details."
                         for code, title in (("2.01", "Acquisition"), ("4.01", "Auditor"),
                                             ("5.02", "Executive departure"), ("3.02", "Share offering")))
        kinds = [e["event_type"] for e in extract_8k_items(text, FILING, BASE + "filing.htm", CFG)]
        self.assertEqual(kinds, ["ACQUISITION", "AUDITOR_CHANGE", "EXECUTIVE_CHANGE", "SEC_MATERIAL_EVENT"])

    def test_10q_10k_extraction_keeps_factual_excerpts(self):
        text = ("Item 2. Management's Discussion and Analysis\nRevenue and expenses are discussed here in detail.\n"
                "Item 1A. Risk Factors\nThe company identifies several material risks to operations.\n"
                "Liquidity and capital resources remain important to operations and future plans.")
        for form in ("10-Q", "10-K"):
            with self.subTest(form=form):
                filing = {**FILING, "form": form}
                events = extract_periodic(text, filing, BASE + "report.htm", CFG)
                self.assertIn("MANAGEMENT_DISCUSSION", {e["event_type"] for e in events})
                self.assertIn("RISK_FACTORS", {e["event_type"] for e in events})
                self.assertTrue(all(e["extraction_confidence"] == "LOW" for e in events))

    def test_exhibits_991_992(self):
        index = ('<table><tr><td><a href="ex991.htm">ex991.htm</a></td><td>EX-99.1</td></tr>'
                 '<tr><td><a href="ex992.htm">ex992.htm</a></td><td>EX-99.2</td></tr></table>')
        links = exhibit_links(index, BASE + ACC + "-index.html")
        self.assertEqual([kind for kind, _ in links], ["EX-99.1", "EX-99.2"])
        self.assertTrue(all(url.startswith(BASE) for _, url in links))

    def test_actual_vs_guidance_not_analyst_beat(self):
        release = extract_release("Quarterly financial results. Revenue of $7.5 million. "
                                  "The company expects revenue of $9 million next quarter.",
                                  FILING, BASE + "ex991.htm", CFG)
        self.assertEqual(release["event_type"], "EARNINGS")
        self.assertTrue(any(f.startswith("ACTUAL_RESULT:") for f in release["verified_facts"]))
        self.assertTrue(any(f.startswith("MANAGEMENT_GUIDANCE:") for f in release["verified_facts"]))
        self.assertFalse(any("BEAT" in f for f in release["verified_facts"]))

    def test_financial_statement_exhibit_is_not_executive_change(self):
        release = extract_release("EX-99.1 Naveris, Inc. Financial Statements for 2025 "
                                  "Report of Independent Auditors; Balance Sheets",
                                  FILING, BASE + "naverisfinancialstat.htm", CFG)
        self.assertEqual(release["event_type"], "FINANCIAL_STATEMENT_EXHIBIT")
        self.assertEqual(release["materiality"], "LOW")

    def test_official_strategic_partnership_retains_source_evidence(self):
        title = "AMD and Anthropic Announce Strategic Partnership"
        text = f"Navigation {title} July 2026 News Highlights {title} Deployment begins next year."
        release = extract_release(text, {**FILING, "form": "IR"},
                                  "https://ir.amd.com/article", CFG,
                                  source_type="COMPANY_IR", title=title)
        self.assertEqual(release["event_type"], "STRATEGIC_PARTNERSHIP")
        self.assertEqual(release["materiality"], "HIGH")
        self.assertTrue(any(f.startswith("OFFICIAL_SOURCE_EXCERPT:") for f in release["verified_facts"]))

    def test_point_in_time_and_baseline(self):
        data = submission(("8-K", "8-K", "10-K"),
                          ("2026-09-28", "2026-09-20", "2026-03-01"),
                          ("2026-09-28T10:00:00Z", "2026-09-20T20:00:00Z", "2026-03-01T20:00:00Z"))
        selected = select_filings(data, ASOF, CFG)
        self.assertEqual([r["form"] for r in selected], ["8-K", "10-K"])
        self.assertFalse(accepted_before("2026-09-27T03:00:00Z", ASOF))
        self.assertFalse(accepted_before(None, ASOF))
        self.assertFalse(ir_snapshot_eligible("2026-09-28T00:00:00Z", ASOF, True))
        self.assertTrue(ir_snapshot_eligible("2026-09-26T00:00:00Z", ASOF, True))

    def test_sec_path_rejects_traversal(self):
        with self.assertRaises(ValueError):
            sec_url(123, ACC, "../secret")

    def test_ir_domain_identity_validation(self):
        cfg = {**CFG, "verified_ir": {"TEST": {"company_name": "Other Corporation",
                                               "hosts": ["investor.other.com"], "feeds": []}}}
        self.assertIsNone(verified_ir_config("TEST", "Test Company", cfg))
        cfg["verified_ir"]["TEST"]["company_name"] = "Test Company"
        self.assertIsNotNone(verified_ir_config("TEST", "Test Company", cfg))

    def test_sec_exhibit_ir_dedup(self):
        a = event(FILING, "EARNINGS", "HIGH", "Results", "Revenue of $7m", BASE + "filing.htm")
        b = event(FILING, "EARNINGS", "HIGH", "Release", "EPS was $0.40", BASE + "ex991.htm")
        c = event({**FILING, "form": "IR"}, "EARNINGS", "HIGH", "IR Release", "Guidance increased",
                  "https://investor.example.com/results", source_type="COMPANY_IR")
        events = merge_events([a, b, c], "TEST")
        self.assertEqual(len(events), 1)
        self.assertEqual(len(events[0]["sources"]), 3)
        self.assertEqual(len(events[0]["verified_facts"]), 3)

    def test_distinct_same_day_acquisitions_do_not_merge(self):
        first = event(FILING, "ACQUISITION", "MATERIAL", "First acquisition", "Deal one", BASE + "first.htm")
        second = event({**FILING, "accession": "0000000123-26-000002"}, "ACQUISITION",
                       "MATERIAL", "Second acquisition", "Deal two", BASE + "second.htm")
        self.assertEqual(len(merge_events([first, second], "TEST")), 2)

    def test_partial_insufficient_and_compression(self):
        values, pct, confidence = coverage([], False, False, sec_partial=True)
        self.assertEqual(values["SEC_CONTENT"], "PARTIAL")
        self.assertEqual(confidence, "INSUFFICIENT")
        entries = [event({**FILING, "accession": str(i)}, "GENERIC_RELEASE", "LOW", "News",
                         "x" * 500, BASE + f"{i}.htm") for i in range(20)]
        important = event(FILING, "RESTATEMENT", "MATERIAL", "Restatement", "Fact", BASE + "risk.htm")
        packet = packet_for_evidence("TEST", ASOF, merge_events([important, *entries], "TEST"), values, pct, confidence, [],
                                     {**CFG, "packet_target_tokens": 500})
        self.assertEqual(packet["recent_material_events"][0]["event_type"], "RESTATEMENT")
        self.assertLess(len(packet["other_events"]), 20)

    def test_ir_feed_publication_time(self):
        raw = (b"<rss><channel><item><title>Quarterly Results</title><link>https://ir.example.com/x</link>"
               b"<pubDate>Sat, 26 Sep 2026 20:00:00 GMT</pubDate></item></channel></rss>")
        self.assertEqual(ir_feed_entries(raw)[0]["published_at"], "2026-09-26T20:00:00+00:00")


class Phase42CacheTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.db.executescript((ROOT / "phase4_schema.sql").read_text())

    def tearDown(self):
        self.db.close()

    def test_cache_incremental_and_network_failure_preserves_content(self):
        calls = []
        def opener(request, timeout):
            calls.append(request.full_url)
            return FakeResponse(b"<p>verified original document</p>", request.full_url)
        client = DocumentClient(self.db, "Test Contact test@example.com", CFG, opener=opener, sleeper=lambda _: None)
        url = BASE + "filing.htm"
        first = client.get("TEST", url, "SEC_CONTENT")
        second = client.get("TEST", url, "SEC_CONTENT")
        self.assertEqual(first, second)
        self.assertEqual((client.requests, client.cache_hits), (1, 1))
        def failing(request, timeout):
            raise urllib.error.HTTPError(request.full_url, 403, "blocked", {}, None)
        client2 = DocumentClient(self.db, "Test Contact test@example.com", CFG, opener=failing, sleeper=lambda _: None)
        self.assertIsNone(client2.get("TEST", url, "SEC_CONTENT", max_age_hours=-1))
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM evidence_source_documents").fetchone()[0], 1)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM evidence_source_errors").fetchone()[0], 1)

    def test_single_source_failure_does_not_abort_ticker(self):
        market = sqlite3.connect(":memory:")
        fundamental = sqlite3.connect(":memory:")
        market.row_factory = fundamental.row_factory = sqlite3.Row
        market.execute("CREATE TABLE securities(ticker TEXT,cik TEXT,company_name TEXT)")
        market.execute("INSERT INTO securities VALUES('TEST','123','Test Company')")
        fundamental.execute("CREATE TABLE sec_cache(cik TEXT,endpoint TEXT,cache_status TEXT,payload BLOB)")
        import zlib
        fundamental.execute("INSERT INTO sec_cache VALUES(?,?,?,?)",
                            ("0000000123", "submissions", "FRESH", zlib.compress(json.dumps(submission()).encode())))
        def failing(request, timeout):
            raise urllib.error.HTTPError(request.full_url, 403, "blocked", {}, None)
        result = collect_ticker(market, fundamental, self.db, "TEST", ASOF, CFG,
                                "Test Contact test@example.com", opener=failing, sleeper=lambda _: None)
        self.assertEqual(result["confidence"], "INSUFFICIENT")
        self.assertGreaterEqual(result["packet"]["filings_found"].__len__(), 1)
        self.assertGreaterEqual(len(result["network_failures"]), 1)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM evidence_packets_v2").fetchone()[0], 1)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM evidence_events_v2").fetchone()[0], 0)
        market.close()
        fundamental.close()


if __name__ == "__main__":
    unittest.main()

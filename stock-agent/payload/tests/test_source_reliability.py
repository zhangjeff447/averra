import io
import json
import socket
import sqlite3
import ssl
import tempfile
import time
import unittest
import urllib.error
from pathlib import Path

from phase3_sec import SECClient, SECError
from phase4_2_evidence import (DocumentClient, collect_ticker, ir_feed_entries,
                              select_ir_entries, verified_ir_config)
from request_diagnostics import RequestAudit, error_class, summarize

ROOT = Path(__file__).resolve().parents[1]
CFG = json.loads((ROOT / "phase4_2_config.json").read_text())


class Response(io.BytesIO):
    headers = {}
    status = 200

    def __init__(self, body, url):
        super().__init__(body)
        self.url = url

    def geturl(self):
        return self.url


class DiagnosticsTests(unittest.TestCase):
    def test_classification(self):
        cases = [(urllib.error.URLError(socket.gaierror()), "DNS_ERROR"),
                 (TimeoutError(), "TIMEOUT"), (ssl.SSLError(), "TLS_ERROR"),
                 (ConnectionResetError(), "CONNECTION_RESET"),
                 (ValueError("secret"), "INVALID_DATA")]
        for exc, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(error_class(exc), expected)

    def test_records_and_summary_never_include_credentials_or_payloads(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "requests.jsonl"
            audit = RequestAudit(path)
            audit.record("SEC_SUBMISSIONS", "https://user:PRIVATE@data.sec.gov/SECRET?key=TOKEN",
                         0, time.monotonic() - .1, error=TimeoutError("PASSWORD"),
                         retry_delay_seconds=5)
            audit.record("SEC_SUBMISSIONS", "https://data.sec.gov/public", 1,
                         time.monotonic(), status=200, response_bytes=42)
            text = path.read_text()
            for secret in ("PRIVATE", "SECRET", "TOKEN", "PASSWORD", "user:"):
                self.assertNotIn(secret, text)
            result = summarize(path)["sources"]["SEC_SUBMISSIONS"]
            self.assertEqual((result["requests"], result["retries"]), (2, 1))
            self.assertEqual(result["errors"], {"TIMEOUT": 1})
            self.assertGreaterEqual(result["max_request_ms"], 100)

    def test_sec_retry_records_each_attempt_without_extra_calls(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "requests.jsonl"
            calls, sleeps = [], []
            def opener(request, timeout):
                calls.append(request.full_url)
                if len(calls) == 1:
                    raise urllib.error.URLError(socket.gaierror())
                return Response(b'{"cik":123}', request.full_url)
            client = SECClient("Test test@example.com", {"retry_seconds": [5, 15, 30],
                "minimum_request_spacing_seconds": 0}, opener=opener, sleeper=sleeps.append,
                audit=RequestAudit(path))
            self.assertEqual(client.request("/submissions/CIK0000000123.json"), {"cik": 123})
            rows = [json.loads(line) for line in path.read_text().splitlines()]
            self.assertEqual(len(calls), 2)
            self.assertEqual(sleeps, [5])
            self.assertEqual(rows[0]["error_class"], "DNS_ERROR")
            self.assertEqual(rows[0]["retry_delay_seconds"], 5)
            self.assertEqual(rows[1]["outcome"], "SUCCESS")

    def test_sec_403_has_no_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "requests.jsonl"
            def opener(request, timeout):
                raise urllib.error.HTTPError(request.full_url, 403, "blocked", {}, None)
            client = SECClient("Test test@example.com", {"retry_seconds": [5, 15, 30],
                "minimum_request_spacing_seconds": 0}, opener=opener,
                sleeper=lambda _: self.fail("403 must not retry"), audit=RequestAudit(path))
            with self.assertRaises(SECError):
                client.request("/submissions/CIK0000000123.json")
            row = json.loads(path.read_text())
            self.assertEqual((row["error_class"], row["retry_delay_seconds"]), ("HTTP_403", 0))

    def test_failed_log_write_does_not_fail_download(self):
        with tempfile.TemporaryDirectory() as directory:
            audit = RequestAudit(directory)  # A directory cannot be opened as a log file.
            with self.assertLogs("request_diagnostics", level="WARNING"):
                audit.record("SEC", "https://data.sec.gov", 0, time.monotonic(), status=200)

    def test_document_cache_avoids_second_request_and_timing_row(self):
        with tempfile.TemporaryDirectory() as directory, sqlite3.connect(":memory:") as db:
            db.executescript((ROOT / "phase4_schema.sql").read_text())
            path = Path(directory) / "requests.jsonl"
            calls = []
            def opener(request, timeout):
                calls.append(request.full_url)
                return Response(b"official document", request.full_url)
            client = DocumentClient(db, "Test test@example.com", CFG, opener=opener,
                                    sleeper=lambda _: None, audit=RequestAudit(path))
            url = "https://www.sec.gov/Archives/test.htm"
            self.assertEqual(client.get("TEST", url, "SEC_CONTENT"), b"official document")
            self.assertEqual(client.get("TEST", url, "SEC_CONTENT"), b"official document")
            self.assertEqual(len(calls), 1)
            self.assertEqual(len(path.read_text().splitlines()), 1)


class IRSourcesTests(unittest.TestCase):
    def test_registry_rejects_wrong_identity(self):
        self.assertIsNone(verified_ir_config("OKTA", "Okta Inc", CFG, "999"))
        self.assertIsNone(verified_ir_config("OKTA", "Unrelated Company", CFG, "1660134"))
        self.assertIsNotNone(verified_ir_config("OKTA", "Okta Inc", CFG, "1660134"))

    def test_naive_or_invalid_publication_is_not_available_evidence(self):
        raw = b"<rss><channel><item><title>x</title><pubDate>Fri, 16 Oct 2026 10:00:00</pubDate></item></channel></rss>"
        self.assertEqual(ir_feed_entries(raw), [])

    def test_material_priority_dedup_and_point_in_time(self):
        def entry(title, day, url):
            return {"title": title, "published_at": day + "T12:00:00+00:00", "url": url,
                    "kind": "CORPORATE_UPDATE"}
        low = entry("Product webinar", "2026-10-15", "https://ir.example.com/low")
        high = entry("Accounting restatement", "2026-10-10", "https://ir.example.com/risk")
        future = entry("Acquisition", "2026-10-18", "https://ir.example.com/future")
        old = entry("Acquisition", "2025-01-01", "https://ir.example.com/old")
        result = select_ir_entries([low, high, high, future, old], "2026-10-16T20:00:00+00:00", CFG)
        self.assertEqual(result, [high, low])

    def test_bounded_collection_preserves_deferred_metadata(self):
        with sqlite3.connect(":memory:") as market, sqlite3.connect(":memory:") as fundamental, \
                sqlite3.connect(":memory:") as research:
            research.executescript((ROOT / "phase4_schema.sql").read_text())
            market.execute("CREATE TABLE securities(ticker TEXT,cik TEXT,company_name TEXT)")
            market.execute("INSERT INTO securities VALUES('TEST','123','Test Company')")
            cfg = {**CFG, "max_ir_releases_per_ticker": 1, "minimum_request_spacing_seconds": 0,
                   "verified_ir": {"TEST": {"company_name": "Test Company", "cik": "0000000123",
                       "hosts": ["ir.example.com"], "feeds": ["https://ir.example.com/feed"]}}}
            calls = []
            def opener(request, timeout):
                url = request.full_url
                calls.append(url)
                if "submissions" in url:
                    raw = json.dumps({"cik": 123, "tickers": ["TEST"], "filings": {"recent": {}}}).encode()
                elif url.endswith("/feed"):
                    raw = b'''<rss><channel>
                    <item><title>Quarterly results</title><pubDate>Thu, 15 Oct 2026 12:00:00 GMT</pubDate><link>https://ir.example.com/earnings</link></item>
                    <item><title>Product webinar</title><pubDate>Fri, 16 Oct 2026 12:00:00 GMT</pubDate><link>https://ir.example.com/webinar</link></item>
                    <item><title>Product webinar</title><pubDate>Fri, 16 Oct 2026 12:00:00 GMT</pubDate><link>https://ir.example.com/webinar</link></item>
                    </channel></rss>'''
                else:
                    raw = b"Quarterly financial results. Revenue of $10 million."
                return Response(raw, url)
            result = collect_ticker(market, fundamental, research, "TEST",
                "2026-10-16T20:00:00+00:00", cfg, "Test test@example.com", opener=opener,
                sleeper=lambda _: None)
            packet = result["packet"]
            self.assertEqual(packet["ir_collection"]["attempted_releases"], 1)
            self.assertEqual(len(packet["ir_collection"]["deferred"]), 1)
            self.assertEqual(packet["coverage"]["IR"], "PARTIAL")
            self.assertNotIn("https://ir.example.com/webinar", calls)
            self.assertIn("https://ir.example.com/earnings", calls)


if __name__ == "__main__":
    unittest.main()

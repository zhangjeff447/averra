import io
import sqlite3
import sys
import unittest
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from quant_screen import init_db
from real_market import MassiveClient, MarketDataError, parse_bar, record_splits, store_grouped, store_tickers, sync, tickers
from sector_metadata import enrich


class FakeClient:
    def __init__(self):
        self.calls = 0

    def get(self, url, params):
        self.calls += 1
        return {"status": "OK", "results": [
            {"ticker": "AAPL", "active": True, "type": "CS", "locale": "us", "market": "stocks",
             "primary_exchange": "XNAS", "name": "Apple", "cik": "0000320193"},
            {"ticker": "FUND", "active": True, "type": "ETF", "locale": "us", "market": "stocks",
             "primary_exchange": "XNAS", "name": "Fund"}], "next_url": None}


class RealMarketTest(unittest.TestCase):
    def test_security_type_filter(self):
        with self.assertRaises(MarketDataError):
            tickers(FakeClient(), {"XNAS": "NASDAQ"})  # fail closed on incomplete master

    def test_invalid_bar_is_isolated_and_incremental_day_skips_duplicates(self):
        db = sqlite3.connect(":memory:")
        init_db(db)
        bars = [{"T": "SPY", "o": 100, "h": 101, "l": 99, "c": 100, "v": 100},
                {"T": "QQQ", "o": 100, "h": 101, "l": 99, "c": 100, "v": 100},
                {"T": "AAPL", "o": 100, "h": 101, "l": 99, "c": 100, "v": 100},
                {"T": "BAD", "o": -1, "h": 1, "l": -2, "c": 1, "v": 100},
                {"T": "AAPL", "o": 100, "h": 101, "l": 99, "c": 100, "v": 100}]
        count, rejects = store_grouped(db, "2026-09-24", bars, {"SPY", "QQQ", "AAPL", "BAD"})
        self.assertEqual(count, 3)
        self.assertEqual(sum(rejects.values()), 2)
        self.assertEqual(db.execute("SELECT COUNT(*) FROM daily_bars").fetchone()[0], 3)
        self.assertEqual(db.execute("SELECT COUNT(*) FROM market_sync_days").fetchone()[0], 1)
        db.close()

    def test_nan_rejected(self):
        with self.assertRaises(MarketDataError):
            parse_bar({"T": "AAPL", "o": float("nan"), "h": 101, "l": 99, "c": 100, "v": 100},
                      "2026-09-24", {"AAPL"})

    def test_invalid_symbol_is_not_retried(self):
        def missing(request, timeout):
            raise urllib.error.HTTPError(request.full_url, 404, "missing", None, None)
        client = MassiveClient("test", spacing=0, opener=missing, sleeper=lambda _: None)
        with self.assertRaises(MarketDataError) as caught:
            client.get("/v2/aggs/ticker/INVALID/range/1/day/2026-09-01/2026-09-24")
        self.assertEqual(caught.exception.code, "INVALID_SYMBOL")
        self.assertEqual(client.request_count, 1)

    def test_transient_network_retries_5_15_30_seconds(self):
        calls, sleeps = [], []
        def intermittent(request, timeout):
            calls.append(request.full_url)
            if len(calls) < 4:
                raise urllib.error.URLError("temporary")
            return io.BytesIO(b'{"status":"OK","results":[]}')
        client = MassiveClient("test", spacing=0, opener=intermittent, sleeper=sleeps.append)
        self.assertEqual(client.get("/v3/reference/tickers")["status"], "OK")
        self.assertEqual(client.request_count, 4)
        self.assertEqual(sleeps, [5, 15, 30])

    def test_removed_master_symbol_becomes_inactive(self):
        db = sqlite3.connect(":memory:")
        init_db(db)
        def record(symbol):
            return (symbol, "COMMON_STOCK", "UNKNOWN", "NASDAQ", 0, 0, 0, 0,
                    symbol, "CS", None, "MASSIVE", "2026-09-25", "PENDING")
        store_tickers(db, {"AAPL": record("AAPL"), "OLD": record("OLD")})
        store_tickers(db, {"AAPL": record("AAPL")})
        self.assertEqual(db.execute("SELECT is_active FROM securities WHERE ticker='AAPL'").fetchone()[0], 1)
        self.assertEqual(db.execute("SELECT is_active FROM securities WHERE ticker='OLD'").fetchone()[0], 0)
        db.close()

    def test_nasdaq_slash_class_share_matches_massive_dot(self):
        db = sqlite3.connect(":memory:")
        init_db(db)
        db.execute("""INSERT INTO securities(ticker,asset_type,country,exchange,is_etf,is_otc,is_preferred,is_warrant,source)
            VALUES('BRK.B','COMMON_STOCK','UNKNOWN','NYSE',0,0,0,0,'MASSIVE')""")
        stats = enrich(db, [{"symbol": "BRK/B", "country": "United States", "sector": ""}],
                       {"sector_to_etf": {}})
        self.assertEqual(stats["us_verified"], 1)
        self.assertEqual(db.execute("SELECT country FROM securities WHERE ticker='BRK.B'").fetchone()[0], "US")
        db.close()

    def test_split_history_records_only_allowed_symbols(self):
        class SplitClient:
            def get(self, path, params):
                return {"status": "OK", "results": [
                    {"id": "split-1", "ticker": "AAPL", "execution_date": "2026-09-24",
                     "adjustment_type": "forward_split"},
                    {"id": "split-2", "ticker": "FUND", "execution_date": "2026-09-24",
                     "adjustment_type": "forward_split"}], "next_url": None}
        db = sqlite3.connect(":memory:")
        init_db(db)
        self.assertEqual(record_splits(db, SplitClient(), "2026-09-01", "2026-09-24", {"AAPL"}), {"AAPL"})
        self.assertEqual(db.execute("SELECT COUNT(*) FROM corporate_actions").fetchone()[0], 1)
        db.close()

    def test_incremental_update_downloads_only_missing_days(self):
        days = ["2026-09-22", "2026-09-23", "2026-09-24"]
        class Client:
            request_count = 0
            grouped = 0
            def get(self, path, params):
                self.request_count += 1
                if path == "/v3/reference/tickers":
                    return {"status": "OK", "next_url": None, "results": [
                        {"ticker": f"Q{i:04d}", "active": True, "type": "CS", "locale": "us",
                         "market": "stocks", "primary_exchange": "XNAS", "name": f"Company {i}"}
                        for i in range(1001)]}
                if path.startswith("/v2/aggs/ticker/SPY/"):
                    return {"status": "OK", "results": [
                        {"t": int(datetime.fromisoformat(day).replace(hour=14, tzinfo=timezone.utc).timestamp() * 1000)}
                        for day in days]}
                if path.startswith("/v2/aggs/grouped/"):
                    self.grouped += 1
                    return {"status": "OK", "results": [
                        {"T": symbol, "o": 100, "h": 101, "l": 99, "c": 100, "v": 100}
                        for symbol in ["SPY", "QQQ"] + [f"Q{i:04d}" for i in range(1001)]]}
                if path == "/stocks/v1/splits":
                    return {"status": "OK", "results": [], "next_url": None}
                raise AssertionError(path)
        db = sqlite3.connect(":memory:")
        db.row_factory = sqlite3.Row
        init_db(db)
        client = Client()
        cfg = {"exchange_mics": {"XNAS": "NASDAQ"}, "sector_etfs": [], "history_sessions": 3}
        _, first = sync(db, client, days[-1], cfg)
        _, second = sync(db, client, days[-1], cfg)
        self.assertEqual(first["updated_days"], 3)
        self.assertEqual(client.grouped, 3)
        self.assertEqual(second["updated_days"], 0)
        self.assertEqual(second["updated_symbols"], 0)
        self.assertEqual(second["skipped_days"], 3)
        db.close()


if __name__ == "__main__":
    unittest.main()

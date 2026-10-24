import io
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from alpaca_market import AlpacaData, daily_request_window, sync, valid_daily
from quant_screen import init_db
from shadow_day1_gate import BENCHMARKS


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


class AlpacaMarketTests(unittest.TestCase):
    def test_historical_sip_and_pagination(self):
        pages = [
            {"bars": {"SPY": [{"t": "2026-09-28T04:00:00Z"}]}, "next_page_token": "page2"},
            {"bars": {"QQQ": [{"t": "2026-09-28T04:00:00Z"}]}, "next_page_token": None},
        ]
        urls = []

        def opener(request, timeout):
            urls.append(request.full_url)
            self.assertEqual(request.full_url.split("?", 1)[0],
                             "https://data.alpaca.markets/v2/stocks/bars")
            return Response(json.dumps(pages.pop(0)).encode())

        client = AlpacaData({"ALPACA_API_KEY": "hidden", "ALPACA_API_SECRET": "hidden"},
                            opener=opener, sleeper=lambda _: None)
        bars = client.bars(["SPY", "QQQ"], "1Day", "2026-09-28", "2026-09-29")
        self.assertEqual(client.requests, 2)
        self.assertEqual([len(bars[x]) for x in ("SPY", "QQQ")], [1, 1])
        self.assertTrue(all("feed=sip" in url and "adjustment=split" in url for url in urls))
        self.assertNotIn("hidden", " ".join(urls))

    def test_rejects_bad_or_wrong_date_daily(self):
        good = {"t": "2026-09-28T04:00:00Z", "o": 10, "h": 11,
                "l": 9, "c": 10.5, "v": 100}
        self.assertTrue(valid_daily(good, "2026-09-28"))
        self.assertFalse(valid_daily({**good, "t": "2026-09-25T04:00:00Z"}, "2026-09-28"))
        self.assertFalse(valid_daily({**good, "c": float("nan")}, "2026-09-28"))
        self.assertFalse(valid_daily({**good, "h": 10}, "2026-09-28"))

    def test_daily_sip_end_is_after_close_but_older_than_15_minutes(self):
        et = ZoneInfo("America/New_York")
        with self.assertRaisesRegex(RuntimeError, "ALPACA_DATA_INCOMPLETE"):
            daily_request_window("2026-09-30", datetime(2026, 9, 30, 16, 15, tzinfo=et))
        start, end = daily_request_window("2026-09-30",
                                           datetime(2026, 9, 30, 16, 20, tzinfo=et))
        self.assertEqual(start, "2026-09-30T00:00:00-04:00")
        self.assertEqual(end, "2026-09-30T16:01:00-04:00")

    def test_normal_future_schedule_date_can_be_used_once_session_has_closed(self):
        et = ZoneInfo("America/New_York")
        _, end = daily_request_window("2026-10-05", datetime(2026, 10, 5, 16, 20, tzinfo=et))
        self.assertEqual(end, "2026-10-05T16:01:00-04:00")
        with self.assertRaisesRegex(RuntimeError, "ALPACA_DATA_INCOMPLETE"):
            daily_request_window("2026-10-05", datetime(2026, 10, 2, 17, 0, tzinfo=et))
        with self.assertRaisesRegex(ValueError, "NON_TRADING_DATE"):
            daily_request_window("2026-10-03", datetime(2026, 10, 5, 17, 0, tzinfo=et))

    def test_historical_gap_repair_preserves_massive_rows_and_requests_only_missing(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "market.db"
            with sqlite3.connect(path) as db:
                init_db(db)
                db.execute("""INSERT INTO securities
                    (ticker,asset_type,country,exchange,is_etf,is_otc,is_preferred,is_warrant,is_active)
                    VALUES('AAPL','COMMON_STOCK','US','NASDAQ',0,0,0,0,1)""")
                for day in ("2026-09-25", "2026-09-28"):
                    for ticker in (*BENCHMARKS, "AAPL"):
                        if day == "2026-09-28" and ticker == "AAPL":
                            continue
                        db.execute("""INSERT INTO daily_bars
                            (ticker,date,open,high,low,close,volume,source)
                            VALUES(?,?,10,11,9,10,100,'MASSIVE')""", (ticker, day))
            calls = []

            class Client:
                requests = 0

                def bars(self, names, *args):
                    calls.append(names)
                    self.requests += 1
                    return {name: [{"t": "2026-09-28T04:00:00Z", "o": 12,
                                   "h": 13, "l": 11, "c": 12, "v": 200}]
                            for name in names}

            with patch("alpaca_market.daily_request_window", return_value=("start", "end")):
                result = sync("2026-09-28", path, Client(), preserve_existing=True)
            self.assertEqual(calls, [["AAPL"]])
            self.assertEqual(result["rows"], 1)
            with sqlite3.connect(path) as db:
                self.assertEqual(db.execute("""SELECT close,source FROM daily_bars
                    WHERE ticker='SPY' AND date='2026-09-28'""").fetchone(), (10, "MASSIVE"))
                self.assertEqual(db.execute("""SELECT close,source FROM daily_bars
                    WHERE ticker='AAPL' AND date='2026-09-28'""").fetchone(), (12, "ALPACA_SIP"))
                self.assertEqual(db.execute("""SELECT source FROM market_sync_days
                    WHERE date='2026-09-28'""").fetchone()[0], "MIXED")


if __name__ == "__main__":
    unittest.main()

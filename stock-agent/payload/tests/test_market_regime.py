import json
import sqlite3
import sys
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from market_regime import ET, evaluate, is_market_holiday, parse_cboe_index, parse_cboe_vxn_close, parse_index
from quant_screen import ROOT, init_db


CFG = json.loads((ROOT / "real_market_config.json").read_text())["volatility"]


def payload(days, closes, quote_at, price=None, missing_ts=False):
    stamps = [int(datetime(d.year, d.month, d.day, 9, 30, tzinfo=ET).timestamp()) for d in days]
    meta = {"regularMarketPrice": closes[-1] if price is None else price}
    if not missing_ts:
        meta["regularMarketTime"] = int(quote_at.timestamp())
    return {"chart": {"result": [{"timestamp": stamps, "meta": meta,
                                  "indicators": {"quote": [{"open": closes, "high": closes,
                                                              "low": closes, "close": closes}]}}]}}


class MarketRegimeTest(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        init_db(self.db)
        self.now = datetime(2026, 9, 25, 15, 0, tzinfo=ET)
        self.days = [date(2026, 9, 23), date(2026, 9, 24), date(2026, 9, 25)]

    def tearDown(self):
        self.db.close()

    def source(self, symbol, _range):
        closes = [19.0, 20.0, 21.0] if symbol == "^VIX" else [25.0, 26.0, 27.0]
        return payload(self.days, closes, self.now - timedelta(minutes=5))

    def test_both_valid_and_previous_close_from_bars(self):
        row = self.source("^VIX", "2y")
        row["chart"]["result"][0]["meta"]["chartPreviousClose"] = 99
        result = parse_index("VIX", row, "2026-09-25", self.now, CFG)
        self.assertEqual(result["status"], "FRESH")
        self.assertEqual(result["previous_close"], 20)
        out = evaluate(self.db, "2026-09-25", self.now, CFG, self.source)
        self.assertEqual(out["regime_source"], "YAHOO_VIX_VXN")
        self.assertEqual(out["confidence"], "HIGH")

    def test_vix_only_when_vxn_stale(self):
        def fetch(symbol, period):
            if symbol == "^VXN":
                return payload(self.days[:-1], [25, 26], self.now - timedelta(days=1))
            return self.source(symbol, period)
        out = evaluate(self.db, "2026-09-25", self.now, CFG, fetch)
        self.assertEqual(out["regime_source"], "YAHOO_VIX_ONLY")
        self.assertEqual(out["vxn"]["status"], "STALE")

    def test_vxn_only_when_vix_stale(self):
        def fetch(symbol, period):
            if symbol == "^VIX":
                return payload(self.days[:-1], [19, 20], self.now - timedelta(days=1))
            return self.source(symbol, period)
        out = evaluate(self.db, "2026-09-25", self.now, CFG, fetch)
        self.assertEqual(out["regime_source"], "YAHOO_VXN_ONLY")

    def test_both_stale_uses_proxy(self):
        self.seed_proxy()
        def fetch(symbol, _):
            return payload(self.days[:-1], [19, 20], self.now - timedelta(days=1))
        out = evaluate(self.db, "2026-09-25", self.now, CFG, fetch)
        self.assertEqual(out["regime_source"], "MARKET_PROXY")
        self.assertTrue(out["fallback_triggered"])
        self.assertEqual(out["confidence"], "MEDIUM")

    def test_request_failure_uses_proxy(self):
        self.seed_proxy()
        def fail(*_):
            raise OSError("network")
        out = evaluate(self.db, "2026-09-25", self.now, CFG, fail)
        self.assertEqual(out["regime_source"], "MARKET_PROXY")
        self.assertEqual(out["vix"]["status"], "UNAVAILABLE")

    def test_market_proxy_keeps_real_inputs_and_does_not_invent_vix(self):
        self.seed_proxy()
        def fail(*_):
            raise OSError("network")
        out = evaluate(self.db, "2026-09-25", self.now, CFG, fail)
        self.assertIsNone(out["vix"]["value"])
        self.assertIsNone(out["vxn"]["value"])
        self.assertEqual(len(out["market_proxy"]["benchmarks"]), 2)
        self.assertIn("realized_vol20", out["market_proxy"]["benchmarks"][0])
        self.assertIn("atr_pct14", out["market_proxy"]["benchmarks"][0])
        self.assertIn("drawdown60", out["market_proxy"]["benchmarks"][0])

    def test_no_yahoo_or_recent_proxy_is_unavailable(self):
        def fail(*_):
            raise OSError("network")
        out = evaluate(self.db, "2026-09-25", self.now, CFG, fail)
        self.assertEqual(out["regime_source"], "MARKET_PROXY")
        self.assertEqual(out["regime"], "UNAVAILABLE")
        self.assertEqual(out["confidence"], "NONE")

    def test_nan_invalid(self):
        row = self.source("^VIX", "2y")
        row["chart"]["result"][0]["indicators"]["quote"][0]["close"][-1] = float("nan")
        self.assertEqual(parse_index("VIX", row, "2026-09-25", self.now, CFG)["status"], "INVALID")

    def test_missing_timestamp_invalid(self):
        row = payload(self.days, [19, 20, 21], self.now, missing_ts=True)
        self.assertEqual(parse_index("VIX", row, "2026-09-25", self.now, CFG)["status"], "INVALID")

    def test_weekend_friday_close(self):
        now = datetime(2026, 9, 26, 12, tzinfo=ET)
        row = payload(self.days, [19, 20, 21], self.now)
        parsed = parse_index("VIX", row, "2026-09-26", now, CFG)
        self.assertEqual(parsed["status"], "LAST_MARKET_CLOSE")
        self.assertEqual(parsed["latest_trading_date"], "2026-09-25")

    def test_thanksgiving_holiday(self):
        self.assertTrue(is_market_holiday(date(2026, 11, 26)))
        now = datetime(2026, 11, 26, 12, tzinfo=ET)
        days = [date(2026, 11, 24), date(2026, 11, 25)]
        row = payload(days, [19, 20], datetime(2026, 11, 25, 16, 15, tzinfo=ET))
        self.assertEqual(parse_index("VIX", row, "2026-11-26", now, CFG)["status"], "LAST_MARKET_CLOSE")

    def test_intraday_delayed(self):
        row = payload(self.days, [19, 20, 21], self.now - timedelta(minutes=90))
        self.assertEqual(parse_index("VIX", row, "2026-09-25", self.now, CFG)["status"], "DELAYED")

    def test_yahoo_previous_close_remains_valid_during_next_session(self):
        now = datetime(2026, 9, 28, 12, tzinfo=ET)
        row = payload(self.days, [19, 20, 21], self.now.replace(hour=16, minute=15))
        result = parse_index("VIX", row, "2026-09-25", now, CFG)
        self.assertEqual(result["status"], "LAST_MARKET_CLOSE")
        self.assertEqual(result["latest_trading_date"], "2026-09-25")

    def test_yahoo_previous_close_is_stale_after_next_session_closes(self):
        now = datetime(2026, 9, 28, 16, 30, tzinfo=ET)
        row = payload(self.days, [19, 20, 21], self.now.replace(hour=16, minute=15))
        self.assertEqual(parse_index("VIX", row, "2026-09-25", now, CFG)["status"], "STALE")

    def test_abnormal_jump_invalid(self):
        row = payload(self.days, [19, 20, 50], self.now - timedelta(minutes=5))
        self.assertEqual(parse_index("VIX", row, "2026-09-25", self.now, CFG)["status"], "INVALID")

    def cboe_history(self, closes=(25, 26, 27)):
        return "DATE,OPEN,HIGH,LOW,CLOSE\n" + "".join(
            f"{day:%m/%d/%Y},{close},{close},{close},{close}\n"
            for day, close in zip(self.days, closes))

    def test_missing_yahoo_close_uses_valid_cboe_close_and_records_source(self):
        self.now = self.now.replace(hour=16, minute=30)
        def fetch(symbol, period):
            row = self.source(symbol, period)
            if symbol == "^VXN":
                row["chart"]["result"][0]["indicators"]["quote"][0]["close"][-1] = None
            return row
        out = evaluate(self.db, "2026-09-25", self.now, CFG, fetch,
                       cboe_fetch=lambda name: self.cboe_history())
        self.assertEqual(out["regime_source"], "YAHOO_CBOE_VIX_VXN")
        self.assertEqual(out["vxn"]["value"], 27)
        self.assertEqual(out["vxn"]["previous_close"], 26)
        self.assertIsNone(out["vxn"]["timestamp"])
        self.assertEqual(out["vxn"]["fallback_reason"], "nan_inf_or_missing_latest_close")
        self.assertEqual(tuple(self.db.execute(
            "SELECT close,source FROM volatility_daily WHERE symbol='VXN' AND date='2026-09-25'").fetchone()),
            (27, "CBOE"))

    def test_valid_yahoo_never_calls_fallback(self):
        def fail(name):
            self.fail("unnecessary Cboe request")
        out = evaluate(self.db, "2026-09-25", self.now, CFG, self.source, cboe_fetch=fail)
        self.assertEqual(out["regime_source"], "YAHOO_VIX_VXN")

    def test_cboe_does_not_supply_a_current_close_during_market_hours(self):
        result = parse_cboe_index(self.cboe_history(), "2026-09-25", self.now, CFG)
        self.assertEqual(result["status"], "INVALID")
        self.assertEqual(result["error"], "completed_close_not_yet_available")

    def test_cboe_stale_null_duplicate_jump_and_bad_ohlc_are_rejected(self):
        self.now = self.now.replace(hour=16, minute=30)
        text = self.cboe_history()
        cases = [text.rsplit("09/25/2026", 1)[0], self.cboe_history((25, 26, "nan")),
                 text + text.splitlines()[-1] + "\n", self.cboe_history((25, 26, 100)),
                 text.replace("27,27,27,27", "27,26,27,27")]
        for case in cases:
            with self.subTest(case=case):
                result = parse_cboe_index(case, "2026-09-25", self.now, CFG)
                self.assertEqual(result["status"], "INVALID")
                self.assertEqual(result["history"], [])

    def test_invalid_yahoo_quote_is_not_stored_as_ready_daily_data(self):
        def fetch(symbol, period):
            return payload(self.days, [19, 20, 100], self.now)
        evaluate(self.db, "2026-09-25", self.now, CFG, fetch)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM volatility_daily").fetchone()[0], 0)

    def test_fallback_failure_keeps_missing_vxn_unavailable(self):
        self.now = self.now.replace(hour=16, minute=30)
        def fetch(symbol, period):
            if symbol == "^VXN":
                raise OSError("Yahoo unavailable")
            return self.source(symbol, period)
        def fail(name):
            raise OSError("Cboe unavailable")
        out = evaluate(self.db, "2026-09-25", self.now, CFG, fetch, cboe_fetch=fail)
        self.assertEqual(out["regime_source"], "YAHOO_VIX_ONLY")
        self.assertEqual(out["vxn"]["cboe_fallback_error"], "OSError")
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM volatility_daily WHERE symbol='VXN'").fetchone()[0], 0)

    def test_cboe_prior_close_on_weekend_and_during_next_session(self):
        for now, asof in [(datetime(2026, 9, 26, 12, tzinfo=ET), "2026-09-26"),
                          (datetime(2026, 9, 28, 12, tzinfo=ET), "2026-09-25")]:
            with self.subTest(now=now):
                result = parse_cboe_index(self.cboe_history(), asof, now, CFG)
                self.assertEqual(result["status"], "LAST_MARKET_CLOSE")
                self.assertEqual(result["latest_trading_date"], "2026-09-25")

    def test_fallback_replaces_yahoo_source_when_same_date_already_exists(self):
        self.now = self.now.replace(hour=16, minute=30)
        evaluate(self.db, "2026-09-25", self.now, CFG, self.source)
        def missing(symbol, period):
            raise OSError("Yahoo unavailable")
        evaluate(self.db, "2026-09-25", self.now, CFG, missing,
                 cboe_fetch=lambda name: self.cboe_history())
        self.assertEqual(self.db.execute(
            "SELECT source FROM volatility_daily WHERE symbol='VXN' AND date='2026-09-25'").fetchone()[0], "CBOE")

    def closing_quote(self):
        return {"symbol": "_VXN", "data": {
            "symbol": "^VXN", "index": "VXN", "security_type": "index",
            "last_trade_time": "2026-09-25T15:15:01-05:00",
            "open": 26.5, "high": 27.5, "low": 26, "close": 27,
            "current_price": 27, "prev_day_close": 26}}

    def prior_cboe_history(self):
        return self.cboe_history().rsplit("09/25/2026", 1)[0]

    def test_current_official_closing_quote_recovers_lagging_daily_csv(self):
        self.now = self.now.replace(hour=16, minute=40)
        def fetch(symbol, period):
            row = self.source(symbol, period)
            if symbol == "^VXN":
                row["chart"]["result"][0]["indicators"]["quote"][0]["close"][-1] = None
            return row
        out = evaluate(self.db, "2026-09-25", self.now, CFG, fetch,
                       cboe_fetch=lambda name: self.prior_cboe_history(),
                       cboe_quote_fetch=lambda name: self.closing_quote())
        self.assertEqual(out["vxn"]["value"], 27)
        self.assertEqual(out["vxn"]["previous_close"], 26)
        self.assertEqual(out["vxn"]["timestamp"], "2026-09-25T20:15:01+00:00")
        self.assertEqual(out["vxn"]["source_detail"], "CBOE_VXN_1615_CLOSING_QUOTE")
        self.assertEqual(out["regime_source"], "YAHOO_CBOE_VIX_VXN")
        self.assertEqual(self.db.execute("SELECT close FROM volatility_daily WHERE symbol='VXN' AND date='2026-09-25'").fetchone()[0], 27)

    def test_closing_quote_identity_and_price_mismatches_rejected(self):
        self.now = self.now.replace(hour=16, minute=40)
        cases = [(None, "symbol", "_VIX"), ("data", "index", "VIX"),
                 ("data", "symbol", "^VIX"), ("data", "security_type", "equity"),
                 ("data", "prev_day_close", 25), ("data", "current_price", 26),
                 ("data", "close", None), ("data", "close", float("nan")),
                 ("data", "high", 25), ("data", "close", 100)]
        for parent, key, value in cases:
            row = self.closing_quote()
            (row[parent] if parent else row)[key] = value
            with self.subTest(key=key, value=value):
                self.assertEqual(parse_cboe_vxn_close(self.prior_cboe_history(), row,
                    "2026-09-25", self.now, CFG)["status"], "INVALID")

    def test_intraday_prior_date_missing_timezone_and_refresh_timestamp_rejected(self):
        self.now = self.now.replace(hour=16, minute=40)
        stamps = ["2026-09-25T14:59:59-05:00", "2026-09-24T15:15:01-05:00",
                  "2026-09-25T16:15:01", "2026-09-25T17:15:01-04:00",
                  "2026-09-25T16:40:01-04:00"]
        for stamp in stamps:
            row = self.closing_quote();row["data"]["last_trade_time"] = stamp
            with self.subTest(stamp=stamp):
                self.assertEqual(parse_cboe_vxn_close(self.prior_cboe_history(), row,
                    "2026-09-25", self.now, CFG)["status"], "INVALID")
        row = self.closing_quote();row["timestamp"] = "2026-09-25 20:30:00"
        del row["data"]["last_trade_time"]
        self.assertEqual(parse_cboe_vxn_close(self.prior_cboe_history(), row,
            "2026-09-25", self.now, CFG)["status"], "INVALID")

    def test_closing_quote_rejected_during_market_hours_and_for_old_asof(self):
        self.assertEqual(parse_cboe_vxn_close(self.prior_cboe_history(), self.closing_quote(),
            "2026-09-25", self.now, CFG)["status"], "INVALID")
        now = datetime(2026, 9, 28, 16, 40, tzinfo=ET)
        self.assertEqual(parse_cboe_vxn_close(self.prior_cboe_history(), self.closing_quote(),
            "2026-09-25", now, CFG)["status"], "INVALID")

    def test_closing_quote_needs_previous_session_csv(self):
        self.now = self.now.replace(hour=16, minute=40)
        text = self.prior_cboe_history().replace("09/24/2026", "09/22/2026")
        self.assertEqual(parse_cboe_vxn_close(text, self.closing_quote(),
            "2026-09-25", self.now, CFG)["status"], "INVALID")

    def test_complete_daily_csv_does_not_request_closing_quote(self):
        self.now = self.now.replace(hour=16, minute=40)
        def unavailable(symbol, period):
            raise OSError("Yahoo unavailable")
        def fail(name):
            self.fail("unnecessary closing quote request")
        out = evaluate(self.db, "2026-09-25", self.now, CFG, unavailable,
                       cboe_fetch=lambda name: self.cboe_history(), cboe_quote_fetch=fail)
        self.assertEqual(out["regime_source"], "CBOE_VIX_VXN")

    def test_quote_failure_preserves_missing_vxn_gate(self):
        self.now = self.now.replace(hour=16, minute=40)
        def unavailable(symbol, period):
            raise OSError("Yahoo unavailable")
        def fail(name):
            raise OSError("Cboe quote unavailable")
        out = evaluate(self.db, "2026-09-25", self.now, CFG, unavailable,
                       cboe_fetch=lambda name: self.prior_cboe_history(), cboe_quote_fetch=fail)
        self.assertIsNone(out["vxn"]["value"])
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM volatility_daily WHERE date='2026-09-25'").fetchone()[0], 0)

    def seed_proxy(self):
        for symbol in ("SPY", "QQQ"):
            for i in range(61):
                day = (date(2026, 7, 27) + timedelta(days=i)).isoformat()
                close = 100 + i * 0.2
                self.db.execute("INSERT INTO daily_bars(ticker,date,open,high,low,close,volume) VALUES(?,?,?,?,?,?,?)",
                                (symbol, day, close, close * 1.01, close * .99, close, 1_000_000))
        self.db.commit()


if __name__ == "__main__":
    unittest.main()

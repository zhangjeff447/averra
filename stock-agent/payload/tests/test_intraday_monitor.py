"""Offline tests: no Alpaca, Telegram, OpenAI, or broker requests."""
from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import intraday_runner as runner
from intraday_calendar import (calendar_ready, events_for_day, import_events, import_market_sessions,
                               init_calendar, market_day, schedule, set_coverage)
from intraday_calendar_refresh import refresh_due
from intraday_monitor import IEXBars, assess, material_evidence, scoped_universe, valid_bars
from intraday_state import begin_check, daily_totals, init_state, reserve_alert, send_reserved
from intraday_luna import IntradayLunaRefresh

ET = ZoneInfo("America/New_York")
ROOT = Path(__file__).resolve().parents[1]
CFG = json.loads((ROOT / "intraday_config.json").read_text())


def at(day, hour, minute):
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=ET).astimezone(timezone.utc)


def bars(day, close=99, opening=100, end=(9, 49)):
    return [{"t": at(day, 9, 30).isoformat(), "o": opening, "h": opening + 1,
             "l": opening - 1, "c": opening, "v": 100, "vw": opening},
            {"t": at(day, *end).isoformat(), "o": close, "h": max(close, opening) + 1,
             "l": min(close, opening) - 1, "c": close, "v": 200, "vw": close}]


def plan():
    return {"status": "CONDITIONAL", "reference_close": 100,
            "buy_zone_low": 98, "buy_zone_high": 102,
            "do_not_chase_above": 102, "atr": 2,
            "nearest_support": 96}


class CalendarTests(unittest.TestCase):
    def test_normal_slots_and_holiday(self):
        self.assertEqual([w["time_et"] for w in schedule(date(2026, 10, 1), [], CFG)],
                         CFG["normal_checks_et"])
        self.assertEqual(schedule(date(2026, 12, 25), [], CFG), [])

    def test_fomc_slots(self):
        event = {"event": "FOMC_DECISION", "scheduled_time_et": "14:00", "impact_level": "MATERIAL"}
        windows = schedule(date(2026, 10, 28), [event], CFG)
        mapping = {w["time_et"]: w["kind"] for w in windows}
        self.assertEqual(mapping["13:55"], "PRE_EVENT_CHECK")
        self.assertEqual(mapping["14:10"], "FIRST_REACTION_CHECK")
        self.assertEqual(mapping["14:35"], "POST_FOMC_REPRICING_CHECK")
        self.assertEqual(mapping["15:45"], "FINAL_FOMC_RISK_CHECK")
        self.assertEqual(len(windows), len(set(mapping)))

    def test_macro_open_and_jolts_merge(self):
        day = date(2026, 10, 2)
        events = [{"event": "NFP", "scheduled_time_et": "08:30", "impact_level": "MATERIAL"},
                  {"event": "JOLTS", "scheduled_time_et": "10:00", "impact_level": "HIGH"}]
        mapping = {w["time_et"]: w for w in schedule(day, events, CFG)}
        self.assertEqual(mapping["09:35"]["kind"], "MACRO_OPENING_CHECK")
        self.assertEqual(mapping["09:50"]["kind"], "PRE_EVENT_CHECK")
        self.assertNotIn("09:55", mapping)
        self.assertEqual(mapping["09:50"]["merged"], 1)
        self.assertEqual(mapping["10:10"]["kind"], "POST_EVENT_CHECK")

    def test_early_close(self):
        slots = [w["time_et"] for w in schedule(date(2026, 11, 27), [], CFG,
                                                  {"session_type": "EARLY_CLOSE", "close_time_et": "13:00"})]
        self.assertEqual(slots, ["09:50", "12:15", "12:45"])

    def test_fed_chair_conference(self):
        event = {"event": "FED_CHAIR_PRESS_CONFERENCE", "scheduled_time_et": "14:30",
                 "impact_level": "HIGH"}
        mapping = {w["time_et"]: w for w in schedule(date(2026, 10, 28), [event], CFG)}
        self.assertEqual(mapping["14:35"]["kind"], "POST_FOMC_REPRICING_CHECK")

    def test_official_import_and_coverage(self):
        db = sqlite3.connect(":memory:")
        init_calendar(db)
        record = {"event": "NFP", "date": "2026-10-02", "scheduled_time_et": "08:30",
                  "source": "https://www.bls.gov/schedule/2026/home.htm", "impact_level": "MATERIAL"}
        self.assertEqual(import_events(db, [record], CFG, at(date(2026, 10, 1), 8, 0)), 1)
        self.assertEqual(events_for_day(db, date(2026, 10, 2))[0]["event"], "NFP")
        with self.assertRaises(ValueError):
            import_events(db, [{**record, "source": "https://fake-bls.gov/schedule"}], CFG)
        for authority, host in (("BLS", "bls.gov"), ("BEA", "bea.gov"),
                                ("FED", "federalreserve.gov"), ("NYSE", "nyse.com")):
            set_coverage(db, authority, "2026-10-31", "https://www." + host + "/schedule", CFG,
                         at(date(2026, 10, 1), 8, 0))
        self.assertTrue(calendar_ready(db, date(2026, 10, 2), CFG, at(date(2026, 10, 2), 8, 0)))
        self.assertFalse(calendar_ready(db, date(2026, 11, 1), CFG, at(date(2026, 11, 1), 8, 0)))
        db.close()


class UnconfirmedPlanTests(unittest.TestCase):
    def test_unconfirmed_plan_is_not_a_holding_and_expires_after_target_day(self):
        day = date(2026, 10, 2)
        book = sqlite3.connect(":memory:")
        book.execute("CREATE TABLE tracked_holdings(ticker TEXT,status TEXT)")
        entry = sqlite3.connect(":memory:")
        entry.execute("CREATE TABLE entry_reference_plans(run_id INTEGER,ticker TEXT,target_trading_date TEXT,plan_json TEXT)")
        entry.execute("INSERT INTO entry_reference_plans VALUES(1,'LITE',?,?)",
                      (day.isoformat(), json.dumps(plan())))
        scope = scoped_universe(book, entry, day)
        self.assertFalse(scope["LITE"]["held"])
        self.assertIsNotNone(scope["LITE"]["plan"])
        book.execute("INSERT INTO tracked_holdings VALUES('LITE','HELD')")
        held_scope = scoped_universe(book, entry, day)
        self.assertTrue(held_scope["LITE"]["held"])
        self.assertIsNone(held_scope["LITE"]["plan"])
        book.execute("DELETE FROM tracked_holdings")
        self.assertNotIn("LITE", scoped_universe(book, entry, date(2026, 10, 5)))
        book.close(); entry.close()

    def test_saved_close_plan_restored_once_without_confirming_a_buy(self):
        from phase5_entry import init_db as init_entry, save_decision
        day = date(2026, 10, 2)
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            report_dir = root / "data" / "shadow_daily" / "2026-10-01"
            report_dir.mkdir(parents=True)
            entry = sqlite3.connect(root / "data" / "entry_data.db")
            init_entry(entry)
            run_id = entry.execute("INSERT INTO entry_runs(asof,started_at,status,source,config_json) "
                                   "VALUES('2026-10-01','2026-10-01T20:00:00+00:00','COMPLETE','ALPACA_SIP','{}')").lastrowid
            result = {"ticker": "LITE", "entry_state": "BUY_TOMORROW_CANDIDATE",
                      "asof": "2026-10-01", "target_session": day.isoformat(),
                      "entry_price_reference": 100.0, "timeframes": {"daily": {"atr14": 4.0, "ema21": 95.0}},
                      "features": {"support_resistance": {"support": 96.0}, "vwap": {"value": 99.0}}}
            save_decision(entry, run_id, "LITE", result, {})
            entry.commit(); entry.close()
            (report_dir / "entry.json").write_text(json.dumps({"run_id": run_id,
                "trading_date": "2026-10-01", "target_session": day.isoformat(), "results": [result]}))
            (report_dir / "final.json").write_text(json.dumps({"status": "PIPELINE_COMPLETE",
                "market_data_as_of": "2026-10-01", "target_session": day.isoformat()}))
            with patch.object(runner, "ROOT", root):
                self.assertEqual(runner.restore_close_entry_plans(day), 1)
                self.assertEqual(runner.restore_close_entry_plans(day), 0)
            entry = sqlite3.connect(root / "data" / "entry_data.db")
            row = entry.execute("SELECT plan_json FROM entry_reference_plans WHERE ticker='LITE'").fetchone()
            self.assertEqual(json.loads(row[0])["recovery_source"], "SAVED_COMPLETED_CLOSE_ENTRY")
            entry.close()

class CalendarRefreshTests(unittest.TestCase):
    def test_revision_unverified_and_session_import(self):
        db = sqlite3.connect(":memory:"); init_calendar(db)
        record = {"event_id": "BLS:CPI:2026-10-14", "event_name": "Consumer Price Index",
                  "event_type": "CPI", "date": "2026-10-14", "scheduled_time_et": "08:30",
                  "source_url": "https://www.bls.gov/schedule/2026/10_sched.htm", "impact_level": "MATERIAL"}
        import_events(db, [record], CFG, at(date(2026, 10, 13), 10, 0))
        import_events(db, [record], CFG, at(date(2026, 10, 13), 11, 0))
        self.assertEqual(db.execute("SELECT COUNT(*) FROM official_calendar_revisions").fetchone()[0], 1)
        import_events(db, [{**record, "scheduled_time_et": "09:00"}], CFG,
                      at(date(2026, 10, 13), 12, 0))
        self.assertEqual(db.execute("SELECT COUNT(*) FROM official_calendar_revisions").fetchone()[0], 2)
        import_events(db, [{**record, "scheduled_time_et": None}], CFG,
                      at(date(2026, 10, 13), 13, 0))
        self.assertEqual(events_for_day(db, date(2026, 10, 14)), [])
        self.assertEqual(db.execute("SELECT calendar_status FROM official_calendar_events").fetchone()[0], "UNVERIFIED")
        import_market_sessions(db, [{"date": "2026-11-27", "session_type": "EARLY_CLOSE",
                                     "close_time_et": "13:00", "source_url": "https://www.nyse.com/trade/hours-calendars"}])
        self.assertEqual(market_day(db, date(2026, 11, 27))["close_time_et"], "13:00")
        db.close()

    def test_generic_fomc_dynamic_and_refresh_failure(self):
        day = date(2026, 10, 28)
        event = {"event": "FOMC_DECISION", "scheduled_time_et": "13:00", "impact_level": "MATERIAL"}
        slots = {w["time_et"] for w in schedule(day, [event], CFG)}
        self.assertIn("12:55", slots)
        self.assertIn("13:10", slots)
        self.assertNotIn("13:55", slots)
        db = sqlite3.connect(":memory:"); init_calendar(db)
        import_events(db, [{"event": "NFP", "date": "2026-10-02", "scheduled_time_et": "08:30",
                            "source": "https://www.bls.gov/schedule/2026/10_sched.htm", "impact_level": "MATERIAL"}],
                      CFG, at(date(2026, 9, 25), 12, 0))
        set_coverage(db, "BLS", "2026-12-31", "https://www.bls.gov/schedule/2026/10_sched.htm", CFG,
                     at(date(2026, 9, 25), 12, 0))
        result = refresh_due(db, at(date(2026, 10, 1), 12, 0), fetch=lambda _: (_ for _ in ()).throw(OSError()))
        self.assertEqual(result["failures"]["BLS"], "OSError")
        self.assertEqual(events_for_day(db, date(2026, 10, 2), CFG, at(date(2026, 10, 2), 9, 0)), [])
        db.close()

    def test_nyse_weekly_revalidation(self):
        db = sqlite3.connect(":memory:"); init_calendar(db)
        old = at(date(2026, 11, 10), 10, 0)
        url = "https://www.nyse.com/trade/hours-calendars"
        import_market_sessions(db, [{"date": "2026-11-27", "session_type": "EARLY_CLOSE",
                                     "close_time_et": "13:00", "source_url": url}], old)
        set_coverage(db, "NYSE", "2026-12-31", url, CFG, old)
        result = refresh_due(db, at(date(2026, 11, 19), 10, 0), fetch=lambda _:
            "each market will close early at 1:00 p.m. on friday, november 27, 2026")
        self.assertEqual(result["attempted_sources"], 1)
        self.assertEqual(result["unverified_events"], 0)
        self.assertIn("2026-11-19", db.execute("SELECT verified_at FROM official_market_sessions").fetchone()[0])
        db.close()


class RiskTests(unittest.TestCase):
    DAY = date(2026, 10, 2)
    NOW = at(DAY, 9, 50)

    def assess(self, close=99, opening=100, role=None, events=(), benchmark=None):
        return assess("AMD", role or {"held": True, "plan": None},
                      bars(self.DAY, close, opening), {"previous_close": 100, "atr": 2},
                      benchmark or {"SPY": {"previous_close": 100, "price": 100, "change_pct": 0},
                                    "QQQ": {"previous_close": 100, "price": 100, "change_pct": 0}},
                      list(events), self.NOW, self.DAY, CFG)

    def test_position_normal_and_price_shock(self):
        self.assertEqual(self.assess()["position_state"], "POSITION_NORMAL")
        self.assertEqual(self.assess(close=97)["position_state"], "REDUCE_CANDIDATE")
        self.assertEqual(self.assess(close=94)["position_state"], "EXIT_CANDIDATE")

    def test_material_negative_thesis_and_exit(self):
        ordinary = {"event_type": "GUIDANCE_CUT", "evidence_id": "SEC1"}
        severe = {"event_type": "RESTATEMENT", "evidence_id": "SEC2"}
        self.assertEqual(self.assess(events=[ordinary])["position_state"], "THESIS_REVIEW")
        self.assertEqual(self.assess(events=[severe])["position_state"], "EXIT_CANDIDATE")

    def test_profit_protection_reference_is_read_only(self):
        role = {"held": True, "plan": None,
                "reference_zones": {"profit_protection_low": 99.5, "trend_failure_low": 95}}
        self.assertEqual(self.assess(close=99, role=role)["position_state"], "TRIM_REVIEW")
        self.assertEqual(self.assess(close=94, role=role)["position_state"], "EXIT_CANDIDATE")

    def test_buy_plan_states(self):
        role = {"held": False, "plan": plan()}
        self.assertEqual(self.assess(role=role)["plan_state"], "ENTRY_PLAN_VALID")
        self.assertEqual(self.assess(close=104, role=role)["plan_state"], "BUY_PLAN_INVALIDATED")
        self.assertEqual(self.assess(role=role, events=[{"event_type": "GUIDANCE_CUT", "evidence_id": "E1"}])["plan_state"], "BUY_PLAN_INVALIDATED")
        selloff = {"SPY": {"previous_close": 100, "price": 98, "change_pct": -2},
                   "QQQ": {"previous_close": 100, "price": 98, "change_pct": -2}}
        self.assertEqual(self.assess(role=role, benchmark=selloff)["plan_state"], "BUY_PLAN_INVALIDATED")
        positive = self.assess(role=role, events=[{"event_type": "MAJOR_CONTRACT", "evidence_id": "E2"}])
        self.assertEqual(positive["plan_state"], "WAIT_FOR_REASSESSMENT")
        self.assertTrue(positive["positive_event_review"])
        self.assertNotIn("BUY_NOW", json.dumps(positive))

    def test_iex_missing_and_stale(self):
        self.assertFalse(valid_bars([], self.DAY, self.NOW, CFG))
        self.assertFalse(valid_bars(bars(self.DAY, end=(9, 30)), self.DAY, self.NOW, CFG))
        self.assertEqual(assess("AMD", {"held": True}, [], {"previous_close": 100, "atr": 2},
                                {}, [], self.NOW, self.DAY, CFG)["status"], "DATA_UNAVAILABLE")

    def test_atr_normalized_and_iex_volume_limit(self):
        result = self.assess(close=98)
        self.assertEqual(result["move_atr"], -1)
        self.assertIsNone(result["rvol"])
        self.assertEqual(result["source"], "IEX")

    def test_material_evidence_time_and_official(self):
        stamp = at(self.DAY, 9, 40).isoformat()
        events = [{"evidence_id": "E1", "materiality": "MATERIAL", "official": True,
                   "accepted_at": stamp},
                  {"evidence_id": "E2", "materiality": "LOW", "official": True,
                   "accepted_at": stamp},
                  {"evidence_id": "E3", "materiality": "MATERIAL", "official": False,
                   "accepted_at": stamp}]
        self.assertEqual([e["evidence_id"] for e in material_evidence(events, at(self.DAY, 9, 30))], ["E1"])
        self.assertEqual(material_evidence(events, at(self.DAY, 9, 45)), [])

    def test_scoped_universe(self):
        book, entry = sqlite3.connect(":memory:"), sqlite3.connect(":memory:")
        book.execute("CREATE TABLE tracked_holdings(ticker TEXT,status TEXT)")
        entry.execute("CREATE TABLE entry_reference_plans(run_id INTEGER,ticker TEXT,target_trading_date TEXT,plan_json TEXT)")
        book.execute("INSERT INTO tracked_holdings VALUES('AMD','HELD')")
        entry.execute("INSERT INTO entry_reference_plans VALUES(?,?,?,?)",
                      (1, "META", self.DAY.isoformat(), json.dumps(plan())))
        result = scoped_universe(book, entry, self.DAY, watch=["AAPL", "NVDA"], max_watch=1)
        self.assertEqual(set(result), {"AMD", "META", "AAPL"})
        book.close(); entry.close()

    def test_iex_data_endpoint_only(self):
        observed = []
        class Response:
            def __enter__(self):
                return self
            def __exit__(self, *_):
                return False
            def read(self, *_):
                return b'{"bars":{"AMD":[]}}'
        def opener(request, timeout):
            observed.append(request)
            return Response()
        client = IEXBars("test-key", "test-secret", opener=opener)
        self.assertEqual(client.fetch(["AMD"], self.DAY, self.NOW, CFG), {"AMD": []})
        self.assertEqual(client.requests, 1)
        self.assertIn("data.alpaca.markets/v2/stocks/bars", observed[0].full_url)
        self.assertIn("feed=iex", observed[0].full_url)
        self.assertNotIn("trading.alpaca", observed[0].full_url)


class MessageTests(unittest.TestCase):
    def sample(self, state, *, held=False):
        return {"ticker": "LITE", "price": 1088.58, "opening_gap_pct": 0.688,
                "move_atr": 0.659, "vwap": 1073.05, "market_regime": "ELEVATED",
                "asof": "2026-10-02T18:44:00Z",
                "position_state": state if held else None,
                "plan_state": None if held else state}

    def test_unconfirmed_buy_plan_alert_explains_price_threshold(self):
        message = runner.alert_text(self.sample("BUY_PLAN_INVALIDATED"), "ENTRY_PLAN", "14:45", [],
                                    scope={"plan": {"do_not_chase_above": 1061.85}}, cfg=CFG)
        self.assertIn("未确认买入计划", message)
        self.assertIn("$1088.58 高于不追价上限 $1061.85", message)
        self.assertIn("不要按昨日区间追买", message)
        self.assertIn("这不是卖出提醒", message)
        self.assertIn("14:44 ET", message)
        self.assertIn("非全市场 SIP", message)
        self.assertIn("未执行交易", message)

    def test_holding_exit_alert_preserves_event_and_review_status(self):
        item = self.sample("EXIT_CANDIDATE", held=True)
        event = {"evidence_id": "SEC-1", "event_type": "RESTATEMENT", "headline": "Restatement"}
        message = runner.alert_text(item, "TRACKED", "14:45", [event],
                                    scope={"reference_zones": {"trend_failure_low": 1100}}, cfg=CFG)
        self.assertIn("已确认持仓", message)
        self.assertIn("卖出候选，需人工复核", message)
        self.assertIn("跌破趋势失效线 $1100.00", message)
        self.assertIn("Restatement [SEC-1] (RESTATEMENT)", message)
        self.assertIn("不是自动卖出指令", message)

    def test_positive_event_and_macro_alert_not_buy_now(self):
        item = self.sample("WAIT_FOR_REASSESSMENT")
        item["positive_event_review"] = True
        event = {"evidence_id": "IR-1", "event_type": "MAJOR_CONTRACT", "headline": "New contract"}
        message = runner.alert_text(item, "ENTRY_PLAN", "14:45", [event], cfg=CFG)
        self.assertIn("等待重新评估", message)
        self.assertIn("BUY_NOW 关闭", message)
        self.assertIn("New contract [IR-1]", message)
        macro = runner.macro_alert_text("14:45", "NFP", {"SPY": {"change_pct": -2.1},
            "QQQ": {"change_pct": -2.5}}, "ELEVATED", "HIGH_VOLATILITY")
        self.assertIn("大盘波动提醒", macro)
        self.assertIn("NFP", macro)
        self.assertIn("-2.10%", macro)
        self.assertIn("未执行交易", macro)


class StateTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        init_state(self.db)

    def tearDown(self):
        self.db.close()

    def test_atomic_start_and_no_retry(self):
        self.assertEqual(begin_check(self.db, "2026-10-02", "09:50", "NORMAL"), "STARTED")
        self.assertEqual(begin_check(self.db, "2026-10-02", "09:50", "NORMAL"), "ALREADY_RUNNING")
        self.db.execute("UPDATE intraday_checks SET status='FAILED' WHERE check_window='09:50'")
        self.db.commit()
        self.assertEqual(begin_check(self.db, "2026-10-02", "09:50", "NORMAL"), "ALREADY_FAILED")

    def test_duplicate_and_uncertain_telegram(self):
        first = reserve_alert(self.db, "2026-10-02", "09:50", "AMD", "E1", "THESIS_REVIEW", "text")
        self.assertIsNotNone(first)
        self.assertIsNone(reserve_alert(self.db, "2026-10-02", "09:50", "AMD", "E1", "THESIS_REVIEW", "text"))
        calls = []
        self.assertEqual(send_reserved(self.db, first, lambda msg: calls.append(msg) or 321), "SENT")
        self.assertEqual(send_reserved(self.db, first, lambda msg: calls.append(msg) or 321), "NOT_RESERVED")
        self.assertEqual(len(calls), 1)
        second = reserve_alert(self.db, "2026-10-02", "12:15", "AMD", "E2", "EXIT_CANDIDATE", "text")
        self.assertEqual(send_reserved(self.db, second, lambda _: (_ for _ in ()).throw(TimeoutError())), "UNKNOWN")
        self.assertEqual(send_reserved(self.db, second, lambda _: 322), "NOT_RESERVED")

    def test_zero_ai_totals(self):
        self.assertEqual(daily_totals(self.db, "2026-10-02")["luna_calls"], 0)
        self.assertEqual(daily_totals(self.db, "2026-10-02")["sol_calls"], 0)


class RunnerPolicyTests(unittest.TestCase):
    def test_luna_missing_key_never_calls_api_or_sol(self):
        calls = []
        refresh = IntradayLunaRefresh(api=lambda *_: calls.append(1))
        with patch("intraday_luna.load_key", return_value=""):
            result = refresh("AMD", [{"evidence_id": "E1"}])
        self.assertEqual(result["status"], "INPUTS_UNAVAILABLE")
        self.assertEqual(result["calls"], 0)
        self.assertEqual(result["sol_calls"], 0)
        self.assertEqual(calls, [])

    def test_disabled_never_calls_iex_or_ai(self):
        db = sqlite3.connect(":memory:")
        init_state(db); init_calendar(db)
        class Never:
            def fetch(self, *_):
                raise AssertionError("NETWORK_MUST_NOT_BE_USED")
        result = runner.run_window(at(date(2026, 10, 2), 9, 50), {**CFG, "enabled": False}, db,
                                   iex=Never(), evidence_collector=lambda *_: (_ for _ in ()).throw(AssertionError()))
        self.assertEqual(result["status"], "DISABLED")
        db.close()

    def test_calendar_unverified_does_not_remove_normal_window(self):
        db = sqlite3.connect(":memory:")
        init_state(db); init_calendar(db)
        day = date(2026, 10, 2)
        self.assertFalse(calendar_ready(db, day, CFG, at(day, 9, 50)))
        self.assertIn("09:50", [w["time_et"] for w in schedule(day, events_for_day(db, day, CFG), CFG)])
        db.close()

    def test_normal_no_llm_new_material_only_and_no_sol(self):
        day = date(2026, 10, 2)
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "data").mkdir()
            book = sqlite3.connect(root / "data" / "stock_agent.db")
            book.execute("CREATE TABLE tracked_holdings(ticker TEXT,status TEXT)")
            book.execute("INSERT INTO tracked_holdings VALUES('AMD','HELD')")
            book.commit(); book.close()
            entry = sqlite3.connect(root / "data" / "entry_data.db")
            entry.execute("CREATE TABLE entry_reference_plans(run_id INTEGER,ticker TEXT,target_trading_date TEXT,plan_json TEXT)")
            entry.commit(); entry.close()
            market = sqlite3.connect(root / "data" / "market_data.db")
            market.execute("CREATE TABLE daily_bars(ticker TEXT,date TEXT,open REAL,high REAL,low REAL,close REAL,volume INTEGER)")
            from market_regime import previous_session
            sessions, cursor = [], day
            for _ in range(15):
                cursor = previous_session(cursor)
                sessions.append(cursor)
            for symbol in ("AMD", "SPY", "QQQ"):
                market.executemany("INSERT INTO daily_bars VALUES(?,?,?,?,?,?,?)",
                                   [(symbol, d.isoformat(), 100, 101, 99, 100, 100000)
                                    for d in sessions])
            market.commit(); market.close()
            db = sqlite3.connect(":memory:")
            init_state(db); init_calendar(db)
            class FakeIEX:
                requests = 0
                def fetch(self, tickers, requested_day, now, cfg):
                    self.requests += 1
                    endpoint = now.astimezone(ET)
                    return {s: bars(requested_day, end=(endpoint.hour, endpoint.minute - 1)) for s in tickers}
            class FakeResearch:
                calls = 0
                def __call__(self, *_):
                    self.calls += 1
                    return {"model": "gpt-5.6-luna", "web_search": False, "sol_calls": 0,
                            "status": "DEEP_RESEARCH_PENDING", "escalate": True,
                            "calls": 1, "input_tokens": 100, "output_tokens": 20, "cost_usd": .001}
            research, sent = FakeResearch(), []
            with patch.object(runner, "ROOT", root):
                import_events(db, [{"event": "NFP", "date": day.isoformat(),
                                    "scheduled_time_et": "08:30",
                                    "source": "https://www.bls.gov/schedule/2026/10_sched.htm",
                                    "impact_level": "MATERIAL"}], CFG,
                              at(date(2026, 10, 1), 18, 0))
                macro = runner.run_window(at(day, 9, 35), {**CFG, "enabled": True}, db,
                    iex=FakeIEX(), evidence_collector=lambda *_: ([], 0, "OK"),
                    sender=lambda text: sent.append(text) or 9, research_refresh=research)
                self.assertEqual(macro["status"], "COMPLETE")
                self.assertEqual(macro["luna_calls"], 0)
                no_event = runner.run_window(at(day, 9, 50), {**CFG, "enabled": True}, db,
                    iex=FakeIEX(), evidence_collector=lambda *_: ([], 1, "OK"),
                    sender=lambda text: sent.append(text) or 10, research_refresh=research)
                self.assertEqual(no_event["status"], "COMPLETE")
                self.assertEqual(no_event["calendar_status"], "CALENDAR_EVENT_SCHEDULING_DEGRADED")
                self.assertEqual(no_event["luna_calls"], 0)
                self.assertEqual(research.calls, 0)
                material = {"evidence_id": "SEC-AMD-1", "event_type": "RESTATEMENT",
                            "materiality": "MATERIAL", "headline": "Restatement", "official": True}
                event = runner.run_window(at(day, 12, 15), {**CFG, "enabled": True}, db,
                    iex=FakeIEX(), evidence_collector=lambda *_: ([material], 1, "OK"),
                    sender=lambda text: sent.append(text) or 11, research_refresh=research)
                self.assertEqual(event["status"], "COMPLETE")
                self.assertEqual(event["luna_calls"], 1)
                self.assertEqual(research.calls, 1)
                self.assertEqual(daily_totals(db, day.isoformat())["sol_calls"], 0)
                self.assertEqual(db.execute("SELECT status FROM intraday_research_queue").fetchone()[0],
                                 "DEEP_RESEARCH_PENDING")
                self.assertTrue(any("未执行交易" in text for text in sent))
                repeat = runner.run_window(at(day, 12, 15), {**CFG, "enabled": True}, db,
                    iex=FakeIEX(), evidence_collector=lambda *_: (_ for _ in ()).throw(AssertionError()),
                    research_refresh=research)
                self.assertEqual(repeat["status"], "ALREADY_COMPLETE")
            db.close()


if __name__ == "__main__":
    unittest.main()

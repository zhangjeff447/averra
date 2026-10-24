#!/usr/bin/env python3
"""One launchd invocation: check, claim, run once, send once, then exit."""
from __future__ import annotations

import argparse
import fcntl
import json
import math
import os
import sqlite3
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from quant_screen import ROOT
from shadow_daily_pipeline import (StageFailure, data_ready, market_update,
                                   paths, run_pipeline)
from shadow_daily_state import (CUTOFF_MINUTE, ET, action, begin, claim_telegram, connect, fail,
                                fail_before_run, initialize, pipeline_complete,
                                mark_missed, ready, record, slot, telegram_sent,
                                telegram_uncertain)
from stock_agent import load_env, request_json
from shadow_schedule import authorized_day
from market_regime import previous_session


def render(report):
    day = report["trading_date"]
    if (not authorized_day(day) or
            report["status"] != "PIPELINE_COMPLETE" or report["market_data_as_of"] != day or
            report["market_data_source"] != "ALPACA_SIP" or report["sol_calls"] != 0 or
            report["no_trade_placed"] is not True):
        raise ValueError("DAILY_TELEGRAM_INPUT_INVALID")
    entry = report["entry_counts"]
    groups = {state: [item for item in report["entry_results"] if item["entry_state"] == state]
              for state in ("BUY_TOMORROW_CANDIDATE", "WATCH", "REJECT_ENTRY",
                            "ENTRY_DATA_INSUFFICIENT")}
    if any(len(items) != entry.get(state, 0) for state, items in groups.items()):
        raise ValueError("DAILY_TELEGRAM_ENTRY_COUNT_MISMATCH")
    generated_at = report.get("generated_at")
    if generated_at:
        instant = datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
        if instant.tzinfo is None:
            raise ValueError("DAILY_TELEGRAM_TIME_INVALID")
        time_label = instant.astimezone(ET).strftime("%Y-%m-%d %H:%M ET")
    else:
        time_label = f"{day} 时间未记录"
    def price(value):
        return f"${value:.2f}" if isinstance(value, (int, float)) and math.isfinite(value) and value > 0 else "未提供"

    def zone(values):
        if (isinstance(values, (list, tuple)) and len(values) == 2 and
                all(isinstance(value, (int, float)) and not isinstance(value, bool) and
                    math.isfinite(value) and value > 0 for value in values) and values[0] <= values[1]):
            return f"{price(values[0])}–{price(values[1])}"
        return "UNAVAILABLE — DATA INSUFFICIENT"

    cases = {case["ticker"]: case for case in report.get("eligibility_cases", [])}
    lines = ["AVERRA STOCKS | POST-CLOSE REFERENCE ZONES", f"生成时间：{time_label}",
             f"基于：{day} Close | 适用：{report['target_session']} Session", "",
             "🟢 BUY TOMORROW（非实时买入）"]
    reasons = {"Daily trend supports entry": "日线趋势支持",
               "Outperforming SPY intraday": "盘中跑赢 SPY",
               "Breakout confirmed by volume": "放量突破",
               "VWAP reclaimed": "收复 VWAP",
               "Support bounce": "支撑位反弹",
               "5M timing is weakening": "5 分钟走势转弱"}
    for candidate in groups["BUY_TOMORROW_CANDIDATE"]:
        plan = candidate.get("buy_plan") or {}
        if (plan.get("status") != "CONDITIONAL" or
                plan.get("generated_from_trading_date") != day or
                plan.get("target_trading_date") != report["target_session"] or
                not all(isinstance(plan.get(key), (int, float)) and
                            math.isfinite(plan[key]) and plan[key] > 0 for key in
                            ("buy_zone_low", "buy_zone_high", "do_not_chase_above")) or
                plan["buy_zone_low"] > plan["buy_zone_high"] or
                plan["buy_zone_high"] > plan["do_not_chase_above"]):
            raise ValueError("DAILY_BUY_REFERENCE_INVALID")
        case = cases.get(candidate["ticker"], {})
        conviction = case.get("effective_conviction")
        lines.extend([f"{candidate['ticker']} | Research {candidate.get('research_quality_tier') or case.get('research_quality_tier') or '未提供'}",
                      f"Conviction {conviction if conviction is not None else '未提供'} | Entry Score {candidate.get('entry_score', '未提供')}"])
        why = candidate.get("why_now") or []
        lines.extend(["Why Buy：" + ("；".join(reasons.get(item, item) for item in why[:3]) or "未记录"),
                      f"Preferred Entry Zone：{price(plan['buy_zone_low'])}–{price(plan['buy_zone_high'])}",
                      f"Do Not Chase Above：{price(plan['do_not_chase_above'])}",
                      f"Support / Resistance：{price(plan.get('nearest_support'))} / {price(plan.get('nearest_resistance'))}",
                      f"Chase Risk：{plan.get('chase_risk') or '未提供'}",
                      "Key Risk / Why Wait：" + (reasons.get(candidate["why_wait"][0], candidate["why_wait"][0])
                                                  if candidate.get("why_wait") else "开盘后条件需重新确认"),
                      f"Generated From：{day} Close | For：{report['target_session']} Session",
                      "高于 Do Not Chase Above → WAIT_FOR_REASSESSMENT（未实时复核）", ""])
    if not groups["BUY_TOMORROW_CANDIDATE"]:
        lines.append("无")
    lines.extend(["", "👀 WATCH",
                  "、".join(item["ticker"] for item in groups["WATCH"]) or "无"])
    if groups["ENTRY_DATA_INSUFFICIENT"]:
        lines.append("数据不足待观察：" +
                     "、".join(item["ticker"] for item in groups["ENTRY_DATA_INSUFFICIENT"]))
    holding_decisions = report["portfolio"].get("holding_decisions", [])
    if len(holding_decisions) != report["portfolio"].get("tracked_count", len(holding_decisions)):
        raise ValueError("DAILY_TELEGRAM_HOLDING_COUNT_MISMATCH")
    sell_actions = {"EXIT_CANDIDATE": "卖出候选", "REDUCE_CANDIDATE": "减仓候选",
                    "TRIM_CANDIDATE": "止盈候选"}
    hold = [item for item in holding_decisions if item["action"] not in sell_actions]
    sell = [item for item in holding_decisions if item["action"] in sell_actions]
    lines.extend(["", "⏸ HOLD / POSITION REVIEW"])
    def add_position(item):
        pos = item.get("position") or {}
        entry_price = pos.get("entry_price")
        current = pos.get("current_price")
        returned = pos.get("return_since_entry_pct")
        profit_text = ("UNAVAILABLE — ENTRY PRICE UNKNOWN" if entry_price is None else
                       zone(item.get("profit_protection_zone")))
        lines.extend([f"{item['ticker']} | {sell_actions.get(item['action'], item['action'])}",
                      f"Entry {price(entry_price)} | Current {price(current)} | Return " +
                      (f"{returned:+.2f}%" if isinstance(returned, (int, float)) else "未提供"),
                      f"Profit Protection Reference：{profit_text}",
                      f"Trend Failure Reference：{zone(item.get('trend_failure_zone'))}",
                      f"Trend {(item.get('trend') or {}).get('state', '未提供')} | Thesis {(item.get('thesis') or {}).get('state', '未提供')} | Exit Score {item.get('exit_score') if item.get('exit_score') is not None else '未提供'}",
                      "Reason：" + "、".join(item.get("reasons") or [item.get("reason", "未记录")]), ""])
        if item["action"] in sell_actions and item.get("sell_review_zone"):
            lines.insert(len(lines) - 1, f"Action Review Zone：{zone(item['sell_review_zone'])}")
    for item in hold:
        add_position(item)
    if not hold:
        lines.append("无")
    lines.extend(["", "🔴 SELL / REDUCE（人工复核）"])
    for item in sell:
        add_position(item)
    if not sell:
        lines.append("无")
    lines.extend(["", "参考区间不是实时信号、止损单或保证成交价格。",
                  "用户自行交易；买入后用 /bought TICKER PRICE 确认，卖出后用 /sold TICKER PRICE 确认。",
                  "No trade was placed."])
    message = "\n".join(lines)
    if len(message) > 3900:
        raise ValueError("DAILY_TELEGRAM_TOO_LONG")
    return message


def send_only(db, day, path):
    report = json.loads(path.read_text())
    message = render(report)
    load_env(ROOT / ".env")
    token = os.environ.get("STOCK_TELEGRAM_BOT_TOKEN") or os.environ.get("STOCK_TELEGRAM_TOKEN")
    chat_id = os.environ.get("STOCK_TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        raise ValueError("STOCK_BOT_CREDENTIALS_MISSING")
    key = claim_telegram(db, day, path)
    try:
        response = request_json(f"https://api.telegram.org/bot{token}/sendMessage",
                                {"chat_id": chat_id, "text": message}, retry=False)
        message_id = (response.get("result") or {}).get("message_id") if response.get("ok") else None
        if not isinstance(message_id, int) or message_id <= 0:
            raise ValueError("TELEGRAM_SEND_UNCONFIRMED")
    except Exception as exc:
        telegram_uncertain(db, day, key)
        raise RuntimeError("TELEGRAM_SEND_UNKNOWN:" + type(exc).__name__) from None
    telegram_sent(db, day, key, message_id)
    return message_id


def run_once(at=None, *, state_path=None, updater=market_update,
             gate=data_ready, pipeline=run_pipeline, sender=send_only):
    injected_at = at
    at = at or datetime.now(timezone.utc)
    due = slot(at)
    if not due:
        eastern = at.astimezone(ET)
        day = eastern.date().isoformat()
        if authorized_day(day) and eastern.hour * 60 + eastern.minute >= CUTOFF_MINUTE:
            with connect(state_path) if state_path else connect() as db:
                item = initialize(db, day)
                if action(item) == "TELEGRAM_ONLY":
                    message_id = sender(db, day, paths(day) / "final.json")
                    return {"status": "COMPLETE", "day": day, "message_id": message_id}
                if action(item) in {"WAITING_FOR_DATA", "READY"}:
                    item = mark_missed(db, day, at)
                return {"status": action(item), "day": day}
        return {"status": "OUTSIDE_APPROVED_SHADOW_SLOTS"}
    day = due["date"]
    with connect(state_path) if state_path else connect() as db:
        item = initialize(db, day)
        decision = action(item)
        if decision in {"ALREADY_COMPLETE", "FAILED_STOP", "RUNNING_STOP",
                        "TELEGRAM_RECONCILE", "MISSED_DATA_WINDOW"}:
            return {"status": decision, "day": day}
        if decision == "TELEGRAM_ONLY":
            message_id = sender(db, day, paths(day) / "final.json")
            return {"status": "COMPLETE", "day": day, "message_id": message_id}
        try:
            market = updater(day)
        except RuntimeError as exc:
            if "ALPACA_DATA_INCOMPLETE" in str(exc) or "ALPACA_NETWORK_ERROR" in str(exc):
                ready(db, day, False)
                return {"status": "WAITING_FOR_DATA", "day": day,
                        "due_slot_et": due["due_slot_et"], "reason": "ALPACA_DAILY_NOT_COMPLETE"}
            fail_before_run(db, day, "MARKET_UPDATE", exc)
            return {"status": "FAILED", "day": day, "failed_phase": "MARKET_UPDATE"}
        except Exception as exc:
            fail_before_run(db, day, "MARKET_UPDATE", exc)
            return {"status": "FAILED", "day": day, "failed_phase": "MARKET_UPDATE"}
        if not gate(day, at):
            ready(db, day, False)
            return {"status": "WAITING_FOR_DATA", "day": day,
                    "due_slot_et": due["due_slot_et"], "reason": "DATE_GATE_NOT_READY"}
        ready(db, day, True)
        started = begin(db, day, injected_at)
        if started["state"] == "MISSED_DATA_WINDOW":
            return {"status": "MISSED_DATA_WINDOW", "day": day}
        try:
            if pipeline is run_pipeline:
                def progress(phase):
                    db.execute("INSERT OR REPLACE INTO shadow_daily_progress VALUES(?,?,?)",
                               (day, phase, datetime.now(timezone.utc).isoformat()))
                path, report = pipeline(day, market, progress=progress)
            else:
                path, report = pipeline(day, market)
            pipeline_complete(db, day, path, {"luna_calls": report["luna_calls"],
                "input_tokens": report["luna_input_tokens"],
                "output_tokens": report["luna_output_tokens"],
                "estimated_cost_usd": report["total_ai_cost_local_estimate_usd"],
                "sol_calls": 0})
        except Exception as exc:
            phase = exc.phase if isinstance(exc, StageFailure) else "PIPELINE"
            fail(db, day, phase, exc)
            return {"status": "FAILED", "day": day, "failed_phase": phase,
                    "error_type": type(exc).__name__}
        message_id = sender(db, day, path)
        return {"status": "COMPLETE", "day": day, "message_id": message_id,
                "luna_calls": report["luna_calls"],
                "estimated_ai_cost_usd": report["total_ai_cost_local_estimate_usd"]}


@contextmanager
def execution_lock(path=None):
    """Held across update, paid work and delivery; released by the OS on exit."""
    path = path or ROOT / "data" / "shadow_daily_execution.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def alert_once(db, day, reason):
    """A separate operational alert, never a repeat of the daily summary."""
    key = f"SHADOW_DAILY:{day}:ALERT:{reason}"
    if db.execute("SELECT 1 FROM shadow_monitor_alerts WHERE alert_key=?", (key,)).fetchone():
        return
    load_env(ROOT / ".env")
    token = os.environ.get("STOCK_TELEGRAM_BOT_TOKEN") or os.environ.get("STOCK_TELEGRAM_TOKEN")
    chat = os.environ.get("STOCK_TELEGRAM_CHAT_ID")
    if not token or not chat:
        raise ValueError("STOCK_BOT_CREDENTIALS_MISSING")
    item = record(db, day)
    phase = item.get("failed_phase") or item.get("telegram_status") or "CHECK"
    message = (f"AVERRA STOCKS | MONITOR ALERT\nDate: {day}\nStatus: {reason}\n"
               f"Phase: {phase}\nThis daily run is stopped pending review.\n"
               "Saved research and portfolio are preserved. No trade was placed.")
    db.execute("INSERT INTO shadow_monitor_alerts VALUES(?,?,?,'IN_FLIGHT',?,NULL,NULL)",
               (key, day, reason, datetime.now(timezone.utc).isoformat()))
    try:
        response = request_json(f"https://api.telegram.org/bot{token}/sendMessage",
                                {"chat_id": chat, "text": message}, retry=False)
        mid = (response.get("result") or {}).get("message_id") if response.get("ok") else None
        if not isinstance(mid, int) or mid <= 0:
            raise ValueError("TELEGRAM_ALERT_UNCONFIRMED")
        db.execute("UPDATE shadow_monitor_alerts SET status='SENT',message_id=? WHERE alert_key=?", (mid, key))
    except Exception as exc:
        db.execute("UPDATE shadow_monitor_alerts SET status='UNKNOWN',error_type=? WHERE alert_key=?",
                   (type(exc).__name__, key))
        # The request may have reached Telegram. Never replay an unknown send.


def monitored_tick(at=None, *, state_path=None, runner=run_once, notifier=alert_once):
    """One-minute local wake; network work occurs at most once per approved slot."""
    at = at or datetime.now(timezone.utc)
    with connect(state_path) if state_path else connect() as db:
        today = at.astimezone(ET).date()
        prior_day = previous_session(today).isoformat()
        latest = db.execute("SELECT MAX(trading_date) FROM shadow_daily_runs").fetchone()[0]
        if latest and latest < prior_day and authorized_day(prior_day):
            initialize(db, prior_day)
        overdue = db.execute("SELECT trading_date FROM shadow_daily_runs WHERE trading_date<? AND state IN ('WAITING_FOR_DATA','READY')",
                             (today.isoformat(),)).fetchall()
        for row in overdue:
            db.execute("UPDATE shadow_daily_runs SET state='MISSED_DATA_WINDOW',missed_at=? WHERE trading_date=?",
                       (at.isoformat(), row[0]))
            notifier(db, row[0], 'MISSED_DATA_WINDOW')
        # Caller holds the process lock. A RUNNING record here has lost its owner.
        for row in db.execute("SELECT trading_date FROM shadow_daily_runs WHERE state='RUNNING'").fetchall():
            day = row[0]
            progress = db.execute("SELECT phase FROM shadow_daily_progress WHERE trading_date=?", (day,)).fetchone()
            fail(db, day, progress[0] if progress else "PROCESS_INTERRUPTED",
                 RuntimeError("PROCESS_INTERRUPTED_MANUAL_REVIEW_REQUIRED"))
        blockers = db.execute("""SELECT * FROM shadow_daily_runs WHERE state='FAILED' OR
            (state='PIPELINE_COMPLETE_TELEGRAM_PENDING' AND telegram_status<>'PENDING')
            ORDER BY trading_date""").fetchall()
        if blockers:
            for item in blockers:
                notifier(db, item["trading_date"], action(dict(item)))
            return {"status": "FAILED_STOP", "day": blockers[0]["trading_date"]}
        # A settled pipeline can deliver its saved summary after cutoff/reboot.
        pending = db.execute("SELECT trading_date,result_path FROM shadow_daily_runs WHERE state='PIPELINE_COMPLETE_TELEGRAM_PENDING' AND telegram_status='PENDING'").fetchone()
        if pending:
            try:
                mid = send_only(db, pending[0], Path(pending[1]))
            except Exception:
                notifier(db, pending[0], "TELEGRAM_RECONCILE")
                return {"status": "TELEGRAM_RECONCILE", "day": pending[0]}
            return {"status": "COMPLETE", "day": pending[0], "message_id": mid}
        eastern = at.astimezone(ET)
        day = eastern.date().isoformat()
        if not authorized_day(day):
            return {"status": "MARKET_CLOSED"}
        item = record(db, day)
        if item and item["state"] == "COMPLETE":
            return {"status": "ALREADY_COMPLETE", "day": day}
        due = slot(at)
        if not due:
            if eastern.hour * 60 + eastern.minute < CUTOFF_MINUTE:
                return {"status": "OUTSIDE_APPROVED_SHADOW_SLOTS"}
            item = initialize(db, day)
            if item["state"] in {"READY", "WAITING_FOR_DATA"}:
                item = mark_missed(db, day, at)
            notifier(db, day, action(item))
            return {"status": action(item), "day": day}
        slot_key = due["due_slot_et"]
        if db.execute("SELECT 1 FROM shadow_daily_checks WHERE trading_date=? AND slot_et=?", (day, slot_key)).fetchone():
            return {"status": "SLOT_ALREADY_CHECKED", "day": day}
        db.execute("INSERT INTO shadow_daily_checks VALUES(?,?,?,NULL,'CHECKING')",
                   (day, slot_key, at.isoformat()))
        try:
            kwargs = {"state_path": state_path} if state_path else {}
            result = runner(at if state_path else None, **kwargs)
        except Exception as exc:
            item = record(db, day)
            if item and item["state"] == "RUNNING":
                fail(db, day, "UNHANDLED", RuntimeError(type(exc).__name__))
            elif item and item["state"] in {"READY", "WAITING_FOR_DATA"}:
                fail_before_run(db, day, "PRECHECK", RuntimeError(type(exc).__name__))
            result = {"status": "FAILED", "day": day, "error_type": type(exc).__name__}
        db.execute("UPDATE shadow_daily_checks SET finished_at=?,status=? WHERE trading_date=? AND slot_et=?",
                   (datetime.now(timezone.utc).isoformat(), result["status"], day, slot_key))
        if result["status"] in {"FAILED", "FAILED_STOP", "MISSED_DATA_WINDOW", "TELEGRAM_RECONCILE"}:
            notifier(db, day, "FAILED_STOP" if result["status"] == "FAILED" else result["status"])
        return result


def exit_status(result):
    return 1 if result.get("status") in {"FAILED", "FAILED_STOP", "MISSED_DATA_WINDOW", "TELEGRAM_RECONCILE"} else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--status", action="store_true", help="read-only; never access market/API")
    args = parser.parse_args()
    if args.status:
        with connect() as db:
            out = {r["trading_date"]: action(dict(r)) for r in db.execute("SELECT * FROM shadow_daily_runs ORDER BY trading_date")}
    else:
        with execution_lock() as acquired:
            out = monitored_tick() if acquired else {"status": "RUNNING_LOCK_HELD"}
        log = ROOT / "data" / "shadow_daily_runs.jsonl"
        with log.open("a") as handle:
            handle.write(json.dumps({"checked_at": datetime.now(timezone.utc).isoformat(),
                                     **out}, sort_keys=True) + "\n")
    print(json.dumps(out, sort_keys=True))
    return 0 if args.status else exit_status(out)


if __name__ == "__main__":
    sys.exit(main())

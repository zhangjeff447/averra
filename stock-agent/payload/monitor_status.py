#!/usr/bin/env python3
"""Read-only local monitor health. Telegram checks/sends require explicit flags."""
from __future__ import annotations

import argparse
import json
import os
import plistlib
import re
import sqlite3
import subprocess
import sys
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from market_history import check_recent_history
from phase4_tiers import cost_report
from quant_screen import ROOT
from shadow_schedule import authorized_day
from stock_agent import load_env, request_json, telegram_send

ET = ZoneInfo("America/New_York")
LABELS = ("com.averra.stock.shadow-v11", "com.averra.stock.commands")
READY_KEY = "STOCK_MONITOR_V1_ONGOING_READY"


def read_db(path):
    return closing(sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True))


def service_status(label, launch_dir=None):
    installed = (launch_dir or Path.home() / "Library" / "LaunchAgents") / (label + ".plist")
    out = {"label": label, "installed": installed.is_file(), "loaded": False}
    if installed.is_file():
        spec = plistlib.loads(installed.read_bytes())
        # Do not include launchd's raw output or EnvironmentVariables.
        out.update({"project_path": spec.get("WorkingDirectory"),
                    "stdout": spec.get("StandardOutPath"), "stderr": spec.get("StandardErrorPath"),
                    "wake_interval_seconds": spec.get("StartInterval"),
                    "keep_alive": spec.get("KeepAlive")})
    result = subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/{label}"],
                            capture_output=True, text=True, timeout=10)
    out["loaded"] = result.returncode == 0
    if out["loaded"]:
        for field in ("state", "pid", "last exit code"):
            match = re.search(r"^\s*" + re.escape(field) + r" = ([A-Za-z0-9 _-]+)$", result.stdout, re.M)
            if match:
                value = match.group(1).strip()
                out[field.replace(" ", "_")] = int(value) if value.lstrip("-").isdigit() else value
    return out


def next_data_check(at, states):
    at = at.astimezone(ET)
    for offset in range(367):
        day = at.date() + timedelta(days=offset)
        if not authorized_day(day.isoformat()) or states.get(day.isoformat()) in {"COMPLETE", "FAILED", "MISSED_DATA_WINDOW"}:
            continue
        for hour, minute in ((16, 20), (16, 35), (16, 50), (17, 5)):
            target = datetime(day.year, day.month, day.day, hour, minute, tzinfo=ET)
            if target > at:
                return target.isoformat()
    return None


def snapshot(root=ROOT, at=None, service_reader=service_status):
    at = at or datetime.now(timezone.utc)
    root = Path(root)
    policy = json.loads((root / "shadow_v11_config.json").read_text())
    models = json.loads((root / "phase4_tiers_config.json").read_text())
    report = {"checked_at": at.isoformat(), "checked_at_et": at.astimezone(ET).isoformat(),
              "read_only": True, "network_requests": 0,
              "services": [service_reader(label) for label in LABELS],
              "policy": {"initial_model": models["initial_research_model"],
                         "deep_model": models["deep_research_model"], "automatic_sol_calls": 0,
                         "web_search": models["web_search_enabled"], "buy_now": False,
                         "daily_initial_calls": policy["max_daily_initial_calls"],
                         "daily_initial_cost_usd": policy["max_daily_initial_cost_usd"],
                         "daily_initial_input_tokens": policy["max_daily_initial_input_tokens"],
                         "daily_initial_output_tokens": policy["max_daily_initial_output_tokens"]},
              "issues": []}
    states = {}
    try:
        with read_db(root / "data" / "shadow_daily_state.db") as db:
            db.row_factory = sqlite3.Row
            runs = [dict(r) for r in db.execute("""SELECT trading_date,state,started_at,completed_at,
                market_data_as_of,failed_phase,error_type,telegram_status,telegram_message_id
                FROM shadow_daily_runs ORDER BY trading_date""")]
        states = {r["trading_date"]: r["state"] for r in runs}
        report["daily_runs"] = runs
        for row in runs:
            if row["state"] == "FAILED" or (row["state"] == "PIPELINE_COMPLETE_TELEGRAM_PENDING" and row["telegram_status"] != "PENDING"):
                report["issues"].append(f"DAILY_BLOCKER:{row['trading_date']}:{row['state']}")
    except (sqlite3.Error, OSError):
        report["issues"].append("DAILY_STATE_UNAVAILABLE")
    report["next_expected_data_check_et"] = next_data_check(at, states)
    report["next_expected_data_check_note"] = "Local one-minute wake checks due slots; actual launch requires Mac awake/logged in."
    report["execution_blocked"] = any(issue.startswith("DAILY_BLOCKER:") for issue in report["issues"])
    if report["execution_blocked"]:
        report["next_expected_data_check_note"] = "Scheduled check time only; unresolved daily failure/delivery blocks execution pending review."
    try:
        with read_db(root / "data" / "stock_agent.db") as db:
            holding_count = db.execute("SELECT COUNT(*) FROM tracked_holdings WHERE status='HELD'").fetchone()[0]
            poll = db.execute("SELECT value FROM runtime_state WHERE key='telegram_last_poll_at'").fetchone()
        heartbeat = poll[0] if poll else None
        age = (at - datetime.fromisoformat(heartbeat)).total_seconds() if heartbeat else None
        report["tracked_holdings"] = holding_count
        report["listener"] = {"last_successful_poll_at": heartbeat,
                              "heartbeat_age_seconds": round(age, 1) if age is not None else None,
                              "heartbeat_fresh": age is not None and -30 <= age <= 180}
        if not report["listener"]["heartbeat_fresh"]:
            report["issues"].append("COMMAND_LISTENER_HEARTBEAT_STALE")
    except (sqlite3.Error, OSError, ValueError):
        report["issues"].append("HOLDINGS_OR_LISTENER_STATE_UNAVAILABLE")
    try:
        with read_db(root / "data" / "market_data.db") as db:
            latest = db.execute("SELECT MAX(date) FROM daily_bars WHERE ticker='SPY'").fetchone()[0]
            report["market_latest_trading_date"] = latest
            history = check_recent_history(db, latest)
            report["recent_history"] = {k: history[k] for k in ("status", "asof", "baseline_symbols", "missing_sessions", "minimum_coverage")}
            report["market_context_dates"] = dict(db.execute("SELECT ticker,MAX(date) FROM daily_bars WHERE ticker IN ('SPY','QQQ','XLK','XLF','XLV','XLY','XLP','XLI','XLE','XLU','XLB','XLRE','XLC') GROUP BY ticker"))
            report["volatility_dates"] = dict(db.execute("SELECT symbol,MAX(date) FROM volatility_daily GROUP BY symbol"))
        if history["status"] != "HISTORY_READY":
            report["issues"].append("RECENT_MARKET_HISTORY_INCOMPLETE")
    except (sqlite3.Error, OSError, ValueError, TypeError):
        report["issues"].append("MARKET_HISTORY_UNAVAILABLE")
    try:
        with read_db(root / "data" / "research_data.db") as db:
            report["ai_budget_utc_day"] = at.astimezone(timezone.utc).date().isoformat()
            report["ai_budget"] = cost_report(db, report["ai_budget_utc_day"],
                {"max_daily_total_ai_cost": policy["max_daily_initial_cost_usd"]})
        report["cost_note"] = "Confirmed usage cost is locally estimated; pending/unknown are separate reservations, not confirmed provider spend."
    except (sqlite3.Error, OSError):
        report["issues"].append("AI_BUDGET_LEDGER_UNAVAILABLE")
    for service in report["services"]:
        if not service["installed"] or not service["loaded"]:
            report["issues"].append("SERVICE_NOT_LOADED:" + service["label"])
    report["status"] = "MONITOR_READY" if not report["issues"] else "MONITOR_NOT_READY"
    return report


def telegram_check(root=ROOT, requester=request_json):
    load_env(Path(root) / ".env")
    token = os.environ.get("STOCK_TELEGRAM_BOT_TOKEN") or os.environ.get("STOCK_TELEGRAM_TOKEN")
    chat = os.environ.get("STOCK_TELEGRAM_CHAT_ID")
    if not token or not chat:
        raise ValueError("STOCK_TELEGRAM_CREDENTIALS_MISSING")
    base = f"https://api.telegram.org/bot{token}/"
    me = requester(base + "getMe", {}, retry=False)
    bot = me.get("result") or {}
    if not me.get("ok") or not bot.get("is_bot") or bot.get("username", "").lower() != "averrastockagentbot":
        raise ValueError("STOCK_TELEGRAM_BOT_IDENTITY_MISMATCH")
    webhook = requester(base + "getWebhookInfo", {}, retry=False)
    if not webhook.get("ok") or (webhook.get("result") or {}).get("url"):
        raise ValueError("TELEGRAM_WEBHOOK_CONFLICT")
    data = requester(base + "getChat", {"chat_id": chat}, retry=False)
    target = data.get("result") or {}
    if not data.get("ok") or str(target.get("id")) != str(chat) or target.get("type") != "private":
        raise ValueError("STOCK_TELEGRAM_CHAT_MISMATCH")
    return {"status": "TELEGRAM_IDENTITY_VERIFIED", "username": bot["username"],
            "bot_id": bot["id"], "chat_id": target["id"], "webhook_present": False,
            "network_requests": 3}


def notify_ready(report, root=ROOT, *, checker=telegram_check, sender=telegram_send):
    path = Path(root) / "data" / "stock_agent.db"
    if not path.is_file():
        raise ValueError("FORMAL_PORTFOLIO_DATABASE_MISSING")
    with closing(sqlite3.connect(path, timeout=30, isolation_level=None)) as db:
        db.execute("""CREATE TABLE IF NOT EXISTS monitor_ready_receipts
            (idempotency_key TEXT PRIMARY KEY,state TEXT NOT NULL,claimed_at TEXT NOT NULL,
             completed_at TEXT,message_id INTEGER,error_type TEXT)""")
        prior = db.execute("SELECT state,message_id FROM monitor_ready_receipts WHERE idempotency_key=?", (READY_KEY,)).fetchone()
        if prior:
            return {"status": "ALREADY_" + prior[0], "message_id": prior[1], "network_requests": 0}
        if report.get("status") != "MONITOR_READY":
            raise ValueError("MONITOR_NOT_READY_SEND_BLOCKED")
        identity = checker(root)
        token = os.environ.get("STOCK_TELEGRAM_BOT_TOKEN") or os.environ.get("STOCK_TELEGRAM_TOKEN")
        chat = os.environ.get("STOCK_TELEGRAM_CHAT_ID")
        message = ("AVERRA STOCKS | AUTOMATIC MONITOR READY\n"
                   "Daily checks: 16:20 / 16:35 / 16:50 / 17:05 America/New_York\n"
                   f"Next expected check: {report['next_expected_data_check_et']}\n"
                   "Telegram commands are active. One completed daily summary per session.\n"
                   "Initial: GPT-5.6 Luna, at most 10 calls / $0.05 daily. Web Search OFF.\n"
                   "Sol: plan only. BUY NOW: OFF.\n"
                   f"Tracked holdings: {report['tracked_holdings']}\n"
                   "Requires this Mac powered on and logged in; no open Terminal required.\n"
                   "No trade was placed.")
        claimed = db.execute("INSERT OR IGNORE INTO monitor_ready_receipts VALUES(?,'IN_FLIGHT',?,NULL,NULL,NULL)",
                             (READY_KEY, datetime.now(timezone.utc).isoformat())).rowcount
        if not claimed:
            prior = db.execute("SELECT state,message_id FROM monitor_ready_receipts WHERE idempotency_key=?", (READY_KEY,)).fetchone()
            return {"status": "ALREADY_" + prior[0], "message_id": prior[1], "network_requests": 3}
        try:
            mid = sender(token, chat, message)
            if not isinstance(mid, int) or isinstance(mid, bool) or mid <= 0:
                raise ValueError("TELEGRAM_READY_SEND_UNCONFIRMED")
        except Exception as exc:
            db.execute("UPDATE monitor_ready_receipts SET state='UNKNOWN',completed_at=?,error_type=? WHERE idempotency_key=?",
                       (datetime.now(timezone.utc).isoformat(), type(exc).__name__, READY_KEY))
            return {"status": "UNKNOWN", "error_type": type(exc).__name__, "network_requests": 4}
        db.execute("UPDATE monitor_ready_receipts SET state='SENT',completed_at=?,message_id=? WHERE idempotency_key=?",
                   (datetime.now(timezone.utc).isoformat(), mid, READY_KEY))
        return {"status": "SENT", "message_id": mid, "identity": identity, "network_requests": 4}


def exit_status(report):
    if report.get("status") != "MONITOR_READY":
        return 1
    notification = report.get("ready_notification")
    return int(notification is not None and notification.get("status") not in {"SENT", "ALREADY_SENT"})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--telegram-check", action="store_true", help="Explicit read-only Telegram identity/webhook check")
    parser.add_argument("--notify-ready", action="store_true", help="Send one idempotent operational readiness message")
    parser.add_argument("--output", type=Path, help="Optionally save the sanitized JSON report")
    args = parser.parse_args()
    try:
        report = snapshot()
        if args.telegram_check:
            report["telegram_check"] = telegram_check()
            report["network_requests"] += report["telegram_check"]["network_requests"]
        if args.notify_ready:
            report["ready_notification"] = notify_ready(report)
            report["network_requests"] += report["ready_notification"]["network_requests"]
            report["read_only"] = False
    except Exception as exc:
        report = {"status": "CHECK_FAILED", "error_type": type(exc).__name__,
                  "reason": "Operational check failed; secret details suppressed"}
    if args.output:
        args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    return exit_status(report)


if __name__ == "__main__":
    sys.exit(main())

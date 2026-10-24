"""Local position notifications; the daily scheduler remains unchanged."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from exit_engine import configuration, sessions_between

PRIORITY = {"EXIT_CANDIDATE": "CRITICAL", "THESIS_REVIEW": "HIGH",
            "REDUCE_CANDIDATE": "HIGH", "TRIM_CANDIDATE": "MEDIUM",
            "HOLD_TIGHT": "MEDIUM", "HOLD": "INFO",
            "HOLD_DATA_INSUFFICIENT": "INFO"}
SEVERITY = {"HOLD": 0, "HOLD_TIGHT": 1, "TRIM_CANDIDATE": 2,
            "THESIS_REVIEW": 3, "REDUCE_CANDIDATE": 3, "EXIT_CANDIDATE": 4}
PROFIT_RANK = {"LOW_PROFIT": 0, "NORMAL_PROFIT": 1, "PROFIT_PROTECTION": 2,
               "STRONG_PROFIT_PROTECTION": 3, "PARABOLIC": 4}
TREND_RANK = {"INTACT": 0, "EMA9_WARNING": 1, "EMA21_BREAKDOWN": 2,
              "SMA50_BREAKDOWN": 3, "MAJOR_BREAKDOWN": 4}


def _stamp():
    return datetime.now(timezone.utc).isoformat()


def _number(value, *, percent=False):
    return (f"{value:+.2f}%" if percent else f"${value:,.2f}") if isinstance(value, (int, float)) else "unavailable"


def position_summary_text(result):
    """Compact report field consumed by the existing one-message Shadow renderer."""
    pos = result.get("position") or {}
    trend = (result.get("trend") or {}).get("state", "unavailable")
    return (f"{result.get('reason', 'REVIEW')} | Entry {_number(pos.get('entry_price'))} | "
            f"Current {_number(pos.get('current_price'))} | "
            f"Return {_number(pos.get('return_since_entry_pct'), percent=True)} | Trend {trend}")


def _worsened(current, previous, cfg):
    old = previous or {}
    if current["action"] == "HOLD_TIGHT":
        current_pos, old_pos = current.get("position") or {}, old.get("position") or {}
        current_dd, old_dd = current_pos.get("drawdown_from_peak_pct"), old_pos.get("drawdown_from_peak_pct")
        if (isinstance(current_dd, (int, float)) and isinstance(old_dd, (int, float)) and
                old_dd - current_dd >= cfg["hold_tight_drawdown_increase_pct"]):
            return "PEAK_DRAWDOWN_INCREASED"
        if PROFIT_RANK.get(current_pos.get("profit_state"), -1) > PROFIT_RANK.get(old_pos.get("profit_state"), -1):
            return "PROFIT_PROTECTION_TIGHTENED"
        if (current.get("event") or {}).get("state") != (old.get("event") or {}).get("state"):
            return "EVENT_RISK_CHANGED"
        return None
    old_score, new_score = old.get("exit_score"), current.get("exit_score")
    if (isinstance(new_score, (int, float)) and isinstance(old_score, (int, float)) and
            new_score - old_score >= cfg["trim_reduce_score_increase"]):
        return "EXIT_SCORE_INCREASED"
    if set(current.get("reasons") or []) - set(old.get("reasons") or []):
        return "NEW_EXIT_REASON"
    if TREND_RANK.get((current.get("trend") or {}).get("state"), -1) > TREND_RANK.get(
            (old.get("trend") or {}).get("state"), -1):
        return "TREND_WORSENED"
    if (current.get("thesis") or {}).get("state") != (old.get("thesis") or {}).get("state"):
        return "THESIS_CHANGED"
    return None


def plan_alert(db, lifecycle_id, trading_date, result, cfg=None):
    """Persist a transition and at most one pending highlight per lifecycle/day."""
    cfg = (cfg or configuration())["notification"]
    ticker, state = result["ticker"], result["action"]
    prior = db.execute("""SELECT details_json FROM exit_decisions WHERE ticker=? AND lifecycle_id=?
        AND trading_date<? ORDER BY trading_date DESC LIMIT 1""",
        (ticker, lifecycle_id, trading_date)).fetchone()
    previous = json.loads(prior[0]) if prior else None
    old_state = previous["action"] if previous else None
    if old_state != state and state in PRIORITY:
        db.execute("""INSERT OR IGNORE INTO exit_state_transitions
            (lifecycle_id,trading_date,ticker,previous_state,exit_state,exit_score,reasons_json,close_price,recorded_at)
            VALUES(?,?,?,?,?,?,?,?,?)""",
            (lifecycle_id, trading_date, ticker, old_state, state, result.get("exit_score"),
             json.dumps(result.get("reasons") or []), result.get("close_price"), _stamp()))
    if state not in SEVERITY or db.execute("""SELECT 1 FROM exit_highlight_alerts WHERE lifecycle_id=?
        AND status IN ('PENDING','UNKNOWN','IN_FLIGHT') LIMIT 1""", (lifecycle_id,)).fetchone():
        return None
    kind = None
    if old_state != state:
        if state == "HOLD" and old_state is None:
            return None
        if old_state in SEVERITY and SEVERITY[state] < SEVERITY[old_state]:
            if cfg["recovery_alert_enabled"]:
                kind = "POSITION_STATUS_IMPROVED"
        elif state != "HOLD":
            kind = "STATE_CHANGED"
    elif state in {"EXIT_CANDIDATE", "THESIS_REVIEW"}:
        last = db.execute("""SELECT trading_date FROM exit_highlight_alerts WHERE lifecycle_id=?
            AND exit_state=? AND status='SENT' ORDER BY trading_date DESC LIMIT 1""",
            (lifecycle_id, state)).fetchone()
        interval = (cfg["exit_reminder_trading_days"] if state == "EXIT_CANDIDATE" else
                    cfg["thesis_review_reminder_trading_days"])
        if not last or sessions_between(last[0], trading_date) >= interval:
            kind = "CRITICAL_REMINDER"
    elif state in {"HOLD_TIGHT", "TRIM_CANDIDATE", "REDUCE_CANDIDATE"}:
        kind = _worsened(result, previous, cfg)
    if not kind:
        return None
    reason_hash = hashlib.sha256(json.dumps(result.get("reasons") or [], sort_keys=True).encode()).hexdigest()[:16]
    key = f"EXIT:{lifecycle_id}:{ticker}:{trading_date}:{state}:{reason_hash}"
    priority = "INFO" if kind == "POSITION_STATUS_IMPROVED" else PRIORITY[state]
    lines = [f"AVERRA STOCKS | POSITION HIGHLIGHT | {priority}",
             f"{ticker}: {old_state or 'NEW_POSITION'} -> {state}",
             f"Date: {trading_date} | Trigger: {kind}",
             f"Exit Score: {result.get('exit_score', 'unavailable')}",
             f"Current: {_number(result.get('close_price'))}",
             f"Reason: {', '.join(result.get('reasons') or []) or 'unavailable'}"]
    if result.get("profit_protection_zone"):
        lines.append(f"Profit protection reference: {result['profit_protection_zone']}")
    if result.get("trend_failure_zone"):
        lines.append(f"Trend failure reference: {result['trend_failure_zone']}")
    lines.append("Reference zones only. No trade was placed.")
    db.execute("""INSERT OR IGNORE INTO exit_highlight_alerts
        (alert_key,lifecycle_id,ticker,trading_date,exit_state,reason_hash,alert_kind,priority,message_text,status)
        VALUES(?,?,?,?,?,?,?,?,?,'PENDING')""",
        (key, lifecycle_id, ticker, trading_date, state, reason_hash, kind, priority, "\n".join(lines)))
    return key


def dispatch_alerts(book, state_path, token, chat_id, sender):
    """Only after confirmed daily summary. Unknown sends are never retried."""
    path = Path(state_path)
    if not path.is_file():
        return 0
    sent = 0
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as state_db:
        pending = book.db.execute("""SELECT alert_key,lifecycle_id,ticker,trading_date,message_text
            FROM exit_highlight_alerts WHERE status='PENDING'
            ORDER BY trading_date,CASE priority WHEN 'CRITICAL' THEN 0 WHEN 'HIGH' THEN 1
              WHEN 'MEDIUM' THEN 2 ELSE 3 END,ticker""").fetchall()
        for item in pending:
            ready = state_db.execute("""SELECT 1 FROM shadow_daily_runs
                WHERE trading_date=? AND state='COMPLETE' AND telegram_status='SENT'
                AND telegram_message_id IS NOT NULL""", (item["trading_date"],)).fetchone()
            if not ready:
                continue
            holding = book.db.execute("SELECT status,confirmed_at FROM tracked_holdings WHERE ticker=?",
                                      (item["ticker"],)).fetchone()
            if not holding or holding["status"] != "HELD" or holding["confirmed_at"] != item["lifecycle_id"]:
                with book.atomic():
                    book.db.execute("UPDATE exit_highlight_alerts SET status='CANCELLED' WHERE alert_key=? AND status='PENDING'",
                                    (item["alert_key"],))
                continue
            with book.atomic():
                claimed = book.db.execute("""UPDATE exit_highlight_alerts SET status='IN_FLIGHT',attempted_at=?
                    WHERE alert_key=? AND status='PENDING'""", (_stamp(), item["alert_key"])).rowcount
            if not claimed:
                continue
            try:
                message_id = sender(token, chat_id, item["message_text"])
                if not isinstance(message_id, int) or isinstance(message_id, bool) or message_id <= 0:
                    raise ValueError("TELEGRAM_SEND_UNCONFIRMED")
            except Exception:
                with book.atomic():
                    book.db.execute("""UPDATE exit_highlight_alerts SET status='UNKNOWN',completed_at=?
                        WHERE alert_key=?""", (_stamp(), item["alert_key"]))
            else:
                with book.atomic():
                    book.db.execute("""UPDATE exit_highlight_alerts SET status='SENT',message_id=?,completed_at=?
                        WHERE alert_key=?""", (message_id, _stamp(), item["alert_key"]))
                sent += 1
    return sent

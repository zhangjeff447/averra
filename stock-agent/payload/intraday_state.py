"""Durable check and Telegram idempotency for the separate intraday monitor."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def now_utc():
    return datetime.now(timezone.utc).isoformat()


def init_state(db):
    db.executescript((Path(__file__).with_name("intraday_state.sql")).read_text())


def begin_check(db, day, window, kind):
    db.execute("BEGIN IMMEDIATE")
    try:
        current = db.execute("SELECT status FROM intraday_checks WHERE trading_date=? AND check_window=?",
                             (day, window)).fetchone()
        if current:
            db.commit()
            return "ALREADY_" + current[0]
        db.execute("INSERT INTO intraday_checks(trading_date,check_window,kind,status,started_at) "
                   "VALUES(?,?,?,'RUNNING',?)", (day, window, kind, now_utc()))
        db.commit()
        return "STARTED"
    except Exception:
        db.rollback()
        raise


def finish_check(db, day, window, *, observations, stats):
    with db:
        for item in observations:
            db.execute("""INSERT INTO intraday_observations VALUES(?,?,?,?,?,?,?,?,?,?)""",
                       (day, window, item["ticker"], item["role"], item["status"], item.get("state"),
                        item.get("asof"), item.get("source", "IEX"), json.dumps(item, sort_keys=True), now_utc()))
        db.execute("""UPDATE intraday_checks SET status='COMPLETE',completed_at=?,tickers_checked=?,
            market_state=?,event_state=?,alerts_generated=?,iex_requests=?,evidence_requests=?,
            luna_calls=?,luna_input_tokens=?,luna_output_tokens=?,luna_cost_usd=?,
            luna_unknown_cost_usd=?,sol_calls=?
            WHERE trading_date=? AND check_window=? AND status='RUNNING'""",
            (now_utc(), stats["tickers_checked"], stats["market_state"], stats["event_state"],
             stats["alerts_generated"], stats["iex_requests"], stats["evidence_requests"],
             stats["luna_calls"], stats["luna_input_tokens"], stats["luna_output_tokens"],
             stats["luna_cost_usd"], stats["luna_unknown_cost_usd"], 0, day, window))


def fail_check(db, day, window, exc):
    with db:
        db.execute("UPDATE intraday_checks SET status='FAILED',completed_at=?,error_type=?,error_message=? "
                   "WHERE trading_date=? AND check_window=? AND status='RUNNING'",
                   (now_utc(), type(exc).__name__, str(exc)[:300], day, window))


def reserve_alert(db, day, window, ticker, event_key, state, message):
    with db:
        row = db.execute("""INSERT OR IGNORE INTO intraday_alerts
          (trading_date,check_window,ticker,event_key,state,message,send_status,created_at)
          VALUES(?,?,?,?,?,?,'RESERVED',?) RETURNING id""",
          (day, window, ticker, event_key, state, message, now_utc())).fetchone()
    return row[0] if row else None


def send_reserved(db, alert_id, sender):
    """No blind retry: SENDING/UNKNOWN may already have reached Telegram."""
    with db:
        row = db.execute("UPDATE intraday_alerts SET send_status='SENDING' "
                         "WHERE id=? AND send_status='RESERVED' RETURNING message", (alert_id,)).fetchone()
    if not row:
        return "NOT_RESERVED"
    try:
        message_id = sender(row[0])
        if not isinstance(message_id, int):
            raise ValueError("TELEGRAM_MESSAGE_ID_MISSING")
    except Exception:
        with db:
            db.execute("UPDATE intraday_alerts SET send_status='UNKNOWN' WHERE id=?", (alert_id,))
        return "UNKNOWN"
    with db:
        db.execute("UPDATE intraday_alerts SET send_status='SENT',telegram_message_id=?,sent_at=? "
                   "WHERE id=? AND send_status='SENDING'", (message_id, now_utc(), alert_id))
    return "SENT"


def daily_totals(db, day):
    row = db.execute("""SELECT COUNT(*),COALESCE(SUM(tickers_checked),0),COALESCE(SUM(iex_requests),0),
      COALESCE(SUM(evidence_requests),0),COALESCE(SUM(luna_calls),0),
      COALESCE(SUM(luna_input_tokens+luna_output_tokens),0),COALESCE(SUM(luna_cost_usd),0),
      COALESCE(SUM(luna_unknown_cost_usd),0),COALESCE(SUM(sol_calls),0) FROM intraday_checks
      WHERE trading_date=? AND status='COMPLETE'""", (day,)).fetchone()
    return dict(zip(("checks", "tickers_checked", "iex_requests", "evidence_requests", "luna_calls",
                     "luna_tokens", "luna_cost_usd", "luna_unknown_cost_usd", "sol_calls"), row))

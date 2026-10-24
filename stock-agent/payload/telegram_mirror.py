#!/usr/bin/env python3
"""Forward confirmed Stock Bot market notifications to one opted-in recipient.

Command replies and personal portfolio/balance/deposit messages are not sources.
The original delivery ledger is read-only; uncertain forwards are never retried.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from quant_screen import ROOT
from stock_agent import load_env, request_json

DB = ROOT / "data" / "telegram_mirror.db"
SOURCES = (
    ("DAILY_SUMMARY", "shadow_daily_state.db",
     "SELECT trading_date,telegram_message_id,completed_at FROM shadow_daily_runs "
     "WHERE state='COMPLETE' AND telegram_status='SENT' AND telegram_message_id IS NOT NULL"),
    ("INTRADAY_ALERT", "intraday_monitor.db",
     "SELECT CAST(id AS TEXT),telegram_message_id,sent_at FROM intraday_alerts "
     "WHERE send_status='SENT' AND telegram_message_id IS NOT NULL"),
    ("EXIT_ALERT", "stock_agent.db",
     "SELECT alert_key,message_id,completed_at FROM exit_highlight_alerts "
     "WHERE status='SENT' AND message_id IS NOT NULL"),
    ("FIVE_DAY_REVIEW", "shadow_validation.db",
     "SELECT review_id,telegram_message_id,completed_at FROM shadow_5d_reviews "
     "WHERE telegram_status='SENT' AND telegram_message_id IS NOT NULL"),
    ("MONITOR_ALERT", "shadow_daily_state.db",
     "SELECT alert_key,message_id,created_at FROM shadow_monitor_alerts "
     "WHERE status='SENT' AND message_id IS NOT NULL"),
)


def stamp():
    return datetime.now(timezone.utc).isoformat()


def connect(path=DB):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=30, isolation_level=None)
    db.execute("PRAGMA busy_timeout=30000")
    db.executescript("""
        CREATE TABLE IF NOT EXISTS mirror_config (
          id INTEGER PRIMARY KEY CHECK(id=1), target_chat_id TEXT NOT NULL,
          activated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS mirror_receipts (
          source TEXT NOT NULL, source_key TEXT NOT NULL, source_message_id INTEGER NOT NULL,
          target_chat_id TEXT NOT NULL, state TEXT NOT NULL,
          target_message_id INTEGER, attempted_at TEXT, completed_at TEXT,
          error_type TEXT,
          PRIMARY KEY(source,source_key,target_chat_id));
    """)
    return db


def activate(db, target_chat_id, *, verifier, activated_at=None):
    """Do not create a forwarding rule until Telegram confirms the target chat."""
    if not str(target_chat_id).isdigit():
        raise ValueError("INVALID_TARGET_CHAT_ID")
    chat = verifier(str(target_chat_id))
    if str(chat.get("id")) != str(target_chat_id) or chat.get("type") != "private":
        raise ValueError("TARGET_CHAT_NOT_VERIFIED")
    if db.execute("SELECT 1 FROM mirror_config WHERE id=1").fetchone():
        raise ValueError("MIRROR_ALREADY_ACTIVE")
    with db:
        db.execute("INSERT INTO mirror_config VALUES(1,?,?)",
                   (str(target_chat_id), activated_at or stamp()))


def confirmed_notifications(root, activated_at):
    """Only bot-originated automatic sends with a confirmed primary message ID."""
    for source, filename, query in SOURCES:
        path = Path(root) / "data" / filename
        if not path.is_file():
            continue
        with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as db:
            try:
                rows = db.execute(query).fetchall()
            except sqlite3.OperationalError:
                continue  # An older database may not yet have this notification table.
        for key, message_id, sent_at in rows:
            if (sent_at and sent_at >= activated_at and isinstance(message_id, int)
                    and not isinstance(message_id, bool) and message_id > 0):
                yield source, str(key), message_id


def deliver(db, root, source_chat_id, forwarder):
    config = db.execute("SELECT target_chat_id,activated_at FROM mirror_config WHERE id=1").fetchone()
    if not config:
        return {"status": "NOT_ACTIVE", "sent": 0, "unknown": 0}
    target, activated_at = config
    sent = unknown = 0
    for source, key, message_id in confirmed_notifications(root, activated_at):
        db.execute("BEGIN IMMEDIATE")
        try:
            db.execute("""INSERT OR IGNORE INTO mirror_receipts
              (source,source_key,source_message_id,target_chat_id,state)
              VALUES(?,?,?,?,'PENDING')""", (source, key, message_id, target))
            claimed = db.execute("""UPDATE mirror_receipts SET state='IN_FLIGHT',attempted_at=?
              WHERE source=? AND source_key=? AND target_chat_id=? AND state='PENDING'
                AND source_message_id=?""", (stamp(), source, key, target, message_id)).rowcount
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            raise
        if not claimed:
            continue  # SENT and UNKNOWN are both terminal; never blindly resend.
        try:
            target_message_id = forwarder(source_chat_id, target, message_id)
            if not isinstance(target_message_id, int) or target_message_id <= 0:
                raise ValueError("FORWARD_UNCONFIRMED")
        except Exception as exc:
            with db:
                db.execute("""UPDATE mirror_receipts SET state='UNKNOWN',completed_at=?,error_type=?
                  WHERE source=? AND source_key=? AND target_chat_id=? AND state='IN_FLIGHT'""",
                           (stamp(), type(exc).__name__, source, key, target))
            unknown += 1
        else:
            with db:
                db.execute("""UPDATE mirror_receipts SET state='SENT',target_message_id=?,completed_at=?
                  WHERE source=? AND source_key=? AND target_chat_id=? AND state='IN_FLIGHT'""",
                           (target_message_id, stamp(), source, key, target))
            sent += 1
    return {"status": "ACTIVE", "sent": sent, "unknown": unknown}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--activate", metavar="CHAT_ID")
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--status", action="store_true")
    args = parser.parse_args()
    if sum(bool(x) for x in (args.activate, args.run, args.status)) != 1:
        parser.error("choose --activate, --run or --status")
    load_env(ROOT / ".env")
    token = os.environ.get("STOCK_TELEGRAM_BOT_TOKEN") or os.environ.get("STOCK_TELEGRAM_TOKEN")
    source_chat_id = os.environ.get("STOCK_TELEGRAM_CHAT_ID")
    if not token or not source_chat_id:
        raise ValueError("STOCK_BOT_CREDENTIALS_MISSING")
    with closing(connect()) as db:
        if args.activate:
            def verify(target):
                response = request_json(f"https://api.telegram.org/bot{token}/getChat",
                                        {"chat_id": target}, retry=False)
                return (response.get("result") or {}) if response.get("ok") else {}
            activate(db, args.activate, verifier=verify)
            print("MIRROR_ACTIVE")
        elif args.run:
            def forward(original, target, message_id):
                response = request_json(f"https://api.telegram.org/bot{token}/forwardMessage",
                                        {"chat_id": target, "from_chat_id": original,
                                         "message_id": message_id}, retry=False)
                return (response.get("result") or {}).get("message_id") if response.get("ok") else None
            print(deliver(db, ROOT, source_chat_id, forward))
        else:
            row = db.execute("SELECT target_chat_id,activated_at FROM mirror_config WHERE id=1").fetchone()
            counts = db.execute("SELECT state,COUNT(*) FROM mirror_receipts GROUP BY state").fetchall()
            print({"status": "ACTIVE" if row else "NOT_ACTIVE", "target_chat_id": row[0] if row else None,
                   "activated_at": row[1] if row else None, "receipts": dict(counts)})


if __name__ == "__main__":
    main()

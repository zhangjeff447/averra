#!/usr/bin/env python3
"""Persistent per-date Shadow Day 2-5 lock and one-message delivery state."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from quant_screen import ROOT
from shadow_schedule import authorized_day, shadow_day_number

DB = ROOT / "data" / "shadow_daily_state.db"
ET = ZoneInfo("America/New_York")
DAYS = {"2026-09-29": 2, "2026-09-30": 3,
        "2026-10-01": 4, "2026-10-02": 5}
SLOTS = ((16, 20), (16, 35), (16, 50), (17, 5))
CUTOFF_MINUTE = 17 * 60 + 10
REQUIRED = ("market", "phase1", "phase2", "phase3", "research", "entry",
            "portfolio", "forward_tracking")


def now_utc():
    return datetime.now(timezone.utc).isoformat()


def slot(now):
    """Allow a delayed launch within the same bounded window, not after 17:10 ET."""
    eastern = now.astimezone(ET)
    day = eastern.date().isoformat()
    if not authorized_day(day) or not 16 * 60 + 20 <= eastern.hour * 60 + eastern.minute < CUTOFF_MINUTE:
        return None
    eligible = [f"{h:02d}:{m:02d}" for h, m in SLOTS
                if h * 60 + m <= eastern.hour * 60 + eastern.minute]
    return {"date": day, "shadow_day": shadow_day_number(day), "due_slot_et": eligible[-1]}


def connect(path=DB):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=30, isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA busy_timeout=30000")
    db.executescript((ROOT / "shadow_daily_state.sql").read_text())
    return db


@contextmanager
def atomic(db):
    db.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        db.execute("ROLLBACK")
        raise
    else:
        db.execute("COMMIT")


def record(db, day):
    if not authorized_day(day):
        raise ValueError("UNAUTHORIZED_SHADOW_DATE")
    item = db.execute("SELECT * FROM shadow_daily_runs WHERE run_type='SHADOW_DAILY' AND trading_date=?",
                      (day,)).fetchone()
    return dict(item) if item else None


def initialize(db, day):
    if not authorized_day(day):
        raise ValueError("UNAUTHORIZED_SHADOW_DATE")
    with atomic(db):
        db.execute("""INSERT OR IGNORE INTO shadow_daily_runs
            (run_type,trading_date,state,telegram_key) VALUES('SHADOW_DAILY',?,'WAITING_FOR_DATA',?)""",
            (day, f"SHADOW_DAILY:{day}:SUMMARY"))
        return record(db, day)


def action(item):
    state = item["state"]
    if state == "COMPLETE":
        return "ALREADY_COMPLETE"
    if state == "FAILED":
        return "FAILED_STOP"
    if state == "MISSED_DATA_WINDOW":
        return "MISSED_DATA_WINDOW"
    if state == "RUNNING":
        return "RUNNING_STOP"
    if state == "PIPELINE_COMPLETE_TELEGRAM_PENDING":
        return "TELEGRAM_ONLY" if item["telegram_status"] == "PENDING" else "TELEGRAM_RECONCILE"
    return state


def ready(db, day, gate_ready):
    with atomic(db):
        item = record(db, day)
        if item["state"] in {"WAITING_FOR_DATA", "READY"}:
            state = "READY" if gate_ready else "WAITING_FOR_DATA"
            db.execute("""UPDATE shadow_daily_runs SET state=?,checked_at=?,market_data_as_of=?
                WHERE run_type='SHADOW_DAILY' AND trading_date=?""",
                (state, now_utc(), day if gate_ready else None, day))
        return record(db, day)


def mark_missed(db, day, at=None):
    at = (at or datetime.now(timezone.utc)).astimezone(ET)
    if at.date().isoformat() != day or at.hour * 60 + at.minute < CUTOFF_MINUTE:
        raise ValueError("SHADOW_CUTOFF_NOT_REACHED")
    with atomic(db):
        if record(db, day)["state"] in {"WAITING_FOR_DATA", "READY"}:
            db.execute("""UPDATE shadow_daily_runs SET state='MISSED_DATA_WINDOW',missed_at=?
                WHERE run_type='SHADOW_DAILY' AND trading_date=?""", (now_utc(), day))
        return record(db, day)


def begin(db, day, at=None):
    at = (at or datetime.now(timezone.utc)).astimezone(ET)
    with atomic(db):
        item = record(db, day)
        if item["state"] != "READY":
            raise ValueError(action(item))
        if at.date().isoformat() != day or at.hour * 60 + at.minute >= CUTOFF_MINUTE:
            db.execute("""UPDATE shadow_daily_runs SET state='MISSED_DATA_WINDOW',missed_at=?
                WHERE run_type='SHADOW_DAILY' AND trading_date=?""", (now_utc(), day))
            return record(db, day)
        db.execute("""UPDATE shadow_daily_runs SET state='RUNNING',run_id=?,started_at=?
            WHERE run_type='SHADOW_DAILY' AND trading_date=? AND state='READY'""",
            (f"SHADOW_DAILY_{day.replace('-', '')}", now_utc(), day))
        return record(db, day)


def pipeline_complete(db, day, path, token_cost):
    path = Path(path).resolve(strict=True)
    raw = path.read_bytes()
    report = json.loads(raw)
    if (report.get("trading_date") != day or report.get("status") != "PIPELINE_COMPLETE" or
            report.get("market_data_as_of") != day or
            any(report.get("phase_statuses", {}).get(key) != "COMPLETE" for key in REQUIRED) or
            report.get("phase_statuses", {}).get("telegram_summary") != "READY"):
        raise ValueError("INCOMPLETE_DAILY_RESULT")
    with atomic(db):
        if record(db, day)["state"] != "RUNNING":
            raise ValueError("DAILY_PIPELINE_NOT_RUNNING")
        db.execute("""UPDATE shadow_daily_runs SET state='PIPELINE_COMPLETE_TELEGRAM_PENDING',
            pipeline_completed_at=?,result_path=?,result_sha256=?,token_cost_json=?,
            telegram_status='PENDING' WHERE run_type='SHADOW_DAILY' AND trading_date=?""",
            (now_utc(), str(path), hashlib.sha256(raw).hexdigest(),
             json.dumps(token_cost, sort_keys=True), day))
        return record(db, day)


def fail(db, day, phase, error):
    with atomic(db):
        if record(db, day)["state"] != "RUNNING":
            raise ValueError("DAILY_PIPELINE_NOT_RUNNING")
        db.execute("""UPDATE shadow_daily_runs SET state='FAILED',failed_at=?,failed_phase=?,
            error_type=?,error_message=? WHERE run_type='SHADOW_DAILY' AND trading_date=?""",
            (now_utc(), phase, type(error).__name__, str(error)[:500], day))
        return record(db, day)


def fail_before_run(db, day, phase, error):
    with atomic(db):
        if record(db, day)["state"] not in {"WAITING_FOR_DATA", "READY"}:
            raise ValueError("DAILY_PRECHECK_ALREADY_ADVANCED")
        db.execute("""UPDATE shadow_daily_runs SET state='FAILED',failed_at=?,failed_phase=?,
            error_type=?,error_message=? WHERE run_type='SHADOW_DAILY' AND trading_date=?""",
            (now_utc(), phase, type(error).__name__, str(error)[:500], day))
        return record(db, day)


def manual_recover_market_update(db, day, *, gate_ready, at=None):
    """One audited human-authorized recovery; never replay a started pipeline."""
    at = (at or datetime.now(timezone.utc)).astimezone(ET)
    if at.date().isoformat() != day or at.hour * 60 + at.minute >= CUTOFF_MINUTE:
        raise ValueError("MANUAL_RECOVERY_OUTSIDE_START_WINDOW")
    if not gate_ready:
        raise ValueError("MANUAL_RECOVERY_DATA_NOT_READY")
    with atomic(db):
        item = record(db, day)
        if (not item or item["state"] != "FAILED" or item["failed_phase"] != "MARKET_UPDATE" or
                item["run_id"] or item["token_cost_json"] or item["result_path"] or
                item["telegram_status"] != "NOT_READY"):
            raise ValueError("MANUAL_RECOVERY_NOT_SAFE")
        stamp = now_utc()
        db.execute("""INSERT INTO shadow_daily_recovery_audit
            (trading_date,recovered_at,prior_phase,prior_error_type,prior_error_message,authorization)
            VALUES(?,?,?,?,?,'USER_AUTHORIZED_REPAIR')""",
            (day, stamp, item["failed_phase"], item["error_type"], item["error_message"]))
        db.execute("""UPDATE shadow_daily_runs SET state='READY',checked_at=?,market_data_as_of=?,
            failed_at=NULL,failed_phase=NULL,error_type=NULL,error_message=NULL
            WHERE run_type='SHADOW_DAILY' AND trading_date=? AND state='FAILED'""",
            (stamp, day, day))
        return record(db, day)


def manual_recover_finalization(db, day, *, artifacts_ready):
    """Resume only finalization of a previously started run; never replay paid phases."""
    if not artifacts_ready:
        raise ValueError("FINALIZATION_ARTIFACTS_INCOMPLETE")
    with atomic(db):
        item = record(db, day)
        if (not item or item["state"] != "FAILED" or item["failed_phase"] != "FINALIZE" or
                item["run_id"] != f"SHADOW_DAILY_{day.replace('-', '')}" or
                not item["started_at"] or item["result_path"] or item["token_cost_json"] or
                item["telegram_status"] != "NOT_READY"):
            raise ValueError("FINALIZATION_RECOVERY_NOT_SAFE")
        stamp = now_utc()
        db.execute("""INSERT INTO shadow_daily_recovery_audit
            (trading_date,recovered_at,prior_phase,prior_error_type,prior_error_message,authorization)
            VALUES(?,?,?,?,?,'USER_AUTHORIZED_REPAIR')""",
            (day, stamp, item["failed_phase"], item["error_type"], item["error_message"]))
        db.execute("""UPDATE shadow_daily_runs SET state='RUNNING',failed_at=NULL,
            failed_phase=NULL,error_type=NULL,error_message=NULL WHERE run_type='SHADOW_DAILY'
            AND trading_date=? AND state='FAILED'""", (day,))
        return record(db, day)


def claim_telegram(db, day, path):
    raw = Path(path).read_bytes()
    with atomic(db):
        item = record(db, day)
        if (action(item) != "TELEGRAM_ONLY" or
                hashlib.sha256(raw).hexdigest() != item["result_sha256"]):
            raise ValueError("TELEGRAM_ALREADY_SENT_OR_UNCERTAIN")
        db.execute("""UPDATE shadow_daily_runs SET telegram_status='IN_FLIGHT',
            telegram_attempted_at=? WHERE run_type='SHADOW_DAILY' AND trading_date=?""",
            (now_utc(), day))
        return item["telegram_key"]


def telegram_sent(db, day, key, message_id):
    if not isinstance(message_id, int) or message_id <= 0:
        raise ValueError("INVALID_TELEGRAM_MESSAGE_ID")
    with atomic(db):
        item = record(db, day)
        if item["telegram_key"] != key or item["telegram_status"] != "IN_FLIGHT":
            raise ValueError("TELEGRAM_SEND_NOT_CLAIMED")
        db.execute("""UPDATE shadow_daily_runs SET state='COMPLETE',telegram_status='SENT',
            telegram_message_id=?,completed_at=? WHERE run_type='SHADOW_DAILY' AND trading_date=?""",
            (message_id, now_utc(), day))
        return record(db, day)


def telegram_uncertain(db, day, key):
    with atomic(db):
        item = record(db, day)
        if item["telegram_key"] != key or item["telegram_status"] != "IN_FLIGHT":
            raise ValueError("TELEGRAM_SEND_NOT_CLAIMED")
        db.execute("""UPDATE shadow_daily_runs SET telegram_status='UNKNOWN'
            WHERE run_type='SHADOW_DAILY' AND trading_date=?""", (day,))
        return record(db, day)

#!/usr/bin/env python3
"""Persistent one-run Shadow Day 1 lock and Telegram delivery record."""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from quant_screen import ROOT
from shadow_day1_gate import DAY, check

RUN_TYPE = "SHADOW_DAY1"
RUN_ID = "SHADOW_DAY1_20260928"
DB_PATH = ROOT / "data" / "shadow_day1_state.db"
KEY = f"{RUN_TYPE}:{DAY}:SUMMARY"
REQUIRED_PHASES = ("market", "phase1", "phase2", "phase3", "research",
                   "entry", "forward_tracking")


class StateError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect(path: Path = DB_PATH) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=30, isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA busy_timeout=30000")
    schema = (ROOT / "shadow_day1_state.sql").read_text()
    previous = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='shadow_runs'").fetchone()
    if previous and "RESUME_AUTHORIZED" not in previous[0]:
        columns = [item[1] for item in db.execute("PRAGMA table_info(shadow_runs)")]
        names = ",".join(columns)
        db.executescript("BEGIN IMMEDIATE; ALTER TABLE shadow_runs RENAME TO shadow_runs_before_resume; "
                         + schema + f" INSERT INTO shadow_runs({names}) SELECT {names} "
                         "FROM shadow_runs_before_resume; DROP TABLE shadow_runs_before_resume; COMMIT;")
    else:
        db.executescript(schema)
    return db


@contextmanager
def locked(db: sqlite3.Connection):
    db.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        db.execute("ROLLBACK")
        raise
    else:
        db.execute("COMMIT")


def row(db: sqlite3.Connection) -> dict | None:
    found = db.execute("SELECT * FROM shadow_runs WHERE shadow_run_type=? AND trading_date=?",
                       (RUN_TYPE, DAY)).fetchone()
    return dict(found) if found else None


def initialize(db: sqlite3.Connection) -> dict:
    with locked(db):
        db.execute("""INSERT OR IGNORE INTO shadow_runs
            (shadow_run_type,trading_date,state,telegram_idempotency_key,updated_at)
            VALUES(?,?,?,?,?)""", (RUN_TYPE, DAY, "WAITING_FOR_DATA", KEY, utc_now()))
        return row(db)


def action(record: dict) -> str:
    state = record["state"]
    if state == "COMPLETE":
        return "ALREADY_COMPLETE"
    if state == "FAILED":
        return "FAILED_STOP"
    if state == "RESUME_AUTHORIZED":
        return "RESUME_AUTHORIZED"
    if state == "RUNNING":
        return "RUNNING_STOP"
    if state == "PIPELINE_COMPLETE_TELEGRAM_PENDING":
        return ("TELEGRAM_ONLY" if record["telegram_delivery_status"] == "PENDING"
                else "TELEGRAM_RECONCILIATION_REQUIRED")
    return state


def refresh_from_market(db: sqlite3.Connection, market: sqlite3.Connection,
                        now: datetime) -> tuple[dict, dict]:
    gate = check(market, now)
    with locked(db):
        current = row(db)
        if current is None:
            raise StateError("initialize first")
        if current["state"] in {"WAITING_FOR_DATA", "READY"}:
            new_state = "READY" if gate["entry_decision_allowed"] else "WAITING_FOR_DATA"
            db.execute("""UPDATE shadow_runs SET state=?,market_data_as_of=?,updated_at=?
                WHERE shadow_run_type=? AND trading_date=?""",
                (new_state, DAY if new_state == "READY" else None, utc_now(), RUN_TYPE, DAY))
        return row(db), gate


def begin_pipeline(db: sqlite3.Connection, market: sqlite3.Connection,
                   now: datetime) -> dict:
    if not check(market, now)["entry_decision_allowed"]:
        raise StateError("SHADOW_DAY1_WAITING_FOR_DATA")
    with locked(db):
        current = row(db)
        if current is None or current["state"] != "READY":
            raise StateError(action(current) if current else "NOT_INITIALIZED")
        db.execute("""UPDATE shadow_runs SET state='RUNNING',run_id=?,started_at=?,
            pipeline_status='RUNNING',updated_at=?
            WHERE shadow_run_type=? AND trading_date=? AND state='READY'""",
            (RUN_ID, utc_now(), utc_now(), RUN_TYPE, DAY))
        return row(db)


def authorize_resume(db: sqlite3.Connection) -> dict:
    """Single user-authorized continuation of this exact failed phase."""
    with locked(db):
        current = row(db)
        if (current is None or current["state"] != "FAILED" or
                current["failed_phase"] != "RESEARCH_PLANNING" or current["resume_count"] != 0):
            raise StateError("MANUAL_RESUME_NOT_ALLOWED")
        db.execute("""UPDATE shadow_runs SET state='RESUME_AUTHORIZED',resume_count=1,
            resume_authorized_at=?,updated_at=? WHERE shadow_run_type=? AND trading_date=?""",
            (utc_now(), utc_now(), RUN_TYPE, DAY))
        return row(db)


def resume_pipeline(db: sqlite3.Connection, market: sqlite3.Connection,
                    fundamental: sqlite3.Connection, now: datetime) -> dict:
    if not check(market, now)["entry_decision_allowed"]:
        raise StateError("SHADOW_DAY1_DATA_NOT_READY")
    quant = market.execute("""SELECT id FROM quant_runs WHERE asof=? AND data_mode='live'
        ORDER BY id DESC LIMIT 1""", (DAY,)).fetchone()
    prior = fundamental.execute("""SELECT quant_run_id,processed_count FROM fundamental_runs
        WHERE scan_date=? ORDER BY id DESC LIMIT 1""", (DAY,)).fetchone()
    if not quant or not prior or prior[0] != quant[0] or prior[1] != 100:
        raise StateError("PHASE1_TO_3_SAVED_RESULTS_INCOMPLETE")
    with locked(db):
        current = row(db)
        if current is None or current["state"] != "RESUME_AUTHORIZED" or current["resume_count"] != 1:
            raise StateError(action(current) if current else "NOT_INITIALIZED")
        db.execute("""UPDATE shadow_runs SET state='RUNNING',pipeline_status='RUNNING',
            updated_at=? WHERE shadow_run_type=? AND trading_date=? AND state='RESUME_AUTHORIZED'""",
            (utc_now(), RUN_TYPE, DAY))
        return row(db)


def pipeline_complete(db: sqlite3.Connection, result_path: Path,
                      token_cost_summary: dict) -> dict:
    result_path = result_path.resolve(strict=True)
    raw = result_path.read_bytes()
    result = json.loads(raw)
    phases = result.get("phase_statuses") or {}
    if (result.get("trading_date") != DAY or result.get("market_data_as_of") != DAY or
            result.get("status") != "PIPELINE_COMPLETE" or
            any(phases.get(name) != "COMPLETE" for name in REQUIRED_PHASES) or
            phases.get("telegram_summary") != "READY"):
        raise StateError("final Day 1 result is not complete for 2026-09-28")
    with locked(db):
        current = row(db)
        if current is None or current["state"] != "RUNNING":
            raise StateError(action(current) if current else "NOT_INITIALIZED")
        db.execute("""UPDATE shadow_runs SET state='PIPELINE_COMPLETE_TELEGRAM_PENDING',
            pipeline_status='COMPLETE',result_path=?,result_sha256=?,
            token_cost_summary_json=?,telegram_delivery_status='PENDING',updated_at=?
            WHERE shadow_run_type=? AND trading_date=?""",
            (str(result_path), hashlib.sha256(raw).hexdigest(),
             json.dumps(token_cost_summary, sort_keys=True), utc_now(), RUN_TYPE, DAY))
        return row(db)


def fail_pipeline(db: sqlite3.Connection, phase: str, error: BaseException) -> dict:
    with locked(db):
        current = row(db)
        if current is None or current["state"] != "RUNNING":
            raise StateError(action(current) if current else "NOT_INITIALIZED")
        stamp = utc_now()
        db.execute("""UPDATE shadow_runs SET state='FAILED',pipeline_status='FAILED',
            failed_phase=?,error_type=?,error_message=?,failed_at=?,updated_at=?
            WHERE shadow_run_type=? AND trading_date=?""",
            (phase, type(error).__name__, str(error)[:1000], stamp, stamp, RUN_TYPE, DAY))
        return row(db)


def claim_telegram(db: sqlite3.Connection) -> str:
    with locked(db):
        current = row(db)
        if current is None or current["state"] != "PIPELINE_COMPLETE_TELEGRAM_PENDING" or \
                current["telegram_delivery_status"] != "PENDING":
            raise StateError(action(current) if current else "NOT_INITIALIZED")
        db.execute("""UPDATE shadow_runs SET telegram_delivery_status='IN_FLIGHT',
            telegram_attempted_at=?,updated_at=? WHERE shadow_run_type=? AND trading_date=?""",
            (utc_now(), utc_now(), RUN_TYPE, DAY))
        return current["telegram_idempotency_key"]


def telegram_sent(db: sqlite3.Connection, key: str, message_id: int) -> dict:
    if not isinstance(message_id, int) or message_id <= 0:
        raise ValueError("positive Telegram message ID required")
    with locked(db):
        current = row(db)
        if current is None or current["telegram_idempotency_key"] != key or \
                current["state"] != "PIPELINE_COMPLETE_TELEGRAM_PENDING" or \
                current["telegram_delivery_status"] != "IN_FLIGHT":
            raise StateError(action(current) if current else "NOT_INITIALIZED")
        stamp = utc_now()
        db.execute("""UPDATE shadow_runs SET state='COMPLETE',telegram_delivery_status='SENT',
            telegram_message_id=?,completed_at=?,updated_at=?
            WHERE shadow_run_type=? AND trading_date=?""",
            (message_id, stamp, stamp, RUN_TYPE, DAY))
        return row(db)


def telegram_failed(db: sqlite3.Connection, key: str, *, certain_no_send: bool) -> dict:
    with locked(db):
        current = row(db)
        if current is None or current["telegram_idempotency_key"] != key or \
                current["state"] != "PIPELINE_COMPLETE_TELEGRAM_PENDING" or \
                current["telegram_delivery_status"] != "IN_FLIGHT":
            raise StateError(action(current) if current else "NOT_INITIALIZED")
        db.execute("""UPDATE shadow_runs SET telegram_delivery_status=?,updated_at=?
            WHERE shadow_run_type=? AND trading_date=?""",
            ("PENDING" if certain_no_send else "UNKNOWN", utc_now(), RUN_TYPE, DAY))
        return row(db)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("status", "init", "refresh", "begin"))
    parser.add_argument("--db", type=Path, default=DB_PATH)
    parser.add_argument("--market-db", type=Path, default=ROOT / "data" / "market_data.db")
    args = parser.parse_args()
    with connect(args.db) as db:
        if args.command == "init":
            current = initialize(db)
        elif args.command == "refresh":
            with sqlite3.connect(f"file:{args.market_db}?mode=ro", uri=True) as market:
                current, _ = refresh_from_market(db, market, datetime.now(timezone.utc))
        elif args.command == "begin":
            with sqlite3.connect(f"file:{args.market_db}?mode=ro", uri=True) as market:
                current = begin_pipeline(db, market, datetime.now(timezone.utc))
        else:
            current = row(db)
        if current is None:
            raise SystemExit("SHADOW_DAY1_STATE_NOT_INITIALIZED")
        print(json.dumps({"shadow_run_type": RUN_TYPE, "trading_date": DAY,
                          "state": current["state"], "action": action(current),
                          "run_id": current["run_id"],
                          "market_data_as_of": current["market_data_as_of"],
                          "telegram_delivery_status": current["telegram_delivery_status"],
                          "telegram_message_id": current["telegram_message_id"],
                          "failed_phase": current["failed_phase"] if current["state"] == "FAILED" else None}, indent=2))


if __name__ == "__main__":
    main()

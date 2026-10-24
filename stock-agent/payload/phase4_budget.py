"""Local Phase 4 request states and conservative budget accounting; never calls an API."""
from __future__ import annotations

import json
import socket
import ssl
import urllib.error
from datetime import datetime, timezone


TRANSITIONS = {
    "RESERVED": {"IN_FLIGHT", "FAILED_NO_REQUEST"},
    "IN_FLIGHT": {"COMPLETED", "FAILED_NO_REQUEST", "FAILED_UNKNOWN"},
    "FAILED_UNKNOWN": {"CONFIRMED_BILLED", "CONFIRMED_NO_CHARGE"},
}


def now():
    return datetime.now(timezone.utc).isoformat()


def migrate_legacy(db):
    """Add state records without changing historical usage or claiming old network stages."""
    rows = db.execute("""SELECT l.id,l.actual_input,l.actual_output,l.status,
        a.ticker IS NOT NULL AS archived FROM research_token_ledger l
        LEFT JOIN research_response_archive a ON a.run_id=l.run_id AND a.ticker=l.ticker
        LEFT JOIN research_request_state s ON s.ledger_id=l.id WHERE s.ledger_id IS NULL""").fetchall()
    with db:
        for row in rows:
            actual = row[1] is not None and row[2] is not None and (row[1] > 0 or row[2] > 0)
            state = "COMPLETED" if actual else "FAILED_UNKNOWN"
            category = "UNKNOWN_NETWORK_ERROR" if row[3] == "URLError" else None
            db.execute("""INSERT INTO research_request_state
                (ledger_id,state,error_category,usage_received,raw_response_saved)
                VALUES(?,?,?,?,?)""", (row[0], state, category, int(actual), int(bool(row[4]))))


def transition(db, ledger_id, state, **fields):
    current = db.execute("SELECT state FROM research_request_state WHERE ledger_id=?", (ledger_id,)).fetchone()
    if not current or state not in TRANSITIONS.get(current[0], set()):
        raise ValueError(f"invalid budget state transition: {current[0] if current else None} -> {state}")
    allowed = {"error_category", "request_id", "request_started_at", "connection_established",
               "response_started", "usage_received", "raw_response_saved"}
    if set(fields) - allowed:
        raise ValueError("unexpected request-state field")
    columns = ",".join(f"{key}=?" for key in fields)
    with db:
        db.execute(f"UPDATE research_request_state SET state=?{',' + columns if columns else ''} WHERE ledger_id=?",
                   (state, *fields.values(), ledger_id))


def mark_stage(db, ledger_id, **fields):
    allowed = {"request_id", "request_started_at", "connection_established",
               "response_started", "usage_received", "raw_response_saved"}
    if not fields or set(fields) - allowed:
        raise ValueError("unexpected request-stage field")
    with db:
        db.execute("UPDATE research_request_state SET " + ",".join(f"{key}=?" for key in fields) +
                   " WHERE ledger_id=?", (*fields.values(), ledger_id))


def record_budget_block(db, day, run_id, ticker, reason):
    """A blocked attempt is visible in history but consumes no call or reservation."""
    with db:
        cur = db.execute("""INSERT INTO research_token_ledger(usage_day,run_id,ticker,
            reserved_input,reserved_output,status,created_at) VALUES(?,?,?,?,?,?,?)""",
            (day, run_id, ticker, 0, 0, "BUDGET_BLOCKED", now()))
        db.execute("""INSERT INTO research_request_state(ledger_id,state,error_category)
            VALUES(?,?,?)""", (cur.lastrowid, "BUDGET_BLOCKED", reason))


def classify_network_error(exc, response_started=None, phase=None):
    if isinstance(exc, urllib.error.HTTPError):
        return ("HTTP_429" if exc.code == 429 else "HTTP_5XX" if 500 <= exc.code <= 599
                else f"HTTP_{exc.code}"), "FAILED_UNKNOWN"
    cause = exc.reason if isinstance(exc, urllib.error.URLError) else exc
    if isinstance(cause, socket.gaierror):
        return "DNS_ERROR", "FAILED_NO_REQUEST"
    if isinstance(cause, ssl.SSLError):
        return "TLS_ERROR", "FAILED_UNKNOWN"
    if isinstance(cause, (ConnectionResetError, BrokenPipeError)):
        return ("STREAM_INTERRUPTED" if response_started else "CONNECTION_RESET"), "FAILED_UNKNOWN"
    if isinstance(cause, (socket.timeout, TimeoutError)):
        return ("READ_TIMEOUT" if response_started or phase == "read" else
                "CONNECT_TIMEOUT" if phase == "connect" else "API_TIMEOUT"), "FAILED_UNKNOWN"
    if isinstance(cause, ConnectionRefusedError):
        return "UNKNOWN_NETWORK_ERROR", "FAILED_UNKNOWN"  # Proxy/server path is not provable.
    return "UNKNOWN_NETWORK_ERROR", "FAILED_UNKNOWN"


def reconcile(db, ledger_id, decision, evidence):
    """Offline evidence review only; aggregate organization totals cannot match a request."""
    row = db.execute("SELECT state,request_id FROM research_request_state WHERE ledger_id=?", (ledger_id,)).fetchone()
    if not row or row[0] != "FAILED_UNKNOWN":
        raise ValueError("only FAILED_UNKNOWN may be reconciled")
    if decision not in {"CONFIRMED_BILLED", "CONFIRMED_NO_CHARGE"}:
        raise ValueError("invalid reconciliation decision")
    if not isinstance(evidence, dict) or evidence.get("source") not in {"provider_request_record", "manual_verified"}:
        raise ValueError("request-level or manually verified evidence required")
    if not evidence.get("reference") or not evidence.get("verified_at"):
        raise ValueError("traceable reconciliation evidence required")
    if evidence["source"] == "provider_request_record" and not row[1]:
        raise ValueError("provider record cannot be matched without the captured request ID")
    if row[1] and evidence.get("request_id") != row[1]:
        raise ValueError("request ID mismatch")
    amount = evidence.get("cost_usd")
    if decision == "CONFIRMED_BILLED" and (not isinstance(amount, (int, float)) or amount < 0):
        raise ValueError("verified billed amount required")
    if decision == "CONFIRMED_NO_CHARGE" and amount != 0:
        raise ValueError("verified zero charge required")
    with db:
        db.execute("""UPDATE research_request_state SET state=?,reconciled_cost_usd=?,
            reconciliation_json=? WHERE ledger_id=?""",
            (decision, amount, json.dumps(evidence, sort_keys=True), ledger_id))


def budget_report(db, day, cfg):
    rows = db.execute("""SELECT l.ticker,l.reserved_input,l.reserved_output,l.actual_input,
        l.actual_output,l.status,d.estimated_total_cost_usd,d.actual_total_cost_usd,
        s.state,s.reconciled_cost_usd FROM research_token_ledger l
        LEFT JOIN research_usage_details d ON d.ledger_id=l.id
        LEFT JOIN research_request_state s ON s.ledger_id=l.id WHERE l.usage_day=?""", (day,)).fetchall()
    confirmed = unknown = reserved = 0.0
    input_used = output_used = input_guard = output_guard = calls = 0
    counts = {}
    for row in rows:
        ticker, rin, rout, ain, aout, old_status, estimated, actual_cost, state, reconciled = row
        state = state or ("COMPLETED" if ain is not None and aout is not None and (ain or aout)
                          else "FAILED_UNKNOWN")
        counts[state] = counts.get(state, 0) + 1
        if state not in {"FAILED_NO_REQUEST", "CONFIRMED_NO_CHARGE", "BUDGET_BLOCKED"}:
            calls += 1
        model = ((ain or 0) * cfg["model_input_usd_per_million"] +
                 (aout or 0) * cfg["model_output_usd_per_million"]) / 1_000_000
        ceiling = estimated if estimated is not None else (
            (rin * cfg["model_input_usd_per_million"] + rout * cfg["model_output_usd_per_million"]) /
            1_000_000 + cfg["web_search_usd_per_call"])
        if state == "COMPLETED":
            confirmed += actual_cost if actual_cost is not None else model
            if actual_cost is None:
                unknown += (cfg["web_search_usd_per_call"] if (ain or aout) else ceiling)
        elif state == "CONFIRMED_BILLED":
            confirmed += reconciled
        elif state == "FAILED_UNKNOWN":
            unknown += ceiling
        elif state in {"RESERVED", "IN_FLIGHT"}:
            reserved += ceiling
        if ain is not None and state in {"COMPLETED", "CONFIRMED_BILLED"}:
            input_used += ain
            output_used += aout or 0
        if state not in {"FAILED_NO_REQUEST", "CONFIRMED_NO_CHARGE", "BUDGET_BLOCKED"}:
            input_guard += ain if ain is not None else rin
            output_guard += aout if aout is not None else rout
    safe = max(0.0, cfg["max_daily_cost_usd"] - confirmed - unknown - reserved)
    pct = 100 * max(calls / cfg["max_daily_ai_calls"], input_guard / cfg["max_daily_input_tokens"],
                    output_guard / cfg["max_daily_output_tokens"],
                    (confirmed + unknown + reserved) / cfg["max_daily_cost_usd"])
    researched = db.execute("""SELECT COUNT(DISTINCT r.ticker) FROM research_results r
        JOIN research_token_ledger l ON l.run_id=r.run_id AND l.ticker=r.ticker
        WHERE l.usage_day=? AND r.status='COMPLETE'""", (day,)).fetchone()[0]
    return {"daily_budget_usd": cfg["max_daily_cost_usd"],
            "confirmed_cost_usd": round(confirmed, 6), "pending_unknown_cost_usd": round(unknown, 6),
            "reserved_cost_usd": round(reserved, 6), "available_safe_budget_usd": round(safe, 6),
            "confirmed_cost_basis": "provider usage times configured model rates or verified reconciliation; not invoice total",
            "state_counts": counts, "stocks_researched": researched,
            "stocks_attempted": len({row[0] for row in rows}), "ai_calls": calls,
            "input_tokens": input_used, "output_tokens": output_used,
            "total_tokens": input_used + output_used, "budget_input_tokens": input_guard,
            "budget_output_tokens": output_guard, "budget_cost_usd_estimate": round(confirmed + unknown + reserved, 6),
            "budget_used_pct": round(pct, 2)}

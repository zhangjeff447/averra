#!/usr/bin/env python3
"""Validate and send the one-time Shadow Day 1 Stock Bot summary."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from quant_screen import ROOT
from shadow_day1_state import (action, claim_telegram, connect, row, telegram_failed,
                               telegram_sent)
from stock_agent import load_env, request_json

REPORT = ROOT / "data" / "shadow_day1_final.json"
RECEIPT = ROOT / "data" / "shadow_day1_telegram_receipt.json"
ALLOWED = {"BUY_TOMORROW_CANDIDATE", "WATCH", "REJECT_ENTRY"}


def validate(report):
    if (report.get("status") != "PIPELINE_COMPLETE" or
            report.get("trading_date") != "2026-09-28" or
            report.get("market_data_as_of") != "2026-09-28" or
            report.get("no_trade_placed") is not True or
            report.get("sol_calls") != 0 or
            report.get("portfolio", {}).get("cash_usd") != 0 or
            report.get("portfolio", {}).get("suggested_allocation_usd") != 0):
        raise ValueError("DAY1_TELEGRAM_SUMMARY_INPUT_INVALID")
    cases = report.get("research_cases", [])
    entries = report.get("entry_results", [])
    if len(cases) != 15 or not all(e.get("entry_state") in ALLOWED for e in entries):
        raise ValueError("DAY1_TELEGRAM_COUNTS_OR_STATE_INVALID")
    eligible = {c["ticker"] for c in cases if c["final_status"] == "FINAL_RESEARCH_AVAILABLE"}
    if any(e["ticker"] not in eligible for e in entries):
        raise ValueError("ENTRY_WITHOUT_FINAL_RESEARCH")
    if any(c["final_status"] == "DEEP_RESEARCH_PENDING" and
           c["ticker"] in {e["ticker"] for e in entries} for c in cases):
        raise ValueError("DEEP_PENDING_DISPLAYED_AS_FINAL")
    if report.get("phase_statuses", {}).get("telegram_summary") != "READY":
        raise ValueError("SUMMARY_NOT_READY")
    return True


def render(report):
    validate(report)
    cases = report["research_cases"]
    entries = report["entry_results"]
    counts = {state: sum(e["entry_state"] == state for e in entries) for state in ALLOWED}
    research = report["research"]
    changes = report["candidate_changes"]
    top = sorted(report["candidate_top30"], key=lambda x: x["rank"])[:5]
    refreshed = sum(c["cache_status"] == "INITIAL_REFRESH_REQUIRED" and
                    c["input_tokens"] > 0 for c in cases)
    deferred = sum(c["final_status"] == "RESEARCH_DEFERRED_BUDGET" for c in cases)
    lines = ["AVERRA STOCKS | SHADOW DAY 1", "2026-09-28 close | Entry for 2026-09-29",
             f"Market: {report['market_regime']} | Daily source: ALPACA_SIP",
             f"Top 30: {len(changes['NEW_ENTRY'])} new, {len(changes['DROPPED'])} dropped, "
             f"{len(changes['RANK_UP'])} rank up, {len(changes['RANK_DOWN'])} rank down",
             f"Research: {research['cached_count']} cached, {refreshed} Luna refreshed, "
             f"{research['luna_calls']} Luna calls, {deferred} deferred, "
             f"{research['deep_pending']} Deep pending (no Sol called)",
             f"Entry: {counts['BUY_TOMORROW_CANDIDATE']} BUY TOMORROW, "
             f"{counts['WATCH']} WATCH, {counts['REJECT_ENTRY']} REJECT ENTRY",
             "Portfolio: cash $0; suggested allocation $0 (NO_CAPITAL_CONFIGURED)",
             "Top 5 candidates:"]
    status = {e["ticker"]: e["entry_state"] for e in entries}
    research_status = {c["ticker"]: c["final_status"] for c in cases}
    for item in top:
        ticker = item["ticker"]
        label = status.get(ticker) or research_status.get(ticker, "NOT_RESEARCHED")
        lines.append(f"#{item['rank']} {ticker} {item['candidate_score']:.1f} | {label}")
    insufficient = [e["ticker"] for e in entries if e.get("signal_validity") == "DATA_INSUFFICIENT"]
    if insufficient:
        lines.append("Entry data insufficient (not a scored rejection): " + ", ".join(insufficient))
    lines += ["Post-close research is for the next session, not a pre-close signal.",
              "No trade was placed."]
    message = "\n".join(lines)
    if len(message) > 3900:
        raise ValueError("DAY1_TELEGRAM_TOO_LONG")
    return message


def send():
    report_bytes = REPORT.read_bytes()
    report = json.loads(report_bytes)
    message = render(report)
    load_env(ROOT / ".env")
    token = os.environ.get("STOCK_TELEGRAM_BOT_TOKEN") or os.environ.get("STOCK_TELEGRAM_TOKEN")
    chat_id = os.environ.get("STOCK_TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        raise ValueError("STOCK_TELEGRAM_CREDENTIALS_MISSING")
    with connect() as db:
        current = row(db)
        if current["state"] == "COMPLETE":
            return {"status": "ALREADY_COMPLETE", "message_id": current["telegram_message_id"]}
        if (action(current) != "TELEGRAM_ONLY" or
                current["result_sha256"] != hashlib.sha256(report_bytes).hexdigest()):
            raise ValueError("DAY1_TELEGRAM_STATE_OR_REPORT_MISMATCH")
        key = claim_telegram(db)
        try:
            reply = request_json(f"https://api.telegram.org/bot{token}/sendMessage",
                                 {"chat_id": chat_id, "text": message}, retry=False)
        except Exception:
            # A request may have reached Telegram. Reconciliation is required; never resend blindly.
            telegram_failed(db, key, certain_no_send=False)
            raise
        message_id = (reply.get("result") or {}).get("message_id") if reply.get("ok") else None
        if not isinstance(message_id, int) or message_id <= 0:
            telegram_failed(db, key, certain_no_send=False)
            raise ValueError("TELEGRAM_SEND_UNCONFIRMED")
        telegram_sent(db, key, message_id)
    receipt = {"status": "SENT", "message_id": message_id,
               "idempotency_key": key, "result_sha256": hashlib.sha256(report_bytes).hexdigest()}
    temporary = RECEIPT.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(receipt, indent=2) + "\n")
    temporary.replace(RECEIPT)
    return receipt


if __name__ == "__main__":
    print(json.dumps(send(), indent=2))

#!/usr/bin/env python3
"""Preview or send one truthful Day 0.1 Telegram validation summary."""
from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone

from day0_1_validate import OUTPUT, ROOT
from stock_agent import load_env, request_json

RECEIPT = ROOT / "data" / "day0_1_telegram_receipt.json"
CORRECTION_RECEIPT = ROOT / "data" / "day0_1_telegram_correction_receipt.json"


def render(report):
    groups = {}
    for name in ("FINAL_RESEARCH_AVAILABLE", "DEEP_RESEARCH_PENDING",
                 "INSUFFICIENT_EVIDENCE", "RESEARCH_FAILED"):
        groups[name] = [c["ticker"] for c in report["research_cases"] if c["final_status"] == name]
    top15 = [c["ticker"] for c in report["research_cases"]]
    return "\n".join([
        "AVERRA STOCKS", "DAY 0.1 VALIDATION | PARTIAL", "",
        f"Candidate Top 15: {', '.join(top15)}",
        f"Initial Research Complete: {report['initial_complete_new']} new; CDNA valid cache",
        f"Deep Pending ({len(groups['DEEP_RESEARCH_PENDING'])}): {', '.join(groups['DEEP_RESEARCH_PENDING']) or 'none'}",
        f"Insufficient Evidence ({len(groups['INSUFFICIENT_EVIDENCE'])}): {', '.join(groups['INSUFFICIENT_EVIDENCE']) or 'none'}",
        f"Research Failed ({len(groups['RESEARCH_FAILED'])}): {', '.join(groups['RESEARCH_FAILED']) or 'none'}",
        "", f"Market Data As Of: {report['market_data_as_of']} close",
        f"Entry: Current {report['entry_counts']['ENTRY_DATA_CURRENT']} | Stale {report['entry_counts']['ENTRY_DATA_STALE']} | Not Available {report['entry_counts']['ENTRY_NOT_AVAILABLE']}",
        "Waiting Next Market Data: no 9/25 BUY_TOMORROW backfill; no realtime BUY_NOW.",
        "Portfolio: Cash $0 | NO_CAPITAL_CONFIGURED | Suggested Allocation $0",
        "", f"Luna {report['cost']['luna_calls']} calls, {report['cost']['input_tokens']} input / {report['cost']['output_tokens']} output tokens",
        f"Local usage-based cost estimate ${report['cost']['actual_or_reserved_cost_usd']:.5f}; Sol 0 calls.",
        "HHH cited an evidence ID absent from its frozen packet; raw response retained, no retry.",
        "No BUY recommendation. No order placed.",
    ])


def validate_summary(report, message):
    cases = report["research_cases"]
    expected = {name: sum(c["final_status"] == name for c in cases) for name in
                ("FINAL_RESEARCH_AVAILABLE", "DEEP_RESEARCH_PENDING",
                 "INSUFFICIENT_EVIDENCE", "RESEARCH_FAILED")}
    if len(cases) != 15 or expected != report["research_counts"] or sum(expected.values()) != 15:
        raise ValueError("SUMMARY_RESEARCH_COUNTS_MISMATCH")
    for case in cases:
        escalation = case.get("escalation") or {}
        if (escalation.get("decision") == "ESCALATE_SOL" and
                case["final_status"] != "DEEP_RESEARCH_PENDING"):
            raise ValueError("SUMMARY_DEEP_PENDING_MISCLASSIFIED")
        if (case["research_status"] == "INSUFFICIENT_EVIDENCE" and
                case["cache_status"] not in {"INSUFFICIENT_EVIDENCE", "UNAVAILABLE"} and
                not escalation):
            raise ValueError("SUMMARY_MISSING_ESCALATION_EVALUATION")
    if (f"Deep Pending ({expected['DEEP_RESEARCH_PENDING']})" not in message or
            f"Insufficient Evidence ({expected['INSUFFICIENT_EVIDENCE']})" not in message or
            f"Research Failed ({expected['RESEARCH_FAILED']})" not in message or
            "No BUY recommendation." not in message):
        raise ValueError("SUMMARY_TEXT_MISMATCH")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--send", action="store_true")
    parser.add_argument("--correction", action="store_true")
    args = parser.parse_args()
    report = json.loads(OUTPUT.read_text())
    if report["status"] != "DAY0_1_PARTIAL":
        raise SystemExit("REPORT_STATUS_CHANGED")
    message = render(report)
    receipt_path = CORRECTION_RECEIPT if args.correction else RECEIPT
    if args.correction:
        if not RECEIPT.exists():
            raise SystemExit("ORIGINAL_SUMMARY_RECEIPT_MISSING")
        previous_id = json.loads(RECEIPT.read_text())["telegram_message_id"]
        message = (f"CORRECTION to Day 0.1 message #{previous_id}: the earlier Insufficient Evidence "
                   "count omitted the frozen hard-escalation check. No new API calls were made.\n\n" + message)
    validate_summary(report, message)
    if not args.send:
        print(message)
        return
    if receipt_path.exists():
        raise SystemExit("DAY0_1_TELEGRAM_ALREADY_SENT")
    load_env(ROOT / ".env")
    token = os.environ.get("STOCK_TELEGRAM_BOT_TOKEN") or os.environ.get("STOCK_TELEGRAM_TOKEN")
    chat = os.environ.get("STOCK_TELEGRAM_CHAT_ID")
    if not token or not chat:
        raise SystemExit("STOCK_BOT_CREDENTIALS_MISSING")
    try:
        result = request_json(f"https://api.telegram.org/bot{token}/sendMessage",
                              {"chat_id": chat, "text": message}, retry=False)
    except Exception as exc:
        raise SystemExit(f"DAY0_1_SEND_UNCERTAIN:{type(exc).__name__}; do not auto-retry") from None
    if not result.get("ok"):
        raise SystemExit("DAY0_1_SEND_UNCERTAIN: no Telegram confirmation; do not auto-retry")
    receipt = {"sent_at": datetime.now(timezone.utc).isoformat(),
               "telegram_message_id": result["result"]["message_id"]}
    receipt_path.write_text(json.dumps(receipt, indent=2) + "\n")
    print("DAY0_1_TELEGRAM_CONFIRMED", receipt["telegram_message_id"])


if __name__ == "__main__":
    main()

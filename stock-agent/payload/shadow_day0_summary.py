#!/usr/bin/env python3
"""Render and optionally send exactly one truthful Day 0 Stock Bot summary."""
from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from stock_agent import load_env, request_json

ROOT = Path(__file__).resolve().parent
REPORT = ROOT / "data" / "shadow_day0_report.json"
RECEIPT = ROOT / "data" / "shadow_day0_summary_receipt.json"


def render(report):
    market = report["market"]
    phase4 = report["phase4"]
    lines = ["AVERRA STOCKS", "SHADOW MODE | DAY 0 PARTIAL",
             f"Market: {report['asof']} daily close | Regime: {market['regime']}",
             "REALTIME_DATA_UNAVAILABLE; no BUY NOW signals.", "",
             f"Scan: US common {market['universe']} | Phase 1 {market['phase1_passed']} | Phase 2 {market['phase2_success']} scored",
             "Quant Top 100 | Candidate Top 30", "",
             f"Research: Initial new 0 | Deep cached {phase4['deep_complete_cached']} | Pending {len(phase4['research_pending'])}",
             "Entry: actionable BUY TOMORROW 0 | historical WAIT 3 (not actionable)",
             "Portfolio: Cash $0 | NO_CAPITAL_CONFIGURED", "",
             "Top 5 candidates (all allocation $0):"]
    for i, row in enumerate(report["top5_trace"], 1):
        if row["phase4_research_tier"] == "DEEP_CACHED":
            status = "cached research; historical WAIT, not actionable"
            conviction = f"{row['phase4_conviction_score']:.1f}"
            entry = f"{row['phase5_entry_score']:.1f}"
        else:
            status = "RESEARCH_PENDING"
            conviction = entry = "pending"
        lines.append(f"{i}. {row['ticker']} Candidate {row['phase3_candidate_score']:.1f} | Conviction {conviction} | Entry {entry} | {status}")
    lines += ["", "Partial failures: 14 need Initial research; latest Entry results are historical diagnostics; company IR unavailable (SEC evidence retained).",
              f"Data API requests {report['api_requests']['total_data']} | new AI tokens 0 | new AI cost $0",
              f"Trace: {report['trace_id']}", "No trade was placed."]
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--send", action="store_true")
    args = parser.parse_args()
    report = json.loads(REPORT.read_text())
    message = render(report)
    if not args.send:
        print(message)
        return
    if RECEIPT.exists():
        raise SystemExit("Summary already sent; refusing duplicate")
    load_env(ROOT / ".env")
    token = os.environ.get("STOCK_TELEGRAM_BOT_TOKEN") or os.environ.get("STOCK_TELEGRAM_TOKEN")
    chat = os.environ.get("STOCK_TELEGRAM_CHAT_ID")
    if not token or not chat:
        raise SystemExit("Stock Bot credentials missing")
    try:
        result = request_json(f"https://api.telegram.org/bot{token}/sendMessage",
                              {"chat_id": chat, "text": message}, retry=False)
    except Exception as exc:
        raise SystemExit(f"SUMMARY_SEND_UNCERTAIN: {type(exc).__name__}; do not auto-retry") from None
    if not result.get("ok"):
        raise SystemExit("SUMMARY_SEND_UNCERTAIN: Telegram did not confirm; do not auto-retry")
    receipt = {"trace_id": report["trace_id"], "sent_at": datetime.now(timezone.utc).isoformat(),
               "telegram_message_id": result["result"]["message_id"]}
    RECEIPT.write_text(json.dumps(receipt, indent=2) + "\n")
    print("SHADOW_SUMMARY_CONFIRMED", receipt["telegram_message_id"])


if __name__ == "__main__":
    main()

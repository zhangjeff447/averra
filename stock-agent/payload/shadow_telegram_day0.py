#!/usr/bin/env python3
"""One-off Telegram command validation against a disposable Stock portfolio."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from portfolio import Portfolio
from stock_agent import authorized_message, command, load_env, request_json, telegram_send

ROOT = Path(__file__).resolve().parent
READ_COMMANDS = {"/start", "/help", "/balance", "/portfolio", "/status"}


def main():
    load_env(ROOT / ".env")
    token = os.environ.get("STOCK_TELEGRAM_BOT_TOKEN") or os.environ.get("STOCK_TELEGRAM_TOKEN")
    chat_id = os.environ.get("STOCK_TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        raise SystemExit("Stock Bot credentials missing")
    started_at = time.time()
    deadline = time.monotonic() + 240
    seen = []
    denied = 0
    offset = 0
    with tempfile.TemporaryDirectory(prefix="SHADOW_TEST_PORTFOLIO-") as folder:
        book = Portfolio(Path(folder) / "portfolio.db")
        try:
            print("SHADOW_TEST_PORTFOLIO_READY", flush=True)
            while time.monotonic() < deadline:
                try:
                    response = request_json(f"https://api.telegram.org/bot{token}/getUpdates",
                                            {"offset": offset, "timeout": 0, "allowed_updates": ["message"]},
                                            retry=False)
                    if not response.get("ok"):
                        raise ValueError("Telegram getUpdates not ok")
                    for update in response.get("result", []):
                        offset = max(offset, update["update_id"] + 1)
                        message = update.get("message", {})
                        if message.get("date", 0) < started_at - 300:
                            continue
                        if not authorized_message(update, chat_id):
                            denied += 1
                            print("UNAUTHORIZED_IGNORED", flush=True)
                            continue
                        raw = message["text"].strip()
                        cmd = raw.split()[0].split("@")[0].lower()
                        if cmd not in READ_COMMANDS and raw != "/deposit 2000":
                            print("OUT_OF_SCOPE_COMMAND_IGNORED", cmd, flush=True)
                            continue
                        reply = command(book, raw, {"stocks": []}, update_id=update["update_id"], source="SHADOW_TEST")
                        telegram_send(token, chat_id, "SHADOW TEST PORTFOLIO\n" + reply)
                        seen.append(cmd)
                        print("COMMAND_OK", cmd, "CASH_CENTS", book.cash_cents(), flush=True)
                    if "/deposit" in seen and all(name in seen for name in READ_COMMANDS) and seen.count("/balance") >= 2 and seen.count("/portfolio") >= 2:
                        break
                    time.sleep(2)
                except Exception as exc:
                    print("POLL_ERROR", type(exc).__name__, flush=True)
                    time.sleep(5)
            result = {"received_commands": seen, "unauthorized_ignored": denied,
                      "test_cash_cents": book.cash_cents(), "test_ledger_rows": len(book.history(100)),
                      "test_portfolio_removed": True, "finished_at": datetime.now(timezone.utc).isoformat()}
            print("SHADOW_TEST_RESULT", json.dumps(result), flush=True)
        finally:
            book.close()


if __name__ == "__main__":
    main()

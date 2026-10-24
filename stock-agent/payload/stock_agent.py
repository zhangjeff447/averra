#!/usr/bin/env python3
"""Averra Stock Agent V1: research alerts and user-confirmed virtual accounting."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import re
import sqlite3
import time
import urllib.error
import urllib.request
from contextlib import closing
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from analysis import allocation, event_triggers, held_action, market_regime, score_stock, security_ok, universe_ok
from mock_data import snapshot as mock_snapshot
from portfolio import Portfolio, money, now, price_is_fresh, usd
from phase6_portfolio import allocate, format_candidate, load_config as phase6_config
from phase6_bridge import from_entry_databases
from holdings_monitor import confirm as confirm_holding, held as held_stocks
from exit_context import saved_entry_thesis
from exit_notifications import dispatch_alerts
from shadow_validation import mark_interrupted_sends_unknown, tick as shadow_validation_tick


ROOT = Path(__file__).resolve().parent
LOG = logging.getLogger("stock_agent")
TICKER = re.compile(r"^[A-Z][A-Z0-9.]{0,9}$")


def load_env(path: Path):
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def config():
    return json.loads((ROOT / "config.json").read_text())


def load_snapshot(mode: str, allow_stale: bool = False):
    if mode == "mock":
        return mock_snapshot()
    path = Path(os.environ.get("STOCK_DATA_FILE", ""))
    if not path.is_file():
        raise ValueError("STOCK_DATA_FILE must point to a normalized JSON snapshot")
    snap = json.loads(path.read_text())
    if not snap.get("asof") or not snap.get("market") or not snap.get("benchmarks") or not isinstance(snap.get("stocks"), list):
        raise ValueError("Invalid snapshot: asof, market, benchmarks, stocks required")
    age = (datetime.now(timezone.utc) - datetime.fromisoformat(snap["asof"].replace("Z", "+00:00"))).total_seconds()
    if not allow_stale and (age < -300 or age > 900):
        raise ValueError(f"Snapshot is stale or future-dated ({age:.0f}s); no new suggestions")
    if not allow_stale:
        market_date = datetime.fromisoformat(snap["asof"].replace("Z", "+00:00")).astimezone(ZoneInfo("America/New_York")).date().isoformat()
        for item in snap["stocks"]:
            if not item.get("bars") or item["bars"][-1].get("date") != market_date:
                raise ValueError(f"Current-day stock bar missing: {item.get('ticker', '?')}")
    return snap


def update_quotes(book: Portfolio, snap):
    for stock in snap["stocks"]:
        if universe_ok(stock):
            book.quote(stock["ticker"], stock["bars"][-1]["close"], snap["asof"])


def scan(book: Portfolio, snap, cfg, mode):
    update_quotes(book, snap)
    scored = {s["ticker"]: score_stock(s, snap) for s in snap["stocks"]}
    ordered = sorted(scored.values(), key=lambda r: (r.get("entry_score", 0), r.get("stock_score", 0)), reverse=True)
    stock_by_ticker = {s["ticker"]: s for s in snap["stocks"]}
    planned = {}
    messages = []
    for result in ordered:
        ticker = result["ticker"]
        if result["action"] == "REJECT":
            continue
        if result["action"] == "BUY NOW CANDIDATE":
            result["action"] = "BUY TOMORROW"
            result["reason"] += "; delayed feed cannot support BUY NOW"
        suggested = allocation(result, stock_by_ticker[ticker], book, snap, cfg, planned)
        if result["action"].startswith("BUY") and suggested <= 0:
            result["action"] = "WATCH"
            result["reason"] += "; allocation blocked by cash/exposure cap"
        planned[ticker] = suggested
        book.add_suggestion(ticker, result["action"], suggested, result["reason"])
        rs = result["relative_strength"]
        messages.append(f"{ticker} — {result['action']}\nStock {result['stock_score']} | Entry {result['entry_score']} | Event {result['event_score']} | Risk {result['risk_score']}\nRS 20d vs SPY/QQQ/sector: {rs['SPY']:+.1f}/{rs['QQQ']:+.1f}/{rs['SECTOR']:+.1f} pp\nSuggested allocation: {usd(suggested)} (proposal only)\n{result['reason']}")
    for pos in book.positions():
        result = scored.get(pos["ticker"])
        action = held_action(result)
        messages.append(f"{pos['ticker']} — {action} | virtual shares {float(pos['shares']):.4f}; user decides any sale")
    regime, _ = market_regime(snap)
    with book.db:
        book.db.execute("INSERT INTO scan_runs(created_at,source_at,mode,candidates) VALUES(?,?,?,?)",
                        (now(), snap["asof"], mode, len(messages)))
    header = f"AVERRA STOCKS | {mode.upper()} | {snap['asof']}\nRegime {regime} | VIX {snap['market']['vix']} VXN {snap['market']['vxn']}\nNo orders are placed. Suggested allocations are not executions."
    return header, messages[:cfg["maximum_messages_per_scan"]]


def phase6_scan(book: Portfolio, path: Path | dict, snapshot_on_close=False):
    """Consume a prepared Phase 5 candidate packet; local allocation only."""
    packet = path if isinstance(path, dict) else json.loads(path.read_text())
    if not isinstance(packet.get("candidates"), list) or not packet.get("asof"):
        raise ValueError("Phase 6 packet requires asof and candidates")
    candidates = packet["candidates"]
    if len({c.get("ticker") for c in candidates}) != len(candidates):
        raise ValueError("Duplicate Phase 6 candidate ticker")
    for c in candidates:
        if not TICKER.fullmatch(c.get("ticker", "")) or not c.get("sector") or not security_ok(c):
            raise ValueError("Phase 6 candidate must be a validated US common stock with sector")
        old_quote = book.get_quote(c["ticker"])
        if old_quote and c.get("price_at"):
            old_at = datetime.fromisoformat(old_quote["observed_at"].replace("Z", "+00:00"))
            new_at = datetime.fromisoformat(c["price_at"].replace("Z", "+00:00"))
            if new_at.tzinfo is None or new_at < old_at:
                raise ValueError(f"PRICE_STALE: {c['ticker']} candidate quote older than stored quote")
        if c.get("state") == "BUY_NOW_CANDIDATE":
            c["state"] = "BUY_TOMORROW_CANDIDATE"
    allocation_cfg = phase6_config()
    with book.atomic():
        for c in candidates:
            if c.get("price") and c.get("price_at") and price_is_fresh(c["price_at"], allocation_cfg["maximum_quote_age_hours"]):
                book.quote(c["ticker"], c["price"], c["price_at"])
        state = book.summary()
        decisions = allocate(book, candidates, asof=packet["asof"], cfg=allocation_cfg,
                             spy_close=packet.get("spy_close"), qqq_close=packet.get("qqq_close"))
        if snapshot_on_close:
            book.daily_snapshot(packet["asof"][:10], packet.get("spy_close"), packet.get("qqq_close"))
    return [format_candidate(d, state["portfolio_value_cents"], state["cash_cents"]) for d in decisions]


def classify_error(exc):
    if isinstance(exc, urllib.error.HTTPError):
        return "TRANSIENT" if exc.code in (429, 500, 502, 503, 504) else "PERMANENT"
    if isinstance(exc, (urllib.error.URLError, TimeoutError)):
        return "TRANSIENT"
    return "PERMANENT"


def request_json(url, payload=None, token=None, retry=True):
    body = json.dumps(payload).encode() if payload is not None else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    for attempt in range(4):
        try:
            req = urllib.request.Request(url, data=body, headers=headers)
            with urllib.request.urlopen(req, timeout=30) as response:
                return json.load(response)
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
            if not retry or classify_error(exc) != "TRANSIENT" or attempt == 3:
                raise
            delay = (5, 15, 30)[attempt]
            LOG.warning("Transient network error; retry in %ss (%s)", delay, type(exc).__name__)
            time.sleep(delay)


def ai_event_note(ticker, event_note, snap):
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        return "AI disabled; deterministic trigger only", False
    model = os.environ.get("STOCK_AI_MODEL", "gpt-6-sol")
    body = {"model": model, "store": False, "max_output_tokens": 220, "reasoning": {"effort": "none"},
            "instructions": "Summarize the supplied stock event in two short sentences. Do not invent facts, recommend an order, or use external tools. Say when evidence is insufficient.",
            "input": json.dumps({"ticker": ticker, "event": event_note, "asof": snap["asof"]})}
    result = request_json("https://api.openai.com/v1/responses", body, key)
    pieces = [part.get("text", "") for item in result.get("output", []) if item.get("type") == "message"
              for part in item.get("content", []) if part.get("type") == "output_text"]
    return " ".join(pieces).strip()[:1000] or "AI returned no text", True


def detect_events(book, snap, cfg):
    notes = []
    for ticker, note, fingerprint in event_triggers(snap):
        if book.db.execute("SELECT 1 FROM event_triggers WHERE fingerprint=?", (fingerprint,)).fetchone():
            continue
        day = now()[:10]
        count = book.db.execute("SELECT COUNT(*) FROM event_triggers WHERE created_at LIKE ? AND ai_called=1", (day + "%",)).fetchone()[0]
        ai_called = bool(os.environ.get("OPENAI_API_KEY")) and count < cfg["maximum_ai_calls_per_day"]
        detail = "AI call limit reached; deterministic trigger only"
        if count < cfg["maximum_ai_calls_per_day"]:
            try:
                detail, _ = ai_event_note(ticker, note, snap)
            except Exception as exc:
                LOG.error("AI event analysis failed: %s", type(exc).__name__)
                detail = "AI unavailable; deterministic trigger only"
        with book.db:
            book.db.execute("INSERT INTO event_triggers VALUES(?,?,?,?,?)", (fingerprint, now(), ticker, note, int(ai_called)))
        notes.append(f"EVENT {ticker}: {note}\n{detail}\nResearch alert only; no order placed.")
    return notes


def command(book: Portfolio, text: str, snap, update_id: int | None = None, source="TELEGRAM"):
    if update_id is None:
        return _command(book, text, snap, source)
    with book.atomic():
        prior = book.db.execute("SELECT reply_text FROM processed_updates WHERE update_id=?", (update_id,)).fetchone()
        if prior:
            return prior["reply_text"]
        reply = _command(book, text, snap, source)
        book.db.execute("INSERT INTO processed_updates(update_id,created_at,reply_text) VALUES(?,?,?)",
                        (update_id, now(), reply))
        return reply


def latest_shadow_record(path=None, complete_only=False):
    """Inspect scheduler state without creating or migrating its database."""
    path = Path(path) if path else ROOT / "data" / "shadow_daily_state.db"
    if not path.is_file():
        return None
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        where = " AND state='COMPLETE'" if complete_only else ""
        row = db.execute("SELECT * FROM shadow_daily_runs WHERE run_type='SHADOW_DAILY'" +
                         where + " ORDER BY trading_date DESC LIMIT 1").fetchone()
        return dict(row) if row else None


def book_data_dir(book):
    filename = book.db.execute("PRAGMA database_list").fetchone()[2]
    return Path(filename).parent if filename else ROOT / "data"


def shadow_candidates(state_path=None):
    item = latest_shadow_record(state_path, complete_only=True)
    if not item or not item.get("result_path"):
        return None
    raw = Path(item["result_path"]).read_bytes()
    if hashlib.sha256(raw).hexdigest() != item["result_sha256"]:
        raise ValueError("Saved daily result failed integrity check")
    report = json.loads(raw)
    if report.get("status") != "PIPELINE_COMPLETE" or report.get("trading_date") != item["trading_date"]:
        raise ValueError("Saved daily result is incomplete")
    entries = {r["ticker"]: r for r in report.get("entry_results", [])}
    lines = [f"Saved candidates as of {item['trading_date']} for {report['target_session']}",
             "Historical observations; confirm a purchase with /bought TICKER."]
    for candidate in sorted(report["eligibility_cases"], key=lambda c: c["candidate_rank"])[:10]:
        ticker = candidate["ticker"]
        entry = entries.get(ticker, {})
        lines.append(f"#{candidate['candidate_rank']} {ticker}: "
                     f"{entry.get('entry_state', candidate['eligibility'])} | "
                     f"Entry {entry.get('entry_score', 'N/A')}")
    shown = {c["ticker"] for c in sorted(report["eligibility_cases"], key=lambda c: c["candidate_rank"])[:10]}
    for result in report["entry_results"]:
        if result["entry_state"] == "BUY_TOMORROW_CANDIDATE" and result["ticker"] not in shown:
            lines.append(f"BUY TOMORROW {result['ticker']}: Entry {result['entry_score']}")
    return "\n".join(lines + ["No trade was placed."])


def command_snapshot(book, text, market_path=None):
    """Use the cached security master for ticker confirmations; never fetch data."""
    parts = text.strip().split()
    if len(parts) < 2 or parts[0].split("@")[0].lower() != "/bought":
        return {"stocks": []}
    ticker = parts[1].upper()
    if not TICKER.fullmatch(ticker):
        return {"stocks": []}
    path = Path(market_path) if market_path else book_data_dir(book) / "market_data.db"
    if not path.is_file():
        return {"stocks": []}
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        row = db.execute("SELECT * FROM securities WHERE ticker=?", (ticker,)).fetchone()
        stock = dict(row) if row else None
        if not stock or not security_ok(stock) or not stock.get("is_active", True):
            return {"stocks": []}
        stock["sector"] = stock.get("normalized_sector") or stock.get("sector") or "Unknown"
        return {"stocks": [stock]}


def _command(book: Portfolio, text: str, snap, source):
    parts = text.strip().split()
    if not parts:
        return "Send /help for commands."
    cmd = parts[0].split("@")[0].lower()
    try:
        if cmd in ("/ops", "/settings_history", "/research_history"):
            from ops_tools import query
            return query(cmd,parts,root=book_data_dir(book).parent)
        if cmd in ("/start", "/help"):
            return "Commands: /bought TICKER [PER_SHARE_PRICE], /sold TICKER [PER_SHARE_PRICE] (full exit), /portfolio, /candidates, /skip [TICKER], /history, /status, /ops, /settings_history, /research_history TICKER [words]. No dollar amount or share count is tracked. No orders are placed."
        if cmd in ("/deposit", "/balance"):
            return "Cash and allocation tracking are retired. Use /portfolio to see confirmed holdings."
        if cmd == "/portfolio" and len(parts) == 1:
            holdings = held_stocks(book.db)
            if not holdings:
                return "No confirmed holdings. Send /bought TICKER after you buy."
            lines = ["Confirmed holdings:"]
            for item in holdings:
                row = book.db.execute("SELECT details_json FROM exit_decisions WHERE ticker=? AND lifecycle_id=? "
                                      "ORDER BY trading_date DESC LIMIT 1",
                                      (item["ticker"], item["confirmed_at"])).fetchone()
                entry = f"${item['entry_price']:.2f}" if item["entry_price"] is not None else "UNKNOWN"
                if not row:
                    lines.append(f"{item['ticker']} | Entry {entry} | awaiting next completed close review")
                    continue
                decision = json.loads(row[0]); position = decision.get("position", {})
                percent = position.get("return_since_entry_pct")
                changed = f"{percent:+.1f}%" if percent is not None else "unavailable"
                current = position.get("current_price")
                current_text = f"${current:.2f}" if current is not None else "unavailable"
                drawdown = position.get("drawdown_from_peak_pct")
                drawdown_text = f"{drawdown:.1f}%" if drawdown is not None else "unavailable"
                peak = position.get("peak_return_pct")
                peak_text = f"{peak:+.1f}%" if peak is not None else "unavailable"
                trend = decision.get("trend", {})
                event = decision.get("event", {})
                thesis = decision.get("thesis", {})
                zone = decision.get("profit_protection_zone")
                trend_zone = decision.get("trend_failure_zone")
                lines.append(f"{item['ticker']} {decision['action']} | Entry {entry} | Current "
                             f"{current_text} | Return {changed}\n"
                             f"Peak return {peak_text} | Peak drawdown {drawdown_text} | "
                             f"Profit {position.get('profit_state', 'unavailable')} | "
                             f"RS {decision.get('relative_strength', {}).get('state', 'unavailable')} | "
                             f"Thesis {thesis.get('state', 'unavailable')} | "
                             f"Evidence IDs {', '.join(thesis.get('evidence_ids', [])) or 'none'}\n"
                             f"Trend {trend.get('state', 'unavailable')} | Support {trend.get('support', 'unavailable')} | "
                             f"EMA21 {trend.get('ema21', 'unavailable')} | ATR {position.get('atr', 'unavailable')} | "
                             f"Earnings {event.get('upcoming_earnings') or event.get('state', 'unavailable')}\n"
                             f"Exit Score {decision.get('exit_score')} | Confidence {decision.get('exit_confidence')} | "
                             f"Reasons {', '.join(decision.get('reasons', []))}\n"
                             f"Profit Protection Zone {zone or 'unavailable'} | "
                             f"Trend Failure Zone {trend_zone or 'unavailable'}")
            return "\n\n".join(lines) + "\nReference zones only. No order was placed."
        if cmd in ("/bought", "/sold") and len(parts) in (2, 3):
            ticker = parts[1].upper()
            if not TICKER.fullmatch(ticker):
                raise ValueError("Invalid ticker")
            if cmd == "/bought":
                stock = next((s for s in snap["stocks"] if s["ticker"] == ticker and security_ok(s)
                              and s.get("is_active", True)), None)
                if stock is None:
                    raise ValueError("Ticker not in the validated common-stock universe")
            with book.atomic():
                result = confirm_holding(book.db, ticker, "BOUGHT" if cmd == "/bought" else "SOLD",
                                         source, price=parts[2] if len(parts) == 3 else None,
                                         entry_thesis=saved_entry_thesis(ticker, book_data_dir(book) / "research_data.db")
                                         if cmd == "/bought" else None)
            if result == "ALREADY_HELD":
                return f"{ticker} already tracked as held. Existing entry price was not changed."
            if cmd == "/bought":
                note = (f"Entry price ${float(parts[2]):.2f} (user-confirmed)." if len(parts) == 3 else
                        "Entry price unknown. Profit-protection features are limited.")
                return f"{ticker} now tracked as held for the next intraday risk check and daily exit review. {note} No broker order was placed."
            return f"{ticker} removed from daily exit checks. " + (
                f"Exit price ${float(parts[2]):.2f} (user-confirmed). " if len(parts) == 3 else
                "Exit price unknown. ") + "No broker order was placed."
        if cmd in ("/bought", "/sold"):
            return f"Use /{cmd[1:]} TICKER [PER_SHARE_PRICE]. Amounts and share counts are not tracked."
        if cmd == "/skip" and len(parts) <= 2:
            ticker = parts[1].upper() if len(parts) == 2 else None
            return "Suggestion marked skipped." if book.skip(ticker) else "No open suggestion found."
        if cmd == "/history" and len(parts) == 1:
            rows = book.db.execute("SELECT confirmed_at,kind,ticker FROM holding_events ORDER BY id DESC LIMIT 20")
            return "\n".join(f"{r['confirmed_at']} {r['kind']} {r['ticker']}" for r in rows) or "No confirmed holding changes."
        if cmd == "/candidates" and len(parts) == 1:
            latest = shadow_candidates(book_data_dir(book) / "shadow_daily_state.db")
            if latest:
                return latest
            return "No completed daily candidate report yet."
        if cmd == "/status" and len(parts) == 1:
            daily = latest_shadow_record(book_data_dir(book) / "shadow_daily_state.db")
            daily_text = (f"Daily run: {daily['trading_date']} {daily['state']}\n"
                          f"Data as of: {daily.get('market_data_as_of') or 'unavailable'}\n"
                          f"Summary delivery: {daily.get('telegram_status')} | message ID {daily.get('telegram_message_id') or 'none'}") if daily else "Daily run: not initialized"
            if daily and daily.get("failed_phase") and daily["state"] == "FAILED":
                daily_text += f"\nFailed phase: {daily['failed_phase']} ({daily.get('error_type')})"
            unknown = book.db.execute("SELECT COUNT(*) FROM telegram_command_receipts WHERE state IN ('UNKNOWN','IN_FLIGHT')").fetchone()[0]
            return f"AVERRA STOCKS | MONITOR ONLY\n{daily_text}\nCommand replies awaiting reconciliation: {unknown}\nTracked holdings: {len(held_stocks(book.db))}\nBUY NOW: OFF\nNo broker or order connection."
        return "Invalid syntax. Send /help."
    except (ValueError, ArithmeticError) as exc:
        return f"Rejected: {exc}"
    except OSError:
        return "Unavailable: local stored data could not be read. No transaction was recorded."


def telegram_send(token, chat_id, message):
    # A failed send is not retried: Telegram sendMessage has no idempotency key.
    response = request_json(f"https://api.telegram.org/bot{token}/sendMessage", {"chat_id": chat_id, "text": message[:3900]}, retry=False)
    message_id = (response.get("result") or {}).get("message_id") if response.get("ok") else None
    if not isinstance(message_id, int) or isinstance(message_id, bool) or message_id <= 0:
        raise ValueError("TELEGRAM_SEND_UNCONFIRMED")
    return message_id


def authorized_message(update, chat_id):
    message = update.get("message", {})
    chat = message.get("chat", {})
    sender = message.get("from", {})
    return (str(chat.get("id")) == str(chat_id)
            and chat.get("type", "private") == "private"
            and str(sender.get("id")) == str(chat_id)
            and isinstance(message.get("text"), str))


def process_telegram_update(book, update, token, chat_id, *, sender=telegram_send, snapshotter=command_snapshot):
    """Commit one user command once, then independently claim its reply once."""
    update_id = update.get("update_id")
    if not isinstance(update_id, int) or isinstance(update_id, bool):
        raise ValueError("INVALID_TELEGRAM_UPDATE_ID")
    if authorized_message(update, chat_id):
        text = update["message"]["text"]
        # /bought with an actual fill can still use prior validated candidate data.
        try:
            snap = snapshotter(book, text)
        except (ValueError, sqlite3.Error, OSError):
            snap = {"stocks": []}
        reply = command(book, text, snap, update_id=update_id)
        with book.atomic():
            book.db.execute("INSERT OR IGNORE INTO telegram_command_receipts(update_id,state) VALUES(?,'PENDING')", (update_id,))
            claimed = book.db.execute("UPDATE telegram_command_receipts SET state='IN_FLIGHT',attempted_at=? WHERE update_id=? AND state='PENDING'",
                                      (now(), update_id)).rowcount
            if not claimed:
                book.db.execute("UPDATE telegram_command_receipts SET state='UNKNOWN',error_type='INTERRUPTED_SEND' WHERE update_id=? AND state='IN_FLIGHT'", (update_id,))
        if claimed:
            try:
                message_id = sender(token, chat_id, reply)
                if not isinstance(message_id, int) or isinstance(message_id, bool) or message_id <= 0:
                    raise ValueError("TELEGRAM_SEND_UNCONFIRMED")
            except Exception as exc:
                # A possibly delivered reply must not be automatically sent again.
                with book.atomic():
                    book.db.execute("UPDATE telegram_command_receipts SET state='UNKNOWN',completed_at=?,error_type=? WHERE update_id=?",
                                    (now(), type(exc).__name__, update_id))
                LOG.error("Telegram command reply unconfirmed: %s", type(exc).__name__)
            else:
                with book.atomic():
                    book.db.execute("UPDATE telegram_command_receipts SET state='SENT',completed_at=?,message_id=? WHERE update_id=?",
                                    (now(), message_id, update_id))
    with book.atomic():
        row = book.db.execute("SELECT value FROM runtime_state WHERE key='telegram_offset'").fetchone()
        offset = max(int(row[0]) if row else 0, update_id + 1)
        book.db.execute("INSERT INTO runtime_state VALUES('telegram_offset',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(offset),))
    return offset


def serve(book, cfg, mode, *, commands_only=False):
    token = os.environ.get("STOCK_TELEGRAM_BOT_TOKEN") or os.environ.get("STOCK_TELEGRAM_TOKEN")
    chat_id = os.environ.get("STOCK_TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        raise ValueError("Independent STOCK_TELEGRAM_BOT_TOKEN and STOCK_TELEGRAM_CHAT_ID required")
    lock_path = ROOT / "data" / "agent.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock = lock_path.open("w")
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("Stock Agent is already running") from exc
        _serve_locked(book, cfg, mode, token, chat_id, commands_only=commands_only)
    finally:
        lock.close()


def _serve_locked(book, cfg, mode, token, chat_id, *, commands_only):
    alerts_enabled = not commands_only and os.environ.get("STOCK_ALERTS_ENABLED", "false").lower() == "true"
    phase6_file = os.environ.get("STOCK_PHASE6_CANDIDATES_FILE")
    phase6_entry_db = os.environ.get("STOCK_PHASE6_ENTRY_DB")
    tz = ZoneInfo(cfg["timezone"])
    row = book.db.execute("SELECT value FROM runtime_state WHERE key='last_scan'").fetchone()
    last_scan = row["value"] if row else None
    offset = int(book.db.execute("SELECT value FROM runtime_state WHERE key='telegram_offset'").fetchone()[0]) if book.db.execute("SELECT value FROM runtime_state WHERE key='telegram_offset'").fetchone() else 0
    with book.atomic():
        book.db.execute("UPDATE telegram_command_receipts SET state='UNKNOWN',error_type='PROCESS_INTERRUPTED' WHERE state='IN_FLIGHT'")
        book.db.execute("UPDATE exit_highlight_alerts SET status='UNKNOWN',completed_at=? WHERE status='IN_FLIGHT'", (now(),))
    if commands_only:
        mark_interrupted_sends_unknown()
    LOG.info("Starting independent stock monitor; mode=%s; commands_only=%s", mode, commands_only)
    while True:
        clock = datetime.now(tz)
        slot = clock.strftime("%Y-%m-%d %H:%M")
        weekday = clock.weekday() < 5
        if alerts_enabled and weekday and clock.strftime("%H:%M") in cfg["scan_times"] and slot != last_scan:
            try:
                if phase6_file:
                    packet = Path(phase6_file)
                elif phase6_entry_db:
                    packet = from_entry_databases(phase6_entry_db, os.environ.get("STOCK_PHASE6_MARKET_DB", str(ROOT / "data" / "market_data.db")))
                else:
                    raise ValueError("Phase 6 alerts require a candidate file or completed Phase 5 database; legacy scan disabled")
                items = phase6_scan(book, packet, snapshot_on_close=clock.strftime("%H:%M") == "16:05")
                last_scan = slot
                with book.db:
                    book.db.execute("INSERT INTO runtime_state VALUES('last_scan',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (slot,))
                for item in items:
                    telegram_send(token, chat_id, item)
            except Exception as exc:
                LOG.exception("Scan failed (%s)", type(exc).__name__)
        try:
            response = request_json(f"https://api.telegram.org/bot{token}/getUpdates", {"offset": offset, "timeout": 0, "allowed_updates": ["message"]})
            if not response.get("ok"):
                raise ValueError("Telegram getUpdates returned not ok")
            for update in response.get("result", []):
                offset = process_telegram_update(book, update, token, chat_id)
            if commands_only:
                try:
                    dispatch_alerts(book, ROOT / "data" / "shadow_daily_state.db", token, chat_id, telegram_send)
                    shadow_validation_tick(ROOT / "data" / "shadow_daily_state.db",
                                           ROOT / "data" / "shadow_daily_results.db",
                                           ROOT / "data" / "stock_agent.db", token, chat_id, telegram_send)
                except (sqlite3.Error, OSError, ValueError, KeyError, TypeError) as exc:
                    LOG.error("Shadow reporting failed: %s", type(exc).__name__)
            with book.atomic():
                book.db.execute("INSERT INTO runtime_state VALUES('telegram_last_poll_at',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (now(),))
            time.sleep(2)
        except Exception as exc:
            LOG.error("Telegram poll failed: %s", type(exc).__name__)
            time.sleep(5)


def main():
    load_env(ROOT / ".env")
    parser = argparse.ArgumentParser(description="Averra Stock Agent V1")
    parser.add_argument("action", choices=("simulate", "scan", "events", "command", "serve", "serve-commands", "phase6-scan", "phase6-from-entry"))
    parser.add_argument("words", nargs="*")
    args = parser.parse_args()
    if args.action in ("scan", "serve", "phase6-scan", "phase6-from-entry"):
        parser.error("Legacy allocation workflow is retired; use the scheduled daily monitor and serve-commands")
    if args.action == "simulate" and args.words:
        parser.error("simulate takes no amount")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    mode = os.environ.get("STOCK_MODE", "mock")
    db_path = os.environ.get("STOCK_DB_FILE", str(ROOT / "data" / "stock_agent.db"))
    if args.action == "simulate":
        from phase6_simulation import run as simulation_run
        print(json.dumps(simulation_run(), indent=2))
        return
    else:
        log_path = ROOT / "data" / "stock_agent.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(log_path, maxBytes=2_000_000, backupCount=2)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        LOG.addHandler(handler)
    book = Portfolio(db_path)
    cfg = config()
    try:
        if args.action in ("serve", "serve-commands"):
            serve(book, cfg, mode, commands_only=args.action == "serve-commands")
        else:
            if args.action not in ("command", "phase6-scan", "phase6-from-entry"):
                snap = load_snapshot(mode)
            elif args.action == "command" and args.words and args.words[0].lower() == "/bought":
                snap = command_snapshot(book, " ".join(args.words))
            else:
                snap = {"stocks": []}
            if args.action not in ("command", "events", "phase6-scan", "phase6-from-entry"):
                update_quotes(book, snap)
            if args.action == "scan":
                header, items = scan(book, snap, cfg, mode)
                print(header, *items, sep="\n\n")
            elif args.action == "events":
                print("\n".join(detect_events(book, snap, cfg)) or "No new event triggers; no AI call")
            elif args.action == "phase6-scan":
                if len(args.words) != 1:
                    parser.error("phase6-scan requires a prepared candidate JSON file")
                print("\n\n".join(phase6_scan(book, Path(args.words[0]))))
            elif args.action == "phase6-from-entry":
                if args.words:
                    parser.error("phase6-from-entry takes no arguments")
                packet = from_entry_databases(os.environ.get("STOCK_PHASE6_ENTRY_DB", str(ROOT / "data" / "entry_data.db")),
                                              os.environ.get("STOCK_PHASE6_MARKET_DB", str(ROOT / "data" / "market_data.db")))
                print("\n\n".join(phase6_scan(book, packet)))
            else:
                print(command(book, " ".join(args.words), snap, source="CLI"))
    finally:
        book.close()


if __name__ == "__main__":
    main()

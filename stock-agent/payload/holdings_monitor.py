"""Ticker-only, user-confirmed holdings and deterministic close-based exit checks."""
from __future__ import annotations

import json
import math
import re
import sqlite3
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from zoneinfo import ZoneInfo

from market_regime import market_session, previous_session

ROOT = Path(__file__).resolve().parent
ET = ZoneInfo("America/New_York")
TICKER = re.compile(r"^[A-Z][A-Z0-9.]{0,9}$")


def config():
    return json.loads((ROOT / "holdings_config.json").read_text())


def held(db):
    return [dict(row) for row in db.execute(
        "SELECT ticker,status,confirmed_at,entry_date,entry_price,entry_price_source,entry_thesis_json "
        "FROM tracked_holdings WHERE status='HELD' ORDER BY ticker")]


def _confirmed_price(value):
    if value is None:
        return None
    try:
        price = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError("Invalid per-share price") from exc
    if not price.is_finite() or not 0 < price <= 10_000_000 or price.as_tuple().exponent < -4:
        raise ValueError("Invalid per-share price")
    return float(price)


def _held_sessions(start, end):
    day = date.fromisoformat(start)
    last = date.fromisoformat(end)
    count = 0
    while day < last:
        day += timedelta(days=1)
        count += market_session(day)
    return count


def confirm(db, ticker, kind, source="TELEGRAM", price=None, entry_thesis=None):
    ticker = ticker.upper()
    if not TICKER.fullmatch(ticker) or kind not in ("BOUGHT", "SOLD"):
        raise ValueError("Invalid holding confirmation")
    previous = db.execute("SELECT status FROM tracked_holdings WHERE ticker=?", (ticker,)).fetchone()
    if kind == "BOUGHT" and previous and previous[0] == "HELD":
        return "ALREADY_HELD"
    if kind == "SOLD" and (not previous or previous[0] != "HELD"):
        raise ValueError("Ticker is not in your confirmed holdings")
    price = _confirmed_price(price)
    stamp = datetime.now(timezone.utc).isoformat()
    entry_date = datetime.fromisoformat(stamp).astimezone(ET).date().isoformat()
    if kind == "BOUGHT":
        db.execute("INSERT INTO tracked_holdings(ticker,status,confirmed_at,entry_date,entry_price,entry_price_source,entry_thesis_json) "
                   "VALUES(?,'HELD',?,?,?,?,?) ON CONFLICT(ticker) DO UPDATE SET "
                   "status='HELD',confirmed_at=excluded.confirmed_at,entry_date=excluded.entry_date,"
                   "entry_price=excluded.entry_price,entry_price_source=excluded.entry_price_source,"
                   "entry_thesis_json=excluded.entry_thesis_json,closed_at=NULL,exit_price=NULL,exit_price_source=NULL",
                   (ticker, stamp, entry_date, price, "USER_CONFIRMED_PRICE" if price is not None else
                    "ENTRY_PRICE_UNKNOWN", json.dumps(entry_thesis) if entry_thesis else None))
        metrics = None
    else:
        held_row = db.execute("SELECT entry_date,entry_price,confirmed_at FROM tracked_holdings WHERE ticker=?", (ticker,)).fetchone()
        saved = db.execute("SELECT details_json FROM exit_decisions WHERE ticker=? AND lifecycle_id=? "
                           "ORDER BY trading_date DESC LIMIT 1", (ticker, held_row[2])).fetchone()
        observed = json.loads(saved[0]).get("position", {}) if saved else {}
        metrics = {"holding_days": _held_sessions(held_row[0], entry_date),
                   "realized_return_pct": round((price / held_row[1] - 1) * 100, 3)
                   if price is not None and held_row[1] else None,
                   "maximum_gain_while_held_pct": observed.get("maximum_gain_while_held_pct"),
                   "maximum_drawdown_while_held_pct": observed.get("maximum_drawdown_while_held_pct")}
        db.execute("UPDATE tracked_holdings SET status='CLOSED',closed_at=?,exit_price=?,exit_price_source=? WHERE ticker=?",
                   (stamp, price, "USER_CONFIRMED_PRICE" if price is not None else "EXIT_PRICE_UNKNOWN", ticker))
        db.execute("UPDATE exit_highlight_alerts SET status='CANCELLED' WHERE ticker=? AND lifecycle_id=? AND status='PENDING'",
                   (ticker, held_row[2]))
    db.execute("INSERT INTO holding_events(ticker,kind,confirmed_at,source,price,price_source,metrics_json) "
               "VALUES(?,?,?,?,?,?,?)", (ticker, kind, stamp, source, price,
               "USER_CONFIRMED_PRICE" if price is not None else
               "ENTRY_PRICE_UNKNOWN" if kind == "BOUGHT" else "EXIT_PRICE_UNKNOWN",
               json.dumps(metrics) if metrics else None))
    return kind


def _ema(values, period):
    result = sum(values[:period]) / period
    weight = 2 / (period + 1)
    for value in values[period:]:
        result = weight * value + (1 - weight) * result
    return result


def _rsi(values, period):
    changes = [after - before for before, after in zip(values[-period-1:-1], values[-period:])]
    gain = sum(max(change, 0) for change in changes) / period
    loss = sum(max(-change, 0) for change in changes) / period
    return 100 if loss == 0 and gain > 0 else 50 if loss == gain == 0 else 100 - 100 / (1 + gain / loss)


def _atr(rows, period=14):
    window = rows[-period-1:]
    return sum(max(current[2] - current[3], abs(current[2] - prior[1]),
                   abs(current[3] - prior[1])) for prior, current in zip(window, window[1:])) / period


def _valid_bar(row):
    try:
        close, high, low = (float(row[field]) for field in (1, 2, 3))
        return row[5] == "OK" and all(math.isfinite(value) and value > 0 for value in
                                      (close, high, low)) and low <= close <= high
    except (TypeError, ValueError, OverflowError):
        return False


def evaluate(market, ticker, day, confirmed_at, cfg=None):
    """A post-close check. Never uses bars after `day` or a pre-confirmation session."""
    cfg = cfg or config()
    if day <= datetime.fromisoformat(confirmed_at).astimezone(ET).date().isoformat():
        return {"ticker": ticker, "action": "WAIT_NEXT_SESSION", "reason": "BUY_CONFIRMED_THIS_SESSION"}
    count = cfg["confirmation_bars"]
    if (count < 1 or cfg["minimum_daily_bars"] < cfg["sma_period"] + count - 1 or
            cfg["minimum_daily_bars"] < max(cfg["rsi_period"], cfg["trailing_lookback_bars"], 15) + 1 or
            cfg["trailing_lookback_bars"] < 2):
        raise ValueError("Invalid holdings monitor config")
    rows = market.execute("SELECT date,close,high,low,source,quality_status FROM daily_bars "
                          "WHERE ticker=? AND date<=? ORDER BY date DESC LIMIT ?",
                          (ticker, day, cfg["minimum_daily_bars"] + count)).fetchall()
    bought_day = datetime.fromisoformat(confirmed_at).astimezone(ET).date().isoformat()
    if (len(rows) < cfg["minimum_daily_bars"] or rows[0][0] != day or
            rows[0][4] != cfg["current_daily_source"] or any(not _valid_bar(row) for row in rows)):
        return {"ticker": ticker, "action": "HOLD_DATA_INSUFFICIENT", "reason": "MISSING_OR_STALE_DAILY_BARS"}
    expected = date.fromisoformat(day)
    for row in rows[:cfg["minimum_daily_bars"]]:
        if row[0] != expected.isoformat():
            return {"ticker": ticker, "action": "HOLD_DATA_INSUFFICIENT", "reason": "MISSING_DAILY_SESSION"}
        expected = previous_session(expected)
    if any(row[0] <= bought_day for row in rows[:count]):
        return {"ticker": ticker, "action": "HOLD", "reason": "AWAITING_POST_BUY_CONFIRMATION_BARS"}
    chronological = list(reversed(rows))
    closes = [float(row[1]) for row in chronological]
    if any(abs(after / before - 1) * 100 > cfg["max_single_day_move_pct"]
           for before, after in zip(closes, closes[1:])):
        return {"ticker": ticker, "action": "HOLD_DATA_INSUFFICIENT", "reason": "PRICE_DISCONTINUITY_REVIEW"}
    atr = _atr(chronological)
    ema21 = _ema(closes, cfg["ema_period"])
    sma50 = sum(closes[-cfg["sma_period"]:]) / cfg["sma_period"]
    rsi = _rsi(closes, cfg["rsi_period"])
    below_sma = all(closes[-offset] < sum(closes[-offset-cfg["sma_period"]+1:len(closes)-offset+1]) /
                    cfg["sma_period"] for offset in range(1, count + 1))
    below_ema = all(closes[-offset] < _ema(closes[:len(closes)-offset+1], cfg["ema_period"])
                    for offset in range(1, count + 1))
    recent = closes[-cfg["trailing_lookback_bars"]:]
    recent_peak = max(recent[:-1])
    protect_level = recent_peak - cfg["trailing_pullback_atr"] * atr
    trailing = (recent_peak - recent[0] >= cfg["trailing_rally_atr"] * atr and
                closes[-1] < protect_level and closes[-1] < _ema(closes, cfg["trailing_ema_period"]))
    overextended = (rsi >= cfg["profit_review_rsi"] and
                    closes[-1] - ema21 >= cfg["profit_review_extension_atr"] * atr)
    action = ("EXIT_CANDIDATE" if below_sma else "REDUCE_CANDIDATE" if below_ema or trailing else
              "TRIM_CANDIDATE" if overextended else "HOLD")
    reason = (f"CLOSE_BELOW_SMA{cfg['sma_period']}_CONFIRMED" if below_sma else
              f"CLOSE_BELOW_EMA{cfg['ema_period']}_CONFIRMED" if below_ema else
              "RALLY_PULLBACK_BELOW_TRAIL" if trailing else
              "RSI_ATR_OVEREXTENSION" if overextended else "TREND_NOT_BROKEN")
    half_width = cfg["sell_review_half_width_atr"] * atr
    prior_high = max(float(row[2]) for row in chronological[-cfg["trailing_lookback_bars"]-1:-1])
    zone_mid = (closes[-1] if action != "HOLD" else prior_high
                if closes[-1] < prior_high <= closes[-1] + 2 * atr else None)
    return {"ticker": ticker, "action": action, "reason": reason,
            "close_price": closes[-1], "ema21": round(ema21, 4),
            "sma50": round(sma50, 4), "rsi14": round(rsi, 2), "atr14": round(atr, 4),
            "sell_review_zone": [round(max(0.01, zone_mid - half_width), 2),
                                 round(zone_mid + half_width, 2)] if zone_mid is not None else None,
            "sell_zone_basis": "ACTION_REFERENCE" if action != "HOLD" else
                               "NEARBY_20D_RESISTANCE" if zone_mid is not None else None,
            "protect_level": round(protect_level, 2) if recent_peak - recent[0] >= cfg["trailing_rally_atr"] * atr else None,
            "data_source": rows[0][4]}


def evaluate_all(book, market, day, research_path=None):
    """Evaluate each confirmed position independently; one bad ticker cannot stop the scan."""
    from exit_context import evidence_events
    from exit_engine import evaluate as exit_evaluate
    from exit_notifications import plan_alert

    def bars_for(ticker, limit):
        cursor = market.execute("""SELECT date,open,high,low,close,volume,source,quality_status
            FROM daily_bars WHERE ticker=? AND date<=? ORDER BY date DESC LIMIT ?""", (ticker, day, limit))
        columns = [item[0] for item in cursor.description]
        return [dict(zip(columns, row)) for row in reversed(cursor.fetchall())]

    decisions = []
    lifecycles = {}
    for item in held(book.db):
        ticker = item["ticker"]
        lifecycles[ticker] = item["confirmed_at"]
        try:
            bars = bars_for(ticker, 900)
            security = market.execute("SELECT mapped_sector_etf,mapping_confidence FROM securities WHERE ticker=?",
                                      (ticker,)).fetchone()
            sector = security[0] if security and security[1] in {"HIGH", "MEDIUM"} else None
            benchmarks = {name: bars_for(name, 30) for name in {"SPY", "QQQ", *([sector] if sector else [])}}
            original = json.loads(item["entry_thesis_json"]) if item.get("entry_thesis_json") else None
            context = {"original_thesis": original, "sector_etf": sector,
                       "events": evidence_events(ticker, day, item["entry_date"] or item["confirmed_at"][:10],
                                                  research_path) if research_path else []}
            result = exit_evaluate(bars, benchmarks, item, context, day)
        except (sqlite3.Error, ValueError, TypeError, KeyError, ArithmeticError) as exc:
            result = {"ticker": ticker, "action": "HOLD_DATA_INSUFFICIENT",
                      "reason": "EXIT_INPUT_ERROR:" + type(exc).__name__, "reasons": ["EXIT_INPUT_ERROR"],
                      "exit_score": None, "exit_confidence": "LOW", "position": {},
                      "profit_protection_zone": None, "trend_failure_zone": None}
        decisions.append(result)
    with book.atomic():
        for item in decisions:
            book.db.execute("INSERT OR IGNORE INTO holding_decisions VALUES(?,?,?,?,?,?,?,?,?)",
                            (day, item["ticker"], item["action"], item["reason"],
                             item.get("close_price"), item.get("ema21"), item.get("sma50"),
                             item.get("data_source"), datetime.now(timezone.utc).isoformat()))
            book.db.execute("""INSERT OR IGNORE INTO exit_decisions
                (trading_date,ticker,exit_state,exit_score,exit_confidence,details_json,evaluated_at,lifecycle_id)
                VALUES(?,?,?,?,?,?,?,?)""",
                (day, item["ticker"], item["action"], item.get("exit_score"),
                 item.get("exit_confidence", "LOW"), json.dumps(item, sort_keys=True),
                 datetime.now(timezone.utc).isoformat(), lifecycles[item["ticker"]]))
            profit = item.get("profit_protection_zone") or [None, None]
            trend = item.get("trend_failure_zone") or [None, None]
            book.db.execute("""INSERT OR IGNORE INTO position_reference_zones
                (lifecycle_id,trading_date,ticker,profit_protection_low,profit_protection_high,
                 trend_failure_low,trend_failure_high,exit_state,exit_score,current_price,recorded_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (lifecycles[item["ticker"]], day, item["ticker"], profit[0], profit[1],
                 trend[0], trend[1], item["action"], item.get("exit_score"),
                 (item.get("position") or {}).get("current_price"),
                 datetime.now(timezone.utc).isoformat()))
            plan_alert(book.db, lifecycles[item["ticker"]], day, item)
    return decisions

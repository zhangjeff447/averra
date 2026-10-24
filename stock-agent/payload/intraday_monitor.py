"""Independent monitor-only intraday risk calculations. No order or BUY_NOW path."""
from __future__ import annotations

import json
import math
import sqlite3
import urllib.parse
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

from market_regime import previous_session

ET = ZoneInfo("America/New_York")
DATA_SOURCE = "IEX"
NEGATIVE_TYPES = {"GUIDANCE_CUT", "RESTATEMENT", "AUDITOR_ISSUE", "OFFERING",
                  "REGULATORY_FAILURE", "CONTRACT_TERMINATION", "MATERIAL_LITIGATION",
                  "ACCOUNTING_INVESTIGATION", "THESIS_FAILURE"}
SEVERE_TYPES = {"RESTATEMENT", "REGULATORY_FAILURE",
                "CONTRACT_TERMINATION", "ACCOUNTING_INVESTIGATION", "THESIS_FAILURE"}


def finite(value):
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def scoped_universe(book, entry, day, *, watch=(), max_watch=0):
    """Only confirmed holdings, today's close-generated plans, and bounded WATCH."""
    held = {row[0] for row in book.execute(
        "SELECT ticker FROM tracked_holdings WHERE status='HELD'")}
    plans = {}
    for ticker, payload in entry.execute(
            "SELECT ticker,plan_json FROM entry_reference_plans WHERE target_trading_date=? "
            "ORDER BY run_id DESC",
            (day.isoformat(),)):
        parsed = json.loads(payload)
        if parsed.get("status") == "CONDITIONAL" and ticker not in plans:
            plans[ticker] = parsed
    limited_watch = [s for s in watch if s not in held and s not in plans][:max_watch]
    zones = {}
    try:
        zones = {r[0]: {"profit_protection_low": r[1], "trend_failure_low": r[2]}
                 for r in book.execute("""SELECT ticker,profit_protection_low,trend_failure_low
                     FROM position_reference_zones WHERE trading_date=? ORDER BY recorded_at""",
                     (previous_session(day).isoformat(),))}
    except sqlite3.OperationalError:
        pass  # Older local ledgers may not yet have close-based reference zones.
    return {ticker: {"held": ticker in held, "plan": None if ticker in held else plans.get(ticker),
                     "reference_zones": zones.get(ticker),
                     "watch": ticker in limited_watch}
            for ticker in sorted(held | set(plans) | set(limited_watch))}


def daily_context(market, ticker, day):
    row = market.execute("SELECT open,high,low,close,volume FROM daily_bars "
                         "WHERE ticker=? AND date=?", (ticker, previous_session(day).isoformat())).fetchone()
    if not row or any(finite(v) is None for v in row[:4]):
        return None
    recent = market.execute("SELECT high,low,close FROM daily_bars WHERE ticker=? AND date<? "
                            "ORDER BY date DESC LIMIT 15", (ticker, day.isoformat())).fetchall()
    if len(recent) < 15:
        return None
    ranges = []
    for i, (high, low, close) in enumerate(recent[:14]):
        prior = recent[i + 1][2]
        if any(finite(v) is None for v in (high, low, close, prior)):
            return None
        ranges.append(max(high - low, abs(high - prior), abs(low - prior)))
    atr = sum(ranges) / len(ranges)
    return {"previous_close": row[3], "atr": atr, "average_daily_volume": row[4]}


def valid_bars(bars, day, now, cfg):
    """Reject stale, future, malformed or non-session IEX data before decisions."""
    if len(bars) < cfg["minimum_bars"]:
        return False
    parsed = []
    for bar in bars:
        try:
            stamp = datetime.fromisoformat(bar["t"].replace("Z", "+00:00"))
        except (KeyError, ValueError):
            return False
        if stamp.tzinfo is None or stamp.astimezone(ET).date() != day or stamp > now:
            return False
        values = [finite(bar.get(k)) for k in ("o", "h", "l", "c", "v")]
        if any(v is None for v in values) or min(values[:4]) <= 0 or values[4] < 0:
            return False
        if values[1] < max(values[0], values[3]) or values[2] > min(values[0], values[3]):
            return False
        parsed.append(stamp)
    age = (now - parsed[-1]).total_seconds() / 60
    return (parsed == sorted(set(parsed)) and 0 <= age <= cfg["max_bar_age_minutes"])


class IEXBars:
    """Read-only Alpaca Market Data; never connects to trading.alpaca.markets."""

    def __init__(self, key, secret, opener=None):
        self.key, self.secret = key, secret
        self.opener = opener or urllib.request.urlopen
        self.requests = 0

    def fetch(self, tickers, day, now, cfg):
        if not self.key or not self.secret:
            raise ValueError("IEX_CREDENTIALS_MISSING")
        result = {}
        # Fetch the session from the open: a rolling window would mislabel the
        # midday first bar as the opening gap/range.
        start = datetime.combine(day, datetime.min.time(), ET).replace(hour=9, minute=30)
        end = min(now.astimezone(ET), datetime.combine(day, datetime.min.time(), ET).replace(hour=16))
        for index in range(0, len(tickers), cfg["iex_max_symbols_per_request"]):
            chunk = tickers[index:index + cfg["iex_max_symbols_per_request"]]
            query = urllib.parse.urlencode({"symbols": ",".join(chunk), "timeframe": "1Min",
                                            "start": start.isoformat(), "end": end.isoformat(),
                                            "feed": "iex", "limit": 10000})
            url = "https://data.alpaca.markets/v2/stocks/bars?" + query
            request = urllib.request.Request(url, headers={"APCA-API-KEY-ID": self.key,
                                                    "APCA-API-SECRET-KEY": self.secret})
            self.requests += 1
            with self.opener(request, timeout=20) as response:
                payload = json.load(response)
            if payload.get("next_page_token"):
                raise ValueError("IEX_PAGINATION_INCOMPLETE")
            result.update(payload.get("bars") or {})
        return result


def material_evidence(events, last_seen):
    """Use only verified official Phase 4A records observed after the last check."""
    accepted = []
    for event in events:
        if event.get("materiality") not in {"HIGH", "MATERIAL"}:
            continue
        if event.get("official") is not True or not event.get("evidence_id"):
            continue
        stamp = event.get("accepted_at") or event.get("published_at")
        if not stamp:
            continue
        try:
            accepted_at = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        except ValueError:
            continue
        if accepted_at.tzinfo is None or accepted_at <= last_seen:
            continue
        accepted.append(event)
    return accepted


def assess(ticker, scope, bars, context, benchmark_bars, events, now, day, cfg):
    if not context or not valid_bars(bars, day, now, cfg):
        return {"ticker": ticker, "status": "DATA_UNAVAILABLE", "source": DATA_SOURCE}
    close, opening = bars[-1]["c"], bars[0]["o"]
    prior, atr = context["previous_close"], context["atr"]
    if not atr or atr <= 0 or not prior or prior <= 0:
        return {"ticker": ticker, "status": "DATA_UNAVAILABLE", "source": DATA_SOURCE}
    change_pct = (close / prior - 1) * 100
    move_atr = (close - prior) / atr
    gap_atr = (opening - prior) / atr
    volume = sum(b["v"] for b in bars)
    recent_volume_ratio = None
    if len(bars) >= 21:
        baseline = sum(b["v"] for b in bars[-21:-1]) / 20
        recent_volume_ratio = bars[-1]["v"] / baseline if baseline > 0 else None
    # IEX volume is venue-only: do not compare it with consolidated daily volume.
    vwap = sum(b["vw"] * b["v"] for b in bars if finite(b.get("vw")) is not None) / volume if volume > 0 and all(finite(b.get("vw")) is not None for b in bars) else None
    bench = {}
    for symbol in ("SPY", "QQQ"):
        item = benchmark_bars.get(symbol)
        base = item.get("previous_close") if item else None
        latest = item.get("price") if item else None
        bench[symbol] = round(change_pct - (latest / base - 1) * 100, 3) if base and latest else None
    severity = max(abs(move_atr), abs(gap_atr))
    market_changes = [benchmark_bars.get(s, {}).get("change_pct") for s in ("SPY", "QQQ")]
    market_move = max((abs(v) for v in market_changes if v is not None), default=0)
    market_selloff = any(v is not None and v <= -cfg["market_shock_pct_high"] for v in market_changes)
    if severity >= cfg["shock_atr_extreme"] or market_move >= cfg["market_shock_pct_extreme"]:
        regime = "EXTREME_VOLATILITY"
    elif severity >= cfg["shock_atr_high"] or market_move >= cfg["market_shock_pct_high"]:
        regime = "HIGH_VOLATILITY"
    elif severity >= cfg["shock_atr_elevated"] or market_move >= cfg["market_shock_pct_elevated"]:
        regime = "ELEVATED"
    else:
        regime = "NORMAL"
    negative = [e for e in events if e.get("event_type") in NEGATIVE_TYPES]
    severe = [e for e in negative if e.get("event_type") in SEVERE_TYPES]
    plan = scope.get("plan")
    plan_state = None
    if plan:
        if negative or regime == "EXTREME_VOLATILITY" or market_selloff or close > plan["do_not_chase_above"] or (plan.get("nearest_support") and close < plan["nearest_support"] - cfg["structure_break_atr"] * atr):
            plan_state = "BUY_PLAN_INVALIDATED"
        elif events or regime == "HIGH_VOLATILITY" or close < plan["buy_zone_low"]:
            plan_state = "WAIT_FOR_REASSESSMENT"
        else:
            plan_state = "ENTRY_PLAN_VALID"
    position_state = None
    if scope.get("held"):
        zones = scope.get("reference_zones") or {}
        if severe:
            position_state = "EXIT_CANDIDATE"
        elif negative:
            position_state = "THESIS_REVIEW"
        elif finite(zones.get("trend_failure_low")) and close < zones["trend_failure_low"]:
            position_state = "EXIT_CANDIDATE"
        elif finite(zones.get("profit_protection_low")) and close < zones["profit_protection_low"]:
            position_state = "TRIM_REVIEW"
        elif move_atr <= -cfg["position_exit_atr"]:
            position_state = "EXIT_CANDIDATE"
        elif move_atr <= -cfg["position_reduce_atr"]:
            position_state = "REDUCE_CANDIDATE"
        elif move_atr <= -cfg["position_alert_atr"] or regime == "EXTREME_VOLATILITY":
            position_state = "HOLD_TIGHT_ALERT"
        elif move_atr >= cfg["position_reduce_atr"] and vwap and close < vwap:
            position_state = "TRIM_REVIEW"
        else:
            position_state = "POSITION_NORMAL"
    return {"ticker": ticker, "status": "OK", "source": DATA_SOURCE,
            "asof": bars[-1]["t"], "price": close, "opening_gap_pct": round((opening / prior - 1) * 100, 3),
            "gap_atr": round(gap_atr, 3), "move_atr": round(move_atr, 3),
            "intraday_drawdown_atr": round((close - max(b["h"] for b in bars)) / atr, 3),
            "vwap": round(vwap, 4) if vwap is not None else None,
            "price_vs_vwap": "ABOVE" if vwap is not None and close > vwap else
                              "BELOW" if vwap is not None and close < vwap else "UNAVAILABLE",
            "iex_volume": volume, "rvol": None, "rvol_reason": "IEX_NOT_COMPARABLE_TO_CONSOLIDATED_HISTORY",
            "iex_recent_volume_ratio": round(recent_volume_ratio, 3) if recent_volume_ratio is not None else None,
            "volume_shock": bool(recent_volume_ratio is not None and
                                 recent_volume_ratio >= cfg["iex_recent_volume_shock_ratio"]),
            "opening_range_high": max(b["h"] for b in bars[:min(5, len(bars))]),
            "opening_range_low": min(b["l"] for b in bars[:min(5, len(bars))]),
            "relative_strength_vs": bench, "market_regime": regime,
            "plan_state": plan_state, "position_state": position_state,
            "positive_event_review": bool(events and not negative and not scope.get("held")),
            "evidence_ids": [e["evidence_id"] for e in events]}

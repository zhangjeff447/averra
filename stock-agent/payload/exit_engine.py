"""Deterministic, evidence-gated position review. No network, LLM or broker calls."""
from __future__ import annotations

import json
import math
from datetime import date, timedelta
from pathlib import Path

from analysis import ema_series, indicators
from market_regime import market_session, previous_session

ROOT = Path(__file__).resolve().parent
STATES = {"HOLD", "HOLD_TIGHT", "TRIM_CANDIDATE", "REDUCE_CANDIDATE",
          "EXIT_CANDIDATE", "THESIS_REVIEW"}
SEVERE_EVENTS = {"GUIDANCE_CUT", "CONTRACT_TERMINATION", "REGULATORY_FAILURE",
                 "ACCOUNTING_RESTATEMENT", "CONFIRMED_FRAUD", "CATALYST_FAILED"}
CAUTION_EVENTS = {"MAJOR_DILUTION", "BUSINESS_DETERIORATION", "FILING_AMENDMENT",
                  "LEGAL_PROCEEDINGS", "MATERIAL_COMMITMENT"}
BINARY_EVENTS = {"FDA_DECISION", "REGULATORY_DECISION", "COURT_DECISION",
                 "MAJOR_CONTRACT", "INVESTOR_DAY", "PRODUCT_LAUNCH", "MA_VOTE"}
MATERIAL_REVIEW_EVENTS = {"GUIDANCE", "FILING_AMENDMENT", "LEGAL_PROCEEDINGS",
                          "SHARE_ISSUANCE", "ACCOUNTING_RESTATEMENT", "AUDITOR_ISSUE",
                          "REGULATORY_FAILURE", "CONTRACT_TERMINATION", "SEC_MATERIAL_EVENT",
                          "MATERIAL_COMMITMENT", "MAJOR_CONTRACT", "FINANCING"}


def configuration():
    cfg = json.loads((ROOT / "exit_config.json").read_text())
    if not math.isclose(sum(cfg["weights"].values()), 1, abs_tol=1e-9):
        raise ValueError("EXIT_WEIGHTS_INVALID")
    return cfg


def sessions_between(start, end):
    current, end = date.fromisoformat(start), date.fromisoformat(end)
    total = 0
    while current < end:
        current += timedelta(days=1)
        total += market_session(current)
    return total


def validate_bars(bars, asof, minimum, max_move):
    if len(bars) < minimum or bars[-1]["date"] != asof:
        return "MISSING_OR_STALE_DAILY_BARS"
    expected = date.fromisoformat(asof)
    for bar in reversed(bars[-minimum:]):
        if bar["date"] != expected.isoformat() or bar.get("quality_status") not in {"OK", "VALID"}:
            return "MISSING_DAILY_SESSION"
        try:
            opened, high, low, close, volume = (float(bar[key]) for key in
                                                 ("open", "high", "low", "close", "volume"))
        except (TypeError, ValueError, KeyError):
            return "INVALID_DAILY_BAR"
        if (not all(math.isfinite(x) for x in (opened, high, low, close, volume)) or
                min(opened, high, low, close) <= 0 or volume < 0 or
                low > min(opened, close) or high < max(opened, close)):
            return "INVALID_DAILY_BAR"
        expected = previous_session(expected)
    for before, after in zip(bars[-minimum:-1], bars[-minimum+1:]):
        if abs(float(after["close"]) / float(before["close"]) - 1) * 100 > max_move:
            return "PRICE_DISCONTINUITY_REVIEW"
    return None


def relative_strength(bars, benchmarks, sector_symbol, thresholds):
    dates = [bar["date"] for bar in bars]
    stock = {bar["date"]: float(bar["close"]) for bar in bars}
    detail = {}
    for label, symbol in (("SPY", "SPY"), ("QQQ", "QQQ"), ("SECTOR", sector_symbol)):
        series = {bar["date"]: float(bar["close"]) for bar in benchmarks.get(symbol, [])} if symbol else {}
        windows = {}
        for days in (5, 10, 20):
            if len(dates) < days + 1 or any(day not in series for day in dates[-days-1:]):
                windows[f"{days}d"] = None
            else:
                stock_return = (stock[dates[-1]] / stock[dates[-days-1]] - 1) * 100
                benchmark_return = (series[dates[-1]] / series[dates[-days-1]] - 1) * 100
                windows[f"{days}d"] = round(stock_return - benchmark_return, 3)
        detail[label] = windows
    broad = [detail[label] for label in ("SPY", "QQQ")]
    required = [item[f"{days}d"] for item in broad for days in (5, 10, 20)]
    if any(value is None for value in required):
        return {"state": "UNAVAILABLE", "windows": detail, "score": None}
    weak = all(item["5d"] <= thresholds["rs_underperformance_5d_pct"] and
               item["10d"] <= thresholds["rs_underperformance_10d_pct"] and
               item["20d"] <= thresholds["rs_underperformance_20d_pct"] for item in broad)
    if detail["SECTOR"]["20d"] is not None:
        weak = weak and detail["SECTOR"]["20d"] < 0
    accelerating = all(item["5d"] / 5 < item["20d"] / 20 for item in broad)
    return {"state": "RS_DETERIORATING" if weak else "STABLE",
            "accelerating": accelerating, "windows": detail,
            "score": 85 if weak and accelerating else 75 if weak else 0}


def thesis_assessment(original, events, asof=None, recent_days=5):
    if not original:
        return {"state": "UNAVAILABLE", "score": None, "evidence_ids": []}
    eligible = [e for e in events if e.get("materiality") in {"HIGH", "MATERIAL"} and
                e.get("official") is True and e.get("verified") is True and e.get("id")]
    broken = [e for e in eligible if e.get("event_type") in SEVERE_EVENTS and
              e.get("thesis_conflict") is True]
    damaged = [e for e in eligible if e.get("event_type") in CAUTION_EVENTS | SEVERE_EVENTS and
               e.get("thesis_conflict") is True]
    if broken:
        return {"state": "THESIS_BROKEN", "score": 100,
                "evidence_ids": sorted({e["id"] for e in broken})}
    if damaged:
        return {"state": "THESIS_DAMAGED", "score": 75,
                "evidence_ids": sorted({e["id"] for e in damaged})}
    recent = [e for e in eligible if e.get("new_since_entry") is True and
              e.get("event_type") in MATERIAL_REVIEW_EVENTS and
              e.get("date") and asof and e["date"] <= asof and
              sessions_between(e["date"], asof) <= recent_days]
    if recent:
        return {"state": "UNRESOLVED_MATERIAL_EVENT", "score": 60,
                "evidence_ids": sorted({e["id"] for e in recent})}
    return {"state": "INTACT_WITH_AVAILABLE_EVIDENCE", "score": 0, "evidence_ids": []}


def event_assessment(events, asof, profit_known, thresholds):
    upcoming = []
    for event in events:
        stamp = event.get("event_date")
        if not stamp or not event.get("official") or not event.get("verified"):
            continue
        try:
            days = sessions_between(asof, stamp)
        except ValueError:
            continue
        if stamp < asof or days > thresholds["earnings_approaching_days"]:
            continue
        upcoming.append({"id": event.get("id"), "type": event.get("event_type"),
                         "date": stamp, "trading_days": days})
    earnings = [e for e in upcoming if e["type"] == "EARNINGS_SCHEDULE"]
    binary = [e for e in upcoming if e["type"] in BINARY_EVENTS]
    imminent = bool(earnings and min(e["trading_days"] for e in earnings) <=
                    thresholds["earnings_imminent_days"])
    state = "EARNINGS_IMMINENT" if imminent else "EARNINGS_APPROACHING" if earnings else "EARNINGS_NORMAL"
    score = 45 if imminent and profit_known else 25 if imminent or binary else 10 if earnings else 0
    return {"state": state, "score": score, "upcoming_earnings": min(
            (e["date"] for e in earnings), default=None),
            "upcoming_material_events": binary, "overnight_gap_risk": imminent and profit_known}


def evaluate(bars, benchmarks, position, context, asof, cfg=None):
    cfg = cfg or configuration()
    t = cfg["thresholds"]
    problem = validate_bars(bars, asof, cfg["minimum_daily_bars"], t["max_single_day_move_pct"])
    if bars and bars[-1].get("source") != cfg["current_daily_source"]:
        problem = "CURRENT_DAILY_SOURCE_INVALID"
    if problem:
        return {"ticker": position["ticker"], "action": "HOLD_DATA_INSUFFICIENT",
                "reason": problem, "exit_score": None, "exit_confidence": "LOW",
                "reasons": [problem], "position": {"entry_price": position.get("entry_price")},
                "profit_protection_zone": None, "trend_failure_zone": None}
    closes = [float(bar["close"]) for bar in bars]
    price = closes[-1]
    entry_date = position.get("entry_date") or position["confirmed_at"][:10]
    if asof <= entry_date:
        return {"ticker": position["ticker"], "action": "HOLD", "reason": "WAIT_NEXT_SESSION",
                "exit_score": None, "exit_confidence": "LOW", "reasons": ["WAIT_NEXT_SESSION"],
                "position": {"entry_price": position.get("entry_price"), "entry_date": entry_date},
                "profit_protection_zone": None, "trend_failure_zone": None}
    post_entry_bars = [bar for bar in bars if bar["date"] > entry_date]
    metrics = indicators(bars)
    atr = metrics["atr14"]
    if not atr or not math.isfinite(atr):
        return {"ticker": position["ticker"], "action": "HOLD_DATA_INSUFFICIENT",
                "reason": "ATR_UNAVAILABLE", "exit_score": None, "exit_confidence": "LOW",
                "reasons": ["ATR_UNAVAILABLE"], "position": {},
                "profit_protection_zone": None, "trend_failure_zone": None}
    ema9 = ema_series(closes, t["ema9_period"])
    ema21 = ema_series(closes, t["ema21_period"])
    enough_post_entry = len(post_entry_bars) >= cfg["confirmation_bars"]
    below_ema21 = enough_post_entry and all(closes[-n] < ema21[-n] - t["ema21_break_atr"] * metrics["atr14"]
                                       for n in range(1, cfg["confirmation_bars"] + 1))
    below_sma50 = enough_post_entry and all(closes[-n] < sum(closes[-n-t["sma50_period"]+1:len(closes)-n+1]) /
                      t["sma50_period"] - t["sma50_break_atr"] * metrics["atr14"]
                      for n in range(1, cfg["confirmation_bars"] + 1))
    below_sma200 = enough_post_entry and all(closes[-n] < sum(closes[-n-t["sma200_period"]+1:len(closes)-n+1]) /
                       t["sma200_period"] - t["sma200_break_atr"] * metrics["atr14"]
                       for n in range(1, cfg["confirmation_bars"] + 1))
    macd_weak = metrics["macd"] < metrics["macd_signal"]
    avg_volume = sum(float(bar["volume"]) for bar in bars[-t["volume_period"]-1:-1]) / t["volume_period"]
    rvol = float(bars[-1]["volume"]) / avg_volume if avg_volume else None
    support = min(float(bar["low"]) for bar in bars[-t["support_lookback"]-1:-1])
    support_broken = price < support
    trend_score = (100 if below_sma200 and below_sma50 else 80 if below_sma50 else
                   55 if below_ema21 else 20 if price < ema9[-1] else 0)
    if macd_weak and trend_score:
        trend_score = min(100, trend_score + 8)
    if support_broken and trend_score:
        trend_score = min(100, trend_score + 8)
    trend_state = ("MAJOR_BREAKDOWN" if below_sma200 and below_sma50 else
                   "SMA50_BREAKDOWN" if below_sma50 else "EMA21_BREAKDOWN" if below_ema21 else
                   "EMA9_WARNING" if price < ema9[-1] else "INTACT")
    entry_price = position.get("entry_price")
    held_days = sessions_between(entry_date, asof)
    since = post_entry_bars if entry_date >= bars[0]["date"] else []
    peak_close = None
    if entry_price and since:
        peak_close = max(float(entry_price), *(float(bar["close"]) for bar in since))
        peak_high = max(float(entry_price), *(float(bar["high"]) for bar in since))
        current_return = (price / entry_price - 1) * 100
        peak_return = (peak_high / entry_price - 1) * 100
        peak_drawdown = (price / peak_close - 1) * 100
        running_peak, worst_drawdown = float(entry_price), 0.0
        for bar in since:
            running_peak = max(running_peak, float(bar["close"]))
            worst_drawdown = min(worst_drawdown, (float(bar["close"]) / running_peak - 1) * 100)
        gain_atr = (peak_close - entry_price) / atr
        profit_state = ("PARABOLIC" if gain_atr >= t["parabolic_profit_atr"] else
                        "STRONG_PROFIT_PROTECTION" if gain_atr >= t["strong_profit_atr"] else
                        "PROFIT_PROTECTION" if gain_atr >= t["rally_profit_atr"] else
                        "NORMAL_PROFIT" if current_return > 0 else "LOW_PROFIT")
        multiple = {"PARABOLIC": t["trail_parabolic_atr"],
                    "STRONG_PROFIT_PROTECTION": t["trail_strong_atr"],
                    "PROFIT_PROTECTION": t["trail_protection_atr"]}.get(
                        profit_state, t["trail_normal_atr"])
        trail = peak_close - multiple * atr
        drawdown_atr = (peak_close - price) / atr
        protection_hit = (profit_state in {"PROFIT_PROTECTION", "STRONG_PROFIT_PROTECTION", "PARABOLIC"}
                          and peak_drawdown <= -t["minimum_peak_drawdown_pct"] and
                          price < trail and price < ema9[-1])
        profit_score = 75 if protection_hit else 25 if profit_state in {
            "PROFIT_PROTECTION", "STRONG_PROFIT_PROTECTION", "PARABOLIC"} else 0
    else:
        peak_close = peak_high = current_return = peak_return = peak_drawdown = None
        worst_drawdown = gain_atr = trail = drawdown_atr = None
        profit_state = "ENTRY_PRICE_UNKNOWN" if entry_price is None else "AWAITING_POST_ENTRY_CLOSE"
        protection_hit = False
        profit_score = None
    extension = (price - metrics["ema21"]) / atr
    accelerating = (price / closes[-6] - 1) * 100 - (closes[-6] / closes[-11] - 1) * 100
    heat = sum((extension >= t["extension_atr"], metrics["rsi"] >= t["rsi_extreme"],
                metrics["kdj_j"] >= t["kdj_extreme"],
                rvol is not None and rvol >= t["volume_climax_rvol"],
                accelerating >= t["acceleration_5d_pct"]))
    strong_trend = price >= metrics["ema21"] and not macd_weak and metrics["adx14"] >= t["strong_trend_adx_min"]
    parabolic = heat >= t["parabolic_signal_count"] and extension >= t["parabolic_extension_atr"]
    overheat_trim = (heat >= t["overheat_signal_count"] and
                     not (strong_trend and heat == t["overheat_signal_count"]) and
                     current_return is not None and current_return > 0)
    rs = relative_strength(bars, benchmarks, context.get("sector_etf"), t)
    thesis = thesis_assessment(context.get("original_thesis"), context.get("events", []),
                               asof, t["thesis_review_recent_days"])
    event = event_assessment(context.get("events", []), asof, current_return is not None and current_return > 0, t)
    stalled = (held_days >= t["time_stop_days"] and entry_price is not None and
               abs(price - entry_price) <= t["time_stop_flat_atr"] * atr and
               rs["state"] == "RS_DETERIORATING" and macd_weak and not event["upcoming_material_events"])
    time_score = 60 if stalled else 0 if entry_price is not None else None
    if parabolic:
        profit_score = max(profit_score or 0, 60) if profit_score is not None else None
    components = {"thesis_health": thesis["score"], "trend_health": trend_score,
                  "profit_protection": profit_score, "relative_strength": rs["score"],
                  "time_opportunity": time_score, "event_risk": event["score"]}
    available = [(name, value) for name, value in components.items() if value is not None]
    total_weight = sum(cfg["weights"][name] for name, _ in available)
    score = round(sum(cfg["weights"][name] * value for name, value in available) / total_weight, 2)
    reasons = []
    if thesis["state"] == "THESIS_BROKEN":
        action = "EXIT_CANDIDATE"; reasons.append("THESIS_BROKEN")
    elif thesis["state"] in {"THESIS_DAMAGED", "UNRESOLVED_MATERIAL_EVENT"}:
        action = "THESIS_REVIEW"; reasons.append(thesis["state"])
    elif trend_state == "MAJOR_BREAKDOWN" or (below_sma50 and rs["state"] == "RS_DETERIORATING"):
        action = "EXIT_CANDIDATE"; reasons.append("MAJOR_TREND_RS_BREAKDOWN")
    elif below_sma50 or below_ema21 or protection_hit or (rs["state"] == "RS_DETERIORATING" and macd_weak and price < ema9[-1]):
        action = "REDUCE_CANDIDATE"; reasons.append("TREND_OR_PROFIT_PROTECTION_WEAKENING")
    elif stalled:
        action = "THESIS_REVIEW"; reasons.append("THESIS_STALLED")
    elif overheat_trim or (event["state"] == "EARNINGS_IMMINENT" and
                           profit_state in {"STRONG_PROFIT_PROTECTION", "PARABOLIC"}):
        action = "TRIM_CANDIDATE"; reasons.append("OVEREXTENSION_OR_EVENT_RISK")
    elif (profit_state in {"PROFIT_PROTECTION", "STRONG_PROFIT_PROTECTION", "PARABOLIC"} or
          (strong_trend and metrics["rsi"] >= t["rsi_extreme"]) or event["state"] == "EARNINGS_IMMINENT"):
        action = "HOLD_TIGHT"; reasons.append("PROFIT_OR_EVENT_MONITORING")
    else:
        action = "HOLD"; reasons.append("TREND_INTACT")
    if rs["state"] == "RS_DETERIORATING":
        reasons.append("RS_DETERIORATING")
    if stalled and "THESIS_STALLED" not in reasons:
        reasons.append("THESIS_STALLED")
    if event["state"] != "EARNINGS_NORMAL":
        reasons.append(event["state"])
    if event["upcoming_material_events"]:
        reasons.append("UPCOMING_MATERIAL_EVENT")
    if parabolic:
        reasons.append("PARABOLIC_MOVE")
    if entry_price is None:
        reasons.append("ENTRY_PRICE_UNKNOWN_PROFIT_LIMITED")
    unavailable = sum(value is None for value in components.values())
    confidence = "HIGH" if unavailable == 0 else "MEDIUM" if unavailable <= 2 else "LOW"
    width = t["zone_half_width_atr"] * atr
    protection_zone = [round(max(0.01, trail - width), 2), round(trail + width, 2)] if trail else None
    failure_level = min(metrics["ema21"], metrics["sma50"]) if below_ema21 or below_sma50 else metrics["ema21"]
    trend_zone = [round(max(0.01, failure_level - width), 2), round(failure_level + width, 2)]
    active_review = action in {"TRIM_CANDIDATE", "REDUCE_CANDIDATE", "EXIT_CANDIDATE"}
    review_zone = [round(max(0.01, price - width), 2), round(price + width, 2)] if active_review else None
    return {"ticker": position["ticker"], "action": action, "reason": reasons[0],
            "exit_score": score, "exit_confidence": confidence, "reasons": reasons,
            "components": components, "thesis": thesis, "trend": {"state": trend_state,
                "ema9": round(ema9[-1], 4), "ema21": round(metrics["ema21"], 4),
                "sma50": round(metrics["sma50"], 4), "sma200": round(metrics["sma200"], 4),
                "macd_line": metrics["macd"], "macd_signal": metrics["macd_signal"],
                "adx": metrics["adx14"], "support": round(support, 4), "rvol": rvol},
            "relative_strength": rs, "event": event,
            "position": {"entry_price": entry_price, "entry_date": entry_date, "current_price": price,
                "highest_close_since_entry": peak_close, "highest_high_since_entry": peak_high,
                "trading_days_held": held_days, "return_since_entry_pct": round(current_return, 3) if current_return is not None else None,
                "peak_return_pct": round(peak_return, 3) if peak_return is not None else None,
                "drawdown_from_peak_pct": round(peak_drawdown, 3) if peak_drawdown is not None else None,
                "peak_drawdown_atr_multiple": round(drawdown_atr, 3) if drawdown_atr is not None else None,
                "maximum_gain_while_held_pct": round(peak_return, 3) if peak_return is not None else None,
                "maximum_drawdown_while_held_pct": round(worst_drawdown, 3) if worst_drawdown is not None else None,
                "atr": round(atr, 4), "atr_pct": round(atr / price * 100, 3),
                "profit_state": profit_state},
            "profit_protection_zone": protection_zone, "trend_failure_zone": trend_zone,
            "sell_review_zone": review_zone,
            "protect_level": round(trail, 2) if trail and price >= trail else None,
            "close_price": price, "ema21": round(metrics["ema21"], 4),
            "sma50": round(metrics["sma50"], 4), "data_source": bars[-1].get("source"),
            "reference_only": True, "no_order_placed": True}

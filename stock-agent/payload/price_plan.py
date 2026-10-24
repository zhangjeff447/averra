"""Close-based, non-executable next-session price references."""
from __future__ import annotations

import math
from datetime import datetime, timezone


def buy_plan(entry, thresholds):
    if entry.get("entry_state") != "BUY_TOMORROW_CANDIDATE":
        return None
    daily = entry.get("timeframes", {}).get("daily", {})
    close = entry.get("entry_price_reference")
    atr = daily.get("atr14")
    features = entry.get("features", {})
    levels = features.get("support_resistance", {})
    support = levels.get("support")
    resistance = levels.get("resistance")
    vwap = (features.get("vwap") or {}).get("value")
    ema21 = daily.get("ema21")
    breakout = features.get("breakout_level")
    if not all(isinstance(value, (int, float)) and math.isfinite(value) and value > 0
               for value in (close, atr)):
        return {"status": "PRICE_DATA_INSUFFICIENT"}
    gap_wait = thresholds["premarket_wait_gap_pct"]
    gap_cancel = thresholds["premarket_cancel_gap_pct"]
    half_width = thresholds["buy_zone_half_width_atr"]
    if not 0 < gap_wait < gap_cancel or not 0 < half_width <= 1:
        raise ValueError("INVALID_OPENING_GAP_THRESHOLDS")
    low = close - half_width * atr
    for level in (support, ema21, vwap, breakout):
        if isinstance(level, (int, float)) and math.isfinite(level) and close - atr <= level <= close:
            low = max(low, level)
    high = min(close + half_width * atr, close * (1 + gap_wait / 100))
    if isinstance(resistance, (int, float)) and math.isfinite(resistance) and resistance > close:
        high = min(high, max(close, resistance - 0.1 * atr))
    if low > high:
        return {"status": "PRICE_DATA_INSUFFICIENT"}
    return {"status": "CONDITIONAL", "reference_close": round(close, 2),
            "buy_zone_low": round(low, 2), "buy_zone_high": round(high, 2),
            "do_not_chase_above": round(high, 2), "atr": round(atr, 4),
            "nearest_support": round(support, 2) if isinstance(support, (int, float)) and math.isfinite(support) and support > 0 else None,
            "nearest_resistance": round(resistance, 2) if isinstance(resistance, (int, float)) and math.isfinite(resistance) and resistance > 0 else None,
            "chase_risk": entry.get("chase_risk"),
            "wait_gap_pct": gap_wait, "cancel_gap_pct": gap_cancel,
            "gap_atr_elevated": thresholds["opening_gap_atr_elevated"],
            "gap_atr_high": thresholds["opening_gap_atr_high"],
            "gap_atr_extreme": thresholds["opening_gap_atr_extreme"],
            "opening_wait_minutes": thresholds["opening_wait_minutes"],
            "opening_rvol_min": thresholds["opening_rvol_min"],
            "basis": "CLOSE_ATR_SUPPORT", "asof": entry.get("asof"),
            "generated_from_trading_date": entry.get("asof"),
            "target_trading_date": entry.get("target_session"),
            "reference_generated_at": datetime.now(timezone.utc).isoformat(),
            "requires_opening_confirmation": True}


def reference_status(plan, observed_price):
    """Offline validity check; never represents a live opening confirmation."""
    if not plan or plan.get("status") != "CONDITIONAL":
        return "REFERENCE_UNAVAILABLE"
    if not isinstance(observed_price, (int, float)) or not math.isfinite(observed_price) or observed_price <= 0:
        return "PRICE_UNAVAILABLE"
    if observed_price > plan["do_not_chase_above"]:
        return "WAIT_FOR_REASSESSMENT"
    return "POST_CLOSE_REFERENCE_ONLY"


def opening_rule(plan, opening_price):
    """A deterministic gate for an observed open; it never confirms a buy."""
    if not plan or plan.get("status") != "CONDITIONAL" or not isinstance(opening_price, (int, float)) or not math.isfinite(opening_price) or opening_price <= 0:
        return "WAIT_FOR_VALID_OPEN"
    gap_pct = (opening_price / plan["reference_close"] - 1) * 100
    gap_atr = (opening_price - plan["reference_close"]) / plan["atr"]
    if gap_pct >= plan["cancel_gap_pct"] or gap_atr >= plan["gap_atr_extreme"]:
        return "CANCEL_CHASE"
    if (gap_pct >= plan["wait_gap_pct"] or gap_atr >= plan["gap_atr_high"] or
            not plan["buy_zone_low"] <= opening_price <= plan["buy_zone_high"]):
        return "WAIT_REASSESS"
    return "ZONE_ONLY_WAIT_FOR_CONFIRMATION"


def opening_assessment(plan, opening_price, opening_data=None):
    """Offline only until reliable opening bars exist; never implies real-time BUY_NOW."""
    decision = opening_rule(plan, opening_price)
    if decision == "WAIT_FOR_VALID_OPEN":
        return {"gap_state": "UNAVAILABLE", "decision": "WAIT", "gap_atr_multiple": None}
    gap_pct = (opening_price / plan["reference_close"] - 1) * 100
    multiple = (opening_price - plan["reference_close"]) / plan["atr"]
    state = ("EXTREME_CHASE_RISK" if decision == "CANCEL_CHASE" else
             "HIGH_CHASE_RISK" if gap_pct >= plan["wait_gap_pct"] or multiple >= plan["gap_atr_high"] else
             "ELEVATED_GAP" if multiple >= plan["gap_atr_elevated"] else "NORMAL_OPEN")
    if decision == "CANCEL_CHASE":
        final = "CANCEL_ENTRY"
    elif decision == "WAIT_REASSESS" or not opening_data:
        final = "WAIT_FOR_REASSESSMENT"
    elif (opening_data.get("minutes_after_open", 0) >= plan["opening_wait_minutes"] and
          all(opening_data.get(key) is not None for key in
              ("price", "vwap", "opening_range_high", "rvol", "rs_spy_pct")) and
          opening_data["price"] >= opening_data["vwap"] and
          opening_data["price"] >= opening_data["opening_range_high"] and
          opening_data["rvol"] >= plan["opening_rvol_min"] and opening_data["rs_spy_pct"] > 0 and
          opening_data.get("material_negative_catalyst") is not True):
        final = "ENTRY_CONFIRMED"
    else:
        final = "WAIT"
    return {"gap_state": state, "decision": final,
            "gap_pct": round(gap_pct, 3), "gap_atr_multiple": round(multiple, 3),
            "live_data_verified": bool(opening_data)}

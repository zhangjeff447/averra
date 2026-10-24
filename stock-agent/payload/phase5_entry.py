#!/usr/bin/env python3
"""Local Phase 5 Entry decisions. Reads frozen research; never calls an LLM or broker."""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
import statistics
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from market_regime import market_session, previous_session
from quant_ranking import moving_features
from real_market import MassiveClient, load_key

ROOT = Path(__file__).resolve().parent
ET = ZoneInfo("America/New_York")
HORIZONS = (1, 5, 10, 20)
STATES = {"BUY_NOW_CANDIDATE", "BUY_TOMORROW_CANDIDATE", "WATCH", "REJECT_ENTRY",
          "ENTRY_DATA_INSUFFICIENT",
          "ENTRY_CONFIRMED", "WAIT", "CANCEL_ENTRY"}


def clamp(value):
    return round(max(0, min(100, value)), 2)


def load_config(path=ROOT / "phase5_config.json"):
    cfg = json.loads(Path(path).read_text())
    weights = cfg["component_weights"]
    if len(weights) != 9 or any(v < 0 for v in weights.values()) or not math.isclose(sum(weights.values()), 1):
        raise ValueError("invalid Entry component weights")
    if not 5 <= cfg["candidate_limit"] <= 10:
        raise ValueError("Phase 5 candidate limit must be 5-10")
    if cfg["thresholds"]["entry_buy_now"] <= cfg["thresholds"]["entry_buy_tomorrow"]:
        raise ValueError("BUY_NOW threshold must exceed close-confirmation threshold")
    t = cfg["thresholds"]
    if not (0 <= t["trend_alignment_strong_down"] < t["trend_alignment_down"] <
            t["trend_alignment_up"] < t["trend_alignment_strong_up"] <= 6):
        raise ValueError("invalid trend alignment thresholds")
    if not 0 < t["close_near_high_fraction"] < 1:
        raise ValueError("invalid close confirmation fraction")
    return cfg


def init_db(db):
    db.executescript((ROOT / "phase5_schema.sql").read_text())


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def regular_bar(ts_ms):
    stamp = datetime.fromtimestamp(ts_ms / 1000, timezone.utc).astimezone(ET)
    minutes = stamp.hour * 60 + stamp.minute
    return market_session(stamp.date()) and 570 <= minutes < 960 and minutes % 5 == 0


def validate_aggregate(row):
    vals = [row.get(k) for k in ("o", "h", "l", "c", "v")]
    if any(v is None or not math.isfinite(float(v)) for v in vals):
        raise ValueError("invalid intraday OHLCV")
    opened, high, low, close, volume = map(float, vals)
    if min(opened, high, low, close) <= 0 or volume < 0 or low > min(opened, close) or high < max(opened, close):
        raise ValueError("invalid intraday bar range")
    vwap = row.get("vw")
    if vwap is not None and (not math.isfinite(float(vwap)) or not low * .95 <= vwap <= high * 1.05):
        raise ValueError("invalid intraday bar VWAP")
    return opened, high, low, close, volume, vwap


def sync_symbol(db, client, ticker, asof, cfg):
    """Refresh a bounded candidate/benchmark cache; no full-market intraday scan."""
    latest = db.execute("SELECT MAX(ts_ms) FROM entry_intraday_bars WHERE ticker=?", (ticker,)).fetchone()[0]
    latest_day = (datetime.fromtimestamp(latest / 1000, timezone.utc).astimezone(ET).date().isoformat()
                  if latest else None)
    first = (date.fromisoformat(latest_day) if latest_day else
             date.fromisoformat(asof) - timedelta(days=cfg["intraday_history_calendar_days"]))
    rows = 0
    if latest_day != asof:
        payload = client.get(f"/v2/aggs/ticker/{ticker}/range/5/minute/{first}/{asof}",
                             {"adjusted": "true", "sort": "asc", "limit": 50000})
        if payload.get("next_url"):
            raise ValueError("intraday pagination required; cache not marked complete")
        batch = []
        for item in payload.get("results") or []:
            ts = item.get("t")
            if not isinstance(ts, (int, float)) or not regular_bar(ts):
                continue
            opened, high, low, close, volume, vwap = validate_aggregate(item)
            batch.append((ticker, int(ts), opened, high, low, close, volume, vwap,
                          "MASSIVE", utc_now()))
        with db:
            db.executemany("""INSERT INTO entry_intraday_bars VALUES(?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(ticker,ts_ms) DO UPDATE SET open=excluded.open,high=excluded.high,
                low=excluded.low,close=excluded.close,volume=excluded.volume,
                bar_vwap=excluded.bar_vwap,fetched_at=excluded.fetched_at""", batch)
        rows += len(batch)
    latest_daily = db.execute("SELECT MAX(date) FROM entry_daily_overlay WHERE ticker=?", (ticker,)).fetchone()[0]
    if latest_daily != asof:
        start = latest_daily or (date.fromisoformat(asof) - timedelta(days=7)).isoformat()
        payload = client.get(f"/v2/aggs/ticker/{ticker}/range/1/day/{start}/{asof}",
                             {"adjusted": "true", "sort": "asc", "limit": 100})
        daily = []
        for item in payload.get("results") or []:
            opened, high, low, close, volume, _ = validate_aggregate(item)
            day = datetime.fromtimestamp(item["t"] / 1000, timezone.utc).astimezone(ET).date().isoformat()
            daily.append((ticker, day, opened, high, low, close, volume, "MASSIVE", utc_now()))
        with db:
            db.executemany("""INSERT INTO entry_daily_overlay VALUES(?,?,?,?,?,?,?,?,?)
                ON CONFLICT(ticker,date) DO UPDATE SET open=excluded.open,high=excluded.high,
                low=excluded.low,close=excluded.close,volume=excluded.volume,
                fetched_at=excluded.fetched_at""", daily)
        rows += len(daily)
    cached = cache_intraday(db, ticker, asof)
    status = ("READY" if cached and cached[-1]["date"] == asof and
              cache_daily_last(db, ticker) == asof else "MISSING_DATA")
    with db:
        for interval in ("5m", "daily"):
            db.execute("""INSERT INTO entry_sync VALUES(?,?,?,?,?,?) ON CONFLICT(ticker,interval)
                DO UPDATE SET last_asof=excluded.last_asof,last_fetch_at=excluded.last_fetch_at,
                status=excluded.status,rows_downloaded=entry_sync.rows_downloaded+excluded.rows_downloaded""",
                (ticker, interval, asof, utc_now(), status, rows))
    return rows, status


def cache_daily_last(db, ticker):
    return db.execute("SELECT MAX(date) FROM entry_daily_overlay WHERE ticker=?", (ticker,)).fetchone()[0]


def cache_intraday(db, ticker, asof):
    rows = db.execute("SELECT ts_ms,open,high,low,close,volume,bar_vwap FROM entry_intraday_bars "
                      "WHERE ticker=? ORDER BY ts_ms", (ticker,)).fetchall()
    out = []
    for row in rows:
        stamp = datetime.fromtimestamp(row[0] / 1000, timezone.utc).astimezone(ET)
        if stamp.date().isoformat() <= asof:
            out.append(dict(ts_ms=row[0], date=stamp.date().isoformat(), minute=stamp.hour * 60 + stamp.minute,
                            open=row[1], high=row[2], low=row[3], close=row[4], volume=row[5], bar_vwap=row[6]))
    return out


def daily_bars(market_db, entry_db, ticker, asof):
    rows = market_db.execute("SELECT date,open,high,low,close,volume FROM daily_bars "
                             "WHERE ticker=? AND date<=? ORDER BY date DESC LIMIT 400", (ticker, asof)).fetchall()
    merged = {r[0]: dict(zip(("date", "open", "high", "low", "close", "volume"), r)) for r in rows}
    overlay = entry_db.execute("SELECT date,open,high,low,close,volume FROM entry_daily_overlay "
                               "WHERE ticker=? AND date<=?", (ticker, asof)).fetchall()
    for row in overlay:
        merged[row[0]] = dict(zip(("date", "open", "high", "low", "close", "volume"), row))
    return [merged[k] for k in sorted(merged)[-400:]]


def aggregate(bars, interval):
    if interval == "5m":
        return bars
    width = {"15m": 15, "1h": 60}[interval]
    grouped = {}
    for row in bars:
        bucket = (row["minute"] - 570) // width
        grouped.setdefault((row["date"], bucket), []).append(row)
    out = []
    for (day, bucket), group in sorted(grouped.items()):
        group.sort(key=lambda r: r["ts_ms"])
        expected = min(width, 960 - (570 + bucket * width)) // 5
        if len({r["minute"] for r in group}) != expected:
            continue  # An incomplete interval is not a full 15M/1H candle.
        out.append({"date": day, "minute": 570 + bucket * width, "ts_ms": group[0]["ts_ms"],
                    "open": group[0]["open"], "high": max(r["high"] for r in group),
                    "low": min(r["low"] for r in group), "close": group[-1]["close"],
                    "volume": sum(r["volume"] for r in group)})
    return out


def session_vwap(bars, asof):
    today = [b for b in bars if b["date"] == asof]
    if not today or sum(b["volume"] for b in today) <= 0:
        return None
    values = []
    cum_value = cum_volume = 0
    for bar in today:
        price = bar["bar_vwap"] or (bar["high"] + bar["low"] + bar["close"]) / 3
        cum_value += price * bar["volume"]
        cum_volume += bar["volume"]
        if cum_volume:
            values.append(cum_value / cum_volume)
    return {"value": values[-1], "slope": values[-1] - values[max(0, len(values)-7)],
            "reclaim": len(values) >= 2 and today[-2]["close"] <= values[-2] < today[-1]["close"],
            "rejection": len(values) >= 2 and today[-2]["close"] >= values[-2] > today[-1]["close"]}


def timeframe_features(bars, interval, cfg, quant_params, asof):
    if len(bars) < cfg["minimum_bars"][interval] or bars[-1]["date"] != asof:
        raise ValueError(f"insufficient_or_stale_{interval}_bars")
    result = moving_features(bars, quant_params)
    reference_volume = statistics.mean(b["volume"] for b in bars[-21:-1])
    result.update(close=bars[-1]["close"], volume=bars[-1]["volume"],
                  rvol=bars[-1]["volume"] / reference_volume if reference_volume else None,
                  volume_trend=(statistics.mean(b["volume"] for b in bars[-5:]) /
                                reference_volume - 1) if reference_volume else None,
                  macd_divergence=None,
                  bar_time=bars[-1].get("ts_ms"), bar_date=bars[-1]["date"])
    return result


def trend(features, cfg):
    f = features
    t = cfg["thresholds"]
    level = sum((f["close"] > f["ema9"], f["ema9"] > f["ema21"],
                 f["close"] > f["sma50"], f["sma50"] > f["sma200"],
                 f["macd_line"] > f["macd_signal"], f["macd_line"] > 0))
    strong = f["adx14"] >= t["trend_adx_min"]
    return ("STRONG_UPTREND" if level >= t["trend_alignment_strong_up"] and strong else
            "UPTREND" if level >= t["trend_alignment_up"] else
            "STRONG_DOWNTREND" if level <= t["trend_alignment_strong_down"] and strong else
            "DOWNTREND" if level <= t["trend_alignment_down"] else "NEUTRAL")


def levels(daily, price, cfg):
    lookback = cfg["thresholds"]["support_lookback_bars"]
    prior = daily[-lookback-1:-1]
    supports = [b["low"] for b in prior if b["low"] <= price]
    resistances = [b["high"] for b in prior if b["high"] >= price]
    supports += [daily[-2]["close"], daily[-2]["low"]]
    resistances += [daily[-2]["high"]]
    support = max((x for x in supports if x <= price), default=None)
    resistance = min((x for x in resistances if x >= price), default=None)
    reward = ((resistance-price)/(price-support) if support and resistance and price > support else None)
    return {"support": support, "resistance": resistance,
            "support_distance_pct": (price/support-1)*100 if support else None,
            "resistance_distance_pct": (resistance/price-1)*100 if resistance else None,
            "risk_reward": reward,
            "support_bounce": bool(support and daily[-1]["low"] <= support < price),
            "resistance_rejection": bool(resistance and daily[-1]["high"] >= resistance > daily[-1]["close"])}


def chase_risk(gap_pct, vwap_distance_pct, atr_extension, recent_move_pct, cfg):
    t = cfg["thresholds"]
    if (gap_pct >= t["gap_extreme_pct"] or atr_extension >= t["atr_extension_extreme"] or
            vwap_distance_pct >= t["vwap_distance_high_pct"] * 2):
        return "EXTREME"
    if (gap_pct >= t["gap_high_pct"] or atr_extension >= t["atr_extension_high"] or
            vwap_distance_pct >= t["vwap_distance_high_pct"] or recent_move_pct >= t["gap_high_pct"]):
        return "HIGH"
    return "MEDIUM" if max(gap_pct, vwap_distance_pct, recent_move_pct) >= t["gap_high_pct"] / 2 else "LOW"


def event_entry_score(event, price_change_pct, rvol, rs_spy_pct, breakout, cfg):
    if not event or event.get("materiality") not in {"MATERIAL", "HIGH"}:
        return 50
    if event.get("direction") != "POSITIVE" or not event.get("credible"):
        return 20
    t = cfg["thresholds"]
    return clamp(25 + 20 * (price_change_pct > 0) + 20 * (rvol >= t["rvol_breakout"])
                 + 15 * (rs_spy_pct >= t["relative_strength_positive_pct"])
                 + 20 * breakout - 25 * (price_change_pct < 0))


def premarket_gate(candidate, gap_pct, market_regime, overnight_news_status, benchmark_context, cfg):
    if candidate != "BUY_TOMORROW_CANDIDATE":
        return "CANCEL_ENTRY"
    if overnight_news_status == "MATERIAL_NEGATIVE" or market_regime == "EXTREME":
        return "CANCEL_ENTRY"
    if (overnight_news_status == "UNKNOWN" or benchmark_context is None or gap_pct is None or
            market_regime == "UNAVAILABLE"):
        return "WAIT"
    t = cfg["thresholds"]
    if gap_pct >= t["premarket_cancel_gap_pct"]:
        return "CANCEL_ENTRY"
    if abs(gap_pct) >= t["premarket_wait_gap_pct"] or benchmark_context == "WEAK":
        return "WAIT"
    return "APPROVED"


def opening_confirmation(premarket_state, minutes_after_open, price, vwap, opening_high,
                         rvol, gap_pct, cfg):
    if premarket_state != "APPROVED" or minutes_after_open < cfg["thresholds"]["opening_wait_minutes"]:
        return "WAIT"
    if None in (price, vwap, opening_high, rvol, gap_pct):
        return "WAIT"
    return ("ENTRY_CONFIRMED" if price >= vwap and price >= opening_high and
            rvol >= cfg["thresholds"]["opening_rvol_min"] and
            gap_pct < cfg["thresholds"]["premarket_wait_gap_pct"] else "WAIT")


def decide(daily, intraday, benchmark_returns, sector_return, research, regime, cfg, quant_params,
           *, event=None, mode="CLOSE", decision_time=None):
    """Only point-in-time bars and a frozen research reference enter the score."""
    if mode not in {"CLOSE", "EVENT"}:
        raise ValueError("unsupported Entry mode")
    asof = daily[-1]["date"]
    event_fresh = False
    if mode == "EVENT":
        if decision_time is None or decision_time.tzinfo is None:
            raise ValueError("timezone-aware event decision time required")
        eastern = decision_time.astimezone(ET)
        if eastern.date().isoformat() != asof:
            raise ValueError("event session/date mismatch")
        cutoff = int(decision_time.timestamp() * 1000)
        intraday = [bar for bar in intraday if bar["ts_ms"] + 300000 <= cutoff]
        today = [bar for bar in intraday if bar["date"] == asof]
        if not today:
            raise ValueError("no completed current-session 5M bars")
        daily = daily[:-1] + [{"date": asof, "open": today[0]["open"],
                 "high": max(b["high"] for b in today), "low": min(b["low"] for b in today),
                 "close": today[-1]["close"], "volume": sum(b["volume"] for b in today)}]
        minutes = eastern.hour * 60 + eastern.minute
        age = (cutoff - (today[-1]["ts_ms"] + 300000)) / 60000
        event_fresh = (570 <= minutes < 960 and
                       age <= cfg["thresholds"]["event_max_bar_age_minutes"])
    frame_bars = {"daily": daily, **{name: aggregate(intraday, name) for name in ("1h", "15m", "5m")}}
    f = {name: timeframe_features(bars, name, cfg, quant_params, asof)
         for name, bars in frame_bars.items()}
    vwap = session_vwap(intraday, asof)
    if vwap is None or not benchmark_returns or any(benchmark_returns.get(k) is None for k in ("SPY", "QQQ")):
        raise ValueError("missing VWAP or benchmark returns")
    t = cfg["thresholds"]
    price = f["5m"]["close"]
    daily_close = daily[-1]["close"]
    prior_close = daily[-2]["close"]
    gap_pct = (daily[-1]["open"] / prior_close - 1) * 100
    change_pct = (price / prior_close - 1) * 100
    vwap_distance_pct = (price / vwap["value"] - 1) * 100
    recent_move_pct = (price / intraday[-min(13, len(intraday))]["close"] - 1) * 100
    atr_extension = max(0, (price - daily[-1]["open"]) / max(f["daily"]["atr14"], .000001))
    chase = chase_risk(gap_pct, vwap_distance_pct, atr_extension, recent_move_pct, cfg)
    rvol = daily[-1]["volume"] / statistics.mean(b["volume"] for b in daily[-21:-1])
    if mode == "EVENT":
        rvol *= 78 / len([b for b in intraday if b["date"] == asof])
    previous_high = max(b["high"] for b in daily[-21:-1])
    breakout = daily_close > previous_high
    rs = {name: change_pct - value for name, value in benchmark_returns.items()}
    if sector_return is not None:
        rs["SECTOR"] = change_pct - sector_return
    support = levels(daily, price, cfg)
    trends = {name: trend(item, cfg) for name, item in f.items()}
    current_event = event if event and event.get("date") == asof else None
    entry_event = event_entry_score(current_event, change_pct, rvol, rs["SPY"], breakout, cfg)
    trend_points = {"STRONG_UPTREND": 100, "UPTREND": 75, "NEUTRAL": 50,
                    "DOWNTREND": 25, "STRONG_DOWNTREND": 0}
    trend_score = .6 * trend_points[trends["daily"]] + .4 * trend_points[trends["1h"]]
    macd_good = sum((f[n]["macd_line"] > f[n]["macd_signal"] and
                     f[n]["macd_hist_slope"] > 0 and f[n]["macd_line"] > 0)
                    for n in ("1h", "15m"))
    momentum_score = 25 + macd_good * 30 + 15 * (f["15m"]["kdj_k"] > f["15m"]["kdj_d"])
    if f["5m"]["rsi14"] >= t["rsi_5m_overheat"]:
        momentum_score -= 20
    location_score = (70 + 15 * (vwap["slope"] > 0) + 15 * vwap["reclaim"]
                      - 40 * vwap["rejection"] - 7 * max(0, vwap_distance_pct))
    volume_score = (50 + 30 * (rvol >= t["rvol_breakout"] and change_pct > 0)
                    + 20 * (breakout and rvol >= t["rvol_breakout"])
                    - 30 * (breakout and rvol < t["rvol_low"])
                    - 15 * (change_pct > 0 and rvol < t["rvol_low"]))
    rs_value = statistics.mean(rs.values())
    rs_score = clamp(50 + 15 * rs_value)
    support_score = (50 + 20 * (support["risk_reward"] is not None and support["risk_reward"] >= 2)
                     + 15 * (support["support_distance_pct"] is not None and
                             support["support_distance_pct"] <= 2)
                     + 15 * support["support_bounce"] - 25 * support["resistance_rejection"]
                     - 25 * (support["resistance_distance_pct"] is not None and
                             support["resistance_distance_pct"] <= 1))
    chase_score = {"LOW": 100, "MEDIUM": 70, "HIGH": 30, "EXTREME": 0}[chase]
    regime_score = {"CALM": 100, "NORMAL": 90, "ELEVATED": 65,
                    "HIGH_RISK": 35, "EXTREME": 0, "UNAVAILABLE": 0}[regime]
    components = {"trend_quality": clamp(trend_score), "momentum_confirmation": clamp(momentum_score),
                  "vwap_price_location": clamp(location_score), "volume_confirmation": clamp(volume_score),
                  "relative_strength": rs_score, "support_resistance": clamp(support_score),
                  "event_confirmation": entry_event, "chase_control": chase_score,
                  "market_regime": regime_score}
    score = clamp(sum(components[k] * weight for k, weight in cfg["component_weights"].items()))
    threshold = t["entry_buy_tomorrow"] + (t["high_risk_entry_add"] if regime == "HIGH_RISK" else 0)
    event_allowed = (event is not None and event.get("materiality") in {"MATERIAL", "HIGH"} and
                     event.get("direction") == "POSITIVE" and event.get("credible") is True and
                     event.get("date") == asof)
    safe = (trends["daily"] not in {"DOWNTREND", "STRONG_DOWNTREND"} and
            trends["1h"] not in {"DOWNTREND", "STRONG_DOWNTREND"} and
            chase != "EXTREME" and regime not in {"EXTREME", "UNAVAILABLE"} and
            not research.get("major_unresolved_risk"))
    if mode == "EVENT" and event_fresh and event_allowed and safe and regime != "HIGH_RISK" and score >= t["entry_buy_now"] and \
            entry_event >= t["event_buy_now"] and research.get("conviction_score", 0) >= t["conviction_buy_now"] and \
            rvol >= t["rvol_breakout"] and rs["SPY"] > 0 and price >= vwap["value"] and breakout and \
            trends["15m"] in {"UPTREND", "STRONG_UPTREND"} and \
            trends["5m"] not in {"DOWNTREND", "STRONG_DOWNTREND"}:
        state = "BUY_NOW_CANDIDATE"
    elif mode == "CLOSE" and safe and score >= threshold and daily_close >= daily[-1]["low"] + \
            t["close_near_high_fraction"] * (daily[-1]["high"] - daily[-1]["low"]) and \
            rvol >= t["rvol_low"] and rs["SPY"] > 0 and \
            trends["15m"] in {"UPTREND", "STRONG_UPTREND"}:
        state = "BUY_TOMORROW_CANDIDATE"
    elif trends["daily"] in {"DOWNTREND", "STRONG_DOWNTREND"} or (score < t["entry_watch"] and mode == "CLOSE"):
        state = "REJECT_ENTRY"
    else:
        state = "WATCH"
    why_now = []
    why_wait = []
    if trends["daily"] in {"UPTREND", "STRONG_UPTREND"}: why_now.append("Daily trend supports entry")
    if rs["SPY"] > 0: why_now.append("Outperforming SPY intraday")
    if breakout and rvol >= t["rvol_breakout"]: why_now.append("Breakout confirmed by volume")
    if vwap["reclaim"]: why_now.append("VWAP reclaimed")
    if support["support_bounce"]: why_now.append("Support bounce")
    if event_allowed and entry_event >= t["event_buy_now"]: why_now.append("Material event confirmed by price and volume")
    if chase in {"HIGH", "EXTREME"}: why_wait.append(f"{chase} chase risk")
    if f["5m"]["rsi14"] >= t["rsi_5m_overheat"]: why_wait.append("5M RSI overextended")
    if trends["5m"] in {"DOWNTREND", "STRONG_DOWNTREND"}: why_wait.append("5M timing is weakening")
    if trends["15m"] in {"DOWNTREND", "STRONG_DOWNTREND"}: why_wait.append("15M entry confirmation missing")
    if vwap_distance_pct >= t["vwap_distance_high_pct"]: why_wait.append("Price far above VWAP")
    if vwap["rejection"]: why_wait.append("VWAP rejection")
    if support["resistance_rejection"]: why_wait.append("Resistance rejection")
    if breakout and rvol < t["rvol_low"]: why_wait.append("Breakout lacks volume")
    if daily_close < daily[-1]["low"] + t["close_near_high_fraction"] * (daily[-1]["high"] - daily[-1]["low"]):
        why_wait.append("Close not near daily high")
    if regime in {"HIGH_RISK", "EXTREME", "UNAVAILABLE"}: why_wait.append(f"Market regime {regime}")
    if mode == "EVENT" and not event_fresh: why_wait.append("Event-mode market bar is stale or outside regular hours")
    if not why_wait and state == "WATCH": why_wait.append("Entry confirmation threshold not met")
    return {"asof": asof, "entry_price_reference": round(price, 4), "entry_score": score,
            "entry_state": state, "components": components, "timeframe_trends": trends,
            "timeframes": f, "chase_risk": chase, "market_regime": regime,
            "event_entry_score": entry_event, "why_now": why_now, "why_wait": why_wait,
            "features": {"vwap": vwap, "vwap_distance_pct": vwap_distance_pct,
                         "rvol": rvol, "gap_pct": gap_pct, "atr_extension": atr_extension,
                         "recent_move_pct": recent_move_pct, "relative_strength_pct": rs,
                         "support_resistance": support, "breakout": breakout,
                         "distance_52w_high_pct": (daily_close/max(b["high"] for b in daily[-252:])-1)*100,
                         "close_location": ((daily_close-daily[-1]["low"])/
                                            max(.000001,daily[-1]["high"]-daily[-1]["low"]))}}


def save_decision(db, run_id, ticker, result, research):
    if result["entry_state"] not in STATES:
        raise ValueError("invalid Entry state")
    with db:
        db.execute("""INSERT INTO entry_decisions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                   (run_id, ticker, utc_now(), result.get("entry_price_reference"), result.get("entry_score"),
                    result["entry_state"], result.get("signal_validity", "POINT_IN_TIME"),
                    result.get("chase_risk"), result.get("market_regime"),
                    json.dumps(result.get("components", {})), json.dumps(result.get("timeframes", {})),
                    json.dumps(result.get("features", {})),
                    json.dumps({"why_now": result.get("why_now", []), "why_wait": result.get("why_wait", [])}),
                    json.dumps(research)))
        plan = result.get("buy_plan") or {}
        if plan.get("status") == "CONDITIONAL":
            db.execute("""INSERT INTO entry_reference_plans VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                       (run_id, ticker, plan["generated_from_trading_date"],
                        plan["target_trading_date"], plan["reference_generated_at"],
                        plan["buy_zone_low"], plan["buy_zone_high"],
                        plan["do_not_chase_above"], plan.get("nearest_support"),
                        plan.get("nearest_resistance"), plan.get("chase_risk"),
                        json.dumps(plan, sort_keys=True)))


def update_forward(entry_db, market_db, asof, horizons=HORIZONS):
    inserted = 0
    rows = entry_db.execute("""SELECT d.run_id,d.ticker,d.entry_price,r.asof FROM entry_decisions d
        JOIN entry_runs r ON r.id=d.run_id WHERE d.entry_price>0
        AND d.signal_validity='POINT_IN_TIME' AND r.asof<=?""", (asof,)).fetchall()
    for run_id, ticker, price, anchor in rows:
        future = daily_bars(market_db, entry_db, ticker, asof)
        future = [b for b in future if b["date"] > anchor]
        for horizon in horizons:
            if len(future) < horizon:
                continue
            window = future[:horizon]
            with entry_db:
                cursor = entry_db.execute("""INSERT OR IGNORE INTO entry_forward VALUES(?,?,?,?,?,?,?,?)""",
                    (run_id, ticker, horizon, anchor, window[-1]["date"],
                     (window[-1]["close"] / price - 1) * 100,
                     (max(b["high"] for b in window) / price - 1) * 100,
                     (min(b["low"] for b in window) / price - 1) * 100))
            inserted += cursor.rowcount
    return inserted


def research_reference(db, ticker):
    row = db.execute("""SELECT run_id,research_status,candidate_score,research_score,
        catalyst_score,conviction_score,thesis_confidence,created_at FROM evidence_reasoner_results
        WHERE ticker=? AND research_status='COMPLETE' ORDER BY created_at DESC LIMIT 1""",
        (ticker,)).fetchone()
    if not row:
        raise ValueError("no reliable frozen Phase 4 research")
    result = dict(zip(("source_run_id", "research_status", "candidate_score", "research_score",
                       "catalyst_score", "conviction_score", "thesis_confidence", "created_at"), row))
    frozen = db.execute("""SELECT packet_json FROM research_priority WHERE ticker=? AND run_id<=?
        ORDER BY run_id DESC LIMIT 1""", (ticker, row[0])).fetchone()
    result["sector"] = json.loads(frozen[0]).get("sector") if frozen else None
    return result


def select_entry_candidates(final_research, watch_tickers, position_tickers, cfg):
    """Select only completed Phase 4 candidates, with monitor names added for reassessment."""
    complete = {row["ticker"]: row for row in final_research
                if row.get("research_status") == "COMPLETE" and
                row.get("final_research_tier") in {"INITIAL", "DEEP", "INITIAL_FALLBACK"}}
    ranked = sorted(complete.values(), key=lambda row: (-row["candidate_score"], row["ticker"]))
    primary = [row["ticker"] for row in ranked[:cfg["candidate_limit"]]]
    additional = sorted(((set(watch_tickers) | set(position_tickers)) & complete.keys()) - set(primary))
    return primary + additional


def current_regime(db, asof):
    row = db.execute("""SELECT regime,regime_source,asof FROM market_regime_runs
        WHERE asof<=? ORDER BY asof DESC,id DESC LIMIT 1""", (asof,)).fetchone()
    if not row or row[2] != asof:
        return "UNAVAILABLE", "NONE"
    return row[0], row[1]


def research_available_by_close(reference, asof):
    completed = datetime.fromisoformat(reference["created_at"].replace("Z", "+00:00"))
    if completed.tzinfo is None:
        return False
    close = datetime.fromisoformat(asof + "T16:00:00").replace(tzinfo=ET)
    return completed.astimezone(ET) <= close


def pilot(tickers=("CDNA", "AMD", "META"), *, client=None, asof=None, market_path=None,
          research_path=None, entry_path=None, report_path=None):
    """Controlled three-stock research-reference pilot; no Telegram or AI integration."""
    if tuple(tickers) != ("CDNA", "AMD", "META"):
        raise ValueError("first pilot is restricted to CDNA, AMD, META")
    started = time.monotonic()
    cfg = load_config()
    asof = asof or previous_session(datetime.now(ET).date()).isoformat()
    market_path = market_path or ROOT / "data" / "market_data.db"
    research_path = research_path or ROOT / "data" / "research_data.db"
    entry_path = entry_path or ROOT / "data" / "entry_data.db"
    report_path = report_path or ROOT / "data" / "phase5_entry_pilot.json"
    quant_params = json.loads((ROOT / "quant_config.json").read_text())["feature_parameters"]
    sector_to_etf = json.loads((ROOT / "real_market_config.json").read_text())["sector_to_etf"]
    market = sqlite3.connect(f"file:{market_path}?mode=ro", uri=True)
    research = sqlite3.connect(f"file:{research_path}?mode=ro", uri=True)
    entry = sqlite3.connect(entry_path)
    init_db(entry)
    if client is None:
        client = MassiveClient(load_key(ROOT / ".env"), spacing=13)
    begun = utc_now()
    with entry:
        entry.execute("UPDATE entry_runs SET ended_at=?,status='INTERRUPTED' WHERE status='RUNNING'", (begun,))
        run_id = entry.execute("INSERT INTO entry_runs(asof,started_at,status,source,config_json) "
                               "VALUES(?,?,?,?,?)", (asof, begun, "RUNNING", "MASSIVE",
                                                  json.dumps(cfg, sort_keys=True))).lastrowid
    report = {"run_id": run_id, "asof": asof, "source": "MASSIVE", "results": {}, "errors": {},
              "api_requests": 0, "downloaded_rows": 0, "ai_calls": 0, "ai_tokens": 0,
              "telegram_messages": 0}
    try:
        symbols = set(tickers) | {"SPY", "QQQ"}
        sector = {}
        references = {}
        for ticker in tickers:
            try:
                references[ticker] = research_reference(research, ticker)
                sector[ticker] = sector_to_etf.get(references[ticker]["sector"])
            except Exception as exc:
                report["errors"][ticker] = f"RESEARCH:{type(exc).__name__}:{exc}"
                sector[ticker] = None
            if sector[ticker]: symbols.add(sector[ticker])
        for symbol in sorted(symbols):
            try:
                count, status = sync_symbol(entry, client, symbol, asof, cfg)
                report["downloaded_rows"] += count
                print(f"DATA {symbol} {status} +{count}", flush=True)
            except Exception as exc:
                report["errors"][symbol] = f"MARKET_DATA:{type(exc).__name__}:{exc}"
                print(f"DATA {symbol} FAILED {type(exc).__name__}", flush=True)
        regime, source = current_regime(market, asof)
        report["market_regime"] = {"regime": regime, "source": source}
        def intraday_return(symbol):
            bars = cache_intraday(entry, symbol, asof)
            daily = daily_bars(market, entry, symbol, asof)
            if not bars or bars[-1]["date"] != asof or len(daily) < 2:
                return None
            return (bars[-1]["close"] / daily[-2]["close"] - 1) * 100
        bench = {name: intraday_return(name) for name in ("SPY", "QQQ")}
        for ticker in tickers:
            try:
                reference = references[ticker]
                daily = daily_bars(market, entry, ticker, asof)
                intra = cache_intraday(entry, ticker, asof)
                if not daily or daily[-1]["date"] != asof or not intra or intra[-1]["date"] != asof:
                    raise ValueError("stale or missing asof bars")
                result = decide(daily, intra, bench, intraday_return(sector[ticker]) if sector[ticker] else None,
                                reference, regime, cfg, quant_params)
                if not research_available_by_close(reference, asof):
                    result["signal_validity"] = "HISTORICAL_DIAGNOSTIC_ONLY"
                    result["entry_state"] = "WAIT"
                    result["why_wait"].append("Frozen Phase 4 research postdates available market bars")
                else:
                    result["signal_validity"] = "POINT_IN_TIME"
                result.update(ticker=ticker, research_reference=reference,
                              market_regime_source=source, data_asof=asof)
                save_decision(entry, run_id, ticker, result, reference)
                report["results"][ticker] = result
            except Exception as exc:
                report["errors"][ticker] = f"ENTRY:{type(exc).__name__}:{exc}"
                report["results"][ticker] = {"ticker": ticker, "entry_state": "WAIT",
                                               "reason": "DATA_OR_RESEARCH_UNAVAILABLE"}
                save_decision(entry, run_id, ticker, report["results"][ticker], {})
        report["api_requests"] = client.request_count
        report["runtime_seconds"] = round(time.monotonic() - started, 2)
        report["forward_observations_written"] = update_forward(entry, market, asof)
        with entry:
            entry.execute("UPDATE entry_runs SET ended_at=?,status=?,api_requests=?,rows_downloaded=? WHERE id=?",
                          (utc_now(), "COMPLETE" if not report["errors"] else "PARTIAL",
                           report["api_requests"], report["downloaded_rows"], run_id))
        cumulative = entry.execute("""SELECT COALESCE(SUM(api_requests),0),COALESCE(SUM(rows_downloaded),0)
            FROM entry_runs WHERE asof=? AND status='COMPLETE'""", (asof,)).fetchone()
        report["pilot_cumulative_api_requests"] = cumulative[0]
        report["pilot_cumulative_downloaded_rows"] = cumulative[1]
        Path(report_path).write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
        return report
    finally:
        market.close(); research.close(); entry.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pilot", action="store_true", help="run CDNA/AMD/META market-data-only pilot")
    args = parser.parse_args()
    if not args.pilot:
        parser.error("only --pilot is enabled; no automatic scheduler or BUY messaging")
    result = pilot()
    print(json.dumps({"asof": result["asof"], "states": {k: v["entry_state"] for k,v in result["results"].items()},
                      "errors": result["errors"], "api_requests": result["api_requests"],
                      "ai_tokens": result["ai_tokens"], "runtime_seconds": result["runtime_seconds"]}, indent=2))


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Validated Yahoo/Cboe VIX/VXN daily context with real SPY/QQQ proxy fallback."""
from __future__ import annotations

import argparse
import csv
import io
import json
import logging
import math
import os
import sqlite3
import statistics
import time
import urllib.error
import urllib.request
from datetime import date, datetime, time as clock_time, timedelta, timezone
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

from quant_screen import ROOT, init_db


ET = ZoneInfo("America/New_York")
SYMBOLS = {"VIX": "^VIX", "VXN": "^VXN"}
REGIMES = ("CALM", "NORMAL", "ELEVATED", "HIGH_RISK", "EXTREME")
LOG = logging.getLogger("market_regime")


def observed(day, month, year):
    result = date(year, month, day)
    return result - timedelta(days=1) if result.weekday() == 5 else result + timedelta(days=1) if result.weekday() == 6 else result


def nth_weekday(year, month, weekday, nth):
    first = date(year, month, 1)
    return first + timedelta(days=(weekday - first.weekday()) % 7 + 7 * (nth - 1))


def last_weekday(year, month, weekday):
    first_next = date(year + (month == 12), month % 12 + 1, 1)
    last = first_next - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def easter(year):
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = (h + l - 7 * m + 114) % 31 + 1
    return date(year, month, day)


def is_market_holiday(day):
    y = day.year
    holidays = {observed(1, 1, y), observed(1, 1, y + 1),
                nth_weekday(y, 1, 0, 3), nth_weekday(y, 2, 0, 3),
                easter(y) - timedelta(days=2), last_weekday(y, 5, 0),
                observed(19, 6, y), observed(4, 7, y),
                nth_weekday(y, 9, 0, 1), nth_weekday(y, 11, 3, 4),
                observed(25, 12, y)}
    return day in holidays


def market_session(day):
    return day.weekday() < 5 and not is_market_holiday(day)


def previous_session(day):
    day -= timedelta(days=1)
    while not market_session(day):
        day -= timedelta(days=1)
    return day


def session_phase(now, cfg):
    now = now.astimezone(ET)
    if not market_session(now.date()):
        return "CLOSED"
    opened = clock_time.fromisoformat(cfg["regular_open_et"])
    closed = clock_time.fromisoformat(cfg["regular_close_et"])
    if now.time().replace(tzinfo=None) < opened:
        return "PRE_MARKET"
    if now.time().replace(tzinfo=None) < closed:
        return "REGULAR"
    return "AFTER_HOURS"


def finite_positive(value):
    try:
        value = float(value)
        return value if math.isfinite(value) and value > 0 else None
    except (TypeError, ValueError):
        return None


def parse_index(name, payload, asof, now, cfg):
    result = {"value": None, "previous_close": None, "daily_change": None,
              "daily_change_pct": None, "timestamp": None, "latest_trading_date": None,
              "status": "UNAVAILABLE", "error": None, "source": "YAHOO", "history": []}
    try:
        chart = payload["chart"]["result"][0]
        quote_rows = chart["indicators"]["quote"][0]
        stamps = chart["timestamp"]
        closes = quote_rows["close"]
        if len(stamps) != len(closes):
            raise ValueError("bar_length_mismatch")
        current_day = now.astimezone(ET).date().isoformat()
        phase = session_phase(now, cfg)
        asof_day = date.fromisoformat(asof)
        target_day = (previous_session(asof_day).isoformat() if not market_session(asof_day)
                      or (asof == current_day and phase == "PRE_MARKET") else asof)
        for i, (stamp, close) in enumerate(zip(stamps, closes)):
            day = datetime.fromtimestamp(stamp, timezone.utc).astimezone(ET).date().isoformat()
            price = finite_positive(close)
            if price is None:
                if day == target_day:
                    raise ValueError("nan_inf_or_missing_latest_close")
                continue
            if day <= target_day:
                result["history"].append({"date": day, "open": finite_positive(quote_rows.get("open", [None] * len(stamps))[i]),
                                          "high": finite_positive(quote_rows.get("high", [None] * len(stamps))[i]),
                                          "low": finite_positive(quote_rows.get("low", [None] * len(stamps))[i]),
                                          "close": price})
        history = result["history"]
        if len(history) < 2 or history[-1]["date"] != target_day:
            result["status"], result["error"] = "STALE", "latest_bar_not_asof"
            return result
        if len({row["date"] for row in history}) != len(history):
            raise ValueError("duplicate_daily_bar")
        value, prior = history[-1]["close"], history[-2]["close"]
        # Yahoo chartPreviousClose can mean the close before the entire requested range.
        # Always derive the true previous daily close from dated bars.
        change = value - prior
        result.update(value=round(value, 4), previous_close=round(prior, 4),
                      daily_change=round(change, 4), daily_change_pct=round(change / prior * 100, 4),
                      latest_trading_date=target_day)
        meta = chart.get("meta") or {}
        raw_ts = meta.get("regularMarketTime")
        if raw_ts is None:
            result["status"], result["error"] = "INVALID", "quote_timestamp_missing"
            return result
        timestamp = datetime.fromtimestamp(float(raw_ts), timezone.utc)
        result["timestamp"] = timestamp.isoformat()
        quote_date = timestamp.astimezone(ET).date().isoformat()
        if quote_date == target_day:
            quote_price = finite_positive(meta.get("regularMarketPrice"))
            if quote_price is None or abs(quote_price / value - 1) > 0.02:
                result["status"], result["error"] = "INVALID", "quote_bar_mismatch"
                return result
        if abs(change / prior) > cfg["max_daily_jump_fraction"]:
            result["status"], result["error"] = "INVALID", "abnormal_jump"
            return result
        unchanged = cfg["unchanged_sessions_warning"]
        if len(history) >= unchanged and len({r["close"] for r in history[-unchanged:]}) == 1:
            result["status"], result["error"] = "INVALID", "unchanged_many_sessions"
            return result
        if target_day != asof and asof == current_day:
            result["status"] = "LAST_MARKET_CLOSE"
            return result
        if asof < current_day:
            if asof == previous_session(now.astimezone(ET).date()).isoformat() and phase in ("CLOSED", "PRE_MARKET", "REGULAR"):
                result["status"] = "LAST_MARKET_CLOSE"
            else:
                result["status"], result["error"] = "STALE", "older_than_last_close"
            return result
        if quote_date != asof:
            result["status"], result["error"] = "STALE", "quote_not_current_session"
            return result
        age = (now.astimezone(timezone.utc) - timestamp).total_seconds()
        if age < -300:
            result["status"], result["error"] = "INVALID", "future_timestamp"
        elif (phase == "AFTER_HOURS" and timestamp.astimezone(ET).time().replace(tzinfo=None)
              >= clock_time.fromisoformat(cfg["last_close_min_time_et"])):
            result["status"] = "LAST_MARKET_CLOSE"
        elif age <= cfg["fresh_seconds"]:
            result["status"] = "FRESH"
        elif age <= cfg["delayed_seconds"]:
            result["status"] = "DELAYED"
        else:
            result["status"], result["error"] = "STALE", "quote_too_old"
    except (KeyError, IndexError, TypeError, ValueError, OverflowError) as exc:
        result["status"], result["error"] = "INVALID", str(exc)
    return result


def yahoo_get(symbol, history_range="2y", opener=None, sleeper=time.sleep, metrics=None):
    opener = opener or urllib.request.urlopen
    url = ("https://query1.finance.yahoo.com/v8/finance/chart/" + quote(symbol, safe="")
           + "?range=" + history_range + "&interval=1d&includePrePost=false&events=div%2Csplits")
    for attempt, delay in enumerate((0, 5, 15, 30)):
        if delay:
            sleeper(delay)
        try:
            request = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "AverraStockAgent/1.0"})
            if metrics is not None:
                metrics["api_requests"] = metrics.get("api_requests", 0) + 1
            with opener(request, timeout=20) as response:
                return json.load(response)
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
            if isinstance(exc, urllib.error.HTTPError) and exc.code in (400, 401, 403, 404):
                raise RuntimeError(f"YAHOO_{symbol.lstrip('^')}_ERROR: HTTP {exc.code}") from exc
            if attempt == 3:
                raise RuntimeError(f"YAHOO_{symbol.lstrip('^')}_ERROR: {type(exc).__name__}") from exc
    raise AssertionError("unreachable")


def proxy_regime(db, asof, cfg):
    latest = db.execute("""SELECT MAX(a.date) FROM daily_bars a JOIN daily_bars b ON a.date=b.date
        WHERE a.ticker='SPY' AND b.ticker='QQQ' AND a.date<=?""", (asof,)).fetchone()[0]
    if not latest or (date.fromisoformat(asof) - date.fromisoformat(latest)).days > cfg["maximum_proxy_lag_calendar_days"]:
        return "UNAVAILABLE", "NONE", {"error": "stale_or_missing_spy_qqq_proxy_data", "latest_date": latest}
    values = []
    for symbol in ("SPY", "QQQ"):
        bars = [dict(row) for row in db.execute("SELECT date,high,low,close FROM daily_bars WHERE ticker=? AND date<=? ORDER BY date DESC LIMIT 61", (symbol, latest))]
        bars.reverse()
        if len(bars) < 61 or bars[-1]["date"] != latest:
            continue
        returns = [bars[i]["close"] / bars[i - 1]["close"] - 1 for i in range(1, len(bars))]
        realized = statistics.pstdev(returns[-20:]) * math.sqrt(252)
        atr_pct = statistics.mean(max(bars[i]["high"] - bars[i]["low"],
                                      abs(bars[i]["high"] - bars[i - 1]["close"]),
                                      abs(bars[i]["low"] - bars[i - 1]["close"]))
                                  for i in range(len(bars) - 14, len(bars))) / bars[-1]["close"]
        drawdown = bars[-1]["close"] / max(row["close"] for row in bars) - 1
        values.append({"symbol": symbol, "realized_vol20": realized, "atr_pct14": atr_pct,
                       "drawdown60": drawdown})
    if not values:
        return "UNAVAILABLE", "NONE", {"error": "insufficient_spy_qqq_proxy_data", "latest_date": latest}
    prior = db.execute("SELECT MAX(date) FROM daily_bars WHERE ticker='SPY' AND date<?", (latest,)).fetchone()[0]
    cross = []
    if prior:
        cross = [row[0] / row[1] - 1 for row in db.execute("""SELECT a.close,b.close FROM daily_bars a
            JOIN daily_bars b ON a.ticker=b.ticker AND b.date=?
            JOIN securities s ON s.ticker=a.ticker AND s.asset_type='COMMON_STOCK' AND s.country='US'
            WHERE a.date=? AND b.close>0""", (prior, latest))]
    risk = max(item["realized_vol20"] + item["atr_pct14"] * 2 + abs(min(0, item["drawdown60"])) * 0.5
               for item in values)
    cuts = cfg["market_proxy"]
    idx = sum(risk >= cuts[name] for name in ("calm_max_volatility", "normal_max_volatility",
                                               "elevated_max_volatility", "high_risk_max_volatility"))
    confidence = "MEDIUM" if len(values) == 2 and latest == asof else "LOW"
    return REGIMES[idx], confidence, {"benchmarks": values, "proxy_risk": risk,
                                      "latest_data_date": latest, "market_breadth_up_fraction":
                                      sum(value > 0 for value in cross) / len(cross) if cross else None,
                                      "cross_sectional_return_volatility": statistics.pstdev(cross) if len(cross) > 1 else None}


def cboe_get(name, metrics=None):
    url = "https://cdn.cboe.com/api/global/us_indices/daily_prices/" + name + "_History.csv"
    request = urllib.request.Request(url, headers={"User-Agent": "AverraStockAgent/1.0"})
    if metrics is not None:
        metrics["api_requests"] = metrics.get("api_requests", 0) + 1
    with urllib.request.urlopen(request, timeout=20) as response:
        return response.read().decode("utf-8-sig")


def cboe_quote_get(name, metrics=None):
    url = "https://cdn.cboe.com/api/global/delayed_quotes/quotes/_" + name + ".json"
    request = urllib.request.Request(url, headers={"User-Agent": "AverraStockAgent/1.0"})
    if metrics is not None:
        metrics["api_requests"] = metrics.get("api_requests", 0) + 1
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.load(response)


def parse_cboe_vxn_close(text, payload, asof, now, cfg):
    """Use the official 16:15 VXN observation while its daily CSV is pending."""
    try:
        quote_row = payload["data"]
        if (payload["symbol"] != "_VXN" or quote_row["symbol"] != "^VXN" or
                quote_row["index"] != "VXN" or quote_row["security_type"] != "index"):
            raise ValueError("cboe_quote_identity_mismatch")
        stamp = datetime.fromisoformat(quote_row["last_trade_time"])
        if stamp.tzinfo is None:
            raise ValueError("cboe_quote_timezone_missing")
        eastern = stamp.astimezone(ET)
        # Cboe's VXN methodology ends dissemination at 16:15 ET. A web
        # refresh timestamp, intraday quote or prior-day observation is insufficient.
        if (asof != now.astimezone(ET).date().isoformat() or
                eastern.date().isoformat() != asof or
                not clock_time(16, 15) <= eastern.time().replace(tzinfo=None) <= clock_time(16, 16) or
                stamp > now or session_phase(now, cfg) != "AFTER_HOURS"):
            raise ValueError("cboe_quote_not_current_session_close")
        rows = list(csv.DictReader(io.StringIO(text)))
        prior_day = previous_session(date.fromisoformat(asof))
        if not rows or datetime.strptime(rows[-1]["DATE"], "%m/%d/%Y").date() != prior_day:
            raise ValueError("cboe_quote_prior_session_missing")
        prior = finite_positive(rows[-1]["CLOSE"])
        reference = finite_positive(quote_row["prev_day_close"])
        price = finite_positive(quote_row["close"])
        current = finite_positive(quote_row["current_price"])
        if (prior is None or reference is None or price is None or current is None or
                abs(prior - reference) > 0.005 or abs(price - current) > 0.005):
            raise ValueError("cboe_quote_close_mismatch")
        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=["DATE", "OPEN", "HIGH", "LOW", "CLOSE"])
        writer.writeheader()
        writer.writerows(rows)
        writer.writerow({"DATE": date.fromisoformat(asof).strftime("%m/%d/%Y"),
                         "OPEN": quote_row["open"], "HIGH": quote_row["high"],
                         "LOW": quote_row["low"], "CLOSE": price})
        result = parse_cboe_index(output.getvalue(), asof, now, cfg)
        if result["status"] != "LAST_MARKET_CLOSE":
            raise ValueError(result["error"])
        result.update(timestamp=stamp.astimezone(timezone.utc).isoformat(),
                      source_detail="CBOE_VXN_1615_CLOSING_QUOTE")
        return result
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        return {"status": "INVALID", "error": str(exc)}


def parse_cboe_index(text, asof, now, cfg):
    result = {"value": None, "previous_close": None, "daily_change": None,
              "daily_change_pct": None, "timestamp": None, "latest_trading_date": None,
              "status": "UNAVAILABLE", "error": None, "source": "CBOE", "history": []}
    try:
        current_day = now.astimezone(ET).date()
        asof_day = date.fromisoformat(asof)
        phase = session_phase(now, cfg)
        target = (previous_session(asof_day) if not market_session(asof_day)
                  or (asof_day == current_day and phase == "PRE_MARKET") else asof_day)
        # Cboe's file contains daily closes, not a live intraday quote.
        if target > current_day or (target == current_day and
                now.astimezone(ET).time().replace(tzinfo=None) <
                clock_time.fromisoformat(cfg["last_close_min_time_et"])):
            raise ValueError("completed_close_not_yet_available")
        if target < previous_session(current_day) or (target < current_day and phase == "AFTER_HOURS"):
            raise ValueError("older_than_last_close")
        rows = []
        for row in csv.DictReader(io.StringIO(text)):
            day = datetime.strptime(row["DATE"], "%m/%d/%Y").date()
            if day <= target:
                rows.append({"date": day.isoformat(), **{
                    field: finite_positive(row[field.upper()]) for field in ("open", "high", "low", "close")}})
        rows = rows[-61:]
        dates = [row["date"] for row in rows]
        if len(rows) < 2 or dates[-1] != target.isoformat():
            raise ValueError("latest_bar_not_asof")
        if dates != sorted(set(dates)):
            raise ValueError("duplicate_or_unordered_daily_bar")
        if any(row["close"] is None for row in rows):
            raise ValueError("nan_inf_or_missing_close")
        latest = rows[-1]
        if (any(latest[field] is None for field in ("open", "high", "low")) or
                not latest["low"] <= min(latest["open"], latest["close"]) <=
                max(latest["open"], latest["close"]) <= latest["high"]):
            raise ValueError("invalid_ohlc")
        value, prior = latest["close"], rows[-2]["close"]
        change = value - prior
        if abs(change / prior) > cfg["max_daily_jump_fraction"]:
            raise ValueError("abnormal_jump")
        unchanged = cfg["unchanged_sessions_warning"]
        if len(rows) >= unchanged and len({row["close"] for row in rows[-unchanged:]}) == 1:
            raise ValueError("unchanged_many_sessions")
        result.update(value=round(value, 4), previous_close=round(prior, 4),
                      daily_change=round(change, 4), daily_change_pct=round(change / prior * 100, 4),
                      latest_trading_date=target.isoformat(), status="LAST_MARKET_CLOSE", history=rows)
    except (KeyError, TypeError, ValueError) as exc:
        result["status"], result["error"] = "INVALID", str(exc)
    return result


def evaluate(db, asof, now, cfg, fetch=yahoo_get, api_metrics=None, cboe_fetch=None, cboe_quote_fetch=None):
    if cboe_fetch is None and fetch is yahoo_get:
        cboe_fetch = cboe_get
    if cboe_quote_fetch is None and fetch is yahoo_get:
        cboe_quote_fetch = cboe_quote_get
    accepted = ("FRESH", "DELAYED", "LAST_MARKET_CLOSE")
    results = {}
    for name, symbol in SYMBOLS.items():
        try:
            payload = (fetch(symbol, cfg["history_range"], metrics=api_metrics)
                       if fetch is yahoo_get else fetch(symbol, cfg["history_range"]))
            results[name] = parse_index(name, payload, asof, now, cfg)
        except Exception as exc:
            results[name] = {"status": "UNAVAILABLE", "error": f"YAHOO_{name}_ERROR:{type(exc).__name__}",
                             "source": "YAHOO", "value": None, "timestamp": None, "history": []}
            LOG.warning("YAHOO_%s_ERROR %s", name, type(exc).__name__)
        if results[name]["status"] not in accepted and cboe_fetch is not None:
            yahoo_error = results[name]["error"] or results[name]["status"]
            try:
                text = (cboe_fetch(name, metrics=api_metrics) if cboe_fetch is cboe_get else cboe_fetch(name))
                fallback = parse_cboe_index(text, asof, now, cfg)
                if (name == "VXN" and fallback["error"] == "latest_bar_not_asof" and
                        cboe_quote_fetch is not None and asof == now.astimezone(ET).date().isoformat() and
                        session_phase(now, cfg) == "AFTER_HOURS"):
                    quote_payload = (cboe_quote_fetch(name, metrics=api_metrics)
                                     if cboe_quote_fetch is cboe_quote_get else cboe_quote_fetch(name))
                    fallback = parse_cboe_vxn_close(text, quote_payload, asof, now, cfg)
                if fallback["status"] in accepted:
                    fallback["fallback_reason"] = yahoo_error
                    results[name] = fallback
                else:
                    results[name]["cboe_fallback_error"] = fallback["error"]
            except (OSError, ValueError) as exc:
                results[name]["cboe_fallback_error"] = type(exc).__name__
        history = results[name].pop("history")
        # A dated but invalid quote must not pass the downstream date gate.
        if results[name]["status"] not in accepted:
            history = []
        with db:
            db.executemany("""INSERT INTO volatility_daily(symbol,date,open,high,low,close,source,fetched_at)
                VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(symbol,date) DO UPDATE SET
                open=excluded.open,high=excluded.high,low=excluded.low,close=excluded.close,
                source=excluded.source,fetched_at=excluded.fetched_at""",
                [(name, row["date"], row["open"], row["high"], row["low"], row["close"], results[name]["source"],
                  datetime.now(timezone.utc).isoformat()) for row in history])
    reliable = [name for name in SYMBOLS if results[name]["status"] in accepted]
    if reliable:
        providers = {results[name]["source"] for name in reliable}
        provider = next(iter(providers)) if len(providers) == 1 else "YAHOO_CBOE"
        source = provider + ("_VIX_VXN" if len(reliable) == 2 else "_" + reliable[0] + "_ONLY")
        scores = []
        for name in reliable:
            cuts = cfg[name.lower() + "_regime_cuts"]
            scores.append(sum(results[name]["value"] >= cut for cut in cuts))
        regime = REGIMES[max(scores)]
        confidence = "HIGH" if len(reliable) == 2 and all(results[n]["status"] != "DELAYED" for n in reliable) else "MEDIUM"
        fallback_reason = None
    else:
        regime, confidence, proxy = proxy_regime(db, asof, cfg)
        source = "MARKET_PROXY"
        fallback_reason = {name: results[name]["error"] or results[name]["status"] for name in SYMBOLS}
    out = {"asof": asof, "evaluated_at": now.astimezone(timezone.utc).isoformat(),
           "market_phase": session_phase(now, cfg), "regime": regime,
           "regime_source": source, "confidence": confidence,
           "fallback_triggered": source == "MARKET_PROXY", "fallback_reason": fallback_reason,
           "vix": results["VIX"], "vxn": results["VXN"]}
    if source == "MARKET_PROXY":
        out["market_proxy"] = proxy
    with db:
        db.execute("INSERT INTO market_regime_runs(asof,evaluated_at,regime,regime_source,confidence,details_json) VALUES(?,?,?,?,?,?)",
                   (asof, out["evaluated_at"], regime, source, confidence, json.dumps(out, sort_keys=True)))
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=ROOT / "data" / "market_data.db")
    parser.add_argument("--config", type=Path, default=ROOT / "real_market_config.json")
    parser.add_argument("--asof", default=datetime.now(ET).date().isoformat())
    parser.add_argument("--output", type=Path, default=ROOT / "data" / "market_regime.json")
    args = parser.parse_args()
    cfg = json.loads(args.config.read_text())["volatility"]
    args.db.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(args.db) as db:
        db.row_factory = sqlite3.Row
        init_db(db)
        result = evaluate(db, args.asof, datetime.now(timezone.utc), cfg)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(f"{result['asof']} {result['regime']} {result['regime_source']} {result['confidence']}")


if __name__ == "__main__":
    main()

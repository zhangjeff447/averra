"""Report-only market drawdown and financial stress observations.

This module has no broker, budget, scoring, or order imports. Its output is
never an instruction to trade. All source requests are bounded HTTP reads.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import os
import statistics
import time as clock
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo


ET = ZoneInfo("America/New_York")
VERSION = "drawdown_stress_report_v1_20261003_intraday_stage2"
OFR_URL = "https://www.financialresearch.gov/financial-stress-index/data/fsi.csv"
NFCI_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=NFCI&cosd=2020-01-01"
YAHOO_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
SYMBOLS = ("SPY", "QQQ", "SMH")
VOL_SYMBOLS = {"vix": "^VIX", "vxn": "^VXN"}
REQUEST_TIMEOUT = 6
MAX_FETCH_WALL_SECONDS = 10
USER_AGENT = "Averra-report-only/1 (daily risk observation)"

# Initial, deterministic observation thresholds, not calibrated probabilities.
RULES = {
    "price_watch_dd20_pct": -5.0,
    "price_elevated_dd60_pct": -10.0,
    "price_high_dd252_pct": -20.0,
    "price_shock_1d_pct": -7.0,
    "price_shock_5d_pct": -12.0,
    "price_persistence_sessions": 3,
    "vol_watch_1d_pct": 15.0,
    "vol_watch_5d_pct": 25.0,
    "ofr_elevated_level": 0.0,
    "ofr_high_level": 2.0,
    "ofr_high_component": 0.5,
    "nfci_elevated_level": 0.0,
    "nfci_high_level": 0.5,
    "stress_persistence_sessions": 3,
    "ofr_shock_1d": 2.0,
}
LEVELS = {"NORMAL": 0, "WATCH": 1, "ELEVATED": 2, "HIGH": 3}


def _finite(value):
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _business_day_before(day, count=1):
    cursor = day
    for _ in range(count):
        cursor -= timedelta(days=1)
        while cursor.weekday() >= 5:
            cursor -= timedelta(days=1)
    return cursor


def completed_price_date(now):
    local = now.astimezone(ET)
    if local.weekday() < 5 and local.time() >= time(16, 0):
        return local.date()
    return _business_day_before(local.date())


def _cache_read(path):
    try:
        value = json.loads(path.read_text())
        return value if value.get("schema") == 1 else None
    except (OSError, ValueError, AttributeError):
        return None


def _cache_write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, sort_keys=True, separators=(",", ":")))
    os.replace(temporary, path)


def _bounded_get(get, counters, url, **kwargs):
    remaining = counters["deadline"] - clock.monotonic()
    if remaining <= 0:
        raise TimeoutError("report_source_budget_expired")
    counters["http_requests"] += 1
    return get(url, timeout=min(REQUEST_TIMEOUT, remaining), **kwargs)


def _csv_data(name, body):
    rows = list(csv.DictReader(io.StringIO(body)))
    date_key = "Date" if name == "ofr" else "observation_date"
    fields = ("OFR FSI", "Credit", "Funding") if name == "ofr" else ("NFCI",)
    if not rows or not all(field in rows[0] for field in (date_key, *fields)):
        raise ValueError(name + "_csv_schema_invalid")
    parsed = []
    for row in rows:
        try:
            observed = date.fromisoformat(row[date_key])
        except (ValueError, TypeError):
            continue
        values = {field: _finite(row.get(field)) for field in fields}
        if all(value is not None for value in values.values()):
            parsed.append({"date": observed.isoformat(), **values})
    if not parsed or [row["date"] for row in parsed] != sorted(set(row["date"] for row in parsed)):
        raise ValueError(name + "_csv_history_invalid")
    return parsed[-300:]


def _yahoo_data(payload, symbol, cutoff):
    chart = payload.get("chart", {}) if isinstance(payload, dict) else {}
    results = chart.get("result") or []
    if not results or not isinstance(results[0], dict):
        raise ValueError(symbol + "_chart_unavailable")
    result = results[0]
    stamp = result.get("timestamp") or []
    indicators = result.get("indicators") or {}
    quote_rows = indicators.get("quote") or []
    adjusted_rows = indicators.get("adjclose") or []
    if not quote_rows:
        raise ValueError(symbol + "_ohlc_missing")
    raw = quote_rows[0]
    adjusted = (adjusted_rows[0].get("adjclose") or []) if adjusted_rows else []
    output = []
    for index, instant in enumerate(stamp):
        observed = datetime.fromtimestamp(instant, timezone.utc).astimezone(ET).date()
        if observed > cutoff:
            continue  # The current, incomplete 15:30 daily bar is never a close.
        try:
            close = _finite(raw["close"][index])
            high = _finite(raw["high"][index])
            low = _finite(raw["low"][index])
            adj = _finite(adjusted[index]) if index < len(adjusted) else None
        except (KeyError, IndexError):
            continue
        if None in (close, high, low, adj) or min(close, high, low, adj) <= 0:
            continue
        factor = adj / close
        if high < low or factor <= 0:
            raise ValueError(symbol + "_ohlc_invalid")
        output.append({"date": observed.isoformat(), "close": adj, "raw_close": close,
                       "high": high * factor, "low": low * factor})
    if not output or [row["date"] for row in output] != sorted(set(row["date"] for row in output)):
        raise ValueError(symbol + "_history_invalid")
    return output[-420:]


def _freshness(name, observed, now):
    day = date.fromisoformat(observed)
    local = now.astimezone(ET)
    if day > local.date():
        return "FUTURE_DATA"
    if name == "ofr":
        reference = local.date() if local.weekday() < 5 else _business_day_before(local.date())
        return "CURRENT_FOR_SOURCE" if day >= _business_day_before(reference, 2) else "STALE"
    if name == "nfci":
        # Wednesday 08:30 ET release covers the previous Friday; on a holiday
        # the Thursday release remains an acceptable one-day grace period.
        recent_wednesday = local.date() - timedelta(days=(local.weekday() - 2) % 7)
        due = datetime.combine(recent_wednesday, time(8, 30), ET)
        required = recent_wednesday - timedelta(days=5)
        if local < due + timedelta(days=1):
            required -= timedelta(days=7)
        return "CURRENT_FOR_SOURCE" if day >= required else "STALE"
    return "CURRENT_FOR_SOURCE" if day == completed_price_date(now) else "STALE"


def _release_status(name, dataset, now):
    rows = dataset.get("rows") or []
    if not rows:
        return "LATEST_RELEASE_UNVERIFIED"
    observed = date.fromisoformat(rows[-1]["date"])
    local = now.astimezone(ET)
    if name == "ofr":
        reference = local.date() if local.weekday() < 5 else _business_day_before(local.date())
        expected = _business_day_before(reference, 2)
    else:
        wednesday = local.date() - timedelta(days=(local.weekday() - 2) % 7)
        expected = wednesday - timedelta(days=5)
        if local < datetime.combine(wednesday, time(8, 30), ET):
            expected -= timedelta(days=7)
    if observed < expected and dataset.get("fetch_error"):
        return "STALE"
    # The NFCI feed here is a FRED mirror, not proof of the official release.
    if name != "ofr" or dataset.get("source") != OFR_URL:
        return "LATEST_RELEASE_UNVERIFIED"
    if observed >= expected and not dataset.get("fetch_error"):
        return "LATEST_PUBLISHED"
    return "LATEST_RELEASE_UNVERIFIED"


def _fetch_csv(name, url, cache_dir, now, get, counters):
    path = cache_dir / (name + ".json")
    saved = _cache_read(path)
    today = now.astimezone(ET).date().isoformat()
    if saved and saved.get("checked_date") == today and _release_status(name, saved, now) == "LATEST_PUBLISHED":
        return dict(saved, cache_status="SAME_DAY_CACHE")
    if name == "nfci" and saved and _release_status(name, saved, now) == "LATEST_PUBLISHED":
        return dict(saved, cache_status="WEEKLY_CACHE")
    headers = {"Accept": "text/csv"}
    if name != "nfci":
        headers["User-Agent"] = USER_AGENT
    if saved and saved.get("etag"):
        headers["If-None-Match"] = saved["etag"]
    try:
        response = _bounded_get(get, counters, url, headers=headers)
        if response.status_code == 304 and saved:
            saved["checked_date"] = today
            _cache_write(path, saved)
            return dict(saved, cache_status="NOT_MODIFIED")
        response.raise_for_status()
        body = response.text
        rows = _csv_data(name, body)
        saved = {"schema": 1, "rows": rows, "source": url,
                 "frequency": "daily" if name == "ofr" else "weekly",
                 "retrieved_at": now.isoformat(), "checked_date": today,
                 "available_at": response.headers.get("Last-Modified"),
                 "available_at_basis": "HTTP Last-Modified; not official release timestamp",
                 "published_at": None, "etag": response.headers.get("ETag"),
                 "version": hashlib.sha256(body.encode()).hexdigest(),
                 "vintage": "latest_revised_series; historical as-known vintage unavailable"}
        _cache_write(path, saved)
        return dict(saved, cache_status="FETCHED")
    except Exception as exc:
        if saved:
            return dict(saved, cache_status="FALLBACK_CACHE", fetch_error=type(exc).__name__)
        return {"rows": [], "source": url, "frequency": "daily" if name == "ofr" else "weekly",
                "retrieved_at": now.isoformat(), "cache_status": "UNAVAILABLE",
                "fetch_error": type(exc).__name__}


def _fetch_price(symbol, cache_dir, now, get, counters):
    cutoff = completed_price_date(now)
    path = cache_dir / ("chart_" + symbol.replace("^", "") + ".json")
    saved = _cache_read(path)
    if (saved and saved.get("rows") and saved["rows"][-1]["date"] == cutoff.isoformat()
            and _finite(saved["rows"][-1].get("raw_close")) is not None):
        return dict(saved, cache_status="COMPLETED_SESSION_CACHE")
    url = YAHOO_URL.format(symbol=quote(symbol, safe=""))
    try:
        response = _bounded_get(get, counters, url,
                                params={"range": "2y", "interval": "1d", "includePrePost": "false",
                                        "events": "div,splits", "includeAdjustedClose": "true"},
                                headers={"Accept": "application/json", "User-Agent": USER_AGENT})
        response.raise_for_status()
        rows = _yahoo_data(response.json(), symbol, cutoff)
        saved = {"schema": 1, "rows": rows, "source": url,
                 "frequency": "daily_completed_session", "retrieved_at": now.isoformat(),
                 "available_at": None, "published_at": None,
                 "adjustment": "Yahoo adjusted close; OHLC scaled by adjusted/raw close ratio",
                 "version": hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest(),
                 "vintage": "latest_adjusted_history; historical as-known vintage unavailable"}
        _cache_write(path, saved)
        return dict(saved, cache_status="FETCHED")
    except Exception as exc:
        if saved:
            return dict(saved, cache_status="FALLBACK_CACHE", fetch_error=type(exc).__name__)
        return {"rows": [], "source": url, "frequency": "daily_completed_session",
                "retrieved_at": now.isoformat(), "cache_status": "UNAVAILABLE",
                "fetch_error": type(exc).__name__}


def _fetch_intraday_quote(symbol, cache_dir, now, get, counters):
    path = cache_dir / ("intraday_" + symbol + ".json")
    saved = _cache_read(path)
    if saved and saved.get("quote_time"):
        try:
            age = (now - datetime.fromisoformat(saved["quote_time"])).total_seconds()
            if saved.get("report_date") == now.astimezone(ET).date().isoformat() and 0 <= age <= 60:
                return dict(saved, cache_status="FRESH_QUOTE_CACHE")
        except ValueError:
            pass
    url = YAHOO_URL.format(symbol=quote(symbol, safe=""))
    try:
        response = _bounded_get(
            get, counters, url,
            params={"range": "1d", "interval": "1m", "includePrePost": "false", "events": "div,splits"},
            headers={"Accept": "application/json", "User-Agent": USER_AGENT},
        )
        response.raise_for_status()
        chart = response.json().get("chart", {}).get("result") or []
        if not chart or not isinstance(chart[0], dict):
            raise ValueError("intraday_chart_unavailable")
        data = chart[0]
        meta = data.get("meta") or {}
        price = _finite(meta.get("regularMarketPrice"))
        previous = _finite(meta.get("previousClose"))
        stamp = meta.get("regularMarketTime")
        if price is None or price <= 0 or stamp is None:
            raise ValueError("intraday_price_or_source_time_missing")
        quote_time = datetime.fromtimestamp(float(stamp), timezone.utc).isoformat()
        action_dates = []
        for action_type in ("dividends", "splits"):
            for event in (data.get("events") or {}).get(action_type, {}).values():
                if isinstance(event, dict) and _finite(event.get("date")) is not None:
                    event_date = datetime.fromtimestamp(float(event["date"]), timezone.utc).astimezone(ET).date()
                    action_dates.append(event_date.isoformat())
        saved = {"schema": 1, "source": url, "price_type": "Yahoo regularMarketPrice",
                 "price": price, "previous_close": previous, "quote_time": quote_time,
                 "retrieved_at": now.isoformat(), "report_date": now.astimezone(ET).date().isoformat(),
                 "frequency": "intraday_quote", "corporate_action_dates": sorted(set(action_dates)),
                 "version": hashlib.sha256(json.dumps({"price": price, "previous": previous,
                   "quote_time": quote_time, "actions": action_dates}, sort_keys=True).encode()).hexdigest()}
        _cache_write(path, saved)
        return dict(saved, cache_status="FETCHED")
    except Exception as exc:
        if saved:
            return dict(saved, cache_status="FALLBACK_CACHE", fetch_error=type(exc).__name__)
        return {"source": url, "price_type": "Yahoo regularMarketPrice", "price": None,
                "previous_close": None, "quote_time": None, "retrieved_at": now.isoformat(),
                "cache_status": "UNAVAILABLE", "fetch_error": type(exc).__name__}


def collect_sources(now, cache_dir, *, volatility_context=None, runtime_quotes=None, get=None):
    if get is None:
        import requests
        get = requests.get
    cache_dir = Path(cache_dir)
    counters = {"http_requests": 0, "deadline": clock.monotonic() + MAX_FETCH_WALL_SECONDS}
    prices = {symbol: _fetch_price(symbol, cache_dir, now, get, counters) for symbol in SYMBOLS}
    ofr = _fetch_csv("ofr", OFR_URL, cache_dir, now, get, counters)
    nfci = _fetch_csv("nfci", NFCI_URL, cache_dir, now, get, counters)
    # Only historical distributions are fetched; current VIX/VXN come from the
    # already-read trading snapshot, never from this report module's own trade inputs.
    vol_history = {name: _fetch_price(symbol, cache_dir, now, get, counters)
                   for name, symbol in VOL_SYMBOLS.items()}
    # Preserve the existing report's fetch priority; optional intraday data is last.
    intraday = {symbol: _fetch_intraday_quote(symbol, cache_dir, now, get, counters) for symbol in SYMBOLS}
    return {"prices": prices, "intraday_quotes": intraday, "runtime_quotes": runtime_quotes or {},
            "ofr": ofr, "nfci": nfci,
            "vol_history": vol_history, "volatility_context": volatility_context or {},
            "request_count": counters["http_requests"]}


def _pct(value):
    return round(value * 100.0, 4)


def _mean(values):
    return sum(values) / len(values)


def _ema(values, span):
    weight = 2.0 / (span + 1)
    result = values[0]
    for value in values[1:]:
        result += weight * (value - result)
    return result


def _price_metrics(dataset, now):
    rows = dataset.get("rows") or []
    if len(rows) < 253:
        return {"status": "INSUFFICIENT_DATA", "reason": "fewer_than_253_completed_sessions"}
    if _freshness("price", rows[-1]["date"], now) != "CURRENT_FOR_SOURCE":
        return {"status": "INSUFFICIENT_DATA", "reason": "completed_price_session_missing_or_future"}
    closes = [_finite(row.get("close")) for row in rows]
    if any(value is None or value <= 0 for value in closes):
        return {"status": "INSUFFICIENT_DATA", "reason": "adjusted_price_invalid"}
    returns = [closes[index] / closes[index - 1] - 1 for index in range(1, len(closes))]
    if any(abs(value) > 0.35 for value in returns[-252:]):
        return {"status": "INSUFFICIENT_DATA", "reason": "split_or_extreme_move_requires_source_review"}
    highs, lows = ([row[key] for row in rows] for key in ("high", "low"))
    if any(_finite(high) is None or _finite(low) is None or high < low or low <= 0
           for high, low in zip(highs, lows)):
        return {"status": "INSUFFICIENT_DATA", "reason": "adjusted_ohlc_invalid"}
    true_ranges = [max(rows[index]["high"] - rows[index]["low"],
                       abs(rows[index]["high"] - closes[index - 1]),
                       abs(rows[index]["low"] - closes[index - 1]))
                   for index in range(len(rows) - 14, len(rows))]
    log_returns = [math.log(closes[index] / closes[index - 1])
                   for index in range(len(closes) - 20, len(closes))]
    drawdowns = {window: _pct(closes[-1] / max(closes[-window:]) - 1)
                 for window in (20, 60, 252)}
    prior_dd20 = _pct(closes[-6] / max(closes[-25:-5]) - 1)
    return {
        "status": "AVAILABLE", "observation_date": rows[-1]["date"], "close": round(closes[-1], 4),
        "drawdown_pct": {str(key): value for key, value in drawdowns.items()},
        "returns_pct": {str(window): _pct(closes[-1] / closes[-window - 1] - 1)
                        for window in (1, 5, 20)},
        "drawdown20_expansion_5d_pp": round(drawdowns[20] - prior_dd20, 4),
        "ema21": round(_ema(closes, 21), 4),
        "sma50": round(_mean(closes[-50:]), 4),
        "sma200": round(_mean(closes[-200:]), 4),
        "atr14": round(_mean(true_ranges), 4),
        "realized_vol20_annual_pct": round(statistics.pstdev(log_returns) * math.sqrt(252) * 100, 4),
        "below_sma50_last3": all(closes[-offset] < _mean(closes[-50 - offset + 1:-offset + 1] if offset > 1 else closes[-50:])
                                 for offset in (1, 2, 3)),
        "below_sma200_last3": all(closes[-offset] < _mean(closes[-200 - offset + 1:-offset + 1] if offset > 1 else closes[-200:])
                                  for offset in (1, 2, 3)),
        "coverage": len(rows), "source": dataset.get("source"),
        "adjustment": dataset.get("adjustment"),
        "limitation": "rolling highest close, not a lifetime or full bear-market drawdown",
    }


def _price_pressure(metrics):
    if metrics.get("status") != "AVAILABLE":
        return {"level": "INSUFFICIENT_DATA", "raw_values": metrics,
                "thresholds": RULES, "triggered_reasons": [metrics.get("reason", "price_missing")],
                "conditions": None, "coverage": "UNAVAILABLE"}
    dd = metrics["drawdown_pct"]
    ret = metrics["returns_pct"]
    reasons = []
    shock = ret["1"] <= RULES["price_shock_1d_pct"] or ret["5"] <= RULES["price_shock_5d_pct"]
    if shock:
        reasons.append("SHOCK_DETECTED:1D_or_5D_adjusted_return")
    if dd["252"] <= RULES["price_high_dd252_pct"] and metrics["below_sma200_last3"]:
        level = "HIGH"
        reasons.append("252_session_drawdown_and_3_sessions_below_SMA200")
    elif dd["60"] <= RULES["price_elevated_dd60_pct"] and metrics["below_sma50_last3"]:
        level = "ELEVATED"
        reasons.append("60_session_drawdown_and_3_sessions_below_SMA50")
    elif dd["20"] <= RULES["price_watch_dd20_pct"] or (metrics["close"] < metrics["ema21"] and ret["5"] <= -2):
        level = "WATCH"
        reasons.append("20_session_drawdown_or_short_trend_weakness")
    else:
        level = "NORMAL"
    if shock and LEVELS[level] < LEVELS["ELEVATED"]:
        level = "ELEVATED"
    conditions = {
        "price_high_252_and_sma200": dd["252"] <= RULES["price_high_dd252_pct"] and metrics["below_sma200_last3"],
        "price_elevated_60_and_sma50": dd["60"] <= RULES["price_elevated_dd60_pct"] and metrics["below_sma50_last3"],
        "price_watch_20_or_ema5": dd["20"] <= RULES["price_watch_dd20_pct"] or (metrics["close"] < metrics["ema21"] and ret["5"] <= -2),
        "price_shock_1d_or_5d": shock,
    }
    return {"level": level, "shock_detected": shock,
            "raw_values": {"drawdown_pct": dd, "returns_pct": ret,
                           "ema21": metrics["ema21"], "sma50": metrics["sma50"],
                           "sma200": metrics["sma200"], "atr14": metrics["atr14"],
                           "realized_vol20_annual_pct": metrics["realized_vol20_annual_pct"],
                           "drawdown20_expansion_5d_pp": metrics["drawdown20_expansion_5d_pp"]},
            "thresholds": {key: value for key, value in RULES.items() if key.startswith("price_")},
            "triggered_reasons": reasons, "conditions": conditions,
            "coverage": "253+_completed_adjusted_sessions",
            "limitation": metrics["limitation"]}


def _intraday_metrics(symbol, daily, quote_data, runtime_quote, now):
    runtime_status = "NO_RUNTIME_QUOTE"
    if isinstance(runtime_quote, dict):
        runtime_status = ("QUOTE_TIME_PROVENANCE_UNVERIFIED" if runtime_quote.get("timestamp")
                          else "RUNTIME_QUOTE_TIME_MISSING")
    result = {"status": "CURRENT_PRICE_UNAVAILABLE", "symbol": symbol,
              "daily_data_as_of": (daily.get("rows") or [{}])[-1].get("date"),
              "source": quote_data.get("source"), "price_type": quote_data.get("price_type"),
              "quote_time": quote_data.get("quote_time"), "retrieved_at": quote_data.get("retrieved_at"),
              "price": _finite(quote_data.get("price")), "cache_status": quote_data.get("cache_status"),
              "runtime_quote_reuse": runtime_status, "reason": quote_data.get("fetch_error")}
    current = result["price"]
    if current is None or current <= 0 or not result["quote_time"]:
        result["reason"] = result["reason"] or "source_price_or_quote_time_missing"
        return result
    try:
        instant = datetime.fromisoformat(result["quote_time"])
        if instant.tzinfo is None:
            raise ValueError("quote_time_without_timezone")
        age = (now - instant).total_seconds()
    except ValueError:
        result["reason"] = "quote_time_invalid"
        return result
    local = instant.astimezone(ET)
    if (not 0 <= age <= 60 or local.date() != now.astimezone(ET).date()
            or not time(9, 30) <= local.time() <= time(16, 0)):
        result.update(status="PRICE_STALE", reason="quote_not_current_regular_session")
        return result
    rows = daily.get("rows") or []
    if (len(rows) < 252 or _freshness("price", rows[-1]["date"], now) != "CURRENT_FOR_SOURCE"):
        result.update(status="PRICE_BASIS_UNVERIFIED", reason="completed_daily_history_missing")
        return result
    previous_raw = _finite(rows[-1].get("raw_close"))
    previous_adjusted = _finite(rows[-1].get("close"))
    quote_previous = _finite(quote_data.get("previous_close"))
    if (previous_raw is None or previous_adjusted is None or quote_previous is None
            or min(previous_raw, previous_adjusted, quote_previous) <= 0
            or not math.isclose(previous_raw, quote_previous, rel_tol=.001, abs_tol=.02)):
        result.update(status="PRICE_BASIS_UNVERIFIED", reason="previous_close_or_adjustment_basis_mismatch")
        return result
    if local.date().isoformat() in (quote_data.get("corporate_action_dates") or []):
        result.update(status="PRICE_BASIS_UNVERIFIED", reason="same_day_corporate_action_requires_review")
        return result
    if abs(current / previous_raw - 1) > .35:
        result.update(status="PRICE_BASIS_UNVERIFIED", reason="possible_split_or_extreme_price_discontinuity")
        return result
    adjusted_current = current * previous_adjusted / previous_raw
    reference_high = max(float(row["close"]) for row in rows[-252:])
    comparison = adjusted_current / reference_high - 1
    result.update(status="INTRADAY_SNAPSHOT", reason=None,
                  previous_close_raw=round(previous_raw, 4),
                  previous_close_date=rows[-1]["date"],
                  change_vs_previous_close_pct=_pct(current / previous_raw - 1),
                  adjustment_factor=round(previous_adjusted / previous_raw, 8),
                  adjusted_equivalent_price=round(adjusted_current, 4),
                  reference_high_252_adjusted=round(reference_high, 4),
                  reference_relation=("ABOVE_REFERENCE_HIGH" if comparison > 0 else
                                      "AT_REFERENCE_HIGH" if comparison == 0 else "BELOW_REFERENCE_HIGH"),
                  intraday_drawdown_pct=_pct(comparison) if comparison < 0 else None,
                  exceeds_reference_high_pct=_pct(comparison) if comparison > 0 else None,
                  basis_limitation="Current raw quote scaled by prior adjusted/raw close; same-day corporate-action feed may be incomplete")
    return result


def _volatility_group(context, history, prices, now):
    values = {}
    for name in ("vix", "vxn"):
        row = context.get(name) or {}
        level = _finite(row.get("level"))
        metadata = {"observation_time": row.get("timestamp"), "retrieved_at": context.get("fetched_at"),
                    "source": row.get("source"), "frequency": "current_quote_plus_completed_daily_history",
                    "published_at": None, "available_at": None,
                    "revision_vintage": (history.get(name) or {}).get("vintage")}
        if row.get("available") is not True or row.get("stale") is not False or level is None:
            values[name] = {**metadata, "status": "UNAVAILABLE", "freshness": "UNAVAILABLE_OR_STALE",
                            "reason": row.get("error") or "volatility_source_missing"}
            continue
        past = (history.get(name) or {}).get("rows") or []
        closes = [_finite(item.get("close")) for item in past[-252:]]
        percentile = (round(sum(value <= level for value in closes) / len(closes) * 100, 2)
                      if past and _freshness("price", past[-1]["date"], now) == "CURRENT_FOR_SOURCE"
                      and len(closes) >= 100 and all(value is not None for value in closes) else None)
        comparison_symbol = "SPY" if name == "vix" else "QQQ"
        realized = (prices.get(comparison_symbol) or {}).get("realized_vol20_annual_pct")
        values[name] = {**metadata, "status": "AVAILABLE", "freshness": "CURRENT_FOR_SNAPSHOT", "level": level,
                        "change_1d_pct": _finite(row.get("change_pct_1d")),
                        "change_5d_pct": _finite(row.get("change_pct_5d")),
                        "rolling_252_percentile": percentile,
                        "realized_20d_pct": realized,
                        "implied_minus_realized_pp": round(level - realized, 4) if realized is not None else None,
                        "realized_vol_relation": "reference_only; implied index and trailing realized volatility measure different periods"}
    if any(value["status"] != "AVAILABLE" for value in values.values()):
        return {"level": "INSUFFICIENT_DATA", "raw_values": values,
                "thresholds": {key: value for key, value in RULES.items() if key.startswith("vol_")},
                "triggered_reasons": ["VIX_or_VXN_missing_or_stale"], "conditions": None,
                "coverage": "PARTIAL_OR_NONE"}
    triggered = [name + "_1d_or_5d_jump" for name, row in values.items()
                 if (row["change_1d_pct"] is not None and row["change_1d_pct"] >= RULES["vol_watch_1d_pct"])
                 or (row["change_5d_pct"] is not None and row["change_5d_pct"] >= RULES["vol_watch_5d_pct"])]
    return {"level": "WATCH" if triggered else "NORMAL", "raw_values": values,
            "thresholds": {key: value for key, value in RULES.items() if key.startswith("vol_")},
            "triggered_reasons": triggered,
            "conditions": {name + "_jump": name + "_1d_or_5d_jump" in triggered for name in values},
            "coverage": "VIX_AND_VXN",
            "limitation": "volatility alone cannot establish systemic financial stress"}


def _financial_group(ofr, nfci, now):
    ofr_rows, nfci_rows = ofr.get("rows") or [], nfci.get("rows") or []
    source_data = {"ofr": {"observation_date": ofr_rows[-1]["date"] if ofr_rows else None,
                           "source": ofr.get("source"), "retrieved_at": ofr.get("retrieved_at"),
                           "frequency": "daily", "available_at": ofr.get("available_at"),
                           "published_at": None, "available_at_basis": ofr.get("available_at_basis"),
                           "vintage": ofr.get("vintage"),
                           "data_version": ofr.get("version"), "quality": ofr.get("cache_status", "UNVERIFIED"),
                           "freshness": _freshness("ofr", ofr_rows[-1]["date"], now) if ofr_rows else "UNAVAILABLE",
                           "release_status": _release_status("ofr", ofr, now)},
                   "nfci": {"observation_date": nfci_rows[-1]["date"] if nfci_rows else None,
                            "source": nfci.get("source"), "retrieved_at": nfci.get("retrieved_at"),
                            "frequency": "weekly", "available_at": nfci.get("available_at"),
                            "published_at": None, "available_at_basis": nfci.get("available_at_basis"),
                            "vintage": nfci.get("vintage"),
                            "data_version": nfci.get("version"), "quality": nfci.get("cache_status", "UNVERIFIED"),
                            "freshness": _freshness("nfci", nfci_rows[-1]["date"], now) if nfci_rows else "UNAVAILABLE",
                            "release_status": _release_status("nfci", nfci, now)}}
    if (not ofr_rows or not nfci_rows or source_data["ofr"]["freshness"] != "CURRENT_FOR_SOURCE"
            or source_data["nfci"]["freshness"] != "CURRENT_FOR_SOURCE"):
        return {"level": "INSUFFICIENT_DATA", "raw_values": None, "sources": source_data,
                "thresholds": {key: value for key, value in RULES.items() if key.startswith(("ofr_", "nfci_", "stress_"))},
                "triggered_reasons": ["credit_or_funding_source_missing_or_stale"],
                "conditions": None, "coverage": "INSUFFICIENT_DATA"}
    last, weekly = ofr_rows[-1], nfci_rows[-1]
    positive_components = last["Credit"] > 0 and last["Funding"] > 0
    persistent = (len(ofr_rows) >= RULES["stress_persistence_sessions"]
                  and all(row["OFR FSI"] > 0 and row["Credit"] > 0 and row["Funding"] > 0
                          for row in ofr_rows[-RULES["stress_persistence_sessions"]:]))
    shock = len(ofr_rows) > 1 and last["OFR FSI"] - ofr_rows[-2]["OFR FSI"] >= RULES["ofr_shock_1d"]
    if (persistent and last["OFR FSI"] >= RULES["ofr_high_level"]
            and last["Credit"] >= RULES["ofr_high_component"]
            and last["Funding"] >= RULES["ofr_high_component"]
            and weekly["NFCI"] >= RULES["nfci_high_level"]):
        level, reasons = "HIGH", ["persistent_OFR_credit_funding_and_NFCI_joint_stress"]
    elif persistent and weekly["NFCI"] > RULES["nfci_elevated_level"]:
        level, reasons = "ELEVATED", ["three_OFR_sessions_and_weekly_NFCI_tightening"]
    elif (last["OFR FSI"] > RULES["ofr_elevated_level"] and positive_components) or weekly["NFCI"] > 0:
        level, reasons = "WATCH", ["credit_funding_or_NFCI_watch"]
    else:
        level, reasons = "NORMAL", []
    if shock:
        reasons.append("SHOCK_DETECTED:OFR_FSI_1d_jump")
        if LEVELS[level] < LEVELS["ELEVATED"]:
            level = "ELEVATED"
    conditions = {
        "ofr_credit_funding_and_nfci_high": (persistent and last["OFR FSI"] >= RULES["ofr_high_level"]
             and last["Credit"] >= RULES["ofr_high_component"]
             and last["Funding"] >= RULES["ofr_high_component"]
             and weekly["NFCI"] >= RULES["nfci_high_level"]),
        "persistent_ofr_and_nfci_elevated": persistent and weekly["NFCI"] > RULES["nfci_elevated_level"],
        "credit_funding_or_nfci_watch": (last["OFR FSI"] > RULES["ofr_elevated_level"]
                                         and positive_components) or weekly["NFCI"] > 0,
        "ofr_shock_1d": shock,
    }
    return {"level": level, "shock_detected": shock,
            "raw_values": {"ofr_fsi": last["OFR FSI"], "ofr_credit": last["Credit"],
                           "ofr_funding": last["Funding"], "nfci": weekly["NFCI"],
                           "ofr_change_1d": round(last["OFR FSI"] - ofr_rows[-2]["OFR FSI"], 4) if len(ofr_rows) > 1 else None,
                           "nfci_change_1w": round(weekly["NFCI"] - nfci_rows[-2]["NFCI"], 4) if len(nfci_rows) > 1 else None,
                           "nfci_rolling_260_percentile": round(sum(row["NFCI"] <= weekly["NFCI"] for row in nfci_rows[-260:])
                                                                / min(len(nfci_rows), 260) * 100, 2)},
            "sources": source_data,
            "thresholds": {key: value for key, value in RULES.items() if key.startswith(("ofr_", "nfci_", "stress_"))},
            "triggered_reasons": reasons, "conditions": conditions,
            "coverage": "OFR_CREDIT_FUNDING_AND_WEEKLY_NFCI",
            "limitation": "OFR components and headline overlap; NFCI overlaps financial markets and is not a separate vote"}


def evaluate_observation(sources, now):
    prices = {symbol: _price_metrics(sources["prices"].get(symbol, {}), now) for symbol in SYMBOLS}
    pressure = {symbol: _price_pressure(prices[symbol]) for symbol in SYMBOLS}
    intraday = {symbol: _intraday_metrics(symbol,
                sources["prices"].get(symbol, {}),
                (sources.get("intraday_quotes") or {}).get(symbol, {}),
                (sources.get("runtime_quotes") or {}).get(symbol), now)
                for symbol in SYMBOLS}
    volatility = _volatility_group(sources.get("volatility_context") or {},
                                   sources.get("vol_history") or {}, prices, now)
    financial = _financial_group(sources.get("ofr") or {}, sources.get("nfci") or {}, now)
    relative = {"qqq_vs_spy_20d_pp": None, "smh_vs_qqq_20d_pp": None}
    if all(prices[symbol].get("status") == "AVAILABLE" for symbol in SYMBOLS):
        relative = {"qqq_vs_spy_20d_pp": round(prices["QQQ"]["returns_pct"]["20"] - prices["SPY"]["returns_pct"]["20"], 4),
                    "smh_vs_qqq_20d_pp": round(prices["SMH"]["returns_pct"]["20"] - prices["QQQ"]["returns_pct"]["20"], 4)}
    limitations = []
    for symbol, item in prices.items():
        if item.get("status") != "AVAILABLE":
            limitations.append(symbol + ":" + item.get("reason", "UNAVAILABLE"))
    for name, data in (("OFR", sources.get("ofr") or {}), ("NFCI", sources.get("nfci") or {})):
        if data.get("fetch_error"):
            limitations.append(name + ":fetch_failed:" + data["fetch_error"])
    if financial["level"] == "INSUFFICIENT_DATA":
        limitations.append("credit/funding stress insufficient; weekly and daily dates are not today's observations")
    for name, item in (financial.get("sources") or {}).items():
        if item.get("release_status") != "LATEST_PUBLISHED":
            limitations.append(name.upper() + ":" + item.get("release_status", "LATEST_RELEASE_UNVERIFIED"))
    if volatility["level"] == "INSUFFICIENT_DATA":
        limitations.append("VIX/VXN missing or stale")
    broad = pressure["SPY"]["level"]
    if financial["level"] == "INSUFFICIENT_DATA":
        overall = "INSUFFICIENT_DATA"
    else:
        overall = financial["level"]
    if broad in LEVELS and LEVELS[broad] >= 2 and financial["level"] in ("NORMAL", "WATCH"):
        interpretation = "股市回调压力升高，系统性压力尚缺充分佐证。"
    elif overall == "INSUFFICIENT_DATA":
        interpretation = "信用或融资数据不足，无法充分评估整体金融压力。"
    elif overall == "NORMAL":
        interpretation = "已覆盖指标未触发升级条件；不代表没有回调、未来不会下跌或市场安全。"
    else:
        interpretation = "仅描述已覆盖信号，不预测确定性危机。"
    price_sources = {}
    for symbol in SYMBOLS:
        dataset = sources["prices"].get(symbol) or {}
        rows = dataset.get("rows") or []
        observed = rows[-1].get("date") if rows else None
        price_sources[symbol] = {
            "observation_date": observed, "published_at": dataset.get("published_at"),
            "available_at": dataset.get("available_at"), "retrieved_at": dataset.get("retrieved_at"),
            "source": dataset.get("source"), "frequency": dataset.get("frequency"),
            "freshness": _freshness("price", observed, now) if observed else "UNAVAILABLE",
            "quality": prices[symbol]["status"], "cache_quality": dataset.get("cache_status"),
            "revision_vintage": dataset.get("vintage"),
            "data_version": dataset.get("version"), "adjustment": dataset.get("adjustment"),
        }
    return {"rule_version": VERSION, "report_time": now.isoformat(),
            "price_as_of": {symbol: prices[symbol].get("observation_date") for symbol in SYMBOLS},
            "daily_data_as_of": {symbol: prices[symbol].get("observation_date") for symbol in SYMBOLS},
            "intraday_snapshot": intraday,
            "price_sources": price_sources,
            "current_drawdown": prices, "drawdown_pressure": pressure,
            "market_internal": {"level": "UNAVAILABLE", "coverage": "NO_VERIFIED_BREADTH_PROXY",
                                "limitation": "not the proportion of all US stocks rising"},
            "relative_performance": relative, "volatility": volatility,
            "credit_funding_stress": financial, "overall_financial_stress": overall,
            "interpretation": interpretation, "limitations": limitations,
            "request_count": sources.get("request_count", 0), "runtime_llm_calls": 0}


def _source_versions(sources):
    versions = {symbol: (sources.get("prices", {}).get(symbol) or {}).get("version") for symbol in SYMBOLS}
    versions.update({"intraday_" + symbol: (sources.get("intraday_quotes", {}).get(symbol) or {}).get("version")
                     for symbol in SYMBOLS})
    versions.update({"ofr": (sources.get("ofr") or {}).get("version"),
                     "nfci": (sources.get("nfci") or {}).get("version")})
    versions.update({name: (sources.get("vol_history", {}).get(name) or {}).get("version")
                     for name in VOL_SYMBOLS})
    context = sources.get("volatility_context") or {}
    versions["volatility_snapshot"] = hashlib.sha256(json.dumps(
        {name: context.get(name) for name in VOL_SYMBOLS}, sort_keys=True, default=str).encode()).hexdigest()
    return versions


def _observation_severity(observation):
    levels = [observation.get("overall_financial_stress")]
    levels.extend((observation.get("drawdown_pressure") or {}).get(symbol, {}).get("level")
                  for symbol in SYMBOLS)
    return max(LEVELS.get(level, -1) for level in levels)


def _report_data_version(sources):
    dates = {}
    for symbol in SYMBOLS:
        rows = ((sources.get("prices") or {}).get(symbol) or {}).get("rows") or []
        dates[symbol] = rows[-1].get("date") if rows else None
    return hashlib.sha256(json.dumps({"rules": VERSION, "sources": _source_versions(sources),
                                     "observed": dates}, sort_keys=True).encode()).hexdigest()[:24]


def record_report(observation, sources, path):
    """Append one as-known version per day; never write trade or budget state."""
    path = Path(path)
    history = _cache_read(path) or {"schema": 1, "reports": []}
    reports = history["reports"]
    version = _report_data_version(sources)
    day = datetime.fromisoformat(observation["report_time"]).astimezone(ET).date().isoformat()
    existing = next((row for row in reports if row["report_date"] == day and row["data_version"] == version), None)
    if existing:
        return existing, "UNCHANGED", reports
    prior = next((row for row in reversed(reports) if row["report_date"] < day), None)
    current_severity = _observation_severity(observation)
    prior_severity = -1 if prior is None else _observation_severity(prior["observation"])
    record_id = day + ":" + version
    episode_id = (prior["episode_id"] if current_severity >= 2 and prior_severity >= 2
                  and prior and prior.get("episode_id") else record_id if current_severity >= 2 else None)
    record = {"report_id": record_id, "report_date": day, "report_time": observation["report_time"],
              "data_version": version, "source_versions": _source_versions(sources),
              "rule_version": VERSION, "observation": observation,
              "episode_id": episode_id, "new_episode": bool(episode_id == record_id),
              "forward_validation": "PENDING"}
    reports.append(record)
    _cache_write(path, history)
    return record, "RECORDED", reports[:-1]


def _direction(observation, prior_reports):
    prior = next((row for row in reversed(prior_reports)
                  if row["report_date"] < datetime.fromisoformat(observation["report_time"]).astimezone(ET).date().isoformat()), None)
    if not prior:
        return "暂无上一份可比日报"
    old = prior["observation"]
    new_level = _observation_severity(observation)
    old_level = _observation_severity(old)
    if new_level < 0 or old_level < 0:
        return "数据覆盖变化，无法可靠比较"
    if new_level > old_level:
        return "升高"
    if new_level < old_level:
        return "缓解"
    return "持平"


def _metric_text(value, suffix=""):
    return "不可用" if value is None else f"{value:.2f}{suffix}"


def render_section(observation, prior_reports=()):
    price = observation["current_drawdown"]
    dates = observation["price_as_of"]
    as_of = dates.get("SPY") if len({dates.get(symbol) for symbol in SYMBOLS}) == 1 else None
    if not as_of:
        as_of = "；".join(f"{symbol} {dates.get(symbol) or '不可用'}" for symbol in SYMBOLS)
    values = []
    for symbol in SYMBOLS:
        item = price[symbol]
        drawdown = (item.get("drawdown_pct") or {}).get("252") if item.get("status") == "AVAILABLE" else None
        values.append(f"{symbol} {_metric_text(abs(drawdown) if drawdown is not None else None, '%')}")
    lines = ["📉 市场回撤｜REPORT_ONLY｜日线截至：" + as_of + "（非盘中实时）",
             "距252日高点：" + " · ".join(values)]
    financial = observation.get('credit_funding_stress') or {}
    raw = financial.get('raw_values') or {}
    lines.append(f"金融压力：{observation.get('overall_financial_stress', 'DATA_UNAVAILABLE')} | OFR {_metric_text(raw.get('ofr_fsi'))} | NFCI {_metric_text(raw.get('nfci'))}")
    sources = financial.get('sources') or {}
    lines.append('数据日期：' + ' · '.join(f"{s.upper()} {(sources.get(s) or {}).get('observation_date') or '不可用'}" for s in ('ofr', 'nfci')))
    intraday = observation.get('intraday_snapshot') or {}
    market = observation.get('market_internal') or {}
    breadth = '已验证' if market.get('level') not in (None, 'UNAVAILABLE') else '未验证'
    lines.append('市场广度：' + breadth + ' | 盘中：' + ' · '.join(f"{s} {(intraday.get(s) or {}).get('status', '不可用')}" for s in SYMBOLS))
    lines.append('仅供观察，不影响交易。')
    return "\n".join(lines)


def report_once(now, cache_dir, history_path, *, volatility_context=None, runtime_quotes=None, get=None):
    sources = collect_sources(now, cache_dir, volatility_context=volatility_context,
                              runtime_quotes=runtime_quotes, get=get)
    history = _cache_read(Path(history_path)) or {"reports": []}
    day = now.astimezone(ET).date().isoformat()
    version = _report_data_version(sources)
    existing = next((row for row in history["reports"] if row["report_date"] == day
                     and row["data_version"] == version), None)
    if existing:
        return {"observation": existing["observation"],
                "section": render_section(existing["observation"], history["reports"]),
                "record_status": "UNCHANGED", "report_id": existing["report_id"],
                "request_count": sources["request_count"], "runtime_llm_calls": 0}
    observation = evaluate_observation(sources, now)
    record, record_status, prior = record_report(observation, sources, history_path)
    return {"observation": observation, "section": render_section(observation, prior),
            "record_status": record_status, "report_id": record["report_id"],
            "request_count": sources["request_count"], "runtime_llm_calls": 0}


def unavailable_section(reason):
    return ("📉 市场回撤｜REPORT_ONLY\n"
            "日线与回撤：数据不可用。\n"
            f"原因：{reason}。\n仅供观察，不影响交易。")


def validate_forward_episode(record, spy_rows, *, event_drawdown_pct=-10.0):
    """Evaluate one independent warning episode only after future windows mature."""
    if not record.get("new_episode"):
        return {"status": "SKIPPED_CONTINUATION"}
    reference_date = (record.get("observation") or {}).get("price_as_of", {}).get("SPY")
    ordered = sorted((row for row in spy_rows if row.get("date") and _finite(row.get("close")) is not None),
                     key=lambda row: row["date"])
    matching = [index for index, row in enumerate(ordered) if row["date"] == reference_date]
    if len(matching) != 1:
        return {"status": "REFERENCE_PRICE_UNAVAILABLE"}
    index = matching[0]
    reference = float(ordered[index]["close"])
    results = {}
    for horizon in (5, 20, 60):
        if len(ordered) <= index + horizon:
            results[str(horizon)] = {"status": "PENDING"}
            continue
        future = ordered[index + 1:index + horizon + 1]
        losses = [(float(row["close"]) / reference - 1) * 100 for row in future]
        event = next((number for number, loss in enumerate(losses, 1)
                      if loss <= event_drawdown_pct), None)
        results[str(horizon)] = {"status": "MATURED", "return_pct": round(losses[-1], 4),
                                 "max_further_drop_pct": round(min(losses), 4),
                                 "sessions_to_event": event,
                                 "false_alert_for_this_horizon": event is None,
                                 "drawdown_already_at_warning_pct": record["observation"]["current_drawdown"]["SPY"].get("drawdown_pct", {}).get("252")}
    warning_drawdown = (record["observation"]["current_drawdown"]["SPY"]
                        .get("drawdown_pct", {}).get("252"))
    return {"status": "MATURED" if results["60"]["status"] == "MATURED" else "PARTIAL_PENDING",
            "reference_date": reference_date, "event_threshold_pct": event_drawdown_pct,
            "warning_timing": ("UNKNOWN" if warning_drawdown is None else
                               "AFTER_THRESHOLD" if warning_drawdown <= event_drawdown_pct
                               else "BEFORE_OR_NO_EVENT"),
            "windows": results, "limitation": "latest-adjusted history is revised; not an as-known vintage"}


def validate_forward_history(reports, spy_rows, *, event_drawdown_pct=-10.0):
    """Count independent alert episodes; mark missed price events only with report coverage."""
    ordered = sorted((row for row in spy_rows if row.get("date") and _finite(row.get("close")) is not None),
                     key=lambda row: row["date"])
    episodes = {}
    for report in reports:
        episode = report.get("episode_id")
        if episode:
            episodes.setdefault(episode, []).append(report)
    alerts = []
    for rows in episodes.values():
        first = next((row for row in rows if row.get("new_episode")), None)
        if first:
            alerts.append({"report_id": first["report_id"],
                           "duration_report_days": len({row["report_date"] for row in rows}),
                           "forward": validate_forward_episode(first, ordered,
                                                               event_drawdown_pct=event_drawdown_pct)})
    by_date = {row["report_date"] for row in reports}
    alert_dates = {row["report_date"] for row in reports if row.get("new_episode")}
    missed = []
    for index in range(252, len(ordered)):
        current = 100 * (float(ordered[index]["close"]) / max(float(row["close"]) for row in ordered[index - 251:index + 1]) - 1)
        prior = 100 * (float(ordered[index - 1]["close"]) / max(float(row["close"]) for row in ordered[index - 252:index]) - 1)
        if current > event_drawdown_pct or prior <= event_drawdown_pct:
            continue
        preceding = {row["date"] for row in ordered[max(0, index - 20):index]}
        if len(preceding & by_date) < 15:
            missed.append({"event_date": ordered[index]["date"], "status": "UNVERIFIED_REPORT_COVERAGE"})
        elif not preceding & alert_dates:
            missed.append({"event_date": ordered[index]["date"], "status": "MISSED_PRICE_EVENT"})
    return {"alerts": alerts, "missed_price_events": missed,
            "limitation": "events use revised adjusted closes; no crisis probability or historical macro vintage implied"}

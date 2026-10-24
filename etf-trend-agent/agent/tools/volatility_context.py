"""Local VIX/VXN retrieval, classification, and deterministic scoring."""
from __future__ import annotations

import math
from datetime import datetime, time, timezone
from typing import Any, Callable, Dict, Optional
from urllib.parse import quote
from zoneinfo import ZoneInfo


YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
SOURCE_NAME = "yahoo_finance_chart"
SOURCE_SYMBOLS = {"vix": "^VIX", "vxn": "^VXN"}
REGIME_THRESHOLDS = {
    "vix": ((15.0, "LOW"), (20.0, "NORMAL"), (25.0, "ELEVATED"), (35.0, "HIGH")),
    "vxn": ((18.0, "LOW"), (24.0, "NORMAL"), (30.0, "ELEVATED"), (40.0, "HIGH")),
}
VOLATILITY_WEIGHTS = {"vix": 3.0, "vxn": 6.0, "relative": 3.0}
MAX_POSITIVE_ADJUSTMENT = 12.0
MAX_NEGATIVE_ADJUSTMENT = -12.0
ET = ZoneInfo("America/New_York")


def _number(value: Any) -> Optional[float]:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _round(value: Optional[float]) -> Optional[float]:
    return None if value is None else round(value, 4)


def _regime(name: str, level: Optional[float]) -> str:
    if level is None:
        return "UNAVAILABLE"
    for ceiling, label in REGIME_THRESHOLDS[name]:
        if level < ceiling:
            return label
    return "EXTREME"


def _trend(change_1d_pct: Optional[float], change_5d_pct: Optional[float]) -> str:
    one = change_1d_pct or 0.0
    five = change_5d_pct or 0.0
    if one >= 3.0 or five >= 5.0:
        return "RISING"
    if one <= -3.0 or five <= -5.0:
        return "FALLING"
    return "FLAT"


def _is_stale(timestamp: Optional[datetime], now: datetime) -> bool:
    if timestamp is None:
        return True
    observed = timestamp if timestamp.tzinfo else timestamp.replace(tzinfo=timezone.utc)
    age_seconds = max(0.0, (now.astimezone(timezone.utc) - observed.astimezone(timezone.utc)).total_seconds())
    local = now.astimezone(ET)
    regular_session = local.weekday() < 5 and time(9, 30) <= local.time().replace(tzinfo=None) < time(16, 0)
    return age_seconds > (45 * 60 if regular_session else 96 * 60 * 60)


def _unavailable(name: str, error: str) -> Dict[str, Any]:
    return {
        "available": False,
        "symbol": SOURCE_SYMBOLS[name],
        "source": SOURCE_NAME,
        "level": None,
        "previous_close": None,
        "change_1d": None,
        "change_pct_1d": None,
        "change_5d": None,
        "change_pct_5d": None,
        "trend": "UNAVAILABLE",
        "regime": "UNAVAILABLE",
        "timestamp": None,
        "stale": True,
        "error": error,
    }


def _parse_index(name: str, payload: Dict[str, Any], now: datetime) -> Dict[str, Any]:
    chart = payload.get("chart") if isinstance(payload, dict) else None
    results = chart.get("result") if isinstance(chart, dict) else None
    if not isinstance(results, list) or not results or not isinstance(results[0], dict):
        return _unavailable(name, "invalid_chart_payload")

    result = results[0]
    meta = result.get("meta") if isinstance(result.get("meta"), dict) else {}
    quote_block = result.get("indicators", {}).get("quote", []) if isinstance(result.get("indicators"), dict) else []
    quote_data = quote_block[0] if isinstance(quote_block, list) and quote_block and isinstance(quote_block[0], dict) else {}
    closes = [_number(value) for value in quote_data.get("close", [])] if isinstance(quote_data.get("close"), list) else []
    closes = [value for value in closes if value is not None]

    level = _number(meta.get("regularMarketPrice"))
    if level is None and closes:
        level = closes[-1]
    previous_close = next((value for value in (
        _number(meta.get("regularMarketPreviousClose")),
        _number(meta.get("previousClose")),
        _number(meta.get("chartPreviousClose")),
    ) if value is not None), None)
    if level is None or previous_close is None or previous_close <= 0:
        return _unavailable(name, "missing_current_or_previous_close")

    raw_timestamp = meta.get("regularMarketTime")
    if raw_timestamp is None:
        timestamps = result.get("timestamp") if isinstance(result.get("timestamp"), list) else []
        raw_timestamp = timestamps[-1] if timestamps else None
    try:
        observed_at = datetime.fromtimestamp(float(raw_timestamp), timezone.utc) if raw_timestamp is not None else None
    except (TypeError, ValueError, OSError):
        observed_at = None

    change_1d = level - previous_close
    change_1d_pct = change_1d / previous_close * 100.0
    five_day_reference = closes[-6] if len(closes) >= 6 and closes[-6] > 0 else None
    change_5d = level - five_day_reference if five_day_reference is not None else None
    change_5d_pct = change_5d / five_day_reference * 100.0 if five_day_reference is not None else None
    stale = _is_stale(observed_at, now)
    return {
        "available": True,
        "symbol": SOURCE_SYMBOLS[name],
        "source": SOURCE_NAME,
        "level": _round(level),
        "previous_close": _round(previous_close),
        "change_1d": _round(change_1d),
        "change_pct_1d": _round(change_1d_pct),
        "change_5d": _round(change_5d),
        "change_pct_5d": _round(change_5d_pct),
        "trend": _trend(change_1d_pct, change_5d_pct),
        "regime": _regime(name, level),
        "timestamp": observed_at.isoformat() if observed_at else None,
        "stale": stale,
        "error": "stale_market_data" if stale else None,
    }


def _fresh(item: Dict[str, Any]) -> bool:
    return item.get("available") is True and item.get("stale") is False and _number(item.get("level")) is not None


def _relative_context(vix: Dict[str, Any], vxn: Dict[str, Any]) -> Dict[str, Any]:
    if not (_fresh(vix) and _fresh(vxn)):
        return {
            "available": False, "spread": None, "ratio": None,
            "vxn_rising_faster": None, "nasdaq_volatility_state": "UNAVAILABLE",
        }
    vix_level = float(vix["level"])
    vxn_level = float(vxn["level"])
    spread = vxn_level - vix_level
    ratio = vxn_level / vix_level if vix_level > 0 else None
    one_day_gap = float(vxn.get("change_pct_1d") or 0.0) - float(vix.get("change_pct_1d") or 0.0)
    five_day_gap = float(vxn.get("change_pct_5d") or 0.0) - float(vix.get("change_pct_5d") or 0.0)
    rising_faster = one_day_gap >= 3.0 or five_day_gap >= 5.0
    regimes = {vix.get("regime"), vxn.get("regime")}
    if vix.get("regime") == "EXTREME" and vxn.get("regime") == "EXTREME":
        state = "EXTREME_FEAR"
    elif (ratio is not None and ratio >= 1.25) or spread >= 8.0 or (rising_faster and vxn.get("regime") in {"ELEVATED", "HIGH", "EXTREME"}):
        state = "NASDAQ_STRESS"
    elif regimes.issubset({"HIGH", "EXTREME"}):
        state = "SYSTEMIC_HIGH_VOL"
    elif vix.get("regime") in {"HIGH", "EXTREME"} and vxn.get("regime") in {"LOW", "NORMAL", "ELEVATED"} and ratio is not None and ratio <= 1.10:
        state = "BROAD_STRESS"
    elif vix.get("trend") == "FALLING" and vxn.get("trend") == "FALLING":
        state = "RECOVERY"
    else:
        state = "NORMAL"
    return {
        "available": True,
        "spread": _round(spread),
        "ratio": _round(ratio),
        "vxn_rising_faster": rising_faster,
        "nasdaq_volatility_state": state,
    }


def _combined_regime(vix: Dict[str, Any], vxn: Dict[str, Any], relative: Dict[str, Any]) -> str:
    fresh = [item for item in (vix, vxn) if _fresh(item)]
    if not fresh:
        return "UNAVAILABLE"
    state = relative.get("nasdaq_volatility_state")
    if state in {"EXTREME_FEAR", "SYSTEMIC_HIGH_VOL", "NASDAQ_STRESS", "BROAD_STRESS", "RECOVERY"}:
        return str(state)
    severity = {"LOW": 0, "NORMAL": 1, "ELEVATED": 2, "HIGH": 3, "EXTREME": 4}
    highest = max(fresh, key=lambda item: severity.get(str(item.get("regime")), -1))
    return str(highest.get("regime") or "UNAVAILABLE")


def unavailable_context(error: str = "volatility_data_unavailable") -> Dict[str, Any]:
    vix = _unavailable("vix", error)
    vxn = _unavailable("vxn", error)
    relative = _relative_context(vix, vxn)
    return {
        "source": SOURCE_NAME,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "vix": vix,
        "vxn": vxn,
        "relative": relative,
        "combined_volatility_regime": "UNAVAILABLE",
        "component_confidence": "UNAVAILABLE",
    }


class VolatilityProvider:
    """Fetch each index once over normal HTTP; no retries and no AI/MCP path."""

    def __init__(self, http_get: Optional[Callable[..., Any]] = None, timeout: float = 8.0):
        self.http_get = http_get
        self.timeout = timeout

    def fetch(self, now: Optional[datetime] = None) -> Dict[str, Any]:
        now = now or datetime.now(timezone.utc)
        get = self.http_get
        if get is None:
            import requests
            get = requests.get
        values: Dict[str, Dict[str, Any]] = {}
        for name, symbol in SOURCE_SYMBOLS.items():
            try:
                response = get(
                    YAHOO_CHART_URL.format(symbol=quote(symbol, safe="")),
                    params={"range": "10d", "interval": "1d", "includePrePost": "false", "events": "div,splits"},
                    headers={"Accept": "application/json", "User-Agent": "qqq-smh-volatility/1"},
                    timeout=self.timeout,
                )
                response.raise_for_status()
                values[name] = _parse_index(name, response.json(), now)
            except Exception as exc:
                values[name] = _unavailable(name, "fetch_failed:" + type(exc).__name__)
        relative = _relative_context(values["vix"], values["vxn"])
        fresh_count = sum(1 for item in values.values() if _fresh(item))
        return {
            "source": SOURCE_NAME,
            "fetched_at": now.isoformat(),
            "vix": values["vix"],
            "vxn": values["vxn"],
            "relative": relative,
            "combined_volatility_regime": _combined_regime(values["vix"], values["vxn"], relative),
            "component_confidence": {2: "FULL", 1: "REDUCED", 0: "UNAVAILABLE"}[fresh_count],
        }


def _metric(indicators: Dict[str, Any], *names: str) -> Optional[float]:
    for name in names:
        value = _number(indicators.get(name))
        if value is not None:
            return value
    return None


def _market_setup(symbol: str, indicators: Dict[str, Any]) -> Dict[str, Any]:
    drawdown = _metric(indicators, "drawdown60_pct", "drawdown_60d", "drawdown_pct") or 0.0
    rsi = _metric(indicators, "rsi14", "rsi_14")
    price = _metric(indicators, "price", "last")
    ema20 = _metric(indicators, "ema20", "ema_20")
    ema50 = _metric(indicators, "ema50", "ema_50")
    histogram = _metric(indicators, "histogram", "macd_histogram")
    vwap = _metric(indicators, "vwap", "vwap_20")
    relative_strength = _metric(indicators, "relative_strength", "relative_strength_score")
    momentum = _metric(indicators, "momentum", "momentum_score")
    structure = _metric(indicators, "structure_score")
    regime = str(indicators.get("market_regime") or indicators.get("regime") or "").upper()

    deep_threshold = 9.0 if symbol == "QQQ" else 13.0
    shallow_threshold = 4.0 if symbol == "QQQ" else 6.0
    below_trend = bool(price is not None and ((ema20 is not None and price < ema20) or (ema50 is not None and price < ema50)))
    accumulation = 0.45 * min(1.0, drawdown / deep_threshold)
    if rsi is not None:
        accumulation += 0.35 * min(1.0, max(0.0, (45.0 - rsi) / 10.0))
    if below_trend or drawdown >= deep_threshold * 1.25 or (vwap is not None and price is not None and price < vwap):
        accumulation += 0.20
    accumulation = min(1.0, accumulation)

    improving_votes = []
    if ema20 is not None and ema50 is not None:
        improving_votes.append(ema20 > ema50)
    if histogram is not None:
        improving_votes.append(histogram > 0)
    if price is not None and ema20 is not None:
        improving_votes.append(price > ema20)
    if price is not None and vwap is not None:
        improving_votes.append(price > vwap)
    if relative_strength is not None:
        improving_votes.append(relative_strength > 0)
    if momentum is not None:
        improving_votes.append(momentum > 0)
    if structure is not None:
        improving_votes.append(structure >= 60)
    if regime:
        improving_votes.append(regime in {"BULL", "BULLISH", "RECOVERY", "RISK_ON"})
    improving = bool(improving_votes) and sum(improving_votes) / len(improving_votes) >= 0.6
    return {
        "drawdown_pct": drawdown,
        "rsi": rsi,
        "deep_drawdown": drawdown >= deep_threshold,
        "shallow_drawdown": drawdown < shallow_threshold,
        "oversold": rsi is not None and rsi <= 35.0,
        "accumulation_strength": accumulation,
        "improving": improving,
    }


def _index_adjustment(item: Dict[str, Any], cap: float, setup: Dict[str, Any]) -> float:
    if not _fresh(item):
        return 0.0
    severity = {"LOW": 0.0, "NORMAL": 0.1, "ELEVATED": 0.45, "HIGH": 0.75, "EXTREME": 1.0}.get(str(item.get("regime")), 0.0)
    if item.get("trend") == "RISING":
        severity = min(1.0, severity + 0.15)
    if item.get("trend") == "FALLING" and setup["improving"]:
        return cap * max(0.25, severity * 0.5)
    if severity < 0.25:
        return 0.0
    if setup["deep_drawdown"] and setup["oversold"]:
        return cap * severity * (0.5 + 0.5 * setup["accumulation_strength"])
    caution = 1.0 if setup["shallow_drawdown"] else max(0.5, 1.0 - setup["accumulation_strength"] * 0.5)
    return -cap * severity * caution


def volatility_adjustment(symbol: str, indicators: Dict[str, Any], context: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    context = context if isinstance(context, dict) else unavailable_context()
    setup = _market_setup(symbol, indicators)
    vix = context.get("vix") if isinstance(context.get("vix"), dict) else {}
    vxn = context.get("vxn") if isinstance(context.get("vxn"), dict) else {}
    relative = context.get("relative") if isinstance(context.get("relative"), dict) else {}
    vix_part = _index_adjustment(vix, VOLATILITY_WEIGHTS["vix"], setup)
    vxn_part = _index_adjustment(vxn, VOLATILITY_WEIGHTS["vxn"], setup)
    relative_part = 0.0
    state = str(relative.get("nasdaq_volatility_state") or "UNAVAILABLE")
    if relative.get("available") is True:
        if state in {"NASDAQ_STRESS", "SYSTEMIC_HIGH_VOL", "EXTREME_FEAR"} or relative.get("vxn_rising_faster") is True:
            relative_part = VOLATILITY_WEIGHTS["relative"] if setup["deep_drawdown"] and setup["oversold"] else -VOLATILITY_WEIGHTS["relative"] * (1.0 if setup["shallow_drawdown"] else 0.75)
        elif state == "BROAD_STRESS":
            relative_part = 1.5 if setup["deep_drawdown"] and setup["oversold"] else -1.5
        elif state == "RECOVERY" and setup["improving"]:
            relative_part = 2.0
    adjustment = max(MAX_NEGATIVE_ADJUSTMENT, min(MAX_POSITIVE_ADJUSTMENT, vix_part + vxn_part + relative_part))
    if adjustment >= 2.0:
        direction = "ACCUMULATION_OPPORTUNITY" if setup["deep_drawdown"] and setup["oversold"] else "POSITIVE_CONFIRMATION"
    elif adjustment <= -2.0:
        direction = "CAUTION"
    else:
        direction = "NEUTRAL"
    magnitude = abs(adjustment)
    strength = "STRONG" if magnitude >= 8.0 else "MODERATE" if magnitude >= 4.0 else "LIGHT" if magnitude > 0 else "NONE"
    if direction == "CAUTION":
        explanation = "Volatility is elevated relative to a limited or unconfirmed pullback."
    elif direction == "ACCUMULATION_OPPORTUNITY":
        explanation = "Elevated volatility coincides with deep drawdown and oversold accumulation conditions."
    elif direction == "POSITIVE_CONFIRMATION":
        explanation = "Falling volatility confirms improving market conditions."
    else:
        explanation = "Volatility adds no material adjustment to the existing strategy."
    return {
        "direction": direction,
        "strength": strength,
        "adjustment": round(adjustment, 2),
        "vix_adjustment": round(vix_part, 2),
        "vxn_adjustment": round(vxn_part, 2),
        "relative_adjustment": round(relative_part, 2),
        "component_confidence": context.get("component_confidence", "UNAVAILABLE"),
        "recommended_buy_intensity_pct": round(max(25.0, min(75.0, 50.0 + adjustment * 25.0 / 12.0)), 1),
        "staged_buy_attractiveness": "INCREASED" if adjustment >= 2.0 else "REDUCED" if adjustment <= -2.0 else "NEUTRAL",
        "market_setup": setup,
        "explanation": explanation,
    }

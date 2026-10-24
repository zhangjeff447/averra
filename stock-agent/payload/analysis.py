"""Deterministic research pipeline over normalized market snapshots."""
from __future__ import annotations

import statistics
from datetime import date


def sma(xs, n):
    return sum(xs[-n:]) / n if len(xs) >= n else None


def ema_series(xs, n):
    if not xs:
        return []
    k = 2 / (n + 1)
    out = [float(xs[0])]
    for x in xs[1:]:
        out.append(float(x) * k + out[-1] * (1 - k))
    return out


def rsi(xs, n=14):
    if len(xs) <= n:
        return None
    changes = [xs[i] - xs[i - 1] for i in range(len(xs) - n, len(xs))]
    gain = sum(max(0, x) for x in changes) / n
    loss = sum(max(0, -x) for x in changes) / n
    return 100 if loss == 0 else 100 - 100 / (1 + gain / loss)


def indicators(bars):
    if len(bars) < 200:
        raise ValueError("At least 200 daily bars required")
    closes = [float(b["close"]) for b in bars]
    highs = [float(b["high"]) for b in bars]
    lows = [float(b["low"]) for b in bars]
    volumes = [float(b["volume"]) for b in bars]
    e12, e26 = ema_series(closes, 12), ema_series(closes, 26)
    macd_series = [a - b for a, b in zip(e12, e26)]
    signal = ema_series(macd_series, 9)[-1]
    rsv = []
    for i in range(len(bars)):
        lo, hi = min(lows[max(0, i - 8):i + 1]), max(highs[max(0, i - 8):i + 1])
        rsv.append(50 if hi == lo else (closes[i] - lo) / (hi - lo) * 100)
    k = d = 50.0
    for v in rsv:
        k, d = (2 * k + v) / 3, (2 * d + k) / 3
    tr = []
    plus_dm, minus_dm = [], []
    for i in range(1, len(bars)):
        up, down = highs[i] - highs[i - 1], lows[i - 1] - lows[i]
        tr.append(max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1])))
        plus_dm.append(up if up > down and up > 0 else 0)
        minus_dm.append(down if down > up and down > 0 else 0)
    atr = sma(tr, 14)
    dx = []
    for i in range(14, len(tr) + 1):
        t = sum(tr[i - 14:i])
        p = 100 * sum(plus_dm[i - 14:i]) / t if t else 0
        m = 100 * sum(minus_dm[i - 14:i]) / t if t else 0
        dx.append(100 * abs(p - m) / (p + m) if p + m else 0)
    typical = [(highs[i] + lows[i] + closes[i]) / 3 for i in range(len(bars) - 19, len(bars))]
    recent_volume = volumes[-20:]
    middle = sma(closes, 20)
    deviation = statistics.pstdev(closes[-20:])
    return {
        "close": closes[-1], "sma20": middle, "sma50": sma(closes, 50), "sma200": sma(closes, 200),
        "ema9": ema_series(closes, 9)[-1], "ema21": ema_series(closes, 21)[-1],
        "rsi": rsi(closes), "macd": macd_series[-1], "macd_signal": signal,
        "kdj_k": k, "kdj_d": d, "kdj_j": 3 * k - 2 * d,
        "vwap20_proxy": sum(p * v for p, v in zip(typical, recent_volume)) / sum(recent_volume),
        "atr14": atr, "bollinger_upper": middle + 2 * deviation,
        "bollinger_lower": middle - 2 * deviation, "adx14": sma(dx, 14),
        "rvol": volumes[-1] / (sum(volumes[-21:-1]) / 20) if sum(volumes[-21:-1]) else 0,
        "return20": closes[-1] / closes[-21] - 1, "return60": closes[-1] / closes[-61] - 1,
        "day_change": closes[-1] / closes[-2] - 1,
    }


def security_ok(stock):
    if stock.get("asset_type") != "COMMON_STOCK" or stock.get("country") != "US":
        return False
    if stock.get("exchange") not in {"NYSE", "NASDAQ", "AMEX"}:
        return False
    if stock.get("is_etf") or stock.get("is_otc") or stock.get("is_preferred") or stock.get("is_warrant"):
        return False
    return True


def universe_ok(stock):
    if not security_ok(stock):
        return False
    bars = stock.get("bars", [])
    return len(bars) >= 200 and bars[-1]["close"] >= 3 and sum(b["close"] * b["volume"] for b in bars[-20:]) / 20 >= 2_000_000


def market_regime(snapshot):
    market = snapshot["market"]
    vix, vxn = float(market["vix"]), float(market["vxn"])
    if vix >= 35 or vxn >= 40:
        return "STRESS", 0.30
    if vix >= 25 or vxn >= 30:
        return "CAUTION", 0.60
    return "NORMAL", 1.0


def event_score(stock, asof):
    score = 50
    flags = []
    for event in stock.get("events", []):
        if event.get("date") != asof[:10]:
            continue
        kind = event.get("type", "UNKNOWN")
        if kind in {"EARNINGS_BEAT", "GUIDANCE_RAISE", "ANALYST_UPGRADE", "CATALYST_POSITIVE"}:
            score += 15
        elif kind in {"EARNINGS_MISS", "GUIDANCE_CUT", "ANALYST_DOWNGRADE", "CATALYST_NEGATIVE"}:
            score -= 25
        flags.append(kind)
    earnings = stock.get("earnings_date")
    if earnings:
        days = (date.fromisoformat(earnings) - date.fromisoformat(asof[:10])).days
        if 0 <= days <= 2:
            score -= 20
            flags.append("EARNINGS_WITHIN_2_DAYS")
    revision = stock.get("fundamentals", {}).get("estimate_revision_90d")
    if revision is not None:
        score += max(-15, min(15, float(revision) * 100))
    return max(0, min(100, round(score))), flags


def fundamental_score(stock):
    f = stock.get("fundamentals", {})
    required = ("revenue_growth_yoy", "earnings_growth_yoy", "operating_margin", "debt_to_equity")
    if any(f.get(key) is None for key in required):
        return None
    growth = max(-1, min(1, float(f["revenue_growth_yoy"])))
    earnings = max(-1, min(1, float(f["earnings_growth_yoy"])))
    margin = max(-1, min(1, float(f["operating_margin"])))
    debt = max(0, float(f["debt_to_equity"]))
    return round(max(0, min(100, 45 + growth * 50 + earnings * 20 + margin * 30 - debt * 8)))


def relative_strength(stock, snapshot):
    bars = stock["bars"]
    def ret(series, days):
        return series[-1]["close"] / series[-days - 1]["close"] - 1
    spy, qqq = snapshot["benchmarks"]["SPY"], snapshot["benchmarks"]["QQQ"]
    sector = snapshot["benchmarks"].get(stock.get("sector_benchmark"))
    if not sector or min(map(len, (spy, qqq, sector))) < 61:
        return None
    return {name: round((ret(bars, 20) - ret(other, 20)) * 100, 2)
            for name, other in (("SPY", spy), ("QQQ", qqq), ("SECTOR", sector))}


def score_stock(stock, snapshot):
    if not universe_ok(stock):
        return {"ticker": stock["ticker"], "action": "REJECT", "reason": "universe/liquidity filter"}
    i = indicators(stock["bars"])
    fundamental = fundamental_score(stock)
    rs = relative_strength(stock, snapshot)
    if fundamental is None or rs is None:
        return {"ticker": stock["ticker"], "action": "REJECT", "reason": "missing fundamentals or benchmark coverage"}
    regime, factor = market_regime(snapshot)
    momentum = round(max(0, min(100, 50 + i["return20"] * 180 + i["return60"] * 80 + sum(rs.values()) * 1.2)))
    technical = round(max(0, min(100, 40 + 8 * (i["close"] > i["ema9"] > i["ema21"])
        + 8 * (i["close"] > i["sma20"] > i["sma50"] > i["sma200"])
        + 8 * (i["macd"] > i["macd_signal"]) + 8 * (i["kdj_k"] > i["kdj_d"])
        + 8 * (i["adx14"] >= 20) + 8 * (i["rvol"] >= 1.2)
        - 15 * (i["rsi"] > 75) - 10 * (i["close"] > i["bollinger_upper"]))))
    event, flags = event_score(stock, snapshot["asof"])
    risk = round(max(0, min(100, 25 + i["atr14"] / i["close"] * 400
        + max(0, i["rsi"] - 70) + (20 if regime == "STRESS" else 10 if regime == "CAUTION" else 0)
        + (25 if "EARNINGS_WITHIN_2_DAYS" in flags else 0))))
    stock_score = round(0.35 * fundamental + 0.35 * momentum + 0.20 * technical + 0.10 * event)
    entry = round(max(0, min(100, 0.5 * technical + 0.25 * momentum + 0.25 * event - max(0, risk - 50) * 0.7)))
    if any(x in flags for x in ("GUIDANCE_CUT", "EARNINGS_MISS")):
        action = "REJECT"
    elif regime != "STRESS" and stock_score >= 72 and entry >= 65 and risk < 65 and "EARNINGS_WITHIN_2_DAYS" not in flags:
        action = "BUY NOW CANDIDATE" if snapshot.get("session") == "OPEN" else "BUY TOMORROW"
    elif stock_score >= 65:
        action = "WATCH"
    else:
        action = "REJECT"
    return {"ticker": stock["ticker"], "sector": stock["sector"], "action": action,
            "stock_score": stock_score, "entry_score": entry, "event_score": event,
            "risk_score": risk, "fundamental_score": fundamental, "momentum_score": momentum,
            "technical_score": technical, "relative_strength": rs, "indicators": i,
            "regime": regime, "regime_factor": factor, "event_flags": flags,
            "reason": f"growth {fundamental}, momentum {momentum}, technical {technical}, event {event}, risk {risk}"}


def correlation(a, b):
    if min(len(a), len(b)) < 61:
        return 0
    x = [a[-i]["close"] / a[-i - 1]["close"] - 1 for i in range(1, 61)]
    y = [b[-i]["close"] / b[-i - 1]["close"] - 1 for i in range(1, 61)]
    try:
        return statistics.correlation(x, y)
    except statistics.StatisticsError:
        return 0


def allocation(result, stock, portfolio, snapshot, config, planned=None):
    if result["action"] not in {"BUY NOW CANDIDATE", "BUY TOMORROW"}:
        return 0
    equity, missing = portfolio.equity_cents()
    if missing or equity <= 0:
        return 0
    planned = planned or {}
    cash = portfolio.cash_cents() - sum(planned.values())
    desired = int(equity * min(0.20, max(0.05, (result["entry_score"] - 50) / 200)) * result["regime_factor"])
    held = {p["ticker"]: p for p in portfolio.positions()}
    def value(p):
        quote = portfolio.get_quote(p["ticker"])
        return round(float(p["shares"]) * float(quote["price"]) * 100) if quote else p["cost_cents"]
    own = value(held[stock["ticker"]]) if stock["ticker"] in held else 0
    sector_used = sum(value(p) for p in held.values() if p["sector"] == stock["sector"])
    by_ticker = {s["ticker"]: s for s in snapshot["stocks"]}
    sector_used += sum(cents for ticker, cents in planned.items() if by_ticker[ticker]["sector"] == stock["sector"])
    corr_used = sum(value(p) for p in held.values() if p["ticker"] != stock["ticker"]
                    and p["ticker"] in by_ticker
                    and correlation(stock["bars"], by_ticker[p["ticker"]]["bars"]) >= config["correlation_threshold"])
    corr_used += sum(cents for ticker, cents in planned.items() if ticker != stock["ticker"]
                     and correlation(stock["bars"], by_ticker[ticker]["bars"]) >= config["correlation_threshold"])
    return max(0, min(desired, cash - int(equity * config["minimum_cash_fraction"]),
        int(equity * config["maximum_single_stock_fraction"]) - own,
        int(equity * config["maximum_sector_fraction"]) - sector_used,
        int(equity * config["maximum_correlated_fraction"]) - corr_used))


def held_action(result):
    if result is None or result["action"] == "REJECT" or result.get("risk_score", 100) >= 80:
        return "EXIT"
    if result["risk_score"] >= 65 or result["stock_score"] < 65:
        return "REDUCE"
    return "HOLD"


def event_triggers(snapshot):
    for stock in snapshot["stocks"]:
        if not universe_ok(stock):
            continue
        bars = stock["bars"]
        day_change = bars[-1]["close"] / bars[-2]["close"] - 1
        average_volume = sum(b["volume"] for b in bars[-21:-1]) / 20
        rvol = bars[-1]["volume"] / average_volume if average_volume else 0
        types = [e.get("type", "UNKNOWN") for e in stock.get("events", []) if e.get("date") == snapshot["asof"][:10]]
        if abs(day_change) >= 0.06 and rvol >= 2:
            types.append("PRICE_VOLUME_SPIKE")
        if types:
            yield stock["ticker"], ", ".join(types), f"{snapshot['asof'][:10]}:{stock['ticker']}:{','.join(sorted(types))}"

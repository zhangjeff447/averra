"""Deterministic portfolio-wide recommendations; never executes a trade."""
from __future__ import annotations

import json
import math
from decimal import Decimal
from pathlib import Path

from portfolio import now, price_is_fresh

ROOT = Path(__file__).resolve().parent


def load_config(path=ROOT / "phase6_config.json"):
    cfg = json.loads(Path(path).read_text())
    if not 0 < cfg["target_min_fraction"] <= cfg["target_max_fraction"] <= cfg["single_stock_max_fraction"] < 1:
        raise ValueError("Invalid single-stock limits")
    if not 0 < cfg["minimum_cash_reserve_fraction"] < 1:
        raise ValueError("Invalid cash reserve")
    return cfg


def correlation(a, b, minimum=20):
    if not a or not b or len(a) != len(b) or len(a) < minimum:
        return None
    if any(not math.isfinite(float(x)) for x in a + b):
        return None
    ma, mb = sum(a) / len(a), sum(b) / len(b)
    va = sum((x - ma) ** 2 for x in a)
    vb = sum((x - mb) ** 2 for x in b)
    return sum((x - ma) * (y - mb) for x, y in zip(a, b)) / math.sqrt(va * vb) if va and vb else None


def correlated(a, b, cfg):
    if a.get("ticker") == b.get("ticker"):
        return True
    corr = correlation(a.get("returns"), b.get("returns"), cfg["minimum_correlation_observations"])
    return (corr is not None and corr >= cfg["correlation_threshold"]) or bool(
        a.get("industry") and a.get("industry") == b.get("industry"))


def quality(candidate):
    try:
        conviction = float(candidate.get("conviction", 0))
        entry = float(candidate.get("entry", 0))
    except (TypeError, ValueError):
        return 0
    # Geometric combination penalizes a weak leg more than an arithmetic average.
    return math.sqrt(max(0, conviction) * max(0, entry)) if all(math.isfinite(x) and x <= 100 for x in (conviction, entry)) else 0


def finite_score(value):
    try:
        score = float(value)
        return score if math.isfinite(score) else 0
    except (TypeError, ValueError):
        return 0


def risk_multiplier(candidate, cfg):
    try:
        score = float(candidate.get("risk_score", cfg["default_risk_score"]))
        if not math.isfinite(score):
            return 0
        multiplier = max(cfg["risk_multiplier_floor"], min(1, score / cfg["risk_score_reference"]))
        for key, limit in (("atr_pct", cfg["max_atr_pct"]),
                           ("realized_vol_pct", cfg["max_realized_vol_pct"]),
                           ("drawdown_pct", cfg["max_drawdown_pct"])):
            value = float(candidate.get(key, 0))
            if not math.isfinite(value) or value < 0:
                return 0
            if value > limit:
                multiplier *= max(cfg["risk_penalty_floor"], limit / value)
    except (TypeError, ValueError):
        return 0
    if candidate.get("chase_risk") == "HIGH":
        multiplier *= cfg["chase_high_multiplier"]
    if candidate.get("regime") == "CAUTION":
        multiplier *= cfg["caution_regime_multiplier"]
    if candidate.get("regime") == "STRESS":
        return 0
    return multiplier


def monitor_position(candidate, position, cfg):
    if candidate.get("material_negative_event") or candidate.get("thesis_broken"):
        return "EXIT_CANDIDATE"
    if candidate.get("concentration_breach") or candidate.get("research_deterioration") or candidate.get("technical_breakdown"):
        return "REDUCE_CANDIDATE"
    if finite_score(candidate.get("entry")) < cfg["minimum_entry"] or candidate.get("chase_risk") == "HIGH":
        return "NO_ADD"
    return "ADD_CANDIDATE" if position else "HOLD"


def allocate(book, candidates, *, asof=None, cfg=None, persist=True, spy_close=None, qqq_close=None):
    cfg = cfg or load_config()
    state = book.summary()
    equity, cash = state["portfolio_value_cents"], state["cash_cents"]
    positions = {p["ticker"]: p for p in state["positions"]}
    candidates = sorted(candidates, key=lambda c: (-quality(c) * risk_multiplier(c, cfg), c["ticker"]))
    planned = {}
    decisions = []
    reserve = math.ceil(equity * cfg["minimum_cash_reserve_fraction"])
    pending_high = any(c.get("state") == "WAIT" and quality(c) >= cfg["high_opportunity_quality"] for c in candidates)
    opportunity_reserve = math.ceil(equity * cfg["opportunity_reserve_fraction"]) if pending_high else 0
    available = max(0, cash - reserve - opportunity_reserve)
    context = {c["ticker"]: c for c in candidates}
    for c in candidates:
        ticker = c["ticker"]
        q = quality(c)
        reasons = []
        held = positions.get(ticker)
        state_name = "WATCH"
        cents = 0
        price = c.get("price")
        observed = c.get("price_at")
        fresh = False
        if price is not None and observed:
            try:
                fresh = price_is_fresh(observed, cfg["maximum_quote_age_hours"]) and math.isfinite(float(price)) and float(price) > 0
            except (TypeError, ValueError, OverflowError):
                fresh = False
        if equity <= 0 or cash <= 0:
            reasons.append("NO_CAPITAL_CONFIGURED")
        elif c.get("state") not in ("BUY_TOMORROW_CANDIDATE", "BUY_NOW_CANDIDATE"):
            reasons.append("ENTRY_NOT_READY")
        elif state["stale_tickers"]:
            state_name = "WAIT_FOR_FRESH_PRICE"
            reasons.append("PORTFOLIO_PRICE_STALE")
        elif finite_score(c.get("conviction")) < cfg["minimum_conviction"] or finite_score(c.get("entry")) < cfg["minimum_entry"]:
            reasons.append("QUALITY_BELOW_THRESHOLD")
        elif not fresh:
            state_name = "WAIT_FOR_FRESH_PRICE"
            reasons.append("PRICE_STALE")
        elif risk_multiplier(c, cfg) == 0:
            reasons.append("MARKET_STRESS_OR_INVALID_RISK")
        else:
            risk = risk_multiplier(c, cfg)
            fraction = cfg["target_min_fraction"] + (cfg["target_max_fraction"] - cfg["target_min_fraction"]) * max(0, min(1, (q - cfg["quality_scale_floor"]) / cfg["quality_scale_range"]))
            target = math.floor(equity * fraction * risk)
            if c.get("research_quality_tier") == "LIMITED_RESEARCH":
                cap = c.get("position_size_cap_fraction")
                if not isinstance(cap, (int, float)) or not 0 < cap < 1:
                    raise ValueError("LIMITED_RESEARCH_POSITION_CAP_REQUIRED")
                target = math.floor(target * cap)
                reasons.append("LIMITED_RESEARCH_POSITION_CAP")
            existing = held["market_value_cents"] if held and held["market_value_cents"] is not None else held["cost_cents"] if held else 0
            same_sector = sum((p["market_value_cents"] if p["market_value_cents"] is not None else p["cost_cents"])
                              for p in positions.values() if p["sector"] == c.get("sector"))
            same_sector += sum(v for t, v in planned.items() if context[t].get("sector") == c.get("sector"))
            peers = [p["ticker"] for p in positions.values() if p["ticker"] != ticker and correlated(c, context.get(p["ticker"], {"ticker": p["ticker"], "industry": p.get("industry")}), cfg)]
            corr_used = sum((positions[t]["market_value_cents"] if positions[t]["market_value_cents"] is not None else positions[t]["cost_cents"]) for t in peers)
            corr_used += sum(v for t, v in planned.items() if t != ticker and correlated(c, context[t], cfg))
            limits = {
                "TARGET": target - existing,
                "SINGLE_STOCK_CAP": math.floor(equity * cfg["single_stock_max_fraction"]) - existing,
                "SECTOR_CAP": math.floor(equity * cfg["sector_max_fraction"]) - same_sector,
                "CORRELATION_CAP": math.floor(equity * cfg["correlated_group_max_fraction"]) - corr_used - existing,
                "CASH_RESERVE": available,
            }
            cents = max(0, min(limits.values()))
            reasons += [k for k, v in limits.items() if v <= cents]
            if cents >= math.ceil(equity * cfg["target_min_fraction"] * cfg["minimum_actionable_fraction_of_target_min"]):
                state_name = "ADD_CANDIDATE" if held else "BUY_TOMORROW_CANDIDATE"
                available -= cents
            else:
                cents = 0
                reasons.append("OPPORTUNITY_COST_OR_CAP")
        if held and cents == 0:
            held_value = held["market_value_cents"] if held["market_value_cents"] is not None else held["cost_cents"]
            checks = {**c, "concentration_breach": bool(equity and held_value / equity > cfg["single_stock_max_fraction"])}
            state_name = monitor_position(checks, held, cfg)
            if state_name == "ADD_CANDIDATE":
                state_name = "HOLD"
        planned[ticker] = cents
        sector_total = sum((p["market_value_cents"] if p["market_value_cents"] is not None else p["cost_cents"])
                           for p in positions.values() if p["sector"] == c.get("sector"))
        sector_total += sum(v for t, v in planned.items() if context[t].get("sector") == c.get("sector"))
        decision = {"ticker": ticker, "state": state_name, "suggested_cents": cents,
                    "quality_score": round(q, 2), "estimated_shares": str(Decimal(cents) / (Decimal(str(price)) * 100)) if cents and fresh else None,
                    "reasons": reasons, "conviction": c.get("conviction"), "entry": c.get("entry"),
                    "sector": c.get("sector"), "chase_risk": c.get("chase_risk"),
                    "risk_score": c.get("risk_score"),
                    "suggested_weight_pct": round(cents * 100 / equity, 2) if equity else 0,
                    "sector_exposure_pct": round(sector_total * 100 / equity, 2) if equity else 0,
                    "portfolio_value_cents": equity, "before_cash_cents": cash,
                    "after_cash_cents": cash - sum(planned.values())}
        decisions.append(decision)
    if persist:
        stamp = asof or now()
        with book.atomic():
            for c, d in zip(candidates, decisions):
                book.db.execute("""INSERT INTO allocation_decisions(created_at,asof,ticker,state,suggested_cents,
                    quality_score,reasons_json,inputs_json,simulation) VALUES(?,?,?,?,?,?,?,?,?)""",
                    (now(), stamp, d["ticker"], d["state"], d["suggested_cents"], d["quality_score"],
                     json.dumps(d["reasons"]), json.dumps(c), int(c.get("simulation", True))))
                book.add_suggestion(d["ticker"], d["state"], d["suggested_cents"], json.dumps(d["reasons"]))
            book.db.execute("INSERT INTO suggested_portfolio_snapshots(created_at,asof,allocations_json,spy_close,qqq_close) VALUES(?,?,?,?,?)",
                            (now(), stamp, json.dumps(decisions), spy_close, qqq_close))
    return decisions


def format_candidate(decision, portfolio_value, cash):
    from portfolio import usd
    estimate = decision["estimated_shares"] or ("PRICE_STALE" if "PRICE_STALE" in decision["reasons"] else "not allocated")
    return (f"AVERRA STOCKS | {decision['ticker']}\nSignal: {decision['state']}\n"
            f"Conviction: {decision['conviction']} | Entry: {decision['entry']}\n"
            f"Risk Score: {decision.get('risk_score')}\n"
            f"Portfolio Value: {usd(portfolio_value)} | Available Cash: {usd(cash)}\n"
            f"Suggested Allocation: {usd(decision['suggested_cents'])} | Proposed Weight: {decision.get('suggested_weight_pct')}%\n"
            f"Estimated Shares: {estimate}\n"
            f"After Purchase: ~{usd(decision['after_cash_cents'])}\n"
            f"Sector: {decision['sector']} ({decision.get('sector_exposure_pct')}% proposed) | Chase Risk: {decision['chase_risk']}\n"
            f"Why: {', '.join(decision['reasons'])}\nNo trade was placed.")

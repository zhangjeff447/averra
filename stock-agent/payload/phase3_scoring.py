"""Deterministic fundamental, trend and candidate scoring. No network or AI."""
from __future__ import annotations

import math
import statistics
from collections import defaultdict


COMPONENTS = ("growth", "profitability", "financial_health", "cash_flow_quality", "valuation")


def clamp(value):
    return max(0, min(100, value))


def normalize(value, bounds, lower=False):
    if value is None or not math.isfinite(value):
        return None
    low, high = bounds
    if low >= high:
        raise ValueError("invalid normalization range")
    score = clamp((value - low) / (high - low) * 100)
    return 100 - score if lower else score


def peer_medians(items, cfg):
    fields = ("gross_margin", "operating_margin", "net_margin", "roa", "roe", "roic")
    sectors = defaultdict(lambda: defaultdict(list))
    for item in items:
        if item.get("data_quality_status") == "DATA_QUALITY_REJECT":
            continue
        sector = item["sector"] if item["sector_confidence"] in {"HIGH", "MEDIUM"} else None
        for field in fields:
            value = item["raw"]["values"].get(field)
            if value is not None:
                sectors["MARKET"][field].append(value)
                if sector:
                    sectors[sector][field].append(value)
    minimum = cfg["sector_relative"]["minimum_peers"]
    return {sector: {field: statistics.median(values) for field, values in fields_map.items() if len(values) >= minimum}
            for sector, fields_map in sectors.items()}


def trend(raw, cfg):
    quarters = raw["quarterly"]
    minimum = cfg["trend"]["minimum_quarters"]
    checks = {}
    def period_metric(name, source, *, direction=1, denominator=None):
        rows = source.get(name, [])
        if len(rows) < minimum:
            return
        last = rows[-4:]
        old = statistics.mean(x["value"] for x in last[:2])
        new = statistics.mean(x["value"] for x in last[2:])
        base = abs(old) if denominator is None else denominator
        if base <= 0:
            return
        checks[name] = direction * (new - old) / base
    period_metric("revenue", quarters)
    period_metric("eps_diluted", quarters)
    period_metric("operating_income", quarters)
    period_metric("operating_cash_flow", quarters)
    period_metric("diluted_shares", quarters, direction=-1)
    period_metric("debt_long_term", raw["instant"], direction=-1)
    rev = {x["end"]: x["value"] for x in quarters.get("revenue", [])}
    for metric in ("gross_profit", "operating_income"):
        rows = [x for x in quarters.get(metric, []) if x["end"] in rev and rev[x["end"]] > 0]
        if len(rows) >= minimum:
            margins = [x["value"] / rev[x["end"]] for x in rows[-4:]]
            checks[f"{metric}_margin"] = statistics.mean(margins[2:]) - statistics.mean(margins[:2])
    ocf = {x["end"]: x["value"] for x in quarters.get("operating_cash_flow", [])}
    capex = {x["end"]: x["value"] for x in quarters.get("capital_expenditure", [])}
    common = sorted(set(ocf) & set(capex))
    if len(common) >= minimum:
        values = [ocf[d] - capex[d] for d in common[-4:]]
        old = statistics.mean(values[:2])
        if old != 0:
            checks["free_cash_flow"] = (statistics.mean(values[2:]) - old) / abs(old)
    if len(checks) < cfg["trend"]["minimum_available_metrics"]:
        return None, "INSUFFICIENT", checks
    clipped = [max(-.5, min(.5, value)) for value in checks.values()]
    aggregate = statistics.median(clipped)
    strong, weak = cfg["trend"]["strong_threshold"], cfg["trend"]["weak_threshold"]
    label = ("STRONGLY_IMPROVING" if aggregate >= strong else "IMPROVING" if aggregate >= weak else
             "STRONGLY_DETERIORATING" if aggregate <= -strong else "DETERIORATING" if aggregate <= -weak else "STABLE")
    return round(clamp(50 + aggregate / strong * 25), 4), label, checks


def quality_flags(values, cfg):
    cut = cfg["flags"]
    flags = []
    def mark(condition, name):
        if condition:
            flags.append(name)
    fcf = values.get("free_cash_flow_ttm")
    mark(fcf is not None and fcf < 0, "NEGATIVE_FCF")
    mark(values.get("fcf_yoy") is not None and values["fcf_yoy"] < cut["fcf_deterioration"], "FCF_DETERIORATING")
    mark(values.get("margin_trend") is not None and values["margin_trend"] < cut["margin_compression"], "MARGIN_COMPRESSION")
    mark(values.get("debt_to_equity") is not None and values["debt_to_equity"] > cut["high_debt_to_equity"], "HIGH_DEBT")
    mark(values.get("current_ratio") is not None and values["current_ratio"] < cut["low_current_ratio"], "LIQUIDITY_RISK")
    mark(values.get("dilution_yoy") is not None and values["dilution_yoy"] > cut["dilution_yoy"], "DILUTION")
    mark(values.get("receivables_vs_revenue") is not None and values["receivables_vs_revenue"] > cut["receivables_minus_revenue_growth"], "RECEIVABLES_GROWING_FASTER_THAN_REVENUE")
    mark(values.get("inventory_growth") is not None and values["inventory_growth"] > cut["inventory_growth"], "INVENTORY_BUILD")
    mark(values.get("net_income_ttm") is not None and values["net_income_ttm"] > 0 and
         values.get("operating_cash_flow_ttm") is not None and values["operating_cash_flow_ttm"] < 0,
         "EARNINGS_CASHFLOW_DIVERGENCE")
    mark(values.get("revenue_acceleration") is not None and values["revenue_acceleration"] < cut["growth_deceleration"],
         "GROWTH_DECELERATION")
    return flags


def score(item, peers, cfg):
    raw = item["raw"]
    values = raw["values"]
    peer_cfg = cfg["sector_relative"]
    sector = item["sector"] if item["sector_confidence"] in {"HIGH", "MEDIUM"} else None
    required = peer_cfg["financial_required_fields"] if sector == peer_cfg["financial_sector"] else cfg["required_fields"]
    missing = [field for field in required if values.get(field) is None]
    coverage = (len(required) - len(missing)) / len(required)
    thresholds = cfg["confidence"]
    confidence = ("HIGH" if coverage >= thresholds["high_coverage"] else
                  "MEDIUM" if coverage >= thresholds["medium_coverage"] else
                  "LOW" if coverage >= thresholds["low_coverage"] else "INSUFFICIENT")
    if item.get("data_quality_status") == "DATA_QUALITY_REJECT":
        confidence = "INSUFFICIENT"
    trend_score, trend_label, trend_checks = trend(raw, cfg)
    weights = cfg["component_features"]
    component_scores, contribution = {}, {}
    for component in COMPONENTS:
        rules = weights[component]
        if component == "financial_health" and sector == peer_cfg["financial_sector"]:
            rules = peer_cfg["financial_features"]
        entries = []
        for name, weight in rules.items():
            value = values.get(name)
            if value is None or weight <= 0:
                continue
            base = normalize(value, cfg["normalization"][name], name in cfg["lower_is_better"])
            peer_name = sector if sector and name in peers.get(sector, {}) else "MARKET"
            reference = peers.get(peer_name, {}).get(name)
            blend = peer_cfg["blend"] if peer_name == sector else peer_cfg["broad_market_blend"]
            if component == "profitability" and reference is not None:
                radius = peer_cfg["peer_range_around_median"]
                relative = normalize(value - reference, [-radius, radius])
                base = base * (1 - blend) + relative * blend
            entries.append({"feature": name, "value": value, "weight": weight, "normalized_score": round(base, 4),
                            "peer_reference": reference if component == "profitability" else None,
                            "peer_scope": peer_name if component == "profitability" and reference is not None else None,
                            "provenance": raw["provenance"].get(name, [])})
        component_scores[component] = round(sum(x["normalized_score"] * x["weight"] for x in entries) /
                                      sum(x["weight"] for x in entries), 4) if entries else None
        contribution[component] = entries
    available = {name: component_scores[name] for name in COMPONENTS if component_scores[name] is not None}
    if confidence == "INSUFFICIENT" or not available:
        fundamental_score = None
    else:
        denom = sum(cfg["component_weights"][name] for name in available)
        base = sum(value * cfg["component_weights"][name] for name, value in available.items()) / denom
        fundamental_score = round(clamp(base + cfg["trend"]["modifier_points"][trend_label]), 4)
    candidate_cfg = cfg["candidate_weights"]
    fundamental_weight = (candidate_cfg["fundamental"] if confidence == "HIGH" else
                          candidate_cfg["medium_fundamental_weight"] if confidence == "MEDIUM" else
                          candidate_cfg["low_fundamental_weight"] if confidence == "LOW" else
                          candidate_cfg["insufficient_fundamental_weight"])
    if fundamental_score is None:
        fundamental_weight = 0
    quant_weight = candidate_cfg["quant"] if confidence == "HIGH" and fundamental_weight else 1 - fundamental_weight
    candidate_score = round(item["quant_score"] * quant_weight + (fundamental_score or 0) * fundamental_weight, 4)
    flags = quality_flags(values, cfg)
    return {"fundamental_score": fundamental_score, "candidate_score": candidate_score,
            "confidence": confidence, "coverage_pct": round(coverage * 100, 2), "missing_fields": missing,
            "component_scores": component_scores, "trend_score": trend_score, "trend_label": trend_label,
            "flags": flags, "effective_quant_weight": round(quant_weight, 4),
            "effective_fundamental_weight": round(fundamental_weight, 4),
            "explanation": {"component_contributions": contribution, "trend_metrics": trend_checks,
                            "trend_modifier": cfg["trend"]["modifier_points"][trend_label],
                            "sector_confidence": item["sector_confidence"],
                            "financial_sector_special_handling": sector == peer_cfg["financial_sector"]}}

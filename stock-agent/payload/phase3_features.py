"""Derive audited quarterly/TTM fundamentals from point-in-time SEC facts."""
from __future__ import annotations

import math
import statistics
from datetime import date


def _finite(value):
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _ratio(numerator, denominator, *, positive_denominator=True):
    a, b = _finite(numerator), _finite(denominator)
    if a is None or b is None or b == 0 or (positive_denominator and b <= 0):
        return None
    value = a / b
    return value if math.isfinite(value) else None


def _change(current, prior):
    return _ratio(current - prior, abs(prior)) if current is not None and prior is not None and prior > 0 else None


def _quarterly(items, quality):
    direct = {r["end"]: r for r in items if r["period"] == "QUARTERLY"}
    cumulative = sorted((r for r in items if r["period"] in {"YTD", "ANNUAL"} and r["start"]),
                        key=lambda r: (r["start"], r["end"]))
    for current in cumulative:
        if current["end"] in direct:
            continue
        previous = [r for r in cumulative if r["start"] == current["start"] and r["end"] < current["end"]]
        previous += [r for r in direct.values() if r["start"] == current["start"] and r["end"] < current["end"]]
        if not previous:
            continue
        prior = max(previous, key=lambda r: r["end"])
        gap = (date.fromisoformat(current["end"]) - date.fromisoformat(prior["end"])).days
        if not quality["minimum_quarter_gap_days"] <= gap <= quality["maximum_quarter_gap_days"]:
            continue
        derived = {**current, "value": current["value"] - prior["value"], "period": "QUARTERLY",
                   "start": prior["end"], "data_quality": "DERIVED_YTD_DIFFERENCE",
                   "derived_from": [prior["accession"], current["accession"]]}
        direct[current["end"]] = derived
    return sorted(direct.values(), key=lambda r: r["end"])


def _recent_four(series, cfg):
    if len(series) < 4:
        return []
    last = series[-4:]
    ends = [date.fromisoformat(item["end"]) for item in last]
    if any(not cfg["minimum_quarter_gap_days"] <= (b - a).days <= cfg["maximum_quarter_gap_days"]
           for a, b in zip(ends, ends[1:])):
        return []
    return last


def build_features(selected, asof, price, split_actions, cfg):
    quality = cfg["quality"]
    series = {name: _quarterly(items, quality) for name, items in selected.items()}
    annual = {name: sorted((r for r in items if r["period"] == "ANNUAL"), key=lambda r: r["end"])
              for name, items in selected.items()}
    instant = {name: sorted((r for r in items if r["period"] == "INSTANT"), key=lambda r: r["end"])
               for name, items in selected.items()}
    features, provenance = {}, {}
    def put(name, value, *sources):
        value = _finite(value)
        bounded = name in cfg["normalization"] or name in {"pe", "price_to_sales", "price_to_book", "ev_to_ebitda", "ev_to_sales"}
        if value is not None and (not bounded or abs(value) <= quality["maximum_abs_ratio"]):
            features[name] = value
            provenance[name] = [s for s in sources if s]
        else:
            features[name] = None

    def q(name, offset=-1):
        values = series.get(name, [])
        return values[offset] if len(values) >= abs(offset) else None

    def i(name, offset=-1):
        values = instant.get(name, [])
        if len(values) < abs(offset):
            return None
        item = values[offset]
        return item if (date.fromisoformat(asof) - date.fromisoformat(item["end"])).days <= quality["max_instant_age_days"] else None

    def ttm(name):
        window = _recent_four(series.get(name, []), quality)
        if not window:
            return None, []
        return sum(x["value"] for x in window), window

    def split_factor_after(fact, through):
        factor = 1.0
        for action in split_actions:
            if fact["available_date"] < action["date"] <= through and action["type"] in {"forward_split", "reverse_split"}:
                factor *= action["split_to"] / action["split_from"]
        return factor

    def yoy_pair(name):
        current, prior = q(name), q(name, -5)
        if not current or not prior:
            return None, None
        gap = (date.fromisoformat(current["end"]) - date.fromisoformat(prior["end"])).days
        return (current, prior) if quality["minimum_annual_days"] <= gap <= quality["maximum_annual_days"] else (None, None)

    for name in ("revenue", "operating_income", "net_income", "operating_cash_flow", "capital_expenditure",
                 "gross_profit", "cost_of_revenue", "interest_expense", "depreciation", "stock_compensation"):
        value, sources = ttm(name)
        put(f"{name}_ttm", value, *sources)
    if features["gross_profit_ttm"] is None and features["revenue_ttm"] is not None and features["cost_of_revenue_ttm"] is not None:
        put("gross_profit_ttm", features["revenue_ttm"] - features["cost_of_revenue_ttm"],
            *provenance.get("revenue_ttm", []), *provenance.get("cost_of_revenue_ttm", []))
    fcf = features["operating_cash_flow_ttm"] - features["capital_expenditure_ttm"] if features["operating_cash_flow_ttm"] is not None and features["capital_expenditure_ttm"] is not None else None
    put("free_cash_flow_ttm", fcf, *provenance.get("operating_cash_flow_ttm", []), *provenance.get("capital_expenditure_ttm", []))
    for name in ("revenue", "eps_diluted", "operating_income", "operating_cash_flow", "capital_expenditure"):
        current, prior = yoy_pair(name)
        prior_value = prior["value"] / split_factor_after(prior, current["end"]) if prior and name == "eps_diluted" else prior["value"] if prior else None
        put(f"{name}_yoy", _change(current["value"], prior_value) if current and prior else None, current, prior)
    revenue_now, revenue_prev = q("revenue"), q("revenue", -2)
    put("revenue_qoq", _change(revenue_now["value"], revenue_prev["value"]) if revenue_now and revenue_prev else None,
        revenue_now, revenue_prev)
    annual_revenue = annual.get("revenue", [])
    if len(annual_revenue) >= 4 and annual_revenue[-1]["value"] > 0 and annual_revenue[-4]["value"] > 0:
        years = (date.fromisoformat(annual_revenue[-1]["end"]) - date.fromisoformat(annual_revenue[-4]["end"])).days / 365.25
        put("revenue_3y_cagr", (annual_revenue[-1]["value"] / annual_revenue[-4]["value"]) ** (1 / years) - 1,
            annual_revenue[-1], annual_revenue[-4])
    else:
        put("revenue_3y_cagr", None)
    if len(series.get("revenue", [])) >= 8 and _recent_four(series["revenue"][-8:-4], quality) and _recent_four(series["revenue"][-4:], quality):
        rs = series["revenue"]
        yoy = [_change(rs[-n]["value"], rs[-n-4]["value"]) for n in (1, 2, 3, 4)]
        put("revenue_acceleration", statistics.mean(yoy[:2]) - statistics.mean(yoy[2:]) if all(x is not None for x in yoy) else None,
            *rs[-8:])
    else:
        put("revenue_acceleration", None)
    for name in ("cash", "debt_current", "debt_long_term", "assets", "equity", "current_assets",
                 "current_liabilities", "inventory", "receivables", "shares_outstanding"):
        row = i(name)
        put(name, row["value"] if row else None, row)
    debt_parts = [features["debt_current"], features["debt_long_term"]]
    put("total_debt", sum(debt_parts) if all(x is not None for x in debt_parts) else None,
        i("debt_current"), i("debt_long_term"))
    features["debt_data_quality"] = "COMPLETE" if all(x is not None for x in debt_parts) else "PARTIAL_OR_MISSING"
    put("net_debt", features["total_debt"] - features["cash"] if features["total_debt"] is not None and features["cash"] is not None else None)
    diluted = q("diluted_shares")
    put("diluted_shares_latest", diluted["value"] if diluted else None, diluted)
    diluted, old_shares = yoy_pair("diluted_shares")
    if diluted:
        put("diluted_shares_latest", diluted["value"], diluted)
    old_share_value = old_shares["value"] * split_factor_after(old_shares, diluted["end"]) if diluted and old_shares else None
    put("dilution_yoy", _change(diluted["value"], old_share_value) if diluted and old_shares else None, diluted, old_shares)
    share_fact = i("shares_outstanding")
    if share_fact and features["shares_outstanding"] is not None:
        multiplier = 1.0
        for action in split_actions:
            if share_fact["end"] < action["date"] <= asof and action["type"] in {"forward_split", "reverse_split"}:
                multiplier *= action["split_to"] / action["split_from"]
        put("split_adjusted_shares_for_valuation", features["shares_outstanding"] * multiplier, share_fact)
        features["share_split_multiplier"] = multiplier
    else:
        put("split_adjusted_shares_for_valuation", None)
        features["share_split_multiplier"] = None
    put("gross_margin", _ratio(features["gross_profit_ttm"], features["revenue_ttm"]))
    put("operating_margin", _ratio(features["operating_income_ttm"], features["revenue_ttm"]))
    put("net_margin", _ratio(features["net_income_ttm"], features["revenue_ttm"]))
    put("roa", _ratio(features["net_income_ttm"], features["assets"]))
    put("roe", _ratio(features["net_income_ttm"], features["equity"]))
    invested = features["equity"] + features["total_debt"] - features["cash"] if all(features[k] is not None for k in ("equity", "total_debt", "cash")) else None
    put("roic", _ratio(features["operating_income_ttm"] * .75, invested) if features["operating_income_ttm"] is not None else None)
    put("fcf_margin", _ratio(fcf, features["revenue_ttm"]))
    put("ocf_to_net_income", _ratio(features["operating_cash_flow_ttm"], features["net_income_ttm"]))
    put("fcf_to_net_income", _ratio(fcf, features["net_income_ttm"]))
    put("current_ratio", _ratio(features["current_assets"], features["current_liabilities"]))
    put("working_capital", features["current_assets"] - features["current_liabilities"] if features["current_assets"] is not None and features["current_liabilities"] is not None else None)
    quick_assets = features["current_assets"] - (features["inventory"] or 0) if features["current_assets"] is not None and features["inventory"] is not None else None
    put("quick_ratio", _ratio(quick_assets, features["current_liabilities"]))
    put("debt_to_equity", _ratio(features["total_debt"], features["equity"]))
    put("net_debt_to_assets", _ratio(features["net_debt"], features["assets"]))
    put("equity_to_assets", _ratio(features["equity"], features["assets"]))
    put("cash_to_assets", _ratio(features["cash"], features["assets"]))
    ebitda = features["operating_income_ttm"] + features["depreciation_ttm"] if features["operating_income_ttm"] is not None and features["depreciation_ttm"] is not None else None
    put("ebitda_ttm", ebitda)
    put("debt_to_ebitda", _ratio(features["total_debt"], ebitda))
    put("interest_coverage", _ratio(features["operating_income_ttm"], features["interest_expense_ttm"]))
    for metric, out_name in (("debt_long_term", "debt_trend"), ("cash", "liquidity_trend"),
                             ("inventory", "inventory_growth"), ("receivables", "receivables_growth")):
        rows = instant.get(metric, [])
        current, prior = (rows[-1], rows[-5]) if len(rows) >= 5 else (None, None)
        put(out_name, _change(current["value"], prior["value"]) if current and prior else None, current, prior)
    put("receivables_vs_revenue", features["receivables_growth"] - features["revenue_yoy"] if features["receivables_growth"] is not None and features["revenue_yoy"] is not None else None)
    current_assets, prior_assets = i("current_assets"), i("current_assets", -5)
    current_liabilities, prior_liabilities = i("current_liabilities"), i("current_liabilities", -5)
    old_working_capital = prior_assets["value"] - prior_liabilities["value"] if prior_assets and prior_liabilities else None
    put("working_capital_change", _change(features["working_capital"], old_working_capital),
        current_assets, prior_assets, current_liabilities, prior_liabilities)
    put("accrual_ratio", _ratio(features["net_income_ttm"] - features["operating_cash_flow_ttm"], features["assets"]) if features["net_income_ttm"] is not None and features["operating_cash_flow_ttm"] is not None else None)
    put("ocf_yoy", features["operating_cash_flow_yoy"])
    current_fcf, old_fcf = None, None
    ocf, capex = series.get("operating_cash_flow", []), series.get("capital_expenditure", [])
    if len(ocf) >= 5 and len(capex) >= 5 and ocf[-1]["end"] == capex[-1]["end"] and ocf[-5]["end"] == capex[-5]["end"]:
        current_fcf, old_fcf = ocf[-1]["value"] - capex[-1]["value"], ocf[-5]["value"] - capex[-5]["value"]
    put("fcf_yoy", _change(current_fcf, old_fcf))
    put("capex_trend", features["capital_expenditure_yoy"])
    quarters = []
    revenue_by_end = {x["end"]: x for x in series.get("revenue", [])}
    for op in series.get("operating_income", [])[-8:]:
        rev = revenue_by_end.get(op["end"])
        if rev and rev["value"] > 0:
            quarters.append({"end": op["end"], "operating_margin": op["value"] / rev["value"]})
    put("margin_trend", statistics.mean(x["operating_margin"] for x in quarters[-2:]) - statistics.mean(x["operating_margin"] for x in quarters[-4:-2]) if len(quarters) >= 4 else None)
    put("profitability_consistency", sum(x["operating_margin"] > 0 for x in quarters[-4:]) / 4 if len(quarters) >= 4 else None)
    market_cap = price * features["split_adjusted_shares_for_valuation"] if price > 0 and features["split_adjusted_shares_for_valuation"] else None
    put("market_cap", market_cap)
    ev = market_cap + features["total_debt"] - features["cash"] if all(x is not None for x in (market_cap, features["total_debt"], features["cash"])) else None
    put("enterprise_value", ev)
    for out_name, num, den in (("pe", market_cap, features["net_income_ttm"]),
                               ("price_to_sales", market_cap, features["revenue_ttm"]),
                               ("price_to_book", market_cap, features["equity"]),
                               ("ev_to_ebitda", ev, ebitda), ("ev_to_sales", ev, features["revenue_ttm"])):
        put(out_name, _ratio(num, den))
    put("fcf_yield", _ratio(fcf, market_cap) if fcf is not None and fcf > 0 else None)
    for name, multiple in (("earnings_yield", "pe"), ("sales_yield", "price_to_sales"),
                           ("book_yield", "price_to_book"), ("ebitda_yield", "ev_to_ebitda"),
                           ("ev_sales_yield", "ev_to_sales")):
        put(name, _ratio(1, features[multiple]))
    put("growth_adjusted_earnings_yield", features["earnings_yield"] * (1 + max(-.5, min(.5, features["revenue_yoy"]))) if features["earnings_yield"] is not None and features["revenue_yoy"] is not None else None)
    put("profitability_adjusted_sales_yield", features["sales_yield"] * (1 + max(-.5, min(.5, features["operating_margin"]))) if features["sales_yield"] is not None and features["operating_margin"] is not None else None)
    features["forward_pe"] = None
    return {"values": features, "provenance": provenance,
            "quarterly": {name: values[-8:] for name, values in series.items()},
            "annual": {name: values[-5:] for name, values in annual.items()},
            "instant": {name: values[-6:] for name, values in instant.items()}}

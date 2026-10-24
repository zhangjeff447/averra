"""Transparent additive technical attribution and raw-feature redundancy."""
from __future__ import annotations

import json
import statistics
from itertools import combinations

from quant_ranking import feature_score


FEATURES = {
    "20D return": "return20", "60D return": "return60", "120D return": "return120",
    "Relative Strength 20D": "rs_spy_20d", "Relative Strength 60D": "rs_spy_60d",
    "MACD": "macd_line_pct", "MACD histogram": "macd_hist_pct",
    "MACD raw": "macd_line", "MACD histogram raw": "macd_hist",
    "RSI": "rsi14", "KDJ": "kdj_k", "EMA trend": "distance_ema21",
    "SMA trend": "distance_sma50", "ADX": "adx14", "RVOL": "rvol",
    "ATR %": "atr_pct", "Realized volatility": "realized_vol20",
    "Drawdown": "max_drawdown120",
}


def technical_attribution(features, rules, groups):
    active = {name: rule for name, rule in rules.items() if rule["enabled"] and rule["weight"] > 0 and features.get(name) is not None}
    covered = [name for names in groups.values() for name in names]
    if set(active) != set(covered) or len(covered) != len(set(covered)):
        raise ValueError("technical groups must partition all active scoring features")
    total = sum(rule["weight"] for rule in active.values())
    details = {}
    deltas = {}
    for group, names in groups.items():
        details[group] = [{"feature": name, "raw_value": features[name], "weight": active[name]["weight"],
                           "normalized_score": feature_score(features[name], active[name]),
                           "score_points": feature_score(features[name], active[name]) * active[name]["weight"] / total}
                          for name in names]
        deltas[group] = sum(item["score_points"] - 50 * item["weight"] / total for item in details[group])
    return {"neutral_baseline": 50, "group_deltas": deltas, "feature_details": details,
            "reconstructed_technical_score": 50 + sum(deltas.values())}


def _corr(rows, left, right):
    pairs = [(a, b) for row in rows if (a := row.get(left)) is not None and (b := row.get(right)) is not None]
    if len(pairs) < 3:
        return None, len(pairs)
    x, y = zip(*pairs)
    if len(set(x)) < 2 or len(set(y)) < 2:
        return None, len(pairs)
    return round(statistics.correlation(x, y), 4), len(pairs)


def redundancy(rows, cfg):
    rows = [{**row, "macd_line_pct": row["macd_line"] / row["close"] if row.get("close") else None}
            for row in rows]
    matrix, flags = {}, []
    for label, key in FEATURES.items():
        matrix[label] = {}
        for other_label, other_key in FEATURES.items():
            corr, sample = _corr(rows, key, other_key)
            matrix[label][other_label] = corr
            if label < other_label and corr is not None:
                level = "HIGH_REDUNDANCY" if abs(corr) >= cfg["high"] else "MODERATE_REDUNDANCY" if abs(corr) >= cfg["moderate"] else None
                if level:
                    flags.append({"left": label, "right": other_label, "correlation": corr,
                                  "sample_size": sample, "level": level})
    return {"matrix": matrix, "flags": sorted(flags, key=lambda item: -abs(item["correlation"]))}


def report(db, run_id, quant_cfg, phase_cfg):
    rows = []
    for row in db.execute("SELECT * FROM quant_scores WHERE run_id=? ORDER BY rank", (run_id,)):
        features = json.loads(row["features_json"])
        attribution = technical_attribution(features, quant_cfg["features"]["technical"], phase_cfg["technical_groups"])
        if abs(attribution["reconstructed_technical_score"] - row["technical_score"]) > 0.0001:
            raise ValueError(f"technical attribution mismatch: {row['ticker']}")
        rows.append({"ticker": row["ticker"], "rank": row["rank"], "momentum_score": row["momentum_score"],
                     "technical_score": row["technical_score"], "quant_score": row["quant_score"],
                     "features": features, "attribution": attribution})
    top = rows[:100]
    def avg(items, key):
        return round(statistics.mean(item[key] for item in items), 4) if items else None
    def group_means(items):
        return {group: avg([r["attribution"]["group_deltas"] for r in items], group)
                for group in phase_cfg["technical_groups"]}
    by_rsi = {cut: [r for r in top if r["features"]["rsi14"] > cut] for cut in (70, 80)}
    high_momentum = sorted(top, key=lambda r: -r["momentum_score"])[:20]
    low_momentum = sorted(top, key=lambda r: r["momentum_score"])[:20]
    flat = [{**r["features"], "momentum_score": r["momentum_score"],
             "technical_score": r["technical_score"]} for r in rows]
    def c(left, right):
        return _corr(flat, left, right)[0]
    return {"top100_group_mean_deltas_from_neutral": group_means(top),
            "rsi_over_70": {"count": len(by_rsi[70]), "mean_technical": avg(by_rsi[70], "technical_score"),
                            "mean_rsi_delta": group_means(by_rsi[70])["RSI"]},
            "rsi_over_80": {"count": len(by_rsi[80]), "mean_technical": avg(by_rsi[80], "technical_score"),
                            "mean_rsi_delta": group_means(by_rsi[80])["RSI"]},
            "high_momentum_top20_group_deltas": group_means(high_momentum),
            "low_momentum_top20_group_deltas": group_means(low_momentum),
            "momentum_technical_correlation_all_scored": c("momentum_score", "technical_score"),
            "rsi_momentum_correlation_all_scored": c("rsi14", "momentum_score"),
            "kdj_momentum_correlation_all_scored": c("kdj_k", "momentum_score"),
            "macd_hist_momentum_correlation_all_scored": c("macd_hist_pct", "momentum_score"),
            "ema_momentum_correlation_all_scored": c("distance_ema21", "momentum_score"),
            "top10_attributions": [{"ticker": r["ticker"], "rank": r["rank"],
                                    "technical_score": r["technical_score"], **r["attribution"]} for r in top[:10]],
            "redundancy_all_scored": redundancy([r["features"] for r in rows], phase_cfg["correlation"])}

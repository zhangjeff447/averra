#!/usr/bin/env python3
"""Read-only real ranking, sector and score-bias report from SQLite."""
from __future__ import annotations

import argparse
import json
import random
import sqlite3
import statistics
from collections import Counter, defaultdict
from pathlib import Path

from quant_screen import ROOT


COMPONENTS = ("momentum", "relative_strength", "technical", "volume", "risk")
TECHNICAL = ("macd_line", "macd_signal", "macd_hist", "macd_hist_slope", "macd_hist_acceleration",
             "macd_above_zero", "rsi14", "kdj_k", "kdj_d", "kdj_j", "ema9", "ema21",
             "sma20", "sma50", "sma200", "adx14", "bollinger_upper", "bollinger_lower",
             "atr14", "atr_pct", "rvol")
SECTORS = ("Technology", "Financials", "Healthcare", "Industrials", "Consumer Discretionary",
           "Consumer Staples", "Energy", "Utilities", "Materials", "Real Estate", "Communication Services")


def correlation(rows, left, right):
    x = [row[left] for row in rows]
    y = [row[right] for row in rows]
    if len(rows) < 3 or len(set(x)) < 2 or len(set(y)) < 2:
        return None
    return round(statistics.correlation(x, y), 4)


def report(db, run_id=None):
    if run_id is None:
        run = db.execute("SELECT * FROM quant_runs WHERE data_mode='live' ORDER BY id DESC LIMIT 1").fetchone()
    else:
        run = db.execute("SELECT * FROM quant_runs WHERE id=?", (run_id,)).fetchone()
    if not run:
        raise ValueError("no real quant run found")
    rows = []
    for item in db.execute("""SELECT q.*,s.company_name,
        CASE WHEN s.mapping_confidence IN ('LOW','UNKNOWN') THEN NULL
             ELSE COALESCE(s.normalized_sector,s.sector) END AS sector,s.industry,s.sector_source
        FROM quant_scores q LEFT JOIN securities s ON s.ticker=q.ticker
        WHERE q.run_id=? ORDER BY q.rank""", (run["id"],)):
        features = json.loads(item["features_json"])
        rows.append({"rank": item["rank"], "ticker": item["ticker"], "company": item["company_name"],
                     "sector": item["sector"], "industry": item["industry"], "sector_source": item["sector_source"],
                     "price": features.get("close"), "quant_score": item["quant_score"],
                     **{name + "_score": item[name + "_score"] for name in COMPONENTS},
                     "rsi14": features.get("rsi14"), "rvol": features.get("rvol"),
                     "atr_pct": features.get("atr_pct"), "features": features,
                     "memory": json.loads(item["memory_json"])})
    top = rows[:100]
    groups = defaultdict(list)
    for row in rows:
        groups[row["sector"] or "UNKNOWN"].append(row)
    sector_top10 = {sector: [{"sector_rank": index,
                              **{k: row[k] for k in ("rank", "ticker", "company", "sector", "price", "quant_score")}}
                             for index, row in enumerate(groups.get(sector, [])[:10], 1)] for sector in SECTORS}
    def average(key):
        vals = [row[key] for row in top if row[key] is not None]
        return round(statistics.mean(vals), 4) if vals else None
    def sector_concentration(items):
        count = Counter(row["sector"] or "UNKNOWN" for row in items)
        return {name: {"count": n, "percent": round(n / len(items) * 100, 2)}
                for name, n in count.most_common()} if items else {}
    diagnostics = {
        "top100_sector_concentration": sector_concentration(top),
        "top20_sector_concentration": sector_concentration(top[:20]),
        "average_rsi14": average("rsi14"),
        "rsi_over_70_percent": round(sum(row["rsi14"] > 70 for row in top) / len(top) * 100, 2) if top else None,
        "rsi_over_80_percent": round(sum(row["rsi14"] > 80 for row in top) / len(top) * 100, 2) if top else None,
        "average_rvol": average("rvol"), "average_atr_pct": average("atr_pct"),
        "average_risk_score": average("risk_score"),
        "average_momentum_score": average("momentum_score"),
        "average_relative_strength_score": average("relative_strength_score"),
        "component_quant_correlations": {name: correlation(top, name + "_score", "quant_score") for name in COMPONENTS},
        "momentum_relative_strength_correlation": correlation(top, "momentum_score", "relative_strength_score"),
        "momentum_technical_correlation": correlation(top, "momentum_score", "technical_score"),
        "relative_strength_technical_correlation": correlation(top, "relative_strength_score", "technical_score"),
        "sector_metadata_coverage_percent": round(sum(row["sector"] in SECTORS for row in rows) / len(rows) * 100, 2) if rows else None,
    }
    sample = random.Random(run["id"]).sample(rows, min(3, len(rows)))
    raw = {row["ticker"]: {name: row["features"].get(name) for name in TECHNICAL} for row in sample}
    def compact(row):
        return {k: v for k, v in row.items() if k not in ("features", "memory", "rsi14", "rvol", "atr_pct", "sector_source", "industry")}
    return {"run_id": run["id"], "asof": run["asof"], "scanned_at": run["scanned_at"],
            "sector_taxonomy": "NASDAQ_SCREENER_NON_GICS",
            "sector_etf_mapping_quality": "HEURISTIC",
            "overall_top100": [compact(row) for row in top],
            "top20_component_scores": [compact(row) for row in top[:20]],
            "sector_top10": sector_top10, "diagnostics": diagnostics,
            "random_raw_technical_features": raw,
            "scored_count": len(rows),
            "sector_unclassified_count": len(groups.get("UNKNOWN", []))}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=ROOT / "data" / "market_data.db")
    parser.add_argument("--run-id", type=int)
    parser.add_argument("--output", type=Path, default=ROOT / "data" / "real_top100.json")
    args = parser.parse_args()
    with sqlite3.connect(args.db) as db:
        db.row_factory = sqlite3.Row
        result = report(db, args.run_id)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(f"run={result['run_id']} asof={result['asof']} scored={result['scored_count']} Top100={len(result['overall_top100'])} sector_coverage={result['diagnostics']['sector_metadata_coverage_percent']}%")


if __name__ == "__main__":
    main()

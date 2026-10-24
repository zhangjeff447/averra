"""Read-only Shadow observation evaluation; no simulated fills, network, or AI."""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import math
import sqlite3
import statistics
from collections import Counter, defaultdict
from contextlib import closing
from datetime import date, datetime, time, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
ET = ZoneInfo("America/New_York")
SCORES = ("quant_score", "fundamental_score", "candidate_score", "research_score", "entry_score")
METRICS = ("return_pct", "excess_spy_pct", "excess_qqq_pct", "mfe_pct", "mae_pct")


def capture_manifest(day, root=ROOT):
    """Fingerprint code/config now; never claim these hashes describe older runs."""
    files = sorted([*root.glob("*.py"), *root.glob("*config.json")])
    return {"trading_date": day, "captured_at": datetime.now(timezone.utc).isoformat(),
            "files": {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in files}}


def finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def ranks(values):
    positions = sorted(range(len(values)), key=lambda i: values[i])
    out = [0.] * len(values)
    start = 0
    while start < len(values):
        end = start + 1
        while end < len(values) and values[positions[end]] == values[positions[start]]:
            end += 1
        for index in positions[start:end]:
            out[index] = (start + end - 1) / 2
        start = end
    return out


def rank_ic(rows, score):
    pairs = [(r[score], r["excess_spy_pct"]) for r in rows if finite(r.get(score))]
    if len(pairs) < 3:
        return None
    x, y = ranks([p[0] for p in pairs]), ranks([p[1] for p in pairs])
    mx, my = statistics.mean(x), statistics.mean(y)
    denominator = math.sqrt(sum((v-mx)**2 for v in x) * sum((v-my)**2 for v in y))
    return sum((a-mx)*(b-my) for a,b in zip(x,y)) / denominator if denominator else None


def stats(rows, minimum_dates):
    dates = defaultdict(list)
    for row in rows:
        dates[row["trading_date"]].append(row)
    result = {"observations": len(rows), "independent_scan_dates": len(dates),
              "status": "DESCRIPTIVE_ONLY" if len(dates) >= minimum_dates else "INSUFFICIENT_HISTORY"}
    for field in METRICS:
        values = [r[field] for r in rows if finite(r[field])]
        per_date = [statistics.mean([r[field] for r in group if finite(r[field])])
                    for group in dates.values() if any(finite(r[field]) for r in group)]
        result[field] = {"count": len(values),
            "mean": round(statistics.mean(values), 4) if values else None,
            "median": round(statistics.median(values), 4) if values else None,
            "date_balanced_mean": round(statistics.mean(per_date), 4) if per_date else None}
    result["positive_return_fraction"] = (sum(r["return_pct"] > 0 for r in rows) / len(rows)) if rows else None
    result["score_rank_ic_vs_spy"] = {}
    for score in SCORES:
        daily = [value for group in dates.values() if (value := rank_ic(group, score)) is not None]
        result["score_rank_ic_vs_spy"][score] = {"dates": len(daily),
            "mean": round(statistics.mean(daily), 4) if daily else None}
    return result


def evaluate(path, asof, minimum_dates=20, root=ROOT):
    limit = date.fromisoformat(asof)
    if minimum_dates < 1:
        raise ValueError("INVALID_MINIMUM_SCAN_DATES")
    with closing(sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        # The corrected view is required: never silently fall back to superseded outcomes.
        rows = [dict(r) for r in db.execute("""SELECT f.*,s.recorded_at,s.quant_score,
            s.fundamental_score,s.candidate_score,s.research_score,s.entry_score,
            s.entry_state,s.research_quality_tier FROM effective_forward f
            JOIN snapshots s ON s.trading_date=f.trading_date AND s.ticker=f.ticker
            WHERE f.trading_date<=? ORDER BY f.trading_date,f.ticker,f.horizon""", (asof,))]
    accepted, excluded, pending = [], Counter(), Counter()
    scan_dates, post_close = set(), set()
    for row in rows:
        try:
            stamp = datetime.fromisoformat(row["recorded_at"].replace("Z", "+00:00"))
            if stamp.tzinfo is None or stamp.astimezone(ET).date() > limit:
                raise ValueError("record unavailable at evaluation cutoff")
        except (ValueError, TypeError, AttributeError):
            excluded["SNAPSHOT_TIME_INVALID_OR_AFTER_ASOF"] += 1
            continue
        scan_dates.add(row["trading_date"])
        if stamp.astimezone(ET) >= datetime.combine(date.fromisoformat(row["trading_date"]), time(16), ET):
            post_close.add((row["trading_date"], row["ticker"]))
        if row["status"] == "PENDING":
            pending[str(row["horizon"])] += 1
            continue
        if row["status"] != "COMPLETE":
            excluded["INVALID_OR_QUARANTINED_OUTCOME"] += 1
            continue
        try:
            end = date.fromisoformat(row["end_date"])
            if end <= date.fromisoformat(row["trading_date"]) or end > limit:
                raise ValueError("invalid future endpoint")
        except (ValueError, TypeError):
            excluded["OUTCOME_TIME_INVALID_OR_AFTER_ASOF"] += 1
            continue
        if not all(finite(row[k]) for k in METRICS[:3]) or any(
                row[k] is not None and not finite(row[k]) for k in METRICS[3:]):
            excluded["NON_FINITE_OUTCOME"] += 1
            continue
        accepted.append(row)
    horizons = {}
    for horizon in sorted({r["horizon"] for r in rows}):
        group = [r for r in accepted if r["horizon"] == horizon]
        cohorts = {}
        for field in ("entry_state", "research_quality_tier"):
            cohorts[field] = {str(value): stats([r for r in group if r[field] == value], minimum_dates)
                for value in sorted({r[field] for r in group}, key=str)}
        horizons[str(horizon)] = {**stats(group, minimum_dates), "pending": pending[str(horizon)],
                                   "cohorts": cohorts}
    manifests = [day for day in sorted(scan_dates)
                 if (root / "data" / "shadow_daily" / day / "run_manifest.json").is_file()]
    return {"report_type": "SHADOW_REFERENCE_OBSERVATIONS_NOT_EXECUTION_BACKTEST", "asof": asof,
        "generated_at": datetime.now(timezone.utc).isoformat(), "minimum_independent_scan_dates": minimum_dates,
        "scan_dates": sorted(scan_dates), "accepted_observations": len(accepted),
        "excluded": dict(excluded), "horizons": horizons,
        "input_rows_sha256": hashlib.sha256(json.dumps(rows, sort_keys=True, allow_nan=True).encode()).hexdigest(),
        "post_close_close_anchor_snapshots": len(post_close), "historical_manifest_dates": manifests,
        "current_files_manifest": capture_manifest(asof, root),
        "limitations": ["历史收盘锚点用于候选表现诊断，不代表收盘后信号能按该收盘价成交。",
            "不是实际交易收益；未模拟买入区间、跳空、滑点、费用或持仓资金。",
            "不生成虚构净值、Sharpe或组合最大回撤；MFE/MAE是单股观察指标。",
            "同日股票及重叠持有期并不独立；按扫描日均衡汇总，少量日期不能证明预测能力。",
            "Research/Entry分组非随机对照；组间差异不能证明AI的因果贡献。",
            "当前代码配置指纹不能补作历史版本；历史版本仅承认当时保存的manifest。"],
        "network_requests": 0, "ai_calls": 0, "strategy_changes": 0}


def render(report):
    escape = lambda value: html.escape(str(value))
    def number(value):
        return "—" if value is None else f"{value:.3f}"
    rows = []
    for horizon, item in report["horizons"].items():
        values = [horizon, item["observations"], item["independent_scan_dates"], item["pending"],
            item["status"], *[number(item[field]["date_balanced_mean"]) for field in METRICS]]
        rows.append("<tr>" + "".join(f"<td>{escape(v)}</td>" for v in values) + "</tr>")
    headers = ["期限/交易日", "已成熟", "扫描日数", "待成熟", "样本状态", "收益%", "超额SPY/百分点", "超额QQQ/百分点", "MFE%", "MAE%"]
    details = json.dumps({"排除项": report["excluded"], "分组与评分IC": report["horizons"]}, ensure_ascii=False, indent=2)
    return ("<!doctype html><html lang='zh-CN'><meta charset='utf-8'><title>Averra Shadow 验证报告</title>"
        "<style>body{font:16px system-ui;background:#091713;color:#dcebe5;margin:4vw;line-height:1.7}"
        "h1{font-size:32px}table{border-collapse:collapse;width:100%}td,th{text-align:left;padding:12px;border-bottom:1px solid #355249}"
        "pre{white-space:pre-wrap;overflow-wrap:anywhere}small{color:#a7bdb4}.table{overflow:auto}</style>"
        f"<h1>Stock Agent · Shadow 验证</h1><p>数据截止：{escape(report['asof'])} · 本地只读 · AI费用 $0</p>"
        "<p>候选股表现诊断，不是实际交易回测。下表按扫描日均衡汇总。</p><div class='table'><table><thead><tr>"
        + "".join(f"<th>{escape(h)}</th>" for h in headers) + "</tr></thead><tbody>" + "".join(rows)
        + "</tbody></table></div><h2>解读限制</h2><ul>"
        + "".join(f"<li>{escape(v)}</li>" for v in report["limitations"])
        + f"</ul><details><summary>完整分组、数据排除与评分相关性</summary><pre>{escape(details)}</pre></details>"
        + f"<small>输入指纹：{escape(report['input_rows_sha256'])}</small></html>")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=ROOT / "data" / "shadow_daily_results.db")
    parser.add_argument("--asof", default=datetime.now(ET).date().isoformat())
    parser.add_argument("--output", type=Path, default=ROOT / "data" / "evaluation")
    args = parser.parse_args()
    if date.fromisoformat(args.asof) > datetime.now(ET).date():
        raise ValueError("FUTURE_EVALUATION_DATE")
    cfg = json.loads((ROOT / "phase26_config.json").read_text())
    report = evaluate(args.db, args.asof, cfg["forward"]["minimum_independent_scan_dates"])
    args.output.mkdir(parents=True, exist_ok=True)
    for suffix, body in (("json", json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False)),
                         ("html", render(report))):
        path = args.output / f"shadow-{args.asof}.{suffix}"
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(body, encoding="utf-8")
        temporary.replace(path)
    print(json.dumps({"output": str(args.output), "observations": report["accepted_observations"],
                      "scan_dates": len(report["scan_dates"]), "ai_calls": 0}))


if __name__ == "__main__":
    main()

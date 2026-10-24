"""Immutable scan snapshots and incremental, calendar-aligned forward outcomes."""
from __future__ import annotations

import json
import statistics
from collections import defaultdict


SCORES = ("momentum", "relative_strength", "technical", "volume", "risk")


def register_scan(db, run_id, regime="UNKNOWN_ASOF"):
    run = db.execute("SELECT asof FROM quant_runs WHERE id=?", (run_id,)).fetchone()
    if not run:
        raise ValueError("unknown quant run")
    rows = db.execute("""SELECT q.*,s.normalized_sector FROM quant_scores q
        LEFT JOIN securities s ON s.ticker=q.ticker WHERE q.run_id=?""", (run_id,)).fetchall()
    with db:
        db.executemany("""INSERT OR IGNORE INTO forward_snapshots VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            [(run_id, row["ticker"], run["asof"], row["rank"], row["quant_score"],
              *(row[f"{name}_score"] for name in SCORES), json.loads(row["features_json"])["close"],
              row["normalized_sector"], regime) for row in rows])
    return len(rows)


def update(db, cfg):
    """Fill only mature horizons; a missing stock or benchmark bar stays pending."""
    horizons = cfg["horizons"]
    latest = db.execute("SELECT MAX(date) FROM daily_bars WHERE ticker='SPY'").fetchone()[0]
    if latest is None:
        return {"inserted": 0, "completed": 0}
    inserted = 0
    snapshots = db.execute("SELECT * FROM forward_snapshots WHERE scan_date<?", (latest,)).fetchall()
    pending = {(r[0], r[1], r[2]) for r in db.execute("SELECT run_id,ticker,horizon FROM forward_observations")}
    session_cache = {}
    for snap in snapshots:
        if all((snap["run_id"], snap["ticker"], h) in pending for h in horizons):
            continue
        if snap["scan_date"] not in session_cache:
            session_cache[snap["scan_date"]] = [r[0] for r in db.execute("""SELECT date FROM daily_bars
                WHERE ticker='SPY' AND date>? ORDER BY date LIMIT ?""", (snap["scan_date"], max(horizons)))]
        sessions = session_cache[snap["scan_date"]]
        for horizon in horizons:
            key = (snap["run_id"], snap["ticker"], horizon)
            if key in pending or len(sessions) < horizon:
                continue
            dates = sessions[:horizon]
            placeholders = ",".join("?" for _ in dates)
            rows = db.execute(f"""SELECT ticker,date,close,high,low FROM daily_bars
                WHERE ticker IN (?,?,?) AND date IN ({placeholders})""",
                (snap["ticker"], "SPY", "QQQ", *dates)).fetchall()
            by_symbol = defaultdict(dict)
            for row in rows:
                by_symbol[row["ticker"]][row["date"]] = row
            if any(len(by_symbol[symbol]) != horizon for symbol in {snap["ticker"], "SPY", "QQQ"}):
                continue
            bases = {"stock": snap["close_price"]}
            for symbol in ("SPY", "QQQ"):
                base = db.execute("SELECT close FROM daily_bars WHERE ticker=? AND date=?",
                                  (symbol, snap["scan_date"])).fetchone()
                if base is None or base[0] <= 0:
                    break
                bases[symbol] = base[0]
            if len(bases) != 3:
                continue
            stock_return = by_symbol[snap["ticker"]][dates[-1]]["close"] / bases["stock"] - 1
            spy_return = by_symbol["SPY"][dates[-1]]["close"] / bases["SPY"] - 1
            qqq_return = by_symbol["QQQ"][dates[-1]]["close"] / bases["QQQ"] - 1
            mfe = max(by_symbol[snap["ticker"]][day]["high"] / bases["stock"] - 1 for day in dates) if horizon in cfg["excursion_horizons"] else None
            mae = min(by_symbol[snap["ticker"]][day]["low"] / bases["stock"] - 1 for day in dates) if horizon in cfg["excursion_horizons"] else None
            with db:
                db.execute("""INSERT OR IGNORE INTO forward_observations VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (*key, dates[-1], stock_return, spy_return, qqq_return,
                     stock_return - spy_return, stock_return - qqq_return, mfe, mae))
            pending.add(key)
            inserted += 1
    return {"inserted": inserted, "completed": len(pending)}


def statistics_report(db, cfg):
    """Descriptive only; same-date rescans are de-duplicated for sample statistics."""
    rows = db.execute("""SELECT s.*,o.horizon,o.stock_return,o.excess_spy,o.excess_qqq,o.mfe,o.mae
        FROM forward_observations o JOIN forward_snapshots s USING(run_id,ticker)
        WHERE s.run_id=(SELECT MAX(s2.run_id) FROM forward_snapshots s2
                        WHERE s2.scan_date=s.scan_date)""").fetchall()
    def summarize(items):
        values = [r["stock_return"] for r in items]
        if not values:
            return {"sample_size": 0}
        mean = lambda key: statistics.mean(v for r in items if (v := r[key]) is not None) if any(r[key] is not None for r in items) else None
        return {"sample_size": len(items), "independent_scan_dates": len({r["scan_date"] for r in items}),
                "average_return": statistics.mean(values), "median_return": statistics.median(values),
                "positive_percent": sum(v > 0 for v in values) / len(values) * 100,
                "average_excess_spy": mean("excess_spy"), "average_excess_qqq": mean("excess_qqq"),
                "average_mfe": mean("mfe"), "average_mae": mean("mae"),
                "predictive_claim_allowed": len({r["scan_date"] for r in items}) >= cfg["minimum_independent_scan_dates"]}
    scores = {}
    ranks = {}
    components = {}
    for horizon in cfg["horizons"]:
        subset = [r for r in rows if r["horizon"] == horizon]
        scores[str(horizon)] = {f"[{low},{high})": summarize([r for r in subset if low <= r["quant_score"] < high])
                                for low, high in cfg["score_buckets"]}
        ranks[str(horizon)] = {f"Top {cut}": summarize([r for r in subset if r["rank"] <= cut])
                               for cut in cfg["rank_buckets"]}
        ranks[str(horizon)]["All Phase 2 Passed"] = summarize(subset)
        minimum = cfg["minimum_independent_scan_dates"]
        components[str(horizon)] = {name: _correlation(subset, f"{name}_score", "stock_return", minimum) for name in SCORES}
        components[str(horizon)]["risk_vs_mae"] = _correlation([r for r in subset if r["mae"] is not None], "risk_score", "mae", minimum)
    total = db.execute("SELECT COUNT(*) FROM forward_snapshots").fetchone()[0]
    effective = db.execute("""SELECT COUNT(*) FROM forward_snapshots s WHERE run_id=(
        SELECT MAX(run_id) FROM forward_snapshots s2 WHERE s2.scan_date=s.scan_date)""").fetchone()[0]
    complete = db.execute("SELECT COUNT(*) FROM forward_observations").fetchone()[0]
    return {"snapshots": total, "effective_unique_date_ticker_snapshots": effective,
            "completed_observations": complete,
            "pending": {str(h): effective - db.execute("""SELECT COUNT(*) FROM forward_observations o
                JOIN forward_snapshots s USING(run_id,ticker) WHERE o.horizon=? AND s.run_id=(
                SELECT MAX(run_id) FROM forward_snapshots s2 WHERE s2.scan_date=s.scan_date)""", (h,)).fetchone()[0]
                        for h in cfg["horizons"]}, "score_buckets": scores, "rank_buckets": ranks,
            "component_correlations": components, "warning": "Descriptive only; no predictive claim until enough independent scan dates."}


def _correlation(rows, x, y, minimum):
    if len({r["scan_date"] for r in rows}) < minimum or len(rows) < 3:
        return None
    xx, yy = [r[x] for r in rows], [r[y] for r in rows]
    return statistics.correlation(xx, yy) if len(set(xx)) > 1 and len(set(yy)) > 1 else None

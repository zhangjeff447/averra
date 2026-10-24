"""Read saved Shadow results; produce one descriptive review per five complete days."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import Counter, defaultdict
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from zoneinfo import ZoneInfo

from quant_screen import ROOT

DB = ROOT / "data" / "shadow_validation.db"
SCHEMA = """
CREATE TABLE IF NOT EXISTS completed_shadow_trading_days (
 trading_date TEXT PRIMARY KEY,run_id TEXT NOT NULL,result_sha256 TEXT NOT NULL,
 recorded_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS shadow_5d_reviews (
 review_id TEXT PRIMARY KEY,window_start TEXT NOT NULL,window_end TEXT NOT NULL,
 trading_dates_json TEXT NOT NULL,generated_at TEXT NOT NULL,report_json TEXT NOT NULL,
 telegram_status TEXT NOT NULL CHECK(telegram_status IN ('PENDING','IN_FLIGHT','SENT','UNKNOWN')),
 telegram_message_id INTEGER,attempted_at TEXT,completed_at TEXT);
"""
ET = ZoneInfo("America/New_York")


def stamp():
    return datetime.now(timezone.utc).isoformat()


def connect(path=DB):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=30)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    return db


def completed_days(db, state_path):
    """Register only hash-verified completed runs; never rewrite prior records."""
    if not Path(state_path).is_file():
        return 0
    with closing(sqlite3.connect(Path(state_path).resolve().as_uri() + "?mode=ro", uri=True)) as state:
        rows = state.execute("""SELECT trading_date,run_id,result_path,result_sha256,market_data_as_of
            FROM shadow_daily_runs WHERE state IN ('COMPLETE','PIPELINE_COMPLETE_TELEGRAM_PENDING')
            ORDER BY trading_date""").fetchall()
    for day, run_id, path, digest, market_day in rows:
        if db.execute("SELECT 1 FROM completed_shadow_trading_days WHERE trading_date=?", (day,)).fetchone():
            continue
        artifact = Path(path) if path else None
        if not artifact or not artifact.is_file() or market_day != day:
            continue
        raw = artifact.read_bytes()
        if hashlib.sha256(raw).hexdigest() != digest:
            continue
        report = json.loads(raw)
        required = ("market", "phase1", "phase2", "phase3", "research", "entry",
                    "portfolio", "forward_tracking")
        if (report.get("trading_date") != day or report.get("market_data_as_of") != day or
                report.get("status") != "PIPELINE_COMPLETE" or
                any(report.get("phase_statuses", {}).get(phase) != "COMPLETE" for phase in required)):
            continue
        with db:
            db.execute("INSERT OR IGNORE INTO completed_shadow_trading_days VALUES(?,?,?,?)",
                       (day, run_id, digest, stamp()))
    return db.execute("SELECT COUNT(*) FROM completed_shadow_trading_days").fetchone()[0]


def _avg(values):
    return round(mean(values), 4) if values else None


def _performance(rows, state, horizon, top10=False):
    valid = [r for r in rows if (state is None or r["entry_state"] == state) and r["horizon"] == horizon and
             r["status"] == "COMPLETE" and r["return_pct"] is not None and
             (not top10 or r["candidate_rank"] <= 10)]
    return {"status": "MATURED" if valid else "NOT_MATURED", "sample_size": len(valid),
            "average_return_pct": _avg([r["return_pct"] for r in valid]),
            "average_mfe_pct": _avg([r["mfe_pct"] for r in valid if r["mfe_pct"] is not None]),
            "average_mae_pct": _avg([r["mae_pct"] for r in valid if r["mae_pct"] is not None]),
            "spy_return_pct": _avg([r["return_pct"] - r["excess_spy_pct"] for r in valid
                                     if r["excess_spy_pct"] is not None]),
            "qqq_return_pct": _avg([r["return_pct"] - r["excess_qqq_pct"] for r in valid
                                     if r["excess_qqq_pct"] is not None])}


def build_review(days, state_path, results_path, stock_path):
    if len(days) != 5 or days != sorted(set(days)):
        raise ValueError("SHADOW_REVIEW_WINDOW_INVALID")
    with closing(sqlite3.connect(Path(state_path).resolve().as_uri() + "?mode=ro", uri=True)) as state:
        reports = []
        for day in days:
            row = state.execute("""SELECT result_path,result_sha256 FROM shadow_daily_runs
                WHERE trading_date=? AND state IN ('COMPLETE','PIPELINE_COMPLETE_TELEGRAM_PENDING')""",
                (day,)).fetchone()
            if not row or not Path(row[0]).is_file():
                raise ValueError("SHADOW_REVIEW_SOURCE_MISSING")
            raw = Path(row[0]).read_bytes()
            if hashlib.sha256(raw).hexdigest() != row[1]:
                raise ValueError("SHADOW_REVIEW_SOURCE_HASH_MISMATCH")
            reports.append(json.loads(raw))
    if not Path(results_path).is_file():
        raise ValueError("SHADOW_FORWARD_DB_MISSING")
    with closing(sqlite3.connect(Path(results_path).resolve().as_uri() + "?mode=ro", uri=True)) as results:
        results.row_factory = sqlite3.Row
        placeholders = ",".join("?" for _ in days)
        rows = [dict(row) for row in results.execute(f"""SELECT s.trading_date,s.ticker,s.candidate_rank,
            s.entry_state,f.horizon,f.status,f.return_pct,f.mfe_pct,f.mae_pct,
            f.excess_spy_pct,f.excess_qqq_pct FROM snapshots s LEFT JOIN effective_forward f
            ON f.trading_date=s.trading_date AND f.ticker=s.ticker
            WHERE s.trading_date IN ({placeholders})""", days)]
    candidates = {(r["trading_date"], r["ticker"]) for r in rows}
    states = {(r["trading_date"], r["ticker"]): r["entry_state"] for r in rows}
    counts = Counter(); research = Counter(); funnel = Counter()
    for report in reports:
        if report.get("sol_calls") != 0:
            raise ValueError("SHADOW_REVIEW_UNEXPECTED_SOL_CALL")
        counts.update(report.get("entry_counts", {}))
        research.update(report.get("eligibility_counts", {}))
        funnel["candidate_top15"] += len(report.get("eligibility_cases", []))
    ordered_by_ticker = defaultdict(list)
    day_index = {day: index for index, day in enumerate(days)}
    for (day, ticker), state in states.items():
        ordered_by_ticker[ticker].append((day, state))
    transitions = Counter()
    watched = {("WATCH", "BUY_TOMORROW_CANDIDATE"): "WATCH_TO_BUY_TOMORROW",
               ("BUY_TOMORROW_CANDIDATE", "WATCH"): "BUY_TOMORROW_TO_WATCH",
               ("WATCH", "REJECT_ENTRY"): "WATCH_TO_REJECT"}
    for history in ordered_by_ticker.values():
        for (earlier_day, earlier), (later_day, later) in zip(sorted(history), sorted(history)[1:]):
            if day_index[later_day] != day_index[earlier_day] + 1:
                continue
            label = watched.get((earlier, later))
            if earlier == "ENTRY_DATA_INSUFFICIENT" and later != "ENTRY_DATA_INSUFFICIENT":
                label = "DATA_INSUFFICIENT_TO_EVALUABLE"
            if label:
                transitions[label] += 1
    lifecycle = []
    closed_lifecycles = []
    if Path(stock_path).is_file():
        with closing(sqlite3.connect(Path(stock_path).resolve().as_uri() + "?mode=ro", uri=True)) as stock:
            opened = {}
            for ticker, kind, confirmed_at, price, metrics in stock.execute("""SELECT ticker,kind,confirmed_at,price,metrics_json
                FROM holding_events ORDER BY id"""):
                if kind == "BOUGHT":
                    opened[ticker] = (confirmed_at, price)
                elif ticker in opened:
                    entry_at, entry_price = opened.pop(ticker)
                    if datetime.fromisoformat(entry_at).astimezone(ET).date().isoformat() in days:
                        closed_lifecycles.append({"ticker": ticker, "entry_at": entry_at,
                                                  "entry_price": entry_price, "exit_at": confirmed_at,
                                                  "exit_price": price,
                                                  "metrics": json.loads(metrics) if metrics else None})
                if datetime.fromisoformat(confirmed_at).astimezone(ET).date().isoformat() in days:
                    lifecycle.append({"ticker": ticker, "kind": kind, "confirmed_at": confirmed_at,
                                      "price": price, "metrics": json.loads(metrics) if metrics else None})
    buy_1d = _performance(rows, "BUY_TOMORROW_CANDIDATE", 1)
    watch_1d = _performance(rows, "WATCH", 1)
    top10_1d = _performance(rows, None, 1, top10=True)
    top10_5d = _performance(rows, None, 5, top10=True)
    total_research = funnel["candidate_top15"]
    limited = research["RESEARCH_LIMITED"]
    insufficient = counts["ENTRY_DATA_INSUFFICIENT"]
    observations = [{"type": "STRATEGY_OBSERVATION", "text":
                     f"ENTRY_DATA_INSUFFICIENT {insufficient}/{sum(counts.values())} entry observations"},
                    {"type": "STRATEGY_OBSERVATION", "text":
                     f"DEEP_REQUIRED {research['DEEP_REQUIRED']}/{total_research} Top-15 observations"}]
    return {"review_type": "SHADOW_5D_REVIEW", "observation_window": {"start": days[0], "end": days[-1]},
            "trading_dates": days, "candidate_count": len(candidates),
            "entry_counts": dict(counts), "research_counts": dict(research),
            "full_limited_ratio": (round(research["FINAL_RESEARCH_AVAILABLE"] / limited, 4)
                                   if limited else None),
            "deep_pending_ratio": round(research["DEEP_REQUIRED"] / total_research, 4) if total_research else None,
            "research_funnel": {"candidate_top15": total_research,
                                "full": research["FINAL_RESEARCH_AVAILABLE"],
                                "limited": limited, "deep_required": research["DEEP_REQUIRED"],
                                "failed": research["RESEARCH_FAILED"]},
            "ai": {"luna_calls": sum(r["luna_calls"] for r in reports),
                   "luna_input_tokens": sum(r["luna_input_tokens"] for r in reports),
                   "luna_output_tokens": sum(r["luna_output_tokens"] for r in reports),
                   "luna_cost_usd": round(sum(r["luna_estimated_cost_usd"] for r in reports), 6),
                   "sol_calls": sum(r["sol_calls"] for r in reports), "sol_cost_usd": 0},
            "buy_tomorrow": {"1d": buy_1d, "5d": _performance(rows, "BUY_TOMORROW_CANDIDATE", 5)},
            "watch": {"1d": watch_1d, "5d": _performance(rows, "WATCH", 5)},
            "candidate_top10": {"1d": top10_1d, "5d": top10_5d,
                                "by_day": {day: {"1d": _performance(
                                    [r for r in rows if r["trading_date"] == day], None, 1, top10=True),
                                    "5d": _performance(
                                    [r for r in rows if r["trading_date"] == day], None, 5, top10=True)}
                                           for day in days}},
            "benchmarks_matched_1d": {"SPY": top10_1d["spy_return_pct"],
                                      "QQQ": top10_1d["qqq_return_pct"],
                                      "sample_size": top10_1d["sample_size"]},
            "benchmarks_matched_5d": {"SPY": top10_5d["spy_return_pct"],
                                      "QQQ": top10_5d["qqq_return_pct"],
                                      "sample_size": top10_5d["sample_size"],
                                      "status": top10_5d["status"]},
            "entry_state_transitions": dict(transitions),
            "tracked_position_sample": sum(e["kind"] == "BOUGHT" for e in lifecycle),
            "exit_validation": "DESCRIPTIVE_LIFECYCLE_ONLY" if any(
                e["kind"] == "BOUGHT" for e in lifecycle) else
                               "EXIT ENGINE REAL VALIDATION NOT STARTED",
            "position_lifecycle_events": lifecycle,
            "closed_position_lifecycles": closed_lifecycles,
            "observations": observations,
            "software_data_bugs": ([{"type": "SOFTWARE_DATA_BUG", "reason": "FORWARD_INVALID_EXCLUDED"}]
                                   if any(r["status"] == "INVALID_EXCLUDED" for r in rows) else []),
            "strategy_parameters_changed": False}


def review_message(report):
    buy = report["buy_tomorrow"]["1d"]
    ret = f"{buy['average_return_pct']:+.2f}%" if buy["average_return_pct"] is not None else "NOT MATURED"
    spy = report["benchmarks_matched_1d"]["SPY"]
    qqq = report["benchmarks_matched_1d"]["QQQ"]
    return (f"AVERRA STOCKS | 5-DAY SHADOW REVIEW\n"
            f"Days: {', '.join(report['trading_dates'])}\n"
            f"BUY {report['entry_counts'].get('BUY_TOMORROW_CANDIDATE', 0)} | "
            f"WATCH {report['entry_counts'].get('WATCH', 0)}\n"
            f"BUY 1D average: {ret} (n={buy['sample_size']})\n"
            f"Top-10 matched SPY / QQQ: "
            f"{f'{spy:+.2f}%' if spy is not None else 'NOT MATURED'} / "
            f"{f'{qqq:+.2f}%' if qqq is not None else 'NOT MATURED'}\n"
            f"AI local estimated cost: ${report['ai']['luna_cost_usd']:.4f}; Sol calls 0\n"
            f"Observations: {report['observations'][0]['text']}; "
            f"{report['observations'][1]['text']}\n"
            "Descriptive only. No strategy change or trade.")


def tick(state_path, results_path, stock_path, token, chat_id, sender, db_path=DB):
    """No market or AI access. Called by the existing Stock command listener."""
    if not Path(state_path).is_file():
        return 0
    with closing(connect(db_path)) as db:
        count = completed_days(db, state_path)
        days = [row[0] for row in db.execute("SELECT trading_date FROM completed_shadow_trading_days ORDER BY trading_date")]
        with closing(sqlite3.connect(Path(state_path).resolve().as_uri() + "?mode=ro", uri=True)) as state:
            sent_days = {row[0] for row in state.execute("""SELECT trading_date FROM shadow_daily_runs
                WHERE state='COMPLETE' AND telegram_status='SENT' AND telegram_message_id IS NOT NULL""")}
        for start in range(0, len(days) - 4, 5):
            window = days[start:start + 5]
            if window[-1] not in sent_days:
                continue
            review_id = f"SHADOW_5D:{window[0]}:{window[-1]}"
            if not db.execute("SELECT 1 FROM shadow_5d_reviews WHERE review_id=?", (review_id,)).fetchone():
                report = build_review(window, state_path, results_path, stock_path)
                out = Path(db_path).parent / "shadow_reviews"
                out.mkdir(parents=True, exist_ok=True)
                artifact = out / f"{review_id.replace(':', '_')}.json"
                temp = artifact.with_suffix(".json.tmp")
                temp.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
                temp.replace(artifact)
                with db:
                    db.execute("""INSERT OR IGNORE INTO shadow_5d_reviews
                        (review_id,window_start,window_end,trading_dates_json,generated_at,report_json,telegram_status)
                        VALUES(?,?,?,?,?,?,'PENDING')""",
                        (review_id, window[0], window[-1], json.dumps(window), stamp(),
                         json.dumps(report, sort_keys=True)))
        for row in db.execute("SELECT review_id,report_json FROM shadow_5d_reviews WHERE telegram_status='PENDING'"):
            report = json.loads(row[1])
            with db:
                claimed = db.execute("""UPDATE shadow_5d_reviews SET telegram_status='IN_FLIGHT',attempted_at=?
                    WHERE review_id=? AND telegram_status='PENDING'""", (stamp(), row[0])).rowcount
            if not claimed:
                continue
            try:
                message_id = sender(token, chat_id, review_message(report))
                if not isinstance(message_id, int) or isinstance(message_id, bool) or message_id <= 0:
                    raise ValueError("REVIEW_TELEGRAM_UNCONFIRMED")
            except Exception:
                with db:
                    db.execute("""UPDATE shadow_5d_reviews SET telegram_status='UNKNOWN',completed_at=?
                        WHERE review_id=?""", (stamp(), row[0]))
            else:
                with db:
                    db.execute("""UPDATE shadow_5d_reviews SET telegram_status='SENT',
                        telegram_message_id=?,completed_at=? WHERE review_id=?""",
                        (message_id, stamp(), row[0]))
        return count


def mark_interrupted_sends_unknown(db_path=DB):
    with closing(connect(db_path)) as db, db:
        db.execute("""UPDATE shadow_5d_reviews SET telegram_status='UNKNOWN',completed_at=?
            WHERE telegram_status='IN_FLIGHT'""", (stamp(),))

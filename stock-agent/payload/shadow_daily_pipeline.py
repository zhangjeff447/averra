#!/usr/bin/env python3
"""Date-scoped Shadow Day 2-5 pipeline; called only after the daily atomic lock."""
from __future__ import annotations

import json
import sqlite3
import time
import zlib
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from alpaca_market import AlpacaData, SOURCE, credentials, sync as alpaca_sync
from day0_1_research import SCHEMA as INITIAL_SCHEMA, configuration as research_configuration
from day0_2_plan import score_case
from market_regime import evaluate, market_session
from phase3_run import init_db as init_fundamental, run as run_fundamental
from phase3_sec import SECClient, load_user_agent
from phase4_2_evidence import collect_ticker
from phase4_run import init_db as init_research, prepare, record_pre
from phase4_tiers import init_db as init_tiers
from phase4b_evidence import cached_inputs, estimate, request_body
from phase5_entry import (cache_intraday, current_regime, daily_bars, decide,
                          init_db as init_entry, load_config as entry_config, save_decision)
from holdings_monitor import evaluate_all
from price_plan import buy_plan, reference_status
from portfolio import Portfolio
from quant_ranking import load_config as quant_config, scan
from quant_screen import ROOT, screen
from real_report import report as quant_report
from shadow_day1_entry import intraday_return, sync_5m
from shadow_day1_initial import current_report, run_one
from shadow_day1_research import build_plan
from shadow_schedule import authorized_day, shadow_day_number
from market_history import check_recent_history, repair_recent_history
from shadow_v11 import (apply_limited_entry, classify_research,
                        entry_data_quality, load_config as v11_config)
from shadow_evaluation import capture_manifest

ET = ZoneInfo("America/New_York")
HORIZONS = (1, 5, 10, 20, 60)
RESULTS_DB = ROOT / "data" / "shadow_daily_results.db"


def paths(day):
    if not authorized_day(day):
        raise ValueError("UNAUTHORIZED_SHADOW_DATE")
    base = ROOT / "data" / "shadow_daily" / day
    base.mkdir(parents=True, exist_ok=True)
    return base


def save(path, item):
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(item, indent=2, ensure_ascii=False) + "\n")
    temp.replace(path)


def market_update(day):
    started = time.monotonic()
    market = ROOT / "data" / "market_data.db"
    with sqlite3.connect(market) as db:
        prior = db.execute("""SELECT status FROM market_sync_runs WHERE asof=? AND source='ALPACA_SIP'
            ORDER BY id DESC LIMIT 1""", (day,)).fetchone()
        needed = ("SPY", "QQQ", "XLK", "XLF", "XLV", "XLY", "XLP", "XLI",
                  "XLE", "XLU", "XLB", "XLRE", "XLC")
        already = bool(prior and prior[0] == "COMPLETE" and all(
            db.execute("SELECT 1 FROM daily_bars WHERE ticker=? AND date=? AND source=?",
                       (ticker, day, SOURCE)).fetchone() for ticker in needed))
    stats = ({"status": "CACHED", "requests": 0, "rows": 0, "date": day}
             if already else alpaca_sync(day, market))
    history = repair_recent_history(day, market)
    stats["history"] = history
    stats["requests"] += history["requests"]
    stats["rows"] += history["rows"]
    with sqlite3.connect(market) as db:
        db.row_factory = sqlite3.Row
        volatility_cfg = json.loads((ROOT / "real_market_config.json").read_text())["volatility"]
        vol = evaluate(db, day, datetime.now(timezone.utc), volatility_cfg)
    save(paths(day) / "market_regime.json", vol)
    stats["volatility_source"] = vol["regime_source"]
    stats["runtime_seconds"] = round(time.monotonic() - started, 2)
    return stats


def data_ready(day, now=None, *, require_session_date=True):
    now = (now or datetime.now(timezone.utc)).astimezone(ET)
    if not authorized_day(day) or (require_session_date and
            (now.date().isoformat() != day or now.hour * 60 + now.minute < 16 * 60 + 20)):
        return False
    with sqlite3.connect(f"file:{ROOT / 'data' / 'market_data.db'}?mode=ro", uri=True) as db:
        needed = ("SPY", "QQQ", "XLK", "XLF", "XLV", "XLY", "XLP", "XLI",
                  "XLE", "XLU", "XLB", "XLRE", "XLC")
        if any(not (row := db.execute("SELECT source FROM daily_bars WHERE ticker=? AND date=?",
                                      (ticker, day)).fetchone()) or row[0] != SOURCE
               for ticker in needed):
            return False
        if any(db.execute("SELECT MAX(date) FROM volatility_daily WHERE symbol=?",
                          (name,)).fetchone()[0] != day for name in ("VIX", "VXN")):
            return False
        sync = db.execute("""SELECT status FROM market_sync_runs WHERE asof=? AND source=?
            ORDER BY id DESC LIMIT 1""", (day, SOURCE)).fetchone()
        return bool(sync and sync[0] == "COMPLETE" and
                    check_recent_history(db, day)["status"] == "HISTORY_READY")


def quant_and_fundamental(day):
    with sqlite3.connect(ROOT / "data" / "market_data.db", timeout=60) as market:
        market.row_factory = sqlite3.Row
        existing = market.execute("""SELECT id FROM quant_runs WHERE asof=? AND data_mode='live'
            ORDER BY id DESC LIMIT 1""", (day,)).fetchone()
        if existing:
            quant_id = existing[0]
            if market.execute("SELECT COUNT(*) FROM quant_scores WHERE run_id=?", (quant_id,)).fetchone()[0] < 100:
                raise ValueError("SAVED_QUANT_RUN_INCOMPLETE")
            phase1 = None
        else:
            phase1 = screen(market, day, top=10000)
            phase2 = scan(market, day, quant_config(ROOT / "quant_config.json"),
                          data_mode="live", top=100)
            quant_id = phase2["run_id"]
        ranked = quant_report(market, quant_id)
        save(paths(day) / "quant.json", {"trading_date": day, "phase1_passed":
             phase1["screened_count"] if phase1 else None, "quant_run_id": quant_id, **ranked})
        with sqlite3.connect(ROOT / "data" / "fundamental_data.db", timeout=60) as fundamental:
            fundamental.row_factory = sqlite3.Row
            init_fundamental(fundamental)
            existing = fundamental.execute("""SELECT id,processed_count FROM fundamental_runs
                WHERE scan_date=? AND quant_run_id=? ORDER BY id DESC LIMIT 1""",
                (day, quant_id)).fetchone()
            if existing:
                if existing[1] != 100:
                    raise ValueError("SAVED_FUNDAMENTAL_RUN_INCOMPLETE")
                fundamental_id = existing[0]
                output = {"status": "CACHED", "fundamental_run_id": fundamental_id,
                          "scan_date": day, "api_requests": 0, "cache_hits": 100}
            else:
                cfg = json.loads((ROOT / "phase3_config.json").read_text())
                client = SECClient(load_user_agent(ROOT / ".env"), cfg["source"])
                output = run_fundamental(market, fundamental, cfg, client)
                fundamental_id = output["fundamental_run_id"]
            save(paths(day) / "fundamental.json", output)
    return {"quant_run_id": quant_id, "fundamental_run_id": fundamental_id,
            "phase1_passed": phase1["screened_count"] if phase1 else None,
            "quant_top100_count": len(ranked["overall_top100"]),
            "candidate_top30_count": 30,
            "phase3_api_requests": output.get("api_requests", 0),
            "phase3_cache_hits": output.get("cache_hits", 0)}


def _new_filing(fundamental, market, ticker, evidence_asof):
    cik = market.execute("SELECT cik FROM securities WHERE ticker=?", (ticker,)).fetchone()
    if not cik or not cik[0]:
        return True
    row = fundamental.execute("""SELECT payload FROM sec_cache WHERE cik=? AND endpoint='submissions'""",
                              (str(int(cik[0])).zfill(10),)).fetchone()
    if not row:
        return True
    recent = json.loads(zlib.decompress(row[0])).get("filings", {}).get("recent", {})
    cutoff = datetime.fromisoformat(evidence_asof.replace("Z", "+00:00"))
    if cutoff.tzinfo is None:
        cutoff = cutoff.replace(tzinfo=timezone.utc)
    for stamp in recent.get("acceptanceDateTime", []):
        accepted = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        if accepted.tzinfo is None:
            accepted = accepted.replace(tzinfo=timezone.utc)
        if accepted > cutoff:
            return True
    return False


def research_priority_and_evidence(day):
    cutoff = datetime.now(timezone.utc)
    with sqlite3.connect(ROOT / "data" / "market_data.db") as market, \
         sqlite3.connect(ROOT / "data" / "fundamental_data.db") as fundamental, \
         sqlite3.connect(ROOT / "data" / "research_data.db") as research:
        for db in (market, fundamental, research):
            db.row_factory = sqlite3.Row
        init_research(research)
        prior = research.execute("""SELECT id FROM research_runs WHERE candidate_scan_date=?
            ORDER BY id DESC LIMIT 1""", (day,)).fetchone()
        if prior:
            run_id = prior[0]
        else:
            cfg = json.loads((ROOT / "phase4_config.json").read_text())
            source, prepared = prepare(market, fundamental, research, cfg, cutoff)
            if source["scan_date"] != day:
                raise ValueError("RESEARCH_PRIORITY_DATE_MISMATCH")
            run_id = record_pre(research, source, prepared, cfg, cutoff, "pre")
        names = [r[0] for r in research.execute("""SELECT ticker FROM research_priority
            WHERE run_id=? AND priority_rank<=15 ORDER BY priority_rank""", (run_id,))]
        if len(names) != 15:
            raise ValueError("AI_PRIORITY_TOP15_INCOMPLETE")
        book = Portfolio(ROOT / "data" / "stock_agent.db")
        try:
            tracked = [row[0] for row in book.db.execute(
                "SELECT ticker FROM tracked_holdings WHERE status='HELD' ORDER BY ticker")]
        finally:
            book.close()
        cfg = json.loads((ROOT / "phase4_2_config.json").read_text())
        user_agent = load_user_agent(ROOT / ".env")
        refreshed = []; cached = []; requests = 0
        prior_names = {r[0] for r in research.execute("""SELECT ticker FROM research_priority
            WHERE run_id=(SELECT MAX(id) FROM research_runs WHERE candidate_scan_date<?)
            AND priority_rank<=15""", (day,))}
        held_errors = {}
        for ticker in list(dict.fromkeys([*names, *tracked])):
            row = research.execute("""SELECT evidence_asof FROM evidence_packets_v2 WHERE ticker=?
                ORDER BY evidence_asof DESC LIMIT 1""", (ticker,)).fetchone()
            stale = (not row or (cutoff - datetime.fromisoformat(row[0])).total_seconds() >
                     v11_config()["evidence_source_check_hours"] * 3600)
            changed = bool(row and _new_filing(fundamental, market, ticker, row[0]))
            if ticker not in prior_names or stale or changed:
                try:
                    result = collect_ticker(market, fundamental, research, ticker,
                                            cutoff.isoformat(), cfg, user_agent)
                    refreshed.append(ticker)
                    requests += result["network_requests"]
                except (ValueError, RuntimeError, sqlite3.Error, OSError) as exc:
                    if ticker in names:
                        raise
                    held_errors[ticker] = type(exc).__name__
            else:
                cached.append(ticker)
        summary = {"run_id": run_id, "top15": names, "evidence_refreshed": refreshed,
                   "evidence_cached": cached, "evidence_network_requests": requests,
                   "tracked_holdings": tracked, "held_evidence_errors": held_errors}
        save(paths(day) / "evidence.json", summary)
        return summary


def _limits(day, policy):
    return {"run_id": f"SHADOW_DAILY_{day.replace('-', '')}_INITIAL",
            "initial_model": "gpt-5.6-luna", "web_search_enabled": False,
            **{key: policy[key] for key in ("max_daily_initial_calls",
                 "max_daily_initial_cost_usd", "max_daily_initial_input_tokens",
                 "max_daily_initial_output_tokens", "max_input_tokens_per_stock",
                 "max_cost_per_stock_usd", "evidence_max_age_hours",
                 "candidate_rank_refresh_threshold", "minimum_evidence_coverage_pct")}}


def initial_research(day, policy):
    from phase4_run import load_key

    cfg, frozen = research_configuration()
    limits = _limits(day, policy)
    with sqlite3.connect(ROOT / "data" / "research_data.db") as db, \
         sqlite3.connect(ROOT / "data" / "fundamental_data.db") as fundamental:
        db.row_factory = fundamental.row_factory = sqlite3.Row
        plan = build_plan(db, fundamental, cfg, frozen, limits, day=day)
        save(paths(day) / "research_plan.json", plan)
        if plan["estimated_calls"] > 10 or plan["estimated_cost_usd"] > .05:
            raise ValueError("DAILY_INITIAL_PLAN_EXCEEDS_BUDGET")
        db.executescript(INITIAL_SCHEMA)
        init_tiers(db)
        if db.execute("SELECT 1 FROM day01_runs WHERE id=?", (plan["run_id"],)).fetchone():
            raise ValueError("DAILY_RESEARCH_ALREADY_STARTED")
        with db:
            db.execute("INSERT INTO day01_runs VALUES(?,?,?,?,?,?,?,?,NULL)",
                (plan["run_id"], plan["source_research_run"], plan["model"], "ACTIVE",
                 plan["max_calls"], plan["max_cost_usd"], plan["plan_hash"],
                 datetime.now(timezone.utc).isoformat()))
            for case in plan["cases"]:
                db.execute("""INSERT INTO day01_states VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (plan["run_id"], case["ticker"], case["cache_status"], case["reason"],
                     case["prior_completed_at"], case["evidence_asof"], None,
                     case["candidate_score"], int(case["material_evidence_change"]),
                     case["evidence_coverage"], case["evidence_confidence"],
                     case["evidence_tokens"], "NOT_RUN", "NOT_RUN"))
        key = load_key(ROOT / ".env") if plan["estimated_calls"] else None
        if plan["estimated_calls"] and not key:
            raise ValueError("OPENAI_API_KEY_MISSING")
        names = [case["ticker"] for case in plan["cases"]]
        for case in plan["cases"]:
            if case["required_action"] == "INITIAL_RESEARCH":
                state = run_one(db, plan, case, cfg, frozen, limits, key, names)
                if state == "RESEARCH_DEFERRED_BUDGET":
                    case["required_action"] = state
        report = current_report(db, plan)
        with db:
            db.execute("UPDATE day01_runs SET status='CLOSED',closed_at=? WHERE id=?",
                       (datetime.now(timezone.utc).isoformat(), plan["run_id"]))
            for case in report["cases"]:
                db.execute("UPDATE day01_states SET research_status=?,final_status=? WHERE run_id=? AND ticker=?",
                           (case["required_action"], case["final_status"], plan["run_id"], case["ticker"]))
                db.execute("""INSERT INTO tier_final_results
                    (run_id,ticker,final_research_tier,status,source_tier,final_research_score,
                     final_catalyst_score,final_thesis,final_confidence,decided_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (plan["run_id"], case["ticker"], case["final_research_tier"] or "UNAVAILABLE",
                     case["final_status"], case["final_research_tier"], case["research_score"],
                     case["catalyst_score"], None, None, datetime.now(timezone.utc).isoformat()))
        save(paths(day) / "research.json", report)
        return report


def research_eligibility(day, research_report, policy):
    plan = json.loads((paths(day) / "research_plan.json").read_text())
    planned = {c["ticker"]: c for c in plan["cases"]}
    with sqlite3.connect(ROOT / "data" / "research_data.db") as db:
        db.row_factory = sqlite3.Row
        results = []
        for case in research_report["cases"]:
            ticker = case["ticker"]
            prior = planned[ticker]
            if prior["cache_status"] == "RESEARCH_FAILED_CACHE":
                results.append({**case, "eligibility": "RESEARCH_FAILED",
                    "final_status": "RESEARCH_FAILED", "final_research_tier": None,
                    "research_quality_tier": None, "research_score": None,
                    "catalyst_score": None, "conviction_score": None,
                    "effective_conviction": None, "entry_observation_allowed": False,
                    "reasons": [prior["reason"]], "error_class": "CACHE_INTEGRITY_FAILED",
                    "candidate_rank": prior["candidate_rank"], "research_completed_at": None,
                    "evidence_asof": prior["evidence_asof"]})
                continue
            source = json.loads(db.execute("""SELECT packet_json FROM research_priority
                WHERE run_id=? AND ticker=?""", (plan["source_research_run"], ticker)).fetchone()[0])
            raw = db.execute("""SELECT packet_json FROM evidence_packets_v2
                WHERE ticker=? AND evidence_asof=? ORDER BY created_at DESC LIMIT 1""",
                (ticker, prior["evidence_asof"])).fetchone()
            if not raw:
                raise ValueError(f"{ticker}: EVIDENCE_MISSING")
            evidence = json.loads(raw[0])
            current = db.execute("""SELECT state,parsed_result_json,error_class FROM day01_attempts
                WHERE run_id=? AND ticker=?""", (plan["run_id"], ticker)).fetchone()
            parsed = (json.loads(current[1]) if current and current[1] else
                      prior.get("prior_result") or {})
            status = (current[0] if current else prior.get("prior_status") or "UNAVAILABLE")
            fail_stamp = db.execute("""SELECT MAX(finished_at) FROM day01_attempts
                WHERE ticker=? AND state='RESEARCH_FAILED_GROUNDING'""", (ticker,)).fetchone()[0]
            complete_stamp = db.execute("""SELECT MAX(finished_at) FROM day01_attempts
                WHERE ticker=? AND state='COMPLETE'""", (ticker,)).fetchone()[0]
            grounding = bool(fail_stamp and (not complete_stamp or fail_stamp > complete_stamp))
            handled = set(parsed.get("score_evidence_ids") or []) | set(parsed.get("catalyst_evidence_ids") or [])
            claims = parsed.get("claims") or {}
            for group in claims.values() if isinstance(claims, dict) else [claims]:
                for claim in group:
                    handled.update(claim.get("evidence_ids", []))
            overrides = db.execute("""SELECT event_type FROM research_overrides WHERE ticker=?
                AND event_date<=?""", (ticker, day)).fetchall()
            candidate = {"quant_score": source["quant"]["score"],
                         "fundamental_score": source["fundamental"]["score"],
                         "candidate_score": case["candidate_score"],
                         "grounding_failure": grounding,
                         "confirmed_material_negative_override": bool(overrides)}
            initial = {"research_status": parsed.get("research_status", status),
                       "research_score": parsed.get("research_score"),
                       "conviction_score": parsed.get("conviction_score"),
                       "evidence_conflicts": parsed.get("evidence_conflicts", []),
                       "handled_evidence_ids": sorted(handled),
                       "error_class": current[2] if current else None}
            esc = case.get("escalation") or prior.get("prior_escalation")
            eligibility = classify_research(candidate, evidence, initial, esc, policy)
            results.append({**case, **eligibility, "candidate_rank": prior["candidate_rank"],
                            "research_completed_at": case["research_completed_at"],
                            "evidence_asof": prior["evidence_asof"]})
    counts = dict(Counter(r["eligibility"] for r in results))
    summary = {"run_id": plan["run_id"], "trading_date": day, "counts": counts,
               "cases": results, "sol_calls": 0,
               "deep_pending": sum(r["eligibility"] == "DEEP_REQUIRED" for r in results)}
    save(paths(day) / "eligibility.json", summary)
    return summary


def deep_triage(day, eligibility, policy):
    pending = [c for c in eligibility["cases"] if c["eligibility"] == "DEEP_REQUIRED"]
    names = [c["ticker"] for c in eligibility["cases"]]
    with sqlite3.connect(ROOT / "data" / "research_data.db") as research:
        research.row_factory = sqlite3.Row
        sectors = Counter()
        run_id = research.execute("SELECT id FROM research_runs WHERE candidate_scan_date=? ORDER BY id DESC LIMIT 1",
                                  (day,)).fetchone()[0]
        source_packets = {}
        for ticker in names:
            packet = json.loads(research.execute("SELECT packet_json FROM research_priority WHERE run_id=? AND ticker=?",
                (run_id, ticker)).fetchone()[0])
            source_packets[ticker] = packet
            sectors[packet.get("sector") or "UNKNOWN"] += 1
        rows = []
        for case in pending:
            ticker = case["ticker"]
            source = source_packets[ticker]
            evidence = json.loads(research.execute("""SELECT packet_json FROM evidence_packets_v2
                WHERE ticker=? ORDER BY evidence_asof DESC LIMIT 1""", (ticker,)).fetchone()[0])
            attempt = research.execute("""SELECT parsed_result_json FROM day01_attempts
                WHERE run_id=? AND ticker=?""", (eligibility["run_id"], ticker)).fetchone()
            parsed = json.loads(attempt[0]) if attempt and attempt[0] else {}
            score, components, group, reason, factors = score_case(
                case["candidate_score"], source["quant"]["score"], source["fundamental"]["score"],
                case["candidate_rank"], sectors[source.get("sector") or "UNKNOWN"], evidence,
                parsed, case.get("escalation") or {})
            rows.append({"ticker": ticker, "score": score, "group": group,
                         "reason": reason, "evidence_coverage_pct": evidence["coverage_pct"],
                         "factors": factors, "components": components})
        rows.sort(key=lambda r: (-r["score"], r["ticker"]))
        for row in [r for r in rows if r["group"] == "P1_DEEP_NOW"][3:]:
            row["group"] = "P2_DEEP_LATER"
            row["reason"] = "P1 capped at three; no automatic Sol call"
        cfg, _ = research_configuration()
        sol_cfg = {**cfg, "model": "gpt-6-sol",
                   "model_input_usd_per_million": policy["deep_cost_estimate_input_usd_per_million"],
                   "model_output_usd_per_million": policy["deep_cost_estimate_output_usd_per_million"]}
        for row in rows:
            if row["group"] != "P1_DEEP_NOW":
                continue
            inputs = cached_inputs(research, row["ticker"], sol_cfg,
                                   allowed_tickers=names, citation_mode="short")
            forecast = estimate(request_body(inputs, sol_cfg), sol_cfg)
            row["estimated_sol_cost_usd"] = forecast["estimated_total_cost_usd"]
            row["estimated_sol_input_tokens"] = forecast["estimated_input_tokens"]
            row["estimated_sol_output_tokens"] = forecast["estimated_output_tokens"]
        out = {"sol_calls": 0, "deep_pending": len(rows),
               "p1_count": sum(r["group"] == "P1_DEEP_NOW" for r in rows),
               "p1_estimated_sol_cost_usd": round(sum(r.get("estimated_sol_cost_usd", 0)
                                                       for r in rows), 6), "rows": rows}
    save(paths(day) / "deep_plan.json", out)
    return out


def next_session(day):
    target = date.fromisoformat(day) + timedelta(days=1)
    while not market_session(target):
        target += timedelta(days=1)
    return target.isoformat()


def observe_entry_references(asof, *, market_path=None, entry_path=None):
    """Persist next-session OHLC against prior close plans; no trading decision."""
    import math

    market_path = market_path or ROOT / "data" / "market_data.db"
    entry_path = entry_path or ROOT / "data" / "entry_data.db"
    with sqlite3.connect(f"file:{market_path}?mode=ro", uri=True) as market, sqlite3.connect(entry_path) as entry:
        init_entry(entry)
        plans = entry.execute("""SELECT run_id,ticker,target_trading_date,preferred_low,preferred_high,
            do_not_chase_above,plan_json FROM entry_reference_plans
            WHERE target_trading_date<=? AND NOT EXISTS (
                SELECT 1 FROM entry_reference_observations o
                WHERE o.run_id=entry_reference_plans.run_id AND o.ticker=entry_reference_plans.ticker)
            ORDER BY target_trading_date,ticker""", (asof,)).fetchall()
        count = 0
        with entry:
            for run_id, ticker, target, low, high, ceiling, plan_json in plans:
                bar = market.execute("""SELECT open,high,low,close,source,quality_status FROM daily_bars
                    WHERE ticker=? AND date=?""", (ticker, target)).fetchone()
                if (not bar or bar[4] != SOURCE or bar[5] not in {"OK", "VALID"} or
                        not all(isinstance(value, (int, float)) and math.isfinite(value) and value > 0
                                for value in bar[:4]) or bar[2] > min(bar[0], bar[3]) or
                        bar[1] < max(bar[0], bar[3])):
                    continue
                plan = json.loads(plan_json)
                touched = bar[2] <= high and bar[1] >= low
                exceeded = bar[1] > ceiling
                entry.execute("""INSERT OR IGNORE INTO entry_reference_observations
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (run_id, ticker, target, *bar[:4], bar[4], bar[5], int(touched),
                     int(exceeded), reference_status(plan, bar[0]),
                     datetime.now(timezone.utc).isoformat()))
                count += 1
    return count


def close_entry(day, eligible, policy):
    started = time.monotonic()
    target = next_session(day)
    market = sqlite3.connect(f"file:{ROOT / 'data' / 'market_data.db'}?mode=ro", uri=True)
    entry = sqlite3.connect(ROOT / "data" / "entry_data.db")
    market.row_factory = entry.row_factory = sqlite3.Row
    init_entry(entry)
    if entry.execute("SELECT 1 FROM entry_runs WHERE asof=? AND source=?",
                     (day, SOURCE)).fetchone():
        raise ValueError("DAILY_ENTRY_ALREADY_STARTED")
    cfg = entry_config()
    quant_params = json.loads((ROOT / "quant_config.json").read_text())["feature_parameters"]
    observed = sorted((c for c in eligible["cases"] if c["entry_observation_allowed"]),
                      key=lambda c: (-c["candidate_score"], c["ticker"]))[:cfg["candidate_limit"]]
    sectors = {c["ticker"]: market.execute("SELECT mapped_sector_etf FROM securities WHERE ticker=?",
                                            (c["ticker"],)).fetchone()[0] for c in observed}
    names = sorted({"SPY", "QQQ", *sectors.values(), *(c["ticker"] for c in observed)} - {None})
    client = AlpacaData(credentials())
    with entry:
        run_id = entry.execute("""INSERT INTO entry_runs(asof,started_at,status,source,config_json)
            VALUES(?,?,?,?,?)""", (day, datetime.now(timezone.utc).isoformat(),
                                     "RUNNING", SOURCE, json.dumps(cfg, sort_keys=True))).lastrowid
    rows = 0; errors = {}
    for ticker in names:
        try:
            count, _ = sync_5m(entry, client, ticker, asof=day)
            rows += count
        except (RuntimeError, ValueError, KeyError, TypeError) as exc:
            errors[ticker] = "MARKET_DATA:" + type(exc).__name__
    regime, regime_source = current_regime(market, day)
    benchmarks = {ticker: intraday_return(market, entry, ticker, day)
                  for ticker in ("SPY", "QQQ")}
    outputs = []
    for case in observed:
        ticker = case["ticker"]
        daily = daily_bars(market, entry, ticker, day)
        intraday = cache_intraday(entry, ticker, day)
        quality = entry_data_quality(daily, intraday, day, cfg, policy)
        if ticker in errors:
            quality["status"] = "ENTRY_DATA_INSUFFICIENT"
            quality["reasons"].append(errors[ticker])
        if quality["status"] != "READY":
            result = {"ticker": ticker, "entry_state": "ENTRY_DATA_INSUFFICIENT",
                      "entry_score": None, "signal_validity": "DATA_INSUFFICIENT",
                      "why_wait": quality["reasons"], "data_quality": quality,
                      "target_session": target}
        else:
            basis_at = (case["research_completed_at"] if case["research_quality_tier"] == "FULL_RESEARCH"
                        else case["evidence_asof"])
            if not basis_at:
                raise ValueError(f"{ticker}: DECISION_BASIS_TIMESTAMP_MISSING")
            reference = {"candidate_score": case["candidate_score"],
                         "research_score": case["research_score"],
                         "conviction_score": case["effective_conviction"],
                         "created_at": basis_at}
            try:
                result = decide(daily, intraday, benchmarks,
                                intraday_return(market, entry, sectors[ticker], day)
                                if sectors[ticker] else None,
                                reference, regime, cfg, quant_params, mode="CLOSE")
                result = apply_limited_entry(result, case, cfg, policy)
                result["buy_plan"] = buy_plan({**result, "target_session": target}, cfg["thresholds"])
                if (result["entry_state"] == "BUY_TOMORROW_CANDIDATE" and
                        (not result["buy_plan"] or result["buy_plan"].get("status") != "CONDITIONAL")):
                    raise ValueError("BUY_REFERENCE_ZONE_UNAVAILABLE")
                if result["entry_state"] == "BUY_NOW_CANDIDATE":
                    raise ValueError("BUY_NOW_DISABLED_NO_REALTIME_SIP")
                result.update(ticker=ticker, target_session=target, data_quality=quality)
                completed = datetime.fromisoformat(basis_at.replace("Z", "+00:00"))
                close = datetime.fromisoformat(day + "T16:00:00").replace(tzinfo=ET)
                result["decision_basis_at"] = basis_at
                result["decision_basis_type"] = ("RESEARCH_RESULT" if case["research_quality_tier"] ==
                                                 "FULL_RESEARCH" else "LIMITED_EVIDENCE")
                result["research_timing"] = "POST_CLOSE_RESEARCH" if completed.astimezone(ET) > close else "PRE_CLOSE_RESEARCH"
                result["signal_validity"] = ("NEXT_SESSION_CANDIDATE" if result["research_timing"] ==
                                              "POST_CLOSE_RESEARCH" else "POINT_IN_TIME")
            except (ValueError, TypeError, KeyError, ZeroDivisionError) as exc:
                result = {"ticker": ticker, "entry_state": "ENTRY_DATA_INSUFFICIENT",
                          "entry_score": None, "signal_validity": "DATA_INSUFFICIENT",
                          "why_wait": ["ENTRY_CALCULATION:" + type(exc).__name__],
                          "data_quality": quality, "target_session": target}
                errors[ticker] = result["why_wait"][0]
        result["research_quality_tier"] = case["research_quality_tier"]
        save_decision(entry, run_id, ticker, result, case)
        outputs.append(result)
    with entry:
        entry.execute("""UPDATE entry_runs SET ended_at=?,status=?,api_requests=?,rows_downloaded=?
            WHERE id=?""", (datetime.now(timezone.utc).isoformat(),
                           "COMPLETE" if not errors else "PARTIAL", client.requests, rows, run_id))
    outcome = {"run_id": run_id, "trading_date": day, "target_session": target,
               "source": SOURCE, "market_regime": regime, "regime_source": regime_source,
               "results": outputs, "errors": errors, "api_requests": client.requests,
               "downloaded_5m_rows": rows,
               "runtime_seconds": round(time.monotonic() - started, 2)}
    save(paths(day) / "entry.json", outcome)
    market.close(); entry.close()
    return outcome


def portfolio_and_forward(day, eligible, entry_report, policy):
    from exit_notifications import position_summary_text
    market = sqlite3.connect(f"file:{ROOT / 'data' / 'market_data.db'}?mode=ro", uri=True)
    fundamental = sqlite3.connect(f"file:{ROOT / 'data' / 'fundamental_data.db'}?mode=ro", uri=True)
    market.row_factory = fundamental.row_factory = sqlite3.Row
    book = Portfolio(ROOT / "data" / "stock_agent.db")
    holding_decisions = evaluate_all(book, market, day, ROOT / "data" / "research_data.db")
    by_ticker = {c["ticker"]: c for c in eligible["cases"]}
    latest = fundamental.execute("SELECT id FROM fundamental_runs WHERE scan_date=? ORDER BY id DESC LIMIT 1",
                                 (day,)).fetchone()[0]
    candidates = fundamental.execute("""SELECT ticker,candidate_rank,quant_score,
        fundamental_score,candidate_score FROM fundamental_scores WHERE run_id=?
        AND candidate_rank<=30 ORDER BY candidate_rank""", (latest,)).fetchall()
    if len(candidates) != 30:
        raise ValueError("CANDIDATE_TOP30_INCOMPLETE")
    entry_by = {r["ticker"]: r for r in entry_report["results"]}
    with sqlite3.connect(RESULTS_DB) as snapshots:
        snapshots.executescript((ROOT / "shadow_daily_results.sql").read_text())
        if snapshots.execute("SELECT 1 FROM snapshots WHERE trading_date=?", (day,)).fetchone():
            raise ValueError("FORWARD_ALREADY_SAVED")
        with snapshots:
            for row in candidates:
                ticker = row[0]
                bar = market.execute("SELECT close,source FROM daily_bars WHERE ticker=? AND date=?",
                                     (ticker, day)).fetchone()
                if not bar or bar[1] != SOURCE:
                    raise ValueError(f"{ticker}: DAILY_SOURCE_INVALID")
                research = by_ticker.get(ticker)
                entry = entry_by.get(ticker)
                quality_tier = research["research_quality_tier"] if research else None
                snapshots.execute("""INSERT INTO snapshots VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (day, ticker, row[1], row[2], row[3], row[4],
                     research["eligibility"] if research else "NOT_RESEARCHED", quality_tier,
                     research["research_score"] if research else None,
                     research["effective_conviction"] if research else None,
                     entry.get("entry_score") if entry else None,
                     entry["entry_state"] if entry else None,
                     bar[0], bar[1], datetime.now(timezone.utc).isoformat()))
                snapshots.executemany("""INSERT INTO forward
                    (trading_date,ticker,horizon,research_quality_tier,status)
                    VALUES(?,?,?,?,'PENDING')""",
                    [(day, ticker, horizon, quality_tier) for horizon in HORIZONS])
    output = {"tracked_count": len(holding_decisions),
              "holding_decisions": [{**item, "reason": position_summary_text(item)}
                                    for item in holding_decisions], "forward_snapshots": 30,
              "forward_pending": 30 * len(HORIZONS)}
    save(paths(day) / "portfolio.json", output)
    book.close(); market.close(); fundamental.close()
    return output


def forward_metrics(stock_anchor, spy_anchor, qqq_anchor, stock_bars, spy_end, qqq_end):
    if not stock_bars or min(stock_anchor, spy_anchor, qqq_anchor, spy_end, qqq_end) <= 0:
        raise ValueError("FORWARD_PRICE_INVALID")
    stock_return = (stock_bars[-1]["close"] / stock_anchor - 1) * 100
    spy_return = (spy_end / spy_anchor - 1) * 100
    qqq_return = (qqq_end / qqq_anchor - 1) * 100
    return (round(stock_return, 4), round(stock_return - spy_return, 4),
            round(stock_return - qqq_return, 4),
            round((max(r["high"] for r in stock_bars) / stock_anchor - 1) * 100, 4),
            round((min(r["low"] for r in stock_bars) / stock_anchor - 1) * 100, 4))


def advance_forward(asof, *, audit_only=False):
    """Fill calendar-aligned horizons; quarantine ambiguous prices, preserve history."""
    import math
    from contextlib import closing

    market = sqlite3.connect(f"file:{ROOT / 'data' / 'market_data.db'}?mode=ro", uri=True)
    market.row_factory = sqlite3.Row
    updated = {"day1": 0, "daily": 0, "quality_issues": []}
    jump_limit = json.loads((ROOT / "phase26_config.json").read_text())["corporate_actions"]["jump_threshold"]
    calendars = {}
    for db_path, flavor in ((ROOT / "data" / "shadow_day1_results.db", "day1"),
                            (RESULTS_DB, "daily")):
        if not db_path.exists():
            continue
        with closing(sqlite3.connect(db_path)) as db:
            if flavor == "day1":
                observations = db.execute("""SELECT f.run_id,f.ticker,s.trading_date,f.horizon,
                    s.close_price,s.spy_close,s.qqq_close,s.market_data_source,f.status,f.end_date
                    FROM shadow_day1_forward f
                    JOIN shadow_day1_snapshots s ON s.run_id=f.run_id AND s.ticker=f.ticker
                    WHERE s.trading_date<?""", (asof,)).fetchall()
            else:
                observations = db.execute("""SELECT NULL,f.ticker,f.trading_date,f.horizon,
                    s.close_price,NULL,NULL,s.market_data_source,f.status,f.end_date
                    FROM forward f JOIN snapshots s ON s.trading_date=f.trading_date AND s.ticker=f.ticker
                    WHERE f.trading_date<?""", (asof,)).fetchall()
            for (run_id, ticker, anchor, horizon, stock_close, spy_close, qqq_close,
                 anchor_source, status, saved_end) in observations:
                key = (anchor, horizon)
                if key not in calendars:
                    dates = []
                    session = date.fromisoformat(anchor)
                    while len(dates) < horizon:
                        session += timedelta(days=1)
                        if market_session(session):
                            dates.append(session.isoformat())
                    calendars[key] = dates
                dates = calendars[key]
                end = dates[-1]
                if end > asof:
                    continue
                reasons = []
                if status == "COMPLETE" and saved_end != end:
                    reasons.append("HORIZON_CALENDAR_MISMATCH")
                series = {}
                for symbol in (ticker, "SPY", "QQQ"):
                    bars = [market.execute("""SELECT close,high,low,source FROM daily_bars
                        WHERE ticker=? AND date=?""", (symbol, stamp)).fetchone()
                        for stamp in [anchor, *dates]]
                    if any(bar is None for bar in bars):
                        reasons.append(f"MISSING_SESSION_BAR:{symbol}")
                        continue
                    if any(any(not isinstance(bar[field], (int, float)) or
                               not math.isfinite(bar[field]) or bar[field] <= 0
                               for field in ("close", "high", "low")) or
                           not bar["low"] <= bar["close"] <= bar["high"] for bar in bars):
                        reasons.append(f"INVALID_PRICE:{symbol}")
                        continue
                    if not bars[0]["source"] or len({bar["source"] for bar in bars}) != 1:
                        reasons.append(f"MIXED_SOURCE_ADJUSTMENT_UNVERIFIED:{symbol}")
                    actions = market.execute("""SELECT action_type FROM corporate_actions
                        WHERE ticker=? AND action_date>? AND action_date<=?""",
                        (symbol, anchor, end)).fetchall()
                    if any("split" in row[0].lower() or row[0].lower() in
                           {"stock_dividend", "spin_off", "spinoff"} for row in actions):
                        reasons.append(f"CORPORATE_ACTION_ADJUSTMENT_UNVERIFIED:{symbol}")
                    if any(abs(after["close"] / before["close"] - 1) > jump_limit
                           for before, after in zip(bars, bars[1:])):
                        reasons.append(f"PRICE_DISCONTINUITY_UNRESOLVED:{symbol}")
                    series[symbol] = bars
                frozen = {ticker: stock_close, "SPY": spy_close, "QQQ": qqq_close}
                for symbol, saved_price in frozen.items():
                    if saved_price is not None and symbol in series and not math.isclose(
                            saved_price, series[symbol][0]["close"], rel_tol=1e-9, abs_tol=1e-9):
                        reasons.append(f"FROZEN_ANCHOR_PRICE_CHANGED:{symbol}")
                if ticker in series and series[ticker][0]["source"] != anchor_source:
                    reasons.append(f"FROZEN_ANCHOR_SOURCE_CHANGED:{ticker}")
                if reasons:
                    issue = {"dataset": flavor, "run_id": run_id, "ticker": ticker,
                        "trading_date": anchor, "horizon": horizon, "expected_end_date": end,
                        "status": status, "saved_end_date": saved_end,
                        "reasons": sorted(set(reasons)), "completed_immutable": status == "COMPLETE",
                        "data_sources": {symbol: sorted({bar["source"] for bar in bars})
                                         for symbol, bars in series.items()}}
                    if set(reasons) == {"HORIZON_CALENDAR_MISMATCH"}:
                        issue["corrected_metrics"] = forward_metrics(stock_close,
                            spy_close or series["SPY"][0]["close"],
                            qqq_close or series["QQQ"][0]["close"], series[ticker][1:],
                            series["SPY"][-1]["close"], series["QQQ"][-1]["close"])
                    updated["quality_issues"].append(issue)
                    continue
                if status == "COMPLETE" or audit_only:
                    continue
                spy_close = spy_close or series["SPY"][0]["close"]
                qqq_close = qqq_close or series["QQQ"][0]["close"]
                metrics = forward_metrics(stock_close, spy_close, qqq_close,
                    series[ticker][1:], series["SPY"][-1]["close"], series["QQQ"][-1]["close"])
                if flavor == "day1":
                    with db:
                        db.execute("""UPDATE shadow_day1_forward SET status='COMPLETE',end_date=?,
                            return_pct=?,excess_spy_pct=?,excess_qqq_pct=?,mfe_pct=?,mae_pct=?
                            WHERE run_id=? AND ticker=? AND horizon=? AND status='PENDING'""",
                            (end, *metrics, run_id, ticker, horizon))
                else:
                    with db:
                        db.execute("""UPDATE forward SET status='COMPLETE',end_date=?,
                            return_pct=?,excess_spy_pct=?,excess_qqq_pct=?,mfe_pct=?,mae_pct=?
                            WHERE trading_date=? AND ticker=? AND horizon=? AND status='PENDING'""",
                            (end, *metrics, anchor, ticker, horizon))
                updated[flavor] += 1
    market.close()
    return updated


def audit_and_correct_forward(asof):
    """Append calendar-horizon corrections; original snapshots/outcomes are immutable.

    Consumers use effective_forward, which excludes unresolved invalid observations
    and substitutes a versioned correction only when every required bar is verified.
    """
    from contextlib import closing

    issues = [item for item in advance_forward(asof, audit_only=True)["quality_issues"]
              if item["status"] == "COMPLETE" and
              "HORIZON_CALENDAR_MISMATCH" in item["reasons"]]
    report = {"asof": asof, "corrections_written": 0, "invalid_excluded": 0,
              "unchanged": 0, "observations": []}
    fields = ("end_date", "return_pct", "excess_spy_pct", "excess_qqq_pct", "mfe_pct", "mae_pct")
    for path, flavor in ((ROOT / "data" / "shadow_day1_results.db", "day1"),
                         (RESULTS_DB, "daily")):
        if not path.exists():
            continue
        with closing(sqlite3.connect(path)) as db:
            db.row_factory = sqlite3.Row
            db.executescript("""CREATE TABLE IF NOT EXISTS forward_corrections (
                run_id TEXT NOT NULL, trading_date TEXT NOT NULL, ticker TEXT NOT NULL,
                horizon INTEGER NOT NULL, version INTEGER NOT NULL,
                quality_status TEXT NOT NULL CHECK(quality_status IN ('CORRECTED','INVALID_EXCLUDED')),
                original_json TEXT NOT NULL, revised_json TEXT, reason TEXT NOT NULL,
                calculated_at TEXT NOT NULL, data_sources_json TEXT NOT NULL,
                PRIMARY KEY(run_id,trading_date,ticker,horizon,version));""")
            base = ("SELECT f.*,s.trading_date FROM shadow_day1_forward f JOIN shadow_day1_snapshots s "
                    "ON s.run_id=f.run_id AND s.ticker=f.ticker" if flavor == "day1" else
                    "SELECT '' AS run_id,f.* FROM forward f")
            corrected_columns = ",".join(
                f"CASE WHEN c.quality_status='CORRECTED' THEN json_extract(c.revised_json,'$.{field}') "
                f"WHEN c.quality_status='INVALID_EXCLUDED' THEN NULL ELSE b.{field} END AS {field}"
                for field in fields)
            db.execute(f"""CREATE VIEW IF NOT EXISTS effective_forward AS
                SELECT b.run_id,b.trading_date,b.ticker,b.horizon,
                CASE WHEN c.quality_status='INVALID_EXCLUDED' THEN 'INVALID_EXCLUDED'
                     ELSE b.status END AS status,
                {corrected_columns},c.version AS correction_version,c.reason AS correction_reason
                FROM ({base}) b LEFT JOIN forward_corrections c ON c.run_id=b.run_id
                AND c.trading_date=b.trading_date AND c.ticker=b.ticker AND c.horizon=b.horizon
                AND c.version=(SELECT MAX(c2.version) FROM forward_corrections c2
                    WHERE c2.run_id=c.run_id AND c2.trading_date=c.trading_date
                    AND c2.ticker=c.ticker AND c2.horizon=c.horizon)""")
            for issue in (item for item in issues if item["dataset"] == flavor):
                key = (issue["run_id"] or "", issue["trading_date"], issue["ticker"], issue["horizon"])
                original = db.execute(f"""SELECT {','.join(fields)} FROM ({base})
                    WHERE run_id=? AND trading_date=? AND ticker=? AND horizon=?""", key).fetchone()
                revised = (dict(zip(fields, (issue["expected_end_date"], *issue["corrected_metrics"])))
                           if "corrected_metrics" in issue else None)
                quality = "CORRECTED" if revised else "INVALID_EXCLUDED"
                encoded = json.dumps(revised, sort_keys=True) if revised else None
                reason = ";".join(issue["reasons"])
                sources = json.dumps(issue["data_sources"], sort_keys=True)
                with db:
                    db.execute("BEGIN IMMEDIATE")
                    prior = db.execute("""SELECT version,quality_status,revised_json,reason,data_sources_json
                        FROM forward_corrections WHERE run_id=? AND trading_date=? AND ticker=?
                        AND horizon=? ORDER BY version DESC LIMIT 1""", key).fetchone()
                    version = prior[0] if prior else 0
                    if prior and tuple(prior)[1:] == (quality, encoded, reason, sources):
                        report["unchanged"] += 1
                    else:
                        version += 1
                        db.execute("INSERT INTO forward_corrections VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                            (*key, version, quality, json.dumps(dict(original), sort_keys=True),
                             encoded, reason, datetime.now(timezone.utc).isoformat(), sources))
                        report["corrections_written"] += int(quality == "CORRECTED")
                    report["invalid_excluded"] += int(quality == "INVALID_EXCLUDED")
                    report["observations"].append({"dataset": flavor, "ticker": key[2],
                        "trading_date": key[1], "horizon": key[3], "quality_status": quality,
                        "version": version, "reasons": issue["reasons"], "revised": revised})
    return report


def finalize(day, market_stats, phase_stats, evidence_stats, research_stats,
             eligibility, entry_stats, portfolio_stats, runtime):
    with sqlite3.connect(f"file:{ROOT / 'data' / 'fundamental_data.db'}?mode=ro", uri=True) as db:
        baseline = db.execute("""SELECT MAX(scan_date) FROM fundamental_runs
            WHERE scan_date<? AND (SELECT COUNT(*) FROM fundamental_scores
                WHERE run_id=fundamental_runs.id AND candidate_rank<=30)=30""", (day,)).fetchone()
        if not baseline or not baseline[0]:
            raise ValueError("CANDIDATE_COMPARISON_BASELINE_MISSING")
        previous = baseline[0]
        prior = {r[0]: r[1] for r in db.execute("""SELECT ticker,candidate_rank FROM fundamental_scores
            WHERE run_id=(SELECT id FROM fundamental_runs WHERE scan_date=? ORDER BY id DESC LIMIT 1)
            AND candidate_rank<=30""", (previous,))}
        current = {r[0]: r[1] for r in db.execute("""SELECT ticker,candidate_rank FROM fundamental_scores
            WHERE run_id=(SELECT id FROM fundamental_runs WHERE scan_date=? ORDER BY id DESC LIMIT 1)
            AND candidate_rank<=30""", (day,))}
    if len(prior) != 30 or len(current) != 30:
        raise ValueError("CANDIDATE_COMPARISON_INCOMPLETE")
    changes = {"NEW_ENTRY": sorted(current.keys() - prior.keys()),
               "DROPPED": sorted(prior.keys() - current.keys()),
               "RANK_UP": sorted(t for t in current.keys() & prior.keys() if current[t] < prior[t]),
               "RANK_DOWN": sorted(t for t in current.keys() & prior.keys() if current[t] > prior[t])}
    counts = dict(Counter(r["entry_state"] for r in entry_stats["results"]))
    report = {"status": "PIPELINE_COMPLETE", "trading_date": day,
        "generated_at": datetime.now(timezone.utc).isoformat(), "shadow_day": shadow_day_number(day),
        "market_data_as_of": day, "market_data_source": SOURCE,
        "market_regime": entry_stats["market_regime"], "target_session": entry_stats["target_session"],
        "market": market_stats, "phase1_to_3": phase_stats,
        "candidate_comparison_baseline": previous, "candidate_changes": changes,
        "evidence": evidence_stats, "research": {k: v for k, v in research_stats.items() if k != "cases"},
        "eligibility_counts": eligibility["counts"], "eligibility_cases": eligibility["cases"],
        "entry_counts": counts, "entry_results": entry_stats["results"],
        "entry_api_requests": entry_stats["api_requests"], "entry_errors": entry_stats["errors"],
        "portfolio": portfolio_stats, "forward_tracking": {"snapshots": 30,
            "pending": 150, "quality_tiers": ["FULL_RESEARCH", "LIMITED_RESEARCH"]},
        "phase_statuses": {k: "COMPLETE" for k in ("market", "phase1", "phase2", "phase3",
            "research", "entry", "portfolio", "forward_tracking")},
        "luna_calls": research_stats["luna_calls"],
        "luna_input_tokens": research_stats["input_tokens"],
        "luna_output_tokens": research_stats["output_tokens"],
        "luna_estimated_cost_usd": research_stats["confirmed_cost_usd"],
        "sol_calls": 0, "total_ai_cost_local_estimate_usd": research_stats["confirmed_cost_usd"],
        "runtime_seconds": round(runtime, 2), "no_trade_placed": True}
    report["phase_statuses"]["telegram_summary"] = "READY"
    output = paths(day) / "final.json"
    save(output, report)
    from ops_tools import completed
    completed(day,report)
    return output, report


class StageFailure(RuntimeError):
    def __init__(self, phase, cause):
        self.phase = phase
        super().__init__(f"{phase}: {type(cause).__name__}: {cause}")


def run_pipeline(day, market_stats, progress=None):
    if not authorized_day(day) or not data_ready(day, require_session_date=False):
        raise ValueError("SHADOW_DAILY_DATA_NOT_READY")
    save(paths(day) / "run_manifest.json", capture_manifest(day))
    started = time.monotonic()
    stage = "PHASE_1_TO_3"
    try:
        if progress: progress(stage)
        phase = quant_and_fundamental(day)
        phase["entry_reference_observations"] = observe_entry_references(day)
        phase["forward_horizons_filled"] = advance_forward(day)
        phase["forward_corrections"] = audit_and_correct_forward(day)
        stage = "PHASE_4A"
        if progress: progress(stage)
        evidence = research_priority_and_evidence(day)
        stage = "INITIAL_RESEARCH"
        if progress: progress(stage)
        research = initial_research(day, v11_config())
        stage = "RESEARCH_ELIGIBILITY"
        if progress: progress(stage)
        eligibility = research_eligibility(day, research, v11_config())
        stage = "DEEP_TRIAGE"
        if progress: progress(stage)
        deep = deep_triage(day, eligibility, v11_config())
        stage = "PHASE_5_ENTRY"
        if progress: progress(stage)
        entry = close_entry(day, eligibility, v11_config())
        stage = "PHASE_6_FORWARD"
        if progress: progress(stage)
        portfolio = portfolio_and_forward(day, eligibility, entry, v11_config())
        stage = "FINALIZE"
        if progress: progress(stage)
        path, result = finalize(day, market_stats, phase, evidence, research,
                                eligibility, entry, portfolio, time.monotonic() - started)
        result["deep_triage"] = deep
        save(path, result)
        return path, result
    except Exception as exc:
        raise StageFailure(stage, exc) from exc

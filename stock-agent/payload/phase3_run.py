#!/usr/bin/env python3
"""SEC-backed, point-in-time fundamentals for the latest REAL Quant Top 100."""
from __future__ import annotations

import argparse
import json
import logging
from logging.handlers import RotatingFileHandler
import math
import os
import random
import sqlite3
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from phase3_features import build_features
from phase3_scoring import COMPONENTS, peer_medians, score
from phase3_sec import SECClient, SECError, extract_facts, load_user_agent, validate_identity
from quant_screen import ROOT


LOG = logging.getLogger("phase3_run")


def init_db(db):
    db.executescript((ROOT / "phase3_schema.sql").read_text())


def load_top100(market):
    latest_day = market.execute("SELECT MAX(date) FROM daily_bars WHERE ticker='SPY'").fetchone()[0]
    run = market.execute("""SELECT * FROM quant_runs WHERE data_mode='live' AND asof=?
        ORDER BY id DESC LIMIT 1""", (latest_day,)).fetchone()
    if not run:
        raise RuntimeError("REAL FUNDAMENTAL DATA FAILED: latest real Quant run unavailable")
    rows = market.execute("""SELECT q.*,s.company_name,s.cik,s.sector,s.normalized_sector,s.mapping_confidence
        FROM quant_scores q JOIN securities s ON s.ticker=q.ticker
        WHERE q.run_id=? ORDER BY q.rank LIMIT 100""", (run["id"],)).fetchall()
    if len(rows) != 100:
        raise RuntimeError("REAL FUNDAMENTAL DATA FAILED: Quant Top 100 incomplete")
    return run, rows


def split_actions(market, ticker, asof):
    actions = []
    for row in market.execute("""SELECT action_date,action_type,details_json FROM corporate_actions
        WHERE ticker=? AND action_date<=? ORDER BY action_date""", (ticker, asof)):
        detail = json.loads(row[2])
        try:
            actions.append({"date": row[0], "type": row[1], "split_from": float(detail["split_from"]),
                            "split_to": float(detail["split_to"])})
        except (KeyError, TypeError, ValueError):
            continue
    return actions


def empty_raw(cfg):
    return {"values": {}, "provenance": {}, "quarterly": {}, "annual": {}, "instant": {}}


def forward_link(market, quant_run_id, results, horizons):
    by_ticker = {r["ticker"]: r for r in results}
    observations = market.execute("""SELECT ticker,horizon,stock_return,excess_spy,excess_qqq,mfe,mae
        FROM forward_observations WHERE run_id=?""", (quant_run_id,)).fetchall()
    joined = [{"ticker": row["ticker"], "horizon": row["horizon"], "quant_score": by_ticker[row["ticker"]]["quant_score"],
               "fundamental_score": by_ticker[row["ticker"]]["fundamental_score"],
               "candidate_score": by_ticker[row["ticker"]]["candidate_score"],
               "stock_return": row["stock_return"], "excess_spy": row["excess_spy"],
               "excess_qqq": row["excess_qqq"], "mfe": row["mfe"], "mae": row["mae"]}
              for row in observations if row["ticker"] in by_ticker]
    return {"quant_run_id": quant_run_id, "linked_observations": len(joined),
            "completed_by_horizon": {str(h): sum(row["horizon"] == h for row in joined) for h in horizons},
            "pending_by_horizon": {str(h): len(results) - sum(row["horizon"] == h for row in joined) for h in horizons},
            "ready_for_predictive_comparison": False,
            "note": "Phase 2.6 outcomes are joined by immutable Quant run ID; no history is rewritten."}


def run(market, db, cfg, client):
    started = time.monotonic()
    quant_run, top = load_top100(market)
    scan_date = quant_run["asof"]
    items, errors = [], {}
    for index, row in enumerate(top, 1):
        ticker, cik = row["ticker"], row["cik"]
        item = {"ticker": ticker, "company": row["company_name"], "sector": row["normalized_sector"],
                "raw_sector": row["sector"], "sector_confidence": row["mapping_confidence"] or "UNKNOWN",
                "cik": cik, "quant_rank": row["rank"], "quant_score": row["quant_score"],
                "raw": empty_raw(cfg), "data_quality_status": "OK"}
        try:
            if not cik:
                raise SECError("IDENTITY_MISMATCH", "missing CIK")
            submissions, facts = client.company(db, cik)
            validate_identity(cik, ticker, row["company_name"], submissions, facts, cfg["quality"])
            selected = extract_facts(facts, submissions, scan_date, cfg)
            price = json.loads(row["features_json"])["close"]
            item["raw"] = build_features(selected, scan_date, price, split_actions(market, ticker, scan_date), cfg)
        except SECError as exc:
            if exc.kind in {"ACCESS_DENIED", "RATE_LIMIT", "NETWORK_ERROR"} and index == 1:
                raise RuntimeError(f"REAL FUNDAMENTAL DATA FAILED: {exc}") from exc
            item["data_quality_status"] = "DATA_QUALITY_REJECT" if exc.kind in {"IDENTITY_MISMATCH", "DATA_CONFLICT"} else "DATA_UNAVAILABLE"
            errors[ticker] = str(exc)
            LOG.warning("%s %s", ticker, exc)
        except (ValueError, KeyError, ZeroDivisionError, OverflowError, TypeError) as exc:
            item["data_quality_status"] = "DATA_QUALITY_REJECT"
            errors[ticker] = f"CALCULATION_ERROR:{type(exc).__name__}"
            LOG.exception("%s fundamental calculation failed", ticker)
        items.append(item)
    if all(item["data_quality_status"] != "OK" for item in items):
        raise RuntimeError("REAL FUNDAMENTAL DATA FAILED: no Top 100 company could be validated")
    peers = peer_medians(items, cfg)
    results = []
    for item in items:
        result = {**item, **score(item, peers, cfg)}
        results.append(result)
    rank_insufficient_last = cfg["candidate_weights"]["rank_insufficient_after_covered"]
    ranked = sorted((r for r in results if r["data_quality_status"] != "DATA_QUALITY_REJECT"),
                    key=lambda r: (rank_insufficient_last and r["confidence"] == "INSUFFICIENT",
                                   -r["candidate_score"], r["ticker"]))
    rejected = [r for r in results if r["data_quality_status"] == "DATA_QUALITY_REJECT"]
    ranked += sorted(rejected, key=lambda r: (-r["quant_score"], r["ticker"]))
    for rank, row in enumerate(ranked, 1):
        row["candidate_rank"] = rank
    def persist():
        with db:
            cursor = db.execute("""INSERT INTO fundamental_runs(quant_run_id,scan_date,scanned_at,config_json,
                source,api_requests,cache_hits,processed_count,elapsed_seconds) VALUES(?,?,?,?,?,?,?,?,?)""",
                (quant_run["id"], scan_date, datetime.now(timezone.utc).isoformat(), json.dumps(cfg, sort_keys=True),
                 "SEC_EDGAR_COMPANYFACTS", client.api_requests, client.cache_hits, len(results),
                 round(time.monotonic() - started, 2)))
            run_id = cursor.lastrowid
            db.executemany("""INSERT INTO fundamental_scores VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                [(run_id, r["ticker"], r["quant_rank"], r["candidate_rank"], r["quant_score"],
                  r["fundamental_score"], r["candidate_score"], r["confidence"], r["coverage_pct"],
                  *(r["component_scores"][name] for name in COMPONENTS), r["trend_score"], r["trend_label"],
                  r["effective_quant_weight"], r["effective_fundamental_weight"],
                  json.dumps(r["flags"], sort_keys=True), json.dumps(r["missing_fields"], sort_keys=True),
                  json.dumps(r["raw"], sort_keys=True), json.dumps(r["explanation"], sort_keys=True),
                  r["data_quality_status"]) for r in results])
        return run_id
    for delay in (0, 5, 15, 30):
        if delay:
            time.sleep(delay)
        try:
            fundamental_run_id = persist()
            break
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower() and "busy" not in str(exc).lower():
                raise
    else:
        raise sqlite3.OperationalError("fundamental database remained locked")
    scores = [r["coverage_pct"] for r in results]
    confidence = Counter(r["confidence"] for r in results)
    fundamental_ranked = sorted((r for r in results if r["fundamental_score"] is not None and
                                 r["data_quality_status"] != "DATA_QUALITY_REJECT"),
                                key=lambda r: (-r["fundamental_score"], r["ticker"]))
    def compact(row):
        return {"rank": row["candidate_rank"], "quant_rank": row["quant_rank"], "ticker": row["ticker"],
                "company": row["company"], "sector": row["sector"], "quant_score": row["quant_score"],
                "fundamental_score": row["fundamental_score"], "candidate_score": row["candidate_score"],
                "confidence": row["confidence"], "coverage_pct": row["coverage_pct"],
                **{name + "_score": row["component_scores"][name] for name in COMPONENTS},
                "trend_score": row["trend_score"], "trend": row["trend_label"],
                "flags": row["flags"], "effective_quant_weight": row["effective_quant_weight"],
                "effective_fundamental_weight": row["effective_fundamental_weight"],
                "data_quality_status": row["data_quality_status"]}
    candidate_top30 = [compact(r) for r in ranked if r["data_quality_status"] != "DATA_QUALITY_REJECT"][:30]
    fundamental_top30 = [compact(r) for r in fundamental_ranked[:30]]
    quant_top30 = {row["ticker"] for row in top[:30]}
    candidate_names = {row["ticker"] for row in candidate_top30}
    sample = random.Random(quant_run["id"]).sample(results, min(3, len(results)))
    output = {"status": "COMPLETE", "scan_date": scan_date, "quant_run_id": quant_run["id"],
              "fundamental_run_id": fundamental_run_id, "data_source": "SEC_EDGAR_COMPANYFACTS_AND_SUBMISSIONS",
              "api_requests": client.api_requests, "cache_hits": client.cache_hits,
              "cache_bytes": db.execute("SELECT SUM(LENGTH(payload)) FROM sec_cache").fetchone()[0] or 0,
              "runtime_seconds": round(time.monotonic() - started, 2), "llm_api_calls": 0,
              "quant_top100_count": len(top), "validated_company_count": sum(r["data_quality_status"] == "OK" for r in results),
              "confidence_counts": {k: confidence[k] for k in ("HIGH", "MEDIUM", "LOW", "INSUFFICIENT")},
              "average_coverage_pct": round(sum(scores) / len(scores), 2), "errors": errors,
              "fundamental_top30": fundamental_top30, "candidate_top30": candidate_top30,
              "candidate_top20_explanations": [{**compact(r), "explanation": r["explanation"]} for r in ranked[:20]],
              "quant_vs_candidate_top30": {"quant_only": sorted(quant_top30 - candidate_names),
                                           "candidate_only": sorted(candidate_names - quant_top30),
                                           "overlap": len(quant_top30 & candidate_names)},
              "all_top100": [compact(r) for r in ranked],
              "raw_fundamental_samples": {r["ticker"]: r["raw"] for r in sample},
              "forward_tracker": forward_link(market, quant_run["id"], results, [1, 5, 10, 20, 60])}
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--market-db", type=Path, default=ROOT / "data" / "market_data.db")
    parser.add_argument("--db", type=Path, default=ROOT / "data" / "fundamental_data.db")
    parser.add_argument("--output", type=Path, default=ROOT / "data" / "phase3_report.json")
    parser.add_argument("--config", type=Path, default=ROOT / "phase3_config.json")
    args = parser.parse_args()
    args.db.parent.mkdir(parents=True, exist_ok=True)
    if not LOG.handlers:
        LOG.addHandler(RotatingFileHandler(ROOT / "data" / "phase3.log", maxBytes=2_000_000, backupCount=2))
    LOG.setLevel(logging.INFO)
    LOG.propagate = False
    try:
        cfg = json.loads(args.config.read_text())
        if not math.isclose(sum(cfg["component_weights"].values()), 1) or not math.isclose(
                cfg["candidate_weights"]["quant"] + cfg["candidate_weights"]["fundamental"], 1):
            raise ValueError("fundamental and candidate weights must each sum to 1")
        user_agent = load_user_agent(ROOT / ".env")
        client = SECClient(user_agent, cfg["source"])
        with sqlite3.connect(args.market_db, timeout=60) as market, sqlite3.connect(args.db, timeout=60) as db:
            market.row_factory = sqlite3.Row
            db.row_factory = sqlite3.Row
            init_db(db)
            result = run(market, db, cfg, client)
        temporary = args.output.with_name(args.output.name + ".tmp")
        temporary.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
        os.replace(temporary, args.output)
        print(json.dumps({key: result[key] for key in ("status", "scan_date", "validated_company_count",
                       "confidence_counts", "api_requests", "cache_hits", "runtime_seconds")}, indent=2))
    except Exception as exc:
        LOG.exception("REAL FUNDAMENTAL DATA FAILED")
        raise SystemExit(f"REAL FUNDAMENTAL DATA FAILED: {type(exc).__name__}: {exc}") from exc


if __name__ == "__main__":
    main()

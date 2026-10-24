"""Local, point-in-time Phase 4 prioritization and evidence validation."""
from __future__ import annotations

import json
import math
import re
import sqlite3
import zlib
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlparse


def clamp(value):
    return max(0.0, min(100.0, float(value)))


def compact(value):
    return json.dumps(value, separators=(",", ":"), sort_keys=True, ensure_ascii=False)


def latest_candidates(fundamental, market):
    run = fundamental.execute("SELECT * FROM fundamental_runs ORDER BY id DESC LIMIT 1").fetchone()
    if not run:
        raise RuntimeError("REAL RESEARCH DATA FAILED: Phase 3 run missing")
    latest_quant = market.execute("SELECT MAX(date) FROM daily_bars WHERE ticker='SPY'").fetchone()[0]
    if run["scan_date"] != latest_quant:
        raise RuntimeError("REAL RESEARCH DATA FAILED: latest Phase 3 is not aligned with market data")
    rows = fundamental.execute("""SELECT * FROM fundamental_scores WHERE run_id=?
        AND candidate_rank<=30 AND data_quality_status='OK' ORDER BY candidate_rank""", (run["id"],)).fetchall()
    if len(rows) != 30:
        raise RuntimeError("REAL RESEARCH DATA FAILED: complete Candidate Top 30 unavailable")
    return run, rows


def filings_for_ticker(fundamental, market, ticker, cutoff, cfg):
    cik_row = market.execute("SELECT cik FROM securities WHERE ticker=?", (ticker,)).fetchone()
    if not cik_row or not cik_row[0]:
        return []
    cache = fundamental.execute("""SELECT payload FROM sec_cache WHERE cik=? AND endpoint='submissions'
        AND cache_status='FRESH'""", (str(int(cik_row[0])).zfill(10),)).fetchone()
    if not cache:
        return []
    data = json.loads(zlib.decompress(cache[0]))
    recent = data.get("filings", {}).get("recent", {})
    filings = []
    for i, (form, day, accession) in enumerate(zip(recent.get("form", []), recent.get("filingDate", []),
                                                     recent.get("accessionNumber", []))):
        if form not in {"8-K", "8-K/A", "10-Q", "10-Q/A", "10-K", "10-K/A"} or day > cutoff.date().isoformat():
            continue
        accepted = recent.get("acceptanceDateTime", [])
        if i < len(accepted) and accepted[i]:
            try:
                accepted_at = datetime.fromisoformat(accepted[i].replace("Z", "+00:00"))
                if accepted_at.tzinfo is None and day == cutoff.date().isoformat():
                    continue
                if accepted_at.tzinfo and accepted_at.astimezone(timezone.utc) > cutoff:
                    continue
            except ValueError:
                continue
        elif day == cutoff.date().isoformat():
            continue  # Unknown same-day acceptance time is not point-in-time safe.
        age = (cutoff.date() - date.fromisoformat(day)).days
        if age > cfg["material_lookback_days"]:
            continue
        items = recent.get("items", [])
        item_codes = items[i] if i < len(items) else ""
        filings.append({"form": form, "date": day, "accession": accession,
                        "items": item_codes, "url": "https://www.sec.gov/Archives/edgar/data/"
                        + str(int(cik_row[0])) + "/" + accession.replace("-", "") + "/",
                        "recent": age <= cfg["recent_days"]})
    return filings[:5]


def earnings_context(filings, cutoff, next_earnings=None):
    last = next((f["date"] for f in filings if f["form"].startswith("8-K") and "2.02" in (f["items"] or "")), None)
    days_since = (cutoff.date() - date.fromisoformat(last)).days if last else None
    future = date.fromisoformat(next_earnings["date"]) if next_earnings else None
    if future and future < cutoff.date():
        future = None
    trading_days = sum((cutoff.date() + timedelta(days=i)).weekday() < 5
                       for i in range(1, (future - cutoff.date()).days + 1)) if future else None
    status = "EARNINGS_IMMINENT" if trading_days is not None and trading_days <= 3 else (
        "PRE_EARNINGS" if trading_days is not None and trading_days <= 10 else (
        "POST_EARNINGS" if days_since is not None and days_since <= 7 else "NORMAL"))
    return {"last_earnings_date": last, "next_earnings_date": future.isoformat() if future else None,
            "days_since_earnings": days_since, "trading_days_until_earnings": trading_days,
            "status": status, "next_date_quality": next_earnings.get("quality") if future else "UNAVAILABLE",
            "next_date_source": next_earnings.get("source") if future else None}


def prior_research(db, ticker, cutoff):
    row = db.execute("""SELECT r.thesis_json,r.candidate_score,r.sources_json,r.created_at
        FROM research_results r WHERE r.ticker=? AND r.status='COMPLETE' AND r.created_at<=?
        ORDER BY r.created_at DESC LIMIT 1""", (ticker, cutoff.isoformat())).fetchone()
    if not row:
        return None
    thesis = json.loads(row[0])
    return {"bull": thesis.get("bull_case"), "bear": thesis.get("bear_case"),
            "key_catalyst": thesis.get("key_catalyst"), "key_risk": thesis.get("key_risk"),
            "candidate_score": row[1], "source_urls": [s.get("url") for s in json.loads(row[2])],
            "researched_at": row[3]}


def packet_for(row, quant, filings, previous, cutoff, cfg):
    q = json.loads(quant["features_json"])
    memory = json.loads(quant["memory_json"])
    flags = json.loads(row["flags_json"])
    packet = {
        "ticker": row["ticker"], "company": row["company_name"], "sector": row["normalized_sector"],
        "candidate_scan_date": row["scan_date"], "research_cutoff": cutoff.isoformat(),
        "quant": {"score": row["quant_score"], "rank": row["quant_rank"],
                  "rank_change_1d": memory.get("rank_change_1d"),
                  "rank_change_5d": memory.get("rank_change_5d"),
                  "ranking_momentum": memory.get("ranking_momentum"),
                  "relative_strength_score": quant["relative_strength_score"],
                  "technical_score": quant["technical_score"], "volume_score": quant["volume_score"],
                  "rsi14": q.get("rsi14"), "rvol": q.get("rvol"),
                  "abnormal_volume": q.get("abnormal_volume"),
                  "rs_spy_20d": q.get("rs_spy_20d"), "rs_qqq_20d": q.get("rs_qqq_20d")},
        "fundamental": {"score": row["fundamental_score"], "candidate_score": row["candidate_score"],
                        "growth": row["growth_score"], "profitability": row["profitability_score"],
                        "health": row["financial_health_score"], "cash_flow": row["cash_flow_quality_score"],
                        "valuation": row["valuation_score"], "trend": row["trend_label"],
                        "confidence": row["confidence"], "coverage_pct": row["coverage_pct"], "flags": flags},
        "earnings": earnings_context(filings, cutoff), "recent_filings": filings,
        "previous_thesis": previous, "news_check_status": "UNAVAILABLE",
        "market_context": {"vix": None, "vxn": None, "regime": "UNAVAILABLE"}
    }
    size = len(compact(packet).encode())
    if size > cfg["research_packet_max_bytes"]:
        raise ValueError(f"research packet too large for {row['ticker']}: {size} bytes")
    return packet, size


def priority(packet, row, quant, fundamental_rank, previous, cfg):
    q = packet["quant"]
    f = packet["fundamental"]
    rank_delta = q["ranking_momentum"]
    candidate_delta = row["candidate_score"] - previous["candidate_score"] if previous else None
    components = {
        "candidate": row["candidate_score"],
        "quant_rank": 100 * (101 - row["quant_rank"]) / 100,
        "fundamental_rank": 100 * (101 - fundamental_rank) / 100,
        "confidence": {"HIGH": 100, "MEDIUM": 70, "LOW": 35, "INSUFFICIENT": 0}[row["confidence"]],
        "coverage": row["coverage_pct"],
        "ranking_momentum": clamp(50 + 3 * rank_delta) if rank_delta is not None else 50,
        "candidate_trend": clamp(50 + 5 * candidate_delta) if candidate_delta is not None else 50,
        "abnormal_volume": clamp(50 + 20 * (q["rvol"] or 1.0) - 20),
        "relative_strength": quant["relative_strength_score"],
        "fundamental_trend": row["trend_score"] or 50,
        "risk_flags": clamp(100 - 12 * len(f["flags"])),
        "earnings_proximity": 75 if packet["earnings"]["status"] == "POST_EARNINGS" else 50,
        "recent_filing": 80 if any(x["recent"] for x in packet["recent_filings"]) else 50,
    }
    weights = cfg["pre_research_weights"]
    value = sum(components[k] * w for k, w in weights.items())
    reasons = sorted(((k, round(components[k], 2), round(components[k] * w, 2))
                      for k, w in weights.items()), key=lambda x: -x[2])
    return round(clamp(value), 4), reasons


def refresh_mode(packet, previous, cfg):
    if not previous:
        return "FULL_RESEARCH"
    changed = abs(packet["fundamental"]["candidate_score"] - previous["candidate_score"])
    new_filing = any(f["date"] > previous["researched_at"][:10] for f in packet["recent_filings"])
    if new_filing or changed >= cfg["full_refresh_score_change"]:
        return "FULL_RESEARCH"
    if changed >= cfg["light_refresh_score_change"] or packet.get("news_check_status") != "VERIFIED_NO_CHANGE":
        return "LIGHT_REFRESH"
    return "CACHED_RESEARCH"


def trusted_url(url, cfg):
    parsed = urlparse(url or "")
    host = (parsed.hostname or "").lower()
    return parsed.scheme == "https" and any(host == d or host.endswith("." + d) for d in cfg["trusted_domains"])


def deduplicate_events(events):
    merged = []
    for event in events:
        words = set(re.findall(r"[a-z0-9]+", event.get("summary", "").lower()))
        for prior in merged:
            earlier = set(re.findall(r"[a-z0-9]+", prior.get("summary", "").lower()))
            similarity = len(words & earlier) / len(words | earlier) if words | earlier else 0
            if (event["event_type"] == prior["event_type"] and
                    abs((date.fromisoformat(event["date"]) - date.fromisoformat(prior["date"])).days) <= 2 and
                    similarity >= 0.55):
                prior["sources"] = list({s["url"]: s for s in prior["sources"] + event["sources"]}.values())
                prior["conflict"] |= event["direction"] != prior["direction"]
                break
        else:
            merged.append({**event, "conflict": False})
    return merged


def score_research(payload, packet, consulted_urls, cfg):
    cutoff = date.fromisoformat(packet["research_cutoff"][:10])
    # Filing-index metadata in the packet is not evidence for a claim about filing contents.
    allowed = set(consulted_urls)
    valid = []
    for event in payload.get("events", [])[:8]:
        try:
            day = date.fromisoformat(event["date"])
            age = (cutoff - day).days
            if age < 0 or age > cfg["material_lookback_days"]:
                continue
            sources = [s for s in event.get("sources", []) if s.get("url") in allowed and
                       trusted_url(s["url"], cfg) and
                       day.isoformat() <= s.get("date", "9999") <= cutoff.isoformat()]
            if not sources:
                continue
            sources = [{"url": s["url"], "date": s["date"],
                        "source_type": "SEC" if (urlparse(s["url"]).hostname or "").endswith("sec.gov") else
                        "REGULATOR" if (urlparse(s["url"]).hostname or "").endswith(("fda.gov", "ftc.gov", "justice.gov"))
                        else "TRUSTED_NEWS", "confidence": "HIGH" if (urlparse(s["url"]).hostname or "").endswith(".gov")
                        else "MEDIUM"} for s in sources]
            valid.append({"event_type": str(event["event_type"])[:50], "date": day.isoformat(),
                          "summary": str(event.get("summary", ""))[:300],
                          "direction": event.get("direction") if event.get("direction") in {"POSITIVE", "NEUTRAL", "NEGATIVE"} else "NEUTRAL",
                          "materiality": event.get("materiality") if event.get("materiality") in {"LOW", "MEDIUM", "HIGH", "MATERIAL"} else "LOW",
                          "sources": sources[:3], "fundamental_impact": clamp(event.get("fundamental_impact", 50)),
                          "duration": clamp(event.get("duration", 50))})
        except (KeyError, TypeError, ValueError):
            continue
    events = deduplicate_events(valid)
    if not events:
        return {"status": "UNAVAILABLE", "reason": "no dated, trusted, consulted source survived verification"}
    for e in events:
        e["conflict"] |= any(x is not e and x["event_type"] == e["event_type"] and x["date"] == e["date"] and
                              x["direction"] != e["direction"] for x in events)
    scores = []
    for e in events:
        age = (cutoff - date.fromisoformat(e["date"])).days
        source_quality = max(100 if urlparse(s["url"]).hostname.endswith("sec.gov") else 75 for s in e["sources"])
        materiality = {"LOW": 25, "MEDIUM": 50, "HIGH": 75, "MATERIAL": 100}[e["materiality"]]
        recency = max(15, 100 - age * 1.2)
        rs = packet["quant"].get("rs_spy_20d")
        rvol = packet["quant"].get("rvol")
        aligned = rs is not None and ((rs > 0) == (e["direction"] == "POSITIVE"))
        confirmation = 75 if aligned and rvol is not None and rvol >= 1.5 else (60 if aligned else 35)
        value = .30 * materiality + .20 * source_quality + .15 * recency + .15 * e["fundamental_impact"] + .10 * e["duration"] + .10 * confirmation
        if e["direction"] == "NEGATIVE":
            value = 100 - value
        if e["conflict"]:
            value = 50 + (value - 50) * .4
        scores.append(value)
    catalyst = round(clamp(sum(scores) / len(scores)), 4)
    dimensions = payload.get("dimensions", {})
    if not all(k in dimensions and isinstance(dimensions[k], (int, float)) and math.isfinite(dimensions[k])
               for k in cfg["research_weights"] if k != "catalyst"):
        return {"status": "UNAVAILABLE", "reason": "missing research dimensions"}
    research = round(clamp(sum((catalyst if k == "catalyst" else clamp(dimensions[k])) * w
                               for k, w in cfg["research_weights"].items())), 4)
    weight = cfg["conviction_weights"]
    conviction = round(clamp(weight["candidate"] * packet["fundamental"]["candidate_score"] +
                             weight["research"] * research), 4)
    sources = list({s["url"]: s for e in events for s in e["sources"]}.values())
    conflict = any(e["conflict"] for e in events)
    confidence = "LOW" if conflict or packet["fundamental"]["confidence"] == "LOW" else (
        "HIGH" if len({urlparse(s["url"]).hostname for s in sources}) >= 2 and
        packet["fundamental"]["confidence"] == "HIGH" else "MEDIUM")
    # Model text cannot establish a hard block. A verified override must be separately confirmed.
    label = "HIGH_CONVICTION_CANDIDATE" if conviction >= 75 and confidence != "LOW" else "NORMAL_CANDIDATE"
    thesis = {key: str(payload.get(key, ""))[:300] for key in
              ("thesis", "bull_case", "bear_case", "key_catalyst", "key_risk", "what_changes_thesis")}
    return {"status": "COMPLETE", "research_score": research, "catalyst_score": catalyst,
            "conviction_score": conviction, "confidence": confidence, "label": label,
            "thesis": thesis, "events": events, "sources": sources,
            "flags": ["SOURCE_CONFLICT"] if conflict else []}


def apply_verified_override(db, ticker, cutoff, result, cfg):
    """Never let model text alone impose a hard block; require a separately verified record."""
    rows = db.execute("""SELECT event_type,event_date,source_url,verified_at,verification_note
        FROM research_overrides WHERE ticker=? AND event_date<=? ORDER BY verified_at DESC""",
        (ticker, cutoff.date().isoformat())).fetchall()
    for row in rows:
        if (row["event_type"] in cfg["hard_block_types"] and trusted_url(row["source_url"], cfg)
                and datetime.fromisoformat(row["verified_at"]) <= cutoff and row["verification_note"].strip()):
            result = {**result, "label": "RESEARCH_BLOCK", "override": dict(row),
                      "flags": result.get("flags", []) + ["VERIFIED_HARD_RISK_OVERRIDE"]}
            break
    return result


def token_report(db, day, cfg):
    from phase4_budget import budget_report
    return budget_report(db, day, cfg)

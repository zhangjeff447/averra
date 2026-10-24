"""Phase 4A: bounded, cached factual evidence; no model calls."""
from __future__ import annotations

import hashlib
import html
import json
import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime


def compact(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def source_rank(item):
    kind = item["source_type"]
    return {"SEC": 0, "REGULATOR": 1, "COMPANY_IR": 2, "OFFICIAL_RELEASE": 3,
            "TRUSTED_NEWS": 4}.get(kind, 5)


def normalize_event(item, ticker, cutoff, retrieved_at):
    day = item.get("date", "")[:10]
    if not day or day > cutoff[:10]:
        return None
    url = item.get("url") or item.get("reference")
    if not url:
        return None
    kind = item.get("source_type", "TRUSTED_NEWS")
    official = kind in {"SEC", "REGULATOR", "COMPANY_IR", "OFFICIAL_RELEASE"}
    title = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", item.get("headline") or item.get("title") or ""))).strip()
    summary = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", item.get("summary") or ""))).strip()[:600]
    event_type = item.get("event_type") or "CORPORATE_UPDATE"
    group = item.get("duplicate_group") or f"{ticker}:{day}:{event_type}:{re.sub('[^a-z0-9]', '', title.lower())[:35]}"
    return {"event_type": event_type, "date": day, "headline": title[:180],
            "short_factual_summary": summary, "source": item.get("source") or urllib.parse.urlparse(url).hostname,
            "source_type": kind, "materiality": max(0, min(5, int(item.get("materiality", 2)))),
            "official": official, "duplicate_group": group, "url": url,
            "content_verified": bool(item.get("content_verified", True)),
            "direction": item.get("direction"), "conflict": False,
            "retrieved_at": item.get("retrieved_at") or retrieved_at,
            "sources": [{"url": url, "source_type": kind, "source_date": day}]}


def deduplicate(records):
    merged = {}
    for item in records:
        group = item["duplicate_group"]
        if group not in merged:
            merged[group] = item
            continue
        old = merged[group]
        sources = old["sources"] + [s for s in item["sources"] if s not in old["sources"]]
        preferred = min((old, item), key=lambda e: (source_rank(e), -e["materiality"]))
        merged[group] = {**preferred, "materiality": max(old["materiality"], item["materiality"]),
                         "sources": sources,
                         "conflict": old.get("conflict", False) or (old.get("direction") is not None and
                                     item.get("direction") is not None and old["direction"] != item["direction"])}
    return sorted(merged.values(), key=lambda e: (-e["materiality"], source_rank(e), e["date"], e["headline"]))


def sec_records(packet):
    records = []
    for filing in packet.get("recent_filings", []):
        items = filing.get("items") or ""
        event_type = "EARNINGS_FILING" if "2.02" in items else "MATERIAL_SEC_FILING"
        materiality = 5 if "1.01" in items else 4 if "2.02" in items else 3
        records.append({"event_type": event_type, "date": filing["date"],
                        "headline": f"{filing['form']} filing indexed by SEC",
                        "summary": f"SEC index metadata: form {filing['form']}; items {items or 'not listed'}. Filing contents not verified.",
                        "source": "SEC EDGAR", "source_type": "SEC", "materiality": materiality,
                        "content_verified": False,
                        "duplicate_group": f"{packet['ticker']}:{filing['accession']}", "url": filing["url"]})
    return records


def headline_category(title):
    text = title.lower()
    if re.search(r"bankrupt|restatement|fraud|fda (approval|reject)|merger|acquisition", text):
        return "MATERIAL_EVENT", 5
    if re.search(r"guidance|outlook|earnings|quarterly results|annual results|dividend cut", text):
        return "EARNINGS_OR_GUIDANCE", 4
    return "CORPORATE_UPDATE", 2


def fetch_feed(url, _source_type):
    request = urllib.request.Request(url, headers={"User-Agent": "AverraStockResearch/1.0"})
    with urllib.request.urlopen(request, timeout=15) as response:
        raw = response.read(300_001)
    if len(raw) > 300_000:
        raise ValueError("evidence feed too large")
    root = ET.fromstring(raw)
    out = []
    for item in root.findall(".//item")[:20]:
        title = item.findtext("title") or ""
        event_type, materiality = headline_category(title)
        published = item.findtext("pubDate") or ""
        try:
            day = parsedate_to_datetime(published).date().isoformat()
        except (TypeError, ValueError):
            day = published[:10]
        out.append({"title": title, "summary": item.findtext("description") or "",
                    "url": item.findtext("link") or "", "date": day,
                    "event_type": event_type, "materiality": materiality})
    return out


def collect_cached(db, packet, cfg, fetcher=fetch_feed, cache_only=False):
    ticker, cutoff = packet["ticker"], packet["research_cutoff"]
    feeds = cfg.get("evidence_feeds", {}).get(ticker, [])
    fingerprint = hashlib.sha256(compact([packet.get("recent_filings", []),
                                          packet.get("evidence_records", []), feeds]).encode()).hexdigest()
    row = db.execute("""SELECT packet_json FROM research_evidence_cache
        WHERE ticker=? AND cutoff=? AND input_hash=?""", (ticker, cutoff, fingerprint)).fetchone()
    if row:
        return json.loads(row[0])
    if cache_only:
        raise RuntimeError("EVIDENCE_CACHE_MISSING: reasoner retry cannot refetch Phase 4A")
    retrieved_at = utc_now()
    records = sec_records(packet)
    failures = []
    allowed = set(cfg.get("trusted_domains", [])) | set(cfg.get("verified_ir_domains", {}).get(ticker, []))
    for record in packet.get("evidence_records", []):
        url = record.get("url", "")
        host = (urllib.parse.urlparse(url).hostname or "").lower()
        if urllib.parse.urlparse(url).scheme == "https" and any(
                host == domain or host.endswith("." + domain) for domain in allowed):
            records.append(record)
    for feed in feeds:
        url = feed["url"]
        host = (urllib.parse.urlparse(url).hostname or "").lower()
        if urllib.parse.urlparse(url).scheme != "https" or not any(
                host == domain or host.endswith("." + domain) for domain in allowed):
            failures.append({"url": url, "error": "UNTRUSTED_FEED"})
            continue
        try:
            for record in fetcher(url, feed["source_type"]):
                record = {**record, "source_type": feed["source_type"], "source": host}
                normalized = normalize_event(record, ticker, cutoff, retrieved_at)
                if normalized:
                    records.append(normalized)
        except (OSError, ValueError, ET.ParseError) as exc:
            failures.append({"url": url, "error": type(exc).__name__})
    normalized = [item if "short_factual_summary" in item else normalize_event(item, ticker, cutoff, retrieved_at)
                  for item in records]
    events = deduplicate([item for item in normalized if item])
    packet_out = {"ticker": ticker, "research_cutoff": cutoff, "collected_at": retrieved_at,
                  "events": events, "fetch_failures": failures,
                  "status": "SUFFICIENT" if any(e["official"] and e["content_verified"] and
                                                e["materiality"] >= 4 for e in events)
                  else "INSUFFICIENT"}
    with db:
        db.execute("""INSERT INTO research_evidence_cache VALUES(?,?,?,?,?)""",
                   (ticker, cutoff, fingerprint, compact(packet_out), retrieved_at))
    return packet_out


def reasoner_packet(packet, evidence, cfg):
    """Drop low-materiality duplication first; never truncate material evidence."""
    events = list(evidence["events"])
    payload = {"ticker": packet["ticker"], "company": packet["company"],
               "research_cutoff": packet["research_cutoff"],
               "quant_summary": {key: packet["quant"].get(key) for key in
                                 ("score", "rank", "relative_strength_score", "ranking_momentum")},
               "fundamental_summary": {key: packet["fundamental"].get(key) for key in
                                       ("score", "growth", "profitability", "health", "cash_flow",
                                        "valuation", "trend", "confidence", "flags")},
               "evidence_status": evidence["status"], "events": events}
    target_bytes = 4 * cfg["phase4b_target_input_tokens"]
    while len(compact(payload).encode()) > target_bytes:
        removable = next((i for i in range(len(events) - 1, -1, -1)
                          if events[i]["materiality"] < 4), None)
        if removable is None:
            break
        events.pop(removable)
    return payload


def needs_web_escalation(evidence):
    if evidence["status"] == "INSUFFICIENT":
        return True
    return any(event.get("conflict") for event in evidence["events"])

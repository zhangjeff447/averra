"""Read-only, point-in-time context for the local Exit Engine."""
from __future__ import annotations

import json
import re
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path


OFFICIAL = {"COMPANY_IR", "SEC_CONTENT", "SEC_EXHIBIT", "OFFICIAL_REGULATORY"}
DATE_PATTERN = re.compile(r"\b(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\.?\s+\d{1,2},\s+20\d{2}\b", re.I)


def saved_entry_thesis(ticker, research_path):
    path = Path(research_path)
    if not path.is_file():
        return None
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        row = db.execute("""SELECT run_id,tier,completed_at,research_score,response_json
            FROM tier_research_results WHERE ticker=? AND status='COMPLETE'
            ORDER BY completed_at DESC LIMIT 1""", (ticker,)).fetchone()
        if not row:
            return None
        response = json.loads(row[4])
        return {"run_id": row[0], "tier": row[1], "completed_at": row[2],
                "research_score": row[3], "thesis_confidence": response.get("thesis_confidence"),
                "claims": {kind: response.get("claims", {}).get(kind, []) for kind in
                           ("BULL_CASE", "BEAR_CASE", "KEY_CATALYST", "KEY_RISK", "THESIS_INVALIDATION")}}


def _scheduled_date(event):
    if event.get("event_date"):
        return event["event_date"]
    if event.get("event_type") != "EARNINGS_SCHEDULE":
        return None
    for fact in event.get("verified_facts", []):
        if not fact.startswith("OFFICIAL_SOURCE_EXCERPT:"):
            continue
        for raw in DATE_PATTERN.findall(fact):
            cleaned = raw.replace(".", "")
            for pattern in ("%B %d, %Y", "%b %d, %Y"):
                try:
                    stamp = datetime.strptime(cleaned, pattern).date().isoformat()
                    if stamp > event.get("date", ""):
                        return stamp
                except ValueError:
                    pass
    return None


def evidence_events(ticker, asof, entry_date, research_path):
    path = Path(research_path)
    if not path.is_file():
        return []
    cutoff = datetime.fromisoformat(asof + "T23:59:59+00:00")
    found = {}
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        rows = db.execute("""SELECT event_json,duplicate_group FROM evidence_events_v2
            WHERE ticker=? AND evidence_asof<=? ORDER BY evidence_asof DESC""",
            (ticker, cutoff.isoformat())).fetchall()
        for raw, group in rows:
            if group in found:
                continue
            item = json.loads(raw)
            if item.get("date", "") > asof:
                continue
            sources = item.get("sources") or []
            official = item.get("source_type") in OFFICIAL and any(
                source.get("source_quality", "").startswith(("OFFICIAL", "VERIFIED_COMPANY"))
                for source in sources)
            verified = official and item.get("extraction_confidence") in {"HIGH", "MEDIUM"}
            found[group] = {"id": group, "event_type": item.get("event_type"),
                            "date": item.get("date"), "event_date": _scheduled_date(item),
                            "materiality": item.get("materiality"), "official": official,
                            "verified": verified, "thesis_conflict": item.get("thesis_conflict") is True and
                            item.get("date", "") >= entry_date,
                            "new_since_entry": item.get("date", "") >= entry_date,
                            "source": sources[0].get("url") if sources else None}
    return list(found.values())

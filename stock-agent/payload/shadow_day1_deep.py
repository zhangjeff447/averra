#!/usr/bin/env python3
"""Offline Deep triage only; this module never invokes a model."""
from __future__ import annotations

import json
import sqlite3

from day0_2_plan import score_case
from quant_screen import ROOT
from shadow_day1_research import DB, FUNDAMENTAL_DB, PLAN

REPORT = ROOT / "data" / "shadow_day1_deep_plan.json"


def build():
    plan = json.loads(PLAN.read_text())
    result = json.loads((ROOT / "data" / "shadow_day1_initial_report.json").read_text())
    if result["run_id"] != plan["run_id"] or result["sol_calls"] != 0:
        raise ValueError("DAY1_DEEP_TRIAGE_INPUT_INVALID")
    planned = {r["ticker"]: r for r in plan["cases"]}
    pending = [r for r in result["cases"] if r["final_status"] == "DEEP_RESEARCH_PENDING"]
    research = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    fundamental = sqlite3.connect(f"file:{FUNDAMENTAL_DB}?mode=ro", uri=True)
    research.row_factory = fundamental.row_factory = sqlite3.Row
    fundamental_run_id = research.execute("SELECT fundamental_run_id FROM research_runs WHERE id=?",
                                          (plan["source_research_run"],)).fetchone()[0]
    sectors = {}
    for case in plan["cases"]:
        raw = research.execute("SELECT packet_json FROM research_priority WHERE run_id=? AND ticker=?",
                               (plan["source_research_run"], case["ticker"])).fetchone()[0]
        sector = json.loads(raw).get("sector") or "UNKNOWN"
        sectors[sector] = sectors.get(sector, 0) + 1
    rows = []
    for item in pending:
        ticker = item["ticker"]
        case = planned[ticker]
        raw = research.execute("SELECT packet_json FROM research_priority WHERE run_id=? AND ticker=?",
                               (plan["source_research_run"], ticker)).fetchone()[0]
        sector = json.loads(raw).get("sector") or "UNKNOWN"
        scores = fundamental.execute("""SELECT candidate_rank,quant_score,fundamental_score
            FROM fundamental_scores WHERE run_id=? AND ticker=?""",
            (fundamental_run_id, ticker)).fetchone()
        packet = research.execute("""SELECT packet_json FROM evidence_packets_v2
            WHERE ticker=? AND evidence_asof=? ORDER BY created_at DESC LIMIT 1""",
            (ticker, case["evidence_asof"])).fetchone()
        if not scores or not packet:
            raise ValueError(f"{ticker}: DEEP_TRIAGE_DATA_MISSING")
        attempt = research.execute("""SELECT parsed_result_json FROM day01_attempts
            WHERE run_id=? AND ticker=?""", (plan["run_id"], ticker)).fetchone()
        parsed = json.loads(attempt[0]) if attempt and attempt[0] else {}
        score, components, group, reason, factors = score_case(
            item["candidate_score"], scores["quant_score"], scores["fundamental_score"],
            scores["candidate_rank"], sectors[sector], json.loads(packet[0]), parsed,
            item["escalation"] or {})
        rows.append({"ticker": ticker, "deep_priority_score": score, "group": group,
                     "reason": reason, "factors": factors, "components": components,
                     "escalation_reasons": (item["escalation"] or {}).get("reasons", [])})
    rows.sort(key=lambda r: (-r["deep_priority_score"], r["ticker"]))
    for row in [r for r in rows if r["group"] == "P1_DEEP_NOW"][3:]:
        row["group"] = "P2_DEEP_LATER"
        row["reason"] = "P1 maximum of three; Sol not authorized for Day 1"
    report = {"status": "TRIAGE_ONLY", "sol_calls": 0, "deep_pending": len(rows),
              "p1_count": sum(r["group"] == "P1_DEEP_NOW" for r in rows), "rows": rows}
    temporary = REPORT.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    temporary.replace(REPORT)
    research.close(); fundamental.close()
    return report


if __name__ == "__main__":
    print(json.dumps(build(), indent=2))

"""One-shot, budgeted Initial refresh for new official MATERIAL evidence only."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone

from day0_1_research import configuration, cost, evidence_for_escalation
from phase4_run import api_research, load_key
from phase4_tiers import budget_transition, escalation, init_db as init_tiers, reserve
from phase4b_evidence import cached_inputs, estimate, request_body, validate_response
from quant_screen import ROOT
from shadow_day1_initial import local_tier_budget, token_capacity


def compact(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


class IntradayLunaRefresh:
    model = "gpt-5.6-luna"
    web_search = False
    auto_sol = False

    def __init__(self, api=api_research):
        self.api = api

    def __call__(self, ticker, events):
        cfg, frozen = configuration()
        limits = json.loads((ROOT / "shadow_v11_config.json").read_text())
        day = datetime.now(timezone.utc).astimezone(__import__("zoneinfo").ZoneInfo("America/New_York")).date().isoformat()
        key = load_key(ROOT / ".env")
        if not key:
            return self.result("INPUTS_UNAVAILABLE")
        with sqlite3.connect(ROOT / "data" / "research_data.db") as db:
            db.row_factory = sqlite3.Row
            init_tiers(db)
            db.execute("""CREATE TABLE IF NOT EXISTS intraday_luna_attempts (
              run_id TEXT PRIMARY KEY, ticker TEXT NOT NULL, evidence_key TEXT NOT NULL,
              state TEXT NOT NULL, requested_model TEXT NOT NULL, returned_model TEXT,
              raw_response_json TEXT, parsed_json TEXT, request_id TEXT,
              input_tokens INTEGER, output_tokens INTEGER, cost_usd REAL,
              created_at TEXT NOT NULL)""")
            try:
                inputs = cached_inputs(db, ticker, cfg, allowed_tickers={ticker}, citation_mode="short")
            except ValueError:
                return self.result("INPUTS_UNAVAILABLE")
            body = request_body(inputs, cfg)
            if body["model"] != self.model or body.get("tools") or body.get("max_tool_calls"):
                raise ValueError("INTRADAY_MODEL_OR_SEARCH_BLOCK")
            estimate_info = estimate(body, cfg)
            predicted_input = estimate_info["estimated_input_tokens"]
            predicted_output = estimate_info["estimated_output_tokens"]
            predicted_cost = estimate_info["estimated_total_cost_usd"]
            unresolved_prior = db.execute("""SELECT 1 FROM tier_budget_ledger
                WHERE ticker=? AND tier='INITIAL' AND state IN ('RESERVED','IN_FLIGHT','FAILED_UNKNOWN')
                LIMIT 1""", (ticker,)).fetchone()
            if (predicted_input > limits["max_input_tokens_per_stock"] or
                    predicted_cost > limits["max_cost_per_stock_usd"] or
                    unresolved_prior or
                    not token_capacity(db, day, limits, predicted_input, predicted_output)):
                return self.result("BUDGET_BLOCKED")
            evidence_key = ",".join(sorted(e["evidence_id"] for e in events))
            run_id = "INTRADAY_" + day.replace("-", "") + "_" + hashlib.sha256(
                (ticker + evidence_key).encode()).hexdigest()[:16]
            prior = db.execute("SELECT state FROM intraday_luna_attempts WHERE run_id=?", (run_id,)).fetchone()
            if prior:
                return self.result(prior[0])
            budget = local_tier_budget(frozen, limits)
            if reserve(db, day, run_id, ticker, "INITIAL", predicted_input,
                       predicted_output, predicted_cost, budget) != "RESERVED":
                return self.result("BUDGET_BLOCKED")
            with db:
                db.execute("INSERT INTO intraday_luna_attempts VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                           (run_id, ticker, evidence_key, "IN_FLIGHT", self.model, None, None,
                            None, None, None, None, None, datetime.now(timezone.utc).isoformat()))
            budget_transition(db, run_id, ticker, "INITIAL", "IN_FLIGHT")
            try:
                raw, metadata = self.api(body, key)
            except Exception:
                budget_transition(db, run_id, ticker, "INITIAL", "FAILED_UNKNOWN")
                with db:
                    db.execute("UPDATE intraday_luna_attempts SET state='FAILED_UNKNOWN' WHERE run_id=?", (run_id,))
                return self.result("FAILED_UNKNOWN", calls=1, unknown_cost_usd=predicted_cost)
            # Paid data is persisted before usage accounting or parsing.
            with db:
                db.execute("UPDATE intraday_luna_attempts SET raw_response_json=?,returned_model=?,request_id=? "
                           "WHERE run_id=?", (compact(raw), raw.get("model"), metadata.get("request_id"), run_id))
            usage = raw.get("usage") or {}
            used_in, used_out = usage.get("input_tokens"), usage.get("output_tokens")
            if used_in is None or used_out is None:
                budget_transition(db, run_id, ticker, "INITIAL", "FAILED_UNKNOWN",
                                  request_id=metadata.get("request_id"))
                with db:
                    db.execute("UPDATE intraday_luna_attempts SET state='FAILED_UNKNOWN' WHERE run_id=?", (run_id,))
                return self.result("FAILED_UNKNOWN", calls=1, unknown_cost_usd=predicted_cost)
            actual_cost = cost(used_in, used_out, usage.get("input_tokens_details"))
            with db:
                db.execute("UPDATE intraday_luna_attempts SET input_tokens=?,output_tokens=?,cost_usd=? "
                           "WHERE run_id=?", (used_in, used_out, actual_cost, run_id))
            budget_transition(db, run_id, ticker, "INITIAL", "COMPLETED", input_tokens=used_in,
                              output_tokens=used_out, cost_usd=actual_cost,
                              request_id=metadata.get("request_id"))
            try:
                if raw.get("model") != self.model:
                    raise ValueError("RETURNED_MODEL_MISMATCH")
                parsed = validate_response(raw, inputs["allowed_evidence_ids"],
                                           inputs["candidate_score"], cfg)
                initial = {**parsed, "status": parsed["research_status"],
                           "confidence": parsed["thesis_confidence"]}
                if parsed["research_status"] == "COMPLETE":
                    initial["handled_evidence_ids"] = sorted(set(
                        parsed["score_evidence_ids"] + parsed["catalyst_evidence_ids"]))
                decision = escalation(inputs["candidate_score"], initial,
                                      evidence_for_escalation(inputs), frozen)
                state = "DEEP_RESEARCH_PENDING" if decision["decision"] == "ESCALATE_SOL" else "COMPLETE"
                with db:
                    db.execute("UPDATE intraday_luna_attempts SET state=?,parsed_json=? WHERE run_id=?",
                               (state, compact({"result": parsed, "escalation": decision}), run_id))
                return self.result(state, calls=1, input_tokens=used_in,
                                   output_tokens=used_out, cost_usd=actual_cost,
                                   escalate=state == "DEEP_RESEARCH_PENDING")
            except (ValueError, KeyError, TypeError, json.JSONDecodeError):
                with db:
                    db.execute("UPDATE intraday_luna_attempts SET state='FAILED_VALIDATION' WHERE run_id=?", (run_id,))
                return self.result("FAILED_VALIDATION", calls=1, input_tokens=used_in,
                                   output_tokens=used_out, cost_usd=actual_cost)

    def result(self, status, *, calls=0, input_tokens=0, output_tokens=0,
               cost_usd=0, unknown_cost_usd=0, escalate=False):
        return {"model": self.model, "web_search": False, "sol_calls": 0,
                "status": status, "calls": calls, "input_tokens": input_tokens,
                "output_tokens": output_tokens, "cost_usd": cost_usd,
                "unknown_cost_usd": unknown_cost_usd, "escalate": escalate}

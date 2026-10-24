#!/usr/bin/env python3
"""Two bounded Robinhood reads only; never preview, submit, or alter a ledger."""

import argparse
import copy
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.validate_robinhood_read_only import ReadOnlyMCPAdapter
from strategies.qqq_smh_agent.score_dynamic_v1 import (
    format_simulated_daily_report, plan, score_tier, select_one_for_existing_executor,
)
from tools.ops_utils import is_regular_market_hours, is_trading_day
from tools.ops_utils import load_env_file
from tools.v1_observed_snapshot import ACTIVE_INTENTS, TERMINAL_ORDERS, observe_position_quote_snapshot
from tools.v1_state_store import V1StateStore


def read_local_intents(stage_db, legacy_state, legacy_ledger):
    state_bytes = legacy_state.read_bytes()
    state = json.loads(state_bytes)
    if not isinstance(state, dict):
        raise ValueError("legacy_execution_state_invalid")
    pending = state.get("pending_submission")
    known_open = state.get("known_open_order_ids")
    if known_open is None or not isinstance(known_open, list):
        raise ValueError("legacy_open_order_state_unavailable")
    local = []
    if pending or known_open or state.get("trading_halted") is True:
        local.append({"status": "EXECUTION_UNKNOWN"})
    store = V1StateStore.open_read_only(stage_db)
    try:
        if not store.initialized() or store.halt_reason():
            raise ValueError("stage_budget_unreconciled")
        saved = store.db.execute("SELECT value FROM meta WHERE key='account_hash'").fetchone()
        source = store.db.execute("SELECT value FROM meta WHERE key='migration_source_hash'").fetchone()
        migrated = store.db.execute(
            "SELECT at FROM audit WHERE event='LEGACY_MIGRATED' ORDER BY seq DESC LIMIT 1").fetchone()
        active_month = store.db.execute("SELECT value FROM meta WHERE key='active_month'").fetchone()
        current_month = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m")
        if (not saved or not source or not migrated or not active_month or active_month[0] != current_month):
            raise ValueError("stage_budget_snapshot_stale")
        source_hash = hashlib.sha256(legacy_ledger.read_bytes()).hexdigest()
        if source_hash != source[0]:
            raise ValueError("migration_delta_pending")
        local.extend({"status": row["status"]} for row in store.open_intents()
                     if row["status"] in ACTIVE_INTENTS)
        return (local, saved[0], store.month_snapshot(), source_hash,
                hashlib.sha256(state_bytes).hexdigest(), migrated[0])
    finally:
        store.db.close()


def read_effective_policy(production_env):
    """Read selected installed limits; never print the full launchd environment."""
    config = load_env_file(production_env)
    loaded = subprocess.run(
        ["launchctl", "print", f"gui/{os.getuid()}/com.qqqsmh.agent"],
        capture_output=True, text=True, timeout=10, check=True).stdout
    keys = ("MONTHLY_BUY_LIMIT", "DAILY_BUY_LIMIT", "MAX_ORDER_AMOUNT", "MIN_ORDER_AMOUNT",
            "SMH_TARGET_WEIGHT", "QQQ_TARGET_WEIGHT")
    overrides = {}
    for key in keys:
        match = re.search(r"(?m)^\s*" + key + r"\s*=>\s*([^\s]+)", loaded)
        if match:
            overrides[key] = match.group(1)
    required = ("MONTHLY_BUY_LIMIT", "DAILY_BUY_LIMIT", "MAX_ORDER_AMOUNT", "MIN_ORDER_AMOUNT")
    if any(key not in overrides for key in required):
        raise ValueError("loaded_trading_limits_unavailable")
    values = {key: overrides.get(key, config.get(key)) for key in keys}
    if values["SMH_TARGET_WEIGHT"] != "0.80" or values["QQQ_TARGET_WEIGHT"] != "0.20":
        raise ValueError("target_weights_not_80_20")
    return {"monthly_new_allowance": values["MONTHLY_BUY_LIMIT"],
            "daily_limit": values["DAILY_BUY_LIMIT"],
            "per_order_limit": min(100.0, float(values["MAX_ORDER_AMOUNT"])),
            "loaded_min_order": values["MIN_ORDER_AMOUNT"],
            "file_min_order": config.get("MIN_ORDER_AMOUNT"),
            "target_weights": {"SMH": values["SMH_TARGET_WEIGHT"],
                               "QQQ": values["QQQ_TARGET_WEIGHT"]}}


def budget_snapshot_freshness(snapshot, migrated_at):
    """Reject a staged budget if broker evidence cannot rule out newer Agent activity."""
    if (snapshot.get("orders_readable") is not True
            or snapshot.get("budget_history_complete") is not True
            or not isinstance(snapshot.get("v1_broker_orders"), list)):
        return "BUDGET_SNAPSHOT_STALE", "BROKER_ORDER_HISTORY_UNVERIFIED"
    try:
        migrated = datetime.fromisoformat(migrated_at.replace("Z", "+00:00"))
        if migrated.tzinfo is None:
            raise ValueError("migration_time_without_zone")
    except (TypeError, ValueError, AttributeError):
        return "BUDGET_SNAPSHOT_STALE", "MIGRATION_TIME_UNVERIFIED"
    for order in snapshot["v1_broker_orders"]:
        if str(order.get("placed_agent") or "").strip().lower() not in {"agentic", "agent"}:
            continue
        observed = []
        for field in ("created_at", "updated_at", "filled_at"):
            raw = order.get(field)
            if not raw:
                continue
            try:
                parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    raise ValueError("order_time_without_zone")
                observed.append(parsed)
            except ValueError:
                return "BUDGET_SNAPSHOT_STALE", "AGENT_ORDER_TIME_UNVERIFIED"
        if not observed:
            return "BUDGET_SNAPSHOT_STALE", "AGENT_ORDER_TIME_MISSING"
        if max(observed) > migrated:
            return "BUDGET_SNAPSHOT_STALE", "AGENT_ORDER_CHANGED_AFTER_MIGRATION"
    return "CURRENT", "NO_AGENT_ORDER_CHANGE_AFTER_MIGRATION"


def current_strategy_scores(snapshot, policy):
    """Historical test helper using the sole signal engine, never an entrypoint."""
    from strategies.qqq_smh_agent.strategy_agent import StrategyAgent
    from tools.daily_history import DailyHistory
    from tools.volatility_context import VolatilityProvider
    from tools.robinhood_runtime import TradingRuntime
    runtime = object.__new__(TradingRuntime)
    history = DailyHistory()
    closes = {s: history.get_closes(s) for s in ('SMH', 'QQQ')}
    volatility = VolatilityProvider().fetch()
    decisions = StrategyAgent().evaluate_signals(runtime._build_holdings(snapshot), float(snapshot['cash']),
        closes, volatility_context=volatility)['decisions']
    scores = {s: decisions[s]['metadata']['signal_score'] for s in decisions}
    return scores, {'source': 'StrategyAgent.evaluate_signals',
        'daily_history': 'completed_daily_sessions_checked_by_DailyHistory'}


def calculate_plan_diagnostic(snapshot, observation, scores, budget, policy, *, state=None, store=None):
    """Reuse the production V1 planner; conditional cash never becomes permitted money."""
    reason = observation.get("reconciliation_status", "OBSERVED_SNAPSHOT_UNVERIFIED")
    conditional = observation.get("conditional_valuation") is True
    if not (observation.get("cash_semantics_verified") is True or conditional):
        return {"PLAN_CALCULATION_STATUS": "DATA_INCOMPLETE", "PLAN_TYPE": "NOT_CALCULATED", "missing": [reason],
                "EXECUTION_ELIGIBILITY": "BLOCKED", "telegram_preview": None}
    if not isinstance(scores, dict) or any(score_tier(scores.get(symbol))[0] == "SCORE_INVALID"
                                               for symbol in ("SMH", "QQQ")):
        return {"PLAN_CALCULATION_STATUS": "DATA_INCOMPLETE", "PLAN_TYPE": "NOT_CALCULATED",
                "missing": ["current_valid_score:SMH_or_QQQ"],
                "EXECUTION_ELIGIBILITY": "BLOCKED", "telegram_preview": None}
    trial = copy.deepcopy(snapshot)
    trial.update(budget, budget_readable=True, monthly_new_allowance=policy["monthly_new_allowance"])
    if conditional:
        trial.pop("cash_normalization", None)
        trial["cash"] = observation["observed_cash_for_weight"]
        trial["buying_power"] = observation["observed_buying_power"]
        trial["portfolio_value"] = observation["rebuilt_total"]
        for symbol in ("SMH", "QQQ"):
            trial["positions"][symbol]["market_value"] = observation["market_values"][symbol]
    else:
        trial["cash_normalization"] = observation
        trial["cash"] = observation["cash_for_weight"]
        trial["buying_power"] = observation["broker_spendable_cash"]
        trial["portfolio_value"] = observation["rebuilt_total"]
    result = plan(trial, scores, daily_limit=policy["daily_limit"],
                  per_order_limit=policy["per_order_limit"], snapshot_id=observation.get("snapshot_id"))
    if "total_assets" not in result:
        return {"PLAN_CALCULATION_STATUS": "DATA_INCOMPLETE", "PLAN_TYPE": "NOT_CALCULATED",
                "missing": [result.get("reason", "plan_inputs_unavailable")],
                "EXECUTION_ELIGIBILITY": "BLOCKED", "telegram_preview": None}
    candidate = select_one_for_existing_executor(result)
    selected_candidate = candidate
    display = copy.deepcopy(result)
    if conditional:
        for item in display["etfs"].values():
            item["calculation_reason"] = item["reason"]
            item["conditional_preapproval_amount"] = item["final_permitted_amount"]
            item["final_permitted_amount"] = "UNKNOWN"
            item["execution_state"] = "NOT_EXECUTABLE"
            item["decision"] = "WAIT"
            item["reason"] = reason
        display["overall"] = "WAIT"
    preview = format_simulated_daily_report(display)
    if conditional:
        preview = ("CONDITIONAL PLAN — NOT EXECUTABLE. Assumption: no unresolved broker approval "
                   "and no approval cash hold; this is unverified. Final permitted amounts are UNKNOWN.\n"
                   + preview)
        preview += "\n" + "\n".join(
            f"{symbol} Calculation Reason: {item['calculation_reason']}"
            for symbol, item in display["etfs"].items())
    risk = "NOT_CHECKED" if candidate else "NO_BUY_CANDIDATE"
    if candidate and state is not None:
        from tools.robinhood_runtime import TradingRuntime
        runtime = object.__new__(TradingRuntime)
        runtime.state = state
        runtime.v1_store = store
        runtime.min_order = 0.0  # Proposed V1 policy; installed launchd still overrides to 20.
        runtime.monthly_limit = float(trial["monthly_budget_limit"])
        runtime.daily_limit = float(policy["daily_limit"])
        risk_decision = {"action": "BUY", "decision_type": "BUY", "symbol": candidate["symbol"],
                         "amount": candidate["amount"], "metadata": {"strategy_version": result["policy_version"]}}
        risk_snapshot = dict(trial, monthly_budget_limit=runtime.monthly_limit)
        if store is None:
            allowed, risk = False, "SMH_DAILY_HISTORY_UNVERIFIED"
        else:
            allowed, risk = TradingRuntime._risk_check(runtime, risk_decision, risk_snapshot)
        if not allowed:
            candidate = None
    eligibility = ("BLOCKED_APPROVAL_STATE_UNVERIFIED" if reason == "APPROVAL_STATE_UNVERIFIED" else
                   "WAIT_PENDING_APPROVAL" if reason == "WAIT_PENDING_APPROVAL" else
                   "BLOCKED_EXISTING_RISK:" + risk if risk not in ("ok", "NO_BUY_CANDIDATE") else
                   "NO_BUY_WAIT" if candidate is None else "SAFETY_GATES_PASSED_DRY_RUN_ONLY")
    return {"PLAN_CALCULATION_STATUS": "COMPLETE",
            "PLAN_TYPE": "CONDITIONAL_PLAN" if conditional else "VERIFIED_PLAN",
            "EXECUTION_ELIGIBILITY": eligibility, "plan": display,
            "conditional_candidate": candidate if conditional else None,
            "execution_candidate": candidate if not conditional else None,
            "selection_reason": ("BLOCKED_EXISTING_RISK:" + risk if selected_candidate and not candidate else
                                 "HIGHEST_FINAL_SCORE_THEN_TARGET_GAP" if candidate else
                                 "NO_BUY_CANDIDATE"),
            "risk_check": risk, "telegram_preview": preview}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=("auto", "non-market", "market"), default="auto")
    parser.add_argument("--stage-db", type=Path, required=True)
    parser.add_argument("--legacy-state", type=Path, required=True)
    parser.add_argument("--legacy-ledger", type=Path, required=True)
    parser.add_argument("--production-env", type=Path)
    args = parser.parse_args()
    now_et = datetime.now(ZoneInfo("America/New_York"))
    report = {"checked_at": now_et.isoformat(), "phase": args.phase,
              "execution_mode": "DRY_RUN_ONLY", "broker_writes": 0,
              "formal_ledger_writes": 0}
    local = []
    stage_account_hash = source_hash = state_hash = migrated_at = None
    budget = None
    policy = None
    try:
        local, stage_account_hash, budget, source_hash, state_hash, migrated_at = read_local_intents(
            args.stage_db, args.legacy_state, args.legacy_ledger)
        report["local_intents"] = "CLEAR" if not local else "ACTIVE_OR_UNKNOWN"
        report["budget_migration"] = "SOURCE_HASH_CURRENT_BROKER_RECHECK_PENDING"
        report["budget_snapshot_as_of"] = migrated_at
    except Exception as exc:
        report["local_intents"] = "UNVERIFIED"
        report["budget_migration"] = "BUDGET_SNAPSHOT_STALE"
        report["budget_snapshot_reason"] = str(exc)
    try:
        policy = read_effective_policy(args.production_env or args.legacy_state.parent.parent / ".env.production")
        report["effective_policy"] = policy
    except Exception as exc:
        report["effective_policy"] = f"UNVERIFIED:{type(exc).__name__}"
    try:
        adapter = ReadOnlyMCPAdapter(workspace=str(ROOT), server_name="robinhood-trading",
                                     require_native_read_proof=True, direct_transport=True,
                                     regular_trade_quotes=True)
        adapter.SYNC_RETRY_DELAYS = ()  # At most two complete account batches.
        report["approval_capability"] = "FROZEN_DISCOVERY_2026-10-03T01:12:05Z:84_TOOLS_NO_APPROVAL_READ"
        before = adapter.fetch_live_account_snapshot()
        report["account_scope"] = ("MATCH" if before.get("agentic_account_confirmed") is True
                                   and stage_account_hash and before.get("v1_account_hash") == stage_account_hash
                                   else "UNVERIFIED_OR_MISMATCH")
        orders = before.get("v1_broker_orders")
        if before.get("orders_readable") is not True or not isinstance(orders, list):
            report["orders"] = "INCOMPLETE"
        elif all(isinstance(order, dict) and str(order.get("state") or order.get("status") or "").lower()
                 in TERMINAL_ORDERS for order in orders):
            report["orders"] = "NO_OPEN_ORDERS"
        else:
            report["orders"] = "OPEN_OR_UNKNOWN"
        if budget is not None:
            freshness, freshness_reason = budget_snapshot_freshness(before, migrated_at)
            report["budget_migration"] = freshness
            report["budget_snapshot_reason"] = freshness_reason
            if freshness != "CURRENT":
                budget = None
        report["positions"] = "READABLE" if before.get("positions_readable") is True else "UNVERIFIED"
        report["approvals"] = "APPROVAL_STATE_UNVERIFIED"  # No missing list is treated as empty.
        if args.phase == "non-market" or not is_trading_day(now_et) or not is_regular_market_hours(now_et):
            report["market_window"] = "LIVE_WINDOW_VALIDATION_PENDING"
            report["PLAN_CALCULATION_STATUS"] = "LIVE_WINDOW_VALIDATION_PENDING"
            report["PLAN_TYPE"] = "NOT_CALCULATED"
            report["EXECUTION_ELIGIBILITY"] = ("BLOCKED_BUDGET_SNAPSHOT_STALE" if budget is None else
                                               "BLOCKED_APPROVAL_STATE_UNVERIFIED")
            report["status"] = report["EXECUTION_ELIGIBILITY"]
            print(json.dumps(report, sort_keys=True))
            return 2
        after = adapter.fetch_live_account_snapshot()
        report["market_window"] = "OBSERVED"
        if source_hash is None or state_hash is None or budget is None or policy is None:
            raise ValueError("BUDGET_SNAPSHOT_STALE" if budget is None else "local_policy_or_intents_unverified")
        if hashlib.sha256(args.legacy_ledger.read_bytes()).hexdigest() != source_hash:
            raise ValueError("migration_delta_during_read")
        if hashlib.sha256(args.legacy_state.read_bytes()).hexdigest() != state_hash:
            raise ValueError("local_execution_state_changed_during_read")
        if before.get("v1_account_hash") != stage_account_hash or after.get("v1_account_hash") != stage_account_hash:
            raise ValueError("stage_broker_account_mismatch")
        for row in (before, after):
            row.update(budget, budget_readable=True)
        result = observe_position_quote_snapshot(before, after, local)
        report["status"] = result["reconciliation_status"]
        report["observation"] = result["observation"]
        report["snapshot_id"] = result.get("snapshot_id")
        report["PLAN_CALCULATION_STATUS"] = "DATA_INCOMPLETE"
        report["PLAN_TYPE"] = "NOT_CALCULATED"
        report["EXECUTION_ELIGIBILITY"] = "BLOCKED_APPROVAL_STATE_UNVERIFIED"
        if result.get("cash_semantics_verified") is True or result.get("conditional_valuation") is True:
            from tools.robinhood_runtime import LiveExecutionState
            scoring_snapshot = copy.deepcopy(before)
            scoring_snapshot.update(budget, budget_readable=True)
            if result.get("conditional_valuation") is True:
                scoring_snapshot["cash"] = result["observed_cash_for_weight"]
                scoring_snapshot["buying_power"] = result["observed_buying_power"]
                scoring_snapshot["portfolio_value"] = result["rebuilt_total"]
                for symbol in ("SMH", "QQQ"):
                    scoring_snapshot["positions"][symbol]["market_value"] = result["market_values"][symbol]
            try:
                scores, provenance = current_strategy_scores(scoring_snapshot, policy)
                report["scores"] = scores
                report["score_provenance"] = provenance
                completed = datetime.fromisoformat(after["v1_read_completed_at"].replace("Z", "+00:00"))
                if (datetime.now(timezone.utc) - completed).total_seconds() > 60:
                    raise ValueError("snapshot_stale_after_scoring")
                for quote in result["observation"]["quotes"].values():
                    provider_time = datetime.fromisoformat(quote["provider_as_of"].replace("Z", "+00:00"))
                    if (datetime.now(timezone.utc) - provider_time).total_seconds() > 60:
                        raise ValueError("quote_stale_after_scoring")
                store = V1StateStore.open_read_only(args.stage_db)
                try:
                    diagnostic = calculate_plan_diagnostic(
                        before, result, scores, budget, policy,
                        state=LiveExecutionState.load(args.legacy_state), store=store)
                finally:
                    store.db.close()
                report.update(diagnostic)
                if (hashlib.sha256(args.legacy_ledger.read_bytes()).hexdigest() != source_hash
                        or hashlib.sha256(args.legacy_state.read_bytes()).hexdigest() != state_hash):
                    raise ValueError("source_changed_during_scoring")
                if policy["loaded_min_order"] != policy["file_min_order"]:
                    report.setdefault("additional_blocks", []).append("LOADED_MIN_ORDER_CONFLICT")
                if local:
                    report.setdefault("additional_blocks", []).append("LOCAL_INTENT_OR_UNKNOWN")
                if report.get("additional_blocks") and report["EXECUTION_ELIGIBILITY"] == "SAFETY_GATES_PASSED_DRY_RUN_ONLY":
                    report["EXECUTION_ELIGIBILITY"] = "BLOCKED_ADDITIONAL_SAFETY_GATES"
            except Exception as exc:
                for key in ("plan", "conditional_candidate", "execution_candidate", "telegram_preview"):
                    report.pop(key, None)
                report["PLAN_CALCULATION_STATUS"] = "DATA_INCOMPLETE"
                report["PLAN_TYPE"] = "NOT_CALCULATED"
                report["missing"] = [type(exc).__name__ + ":" + str(exc)]
                report["EXECUTION_ELIGIBILITY"] = "BLOCKED"
        else:
            report["missing"] = [result["reconciliation_status"]]
        print(json.dumps(report, sort_keys=True))
        return 0 if report["EXECUTION_ELIGIBILITY"] == "SAFETY_GATES_PASSED_DRY_RUN_ONLY" else 2
    except Exception as exc:
        report["status"] = ("BUDGET_SNAPSHOT_STALE" if report.get("budget_migration") == "BUDGET_SNAPSHOT_STALE"
                            else "OBSERVED_SNAPSHOT_UNVERIFIED")
        report["reason"] = str(exc)
        report["PLAN_CALCULATION_STATUS"] = "DATA_INCOMPLETE"
        report["PLAN_TYPE"] = "NOT_CALCULATED"
        report["EXECUTION_ELIGIBILITY"] = ("BLOCKED_BUDGET_SNAPSHOT_STALE" if budget is None else "BLOCKED")
        print(json.dumps(report, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit("Use run_agent.py; legacy direct entry is disabled in the unified candidate.")

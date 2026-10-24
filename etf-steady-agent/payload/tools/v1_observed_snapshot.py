"""Two-read, zero-open-order valuation contract for isolated ETF V1."""

import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from tools.ops_utils import is_regular_market_hours, is_trading_day


TERMINAL_ORDERS = {"filled", "cancelled", "canceled", "rejected", "failed",
                   "expired", "voided", "partially_filled_rest_cancelled"}
from tools.v1_state_store import ACTIVE
from tools.approval_compatibility import NATIVE_MODE
ACTIVE_INTENTS = set(ACTIVE)
OTHER_ASSETS = ("options_value", "futures_value", "event_contracts_value",
                "crypto_value", "mutual_funds_value", "fixed_income_value")


def _number(value):
    try:
        result = Decimal(str(value))
    except (ValueError, TypeError, InvalidOperation):
        return None
    return result if result.is_finite() and result >= 0 else None


def _time(value):
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return result.astimezone(timezone.utc) if result.tzinfo else None
    except (ValueError, TypeError):
        return None


def observe_position_quote_snapshot(before, after, local_intents=(), *, now=None,
                                    max_age_seconds=60, value_tolerance="0.25", approval_mode=NATIVE_MODE):
    """Accept two bounded read-only observations, never a broker atomic snapshot."""
    now = now or datetime.now(timezone.utc)
    evidence = {"valuation_method": "OBSERVED_POSITION_QUOTE_SNAPSHOT_V1",
                "valuation_started_at": before.get("v1_read_started_at"),
                "valuation_completed_at": after.get("v1_read_completed_at"),
                "account_received_at": before.get("v1_read_completed_at"),
                "positions_received_at": before.get("v1_read_completed_at"),
                "orders_received_at": before.get("v1_read_completed_at"),
                "cash_received_at": before.get("v1_read_completed_at"),
                "recheck_account_received_at": after.get("v1_read_completed_at"),
                "recheck_positions_received_at": after.get("v1_read_completed_at"),
                "recheck_orders_received_at": after.get("v1_read_completed_at"),
                "recheck_cash_received_at": after.get("v1_read_completed_at"),
                "receipt_basis": "batch_completion_upper_bound;not_atomic",
                "portfolio_provider_as_of": "UNKNOWN",
                "quotes": {}, "consistency_check_result": "OBSERVED_SNAPSHOT_UNVERIFIED"}

    def wait(reason):
        evidence["consistency_check_result"] = reason
        return {"cash_semantics_verified": False, "reconciliation_status": reason,
                "observation": evidence}

    times = [_time(row.get(key)) for row, key in (
        (before, "v1_read_started_at"), (before, "v1_read_completed_at"),
        (after, "v1_read_started_at"), (after, "v1_read_completed_at"))]
    if any(value is None for value in times) or times != sorted(times):
        return wait("OBSERVED_SNAPSHOT_UNVERIFIED")
    if (now - times[0]).total_seconds() > max_age_seconds or (now - times[0]).total_seconds() < -5:
        return wait("OBSERVED_SNAPSHOT_UNVERIFIED")
    if (times[3] - times[0]).total_seconds() > max_age_seconds:
        return wait("OBSERVED_SNAPSHOT_UNVERIFIED")
    local_now = times[3].astimezone(ZoneInfo("America/New_York"))
    if not is_trading_day(local_now) or not is_regular_market_hours(local_now):
        return wait("OBSERVED_SNAPSHOT_UNVERIFIED")

    account_hash = before.get("v1_account_hash")
    if (not account_hash or account_hash != after.get("v1_account_hash")
            or before.get("agentic_account_confirmed") is not True
            or after.get("agentic_account_confirmed") is not True):
        return wait("ACCOUNT_SCOPE_UNVERIFIED")
    if (before.get("budget_readable") is not True or after.get("budget_readable") is not True
            or any(before.get(key) != after.get(key) for key in (
                "monthly_budget_limit", "monthly_filled_buys", "monthly_new_allowance"))):
        return wait("RECONCILIATION_REQUIRED")
    approval_block = None
    approval_proofs = []
    for row in (before, after):
        if row.get("positions_readable") is not True:
            return wait("OBSERVED_SNAPSHOT_UNVERIFIED")
        if row.get("v1_other_equity_positions") != []:
            return wait("ACCOUNT_SCOPE_UNVERIFIED")
        if _number(row.get("v1_unsettled_funds")) != 0:
            return wait("OBSERVED_SNAPSHOT_UNVERIFIED")
        if row.get("orders_readable") is not True or not isinstance(row.get("v1_broker_orders"), list):
            return wait("ORDERS_INCOMPLETE")
        approvals = row.get("v1_pending_approvals")
        approvals_valid = isinstance(approvals, dict) and (
                approvals.get("source") == "broker_mcp"
                and approvals.get("account_hash") == account_hash
                and approvals.get("complete") is True
                and approvals.get("statuses_verified") is True
                and isinstance(approvals.get("active_count"), int)
                and approvals["active_count"] >= 0)
        if not approvals_valid:
            approval_block = "APPROVAL_STATE_UNVERIFIED"
        elif approvals["active_count"] and approval_block is None:
            approval_block = "WAIT_PENDING_APPROVAL"
        if approval_mode == NATIVE_MODE:
            setting = row.get("v1_approval_setting")
            if not isinstance(setting, dict) or not (
                    setting.get("source") == "broker_mcp"
                    and setting.get("account_hash") == account_hash
                    and type(setting.get("enabled")) is bool):
                approval_block = "APPROVAL_STATE_UNVERIFIED"
                approval_proofs.append(None)
            else:
                approval_proofs.append((setting["enabled"], approvals["active_count"])
                                        if approvals_valid else None)
                if setting["enabled"] and approvals_valid and approval_block is None:
                    approval_block = "TRADE_APPROVAL_REQUIRED"
        for order in row["v1_broker_orders"]:
            if not isinstance(order, dict):
                return wait("ORDERS_INCOMPLETE")
            if str(order.get("state") or order.get("status") or "").lower() not in TERMINAL_ORDERS:
                return wait("WAIT_OPEN_ORDER")
    if any(str(row.get("status") or "").upper() in ACTIVE_INTENTS for row in local_intents):
        return wait("RECONCILIATION_REQUIRED")
    if approval_mode == NATIVE_MODE:
        if len(approval_proofs) != 2 or None in approval_proofs:
            approval_block = "APPROVAL_STATE_UNVERIFIED"
        elif approval_proofs[0] != approval_proofs[1]:
            return wait("RECONCILIATION_REQUIRED")
    if json.dumps(before["v1_broker_orders"], sort_keys=True, default=str) != json.dumps(
            after["v1_broker_orders"], sort_keys=True, default=str):
        return wait("RECONCILIATION_REQUIRED")

    fields = []
    for row in (before, after):
        item = row.get("v1_portfolio_fields")
        if not isinstance(item, dict):
            return wait("OBSERVED_SNAPSHOT_UNVERIFIED")
        cash = _number(item.get("cash"))
        power = _number(item.get("unleveraged_buying_power"))
        reported = _number(item.get("total_value"))
        equity = _number(item.get("equity_value"))
        deposits = _number(item.get("pending_deposits"))
        if None in (cash, power, reported, equity, deposits) or deposits != 0:
            return wait("OBSERVED_SNAPSHOT_UNVERIFIED")
        if any(_number(item.get(key)) != 0 for key in OTHER_ASSETS):
            return wait("ACCOUNT_SCOPE_UNVERIFIED")
        fields.append((cash, power, reported, equity))
    tolerance = Decimal(value_tolerance)
    evidence["broker_reported_total"] = str(fields[0][2])
    evidence["recheck_broker_reported_total"] = str(fields[1][2])
    evidence["reported_total_change"] = str(fields[1][2] - fields[0][2])
    evidence["broker_total_minus_equity_cash"] = [str(row[2] - row[3] - row[0]) for row in fields]
    if (fields[0][:2] != fields[1][:2]
            or abs(fields[0][2] - fields[1][2]) > tolerance):
        return wait("OBSERVED_SNAPSHOT_UNVERIFIED")
    account_total_mismatch = any(abs(row[2] - row[3] - row[0]) > tolerance for row in fields)

    values = {}
    for symbol in ("SMH", "QQQ"):
        first = (before.get("positions") or {}).get(symbol)
        second = (after.get("positions") or {}).get(symbol)
        if not isinstance(first, dict) or not isinstance(second, dict):
            return wait("OBSERVED_SNAPSHOT_UNVERIFIED")
        quantity = _number(first.get("quantity", first.get("qty")))
        later_quantity = _number(second.get("quantity", second.get("qty")))
        if quantity is None or quantity != later_quantity:
            return wait("OBSERVED_SNAPSHOT_UNVERIFIED")
        quote = (before.get("quotes") or {}).get(symbol)
        if not isinstance(quote, dict) or quote.get("source") != "robinhood_mcp":
            return wait("OBSERVED_SNAPSHOT_UNVERIFIED")
        price = _number(quote.get("last"))
        provider_as_of = _time(quote.get("valuation_provider_as_of"))
        received_at = _time(quote.get("received_at"))
        evidence["quotes"][symbol] = {
            "price_type": quote.get("price_type"), "source": quote.get("source"),
            "provider_as_of": quote.get("valuation_provider_as_of") or "UNKNOWN",
            "received_at": quote.get("received_at"), "price": str(price) if price is not None else None}
        if (price is None or price <= 0 or quote.get("price_type") != "last"
                or quote.get("price_session") != "regular" or quote.get("is_stale") is not False
                or provider_as_of is None or received_at is None
                or not times[0] <= received_at <= times[3]
                or not 0 <= (now - provider_as_of).total_seconds() <= max_age_seconds):
            return wait("OBSERVED_SNAPSHOT_UNVERIFIED")
        values[symbol] = quantity * price
    rebuilt = sum(values.values()) + fields[0][0]
    difference = fields[0][2] - rebuilt
    evidence.update({"rebuilt_total": str(rebuilt), "valuation_difference": str(difference)})
    if (account_total_mismatch or abs(difference) > tolerance
            or abs(fields[1][2] - rebuilt) > tolerance):
        return wait("OBSERVED_SNAPSHOT_UNVERIFIED")
    evidence["consistency_check_result"] = "OBSERVED_SNAPSHOT_VERIFIED"
    fingerprint = {"account_hash": account_hash, "fields": [str(x) for x in fields[0]],
                   "positions": {s: str(values[s]) for s in values}, "evidence": evidence,
                   "orders": before["v1_broker_orders"]}
    snapshot_id = hashlib.sha256(json.dumps(fingerprint, sort_keys=True, default=str).encode()).hexdigest()[:24]
    evidence["snapshot_id"] = snapshot_id
    if approval_block and approval_mode == NATIVE_MODE:
        evidence["consistency_check_result"] = "OBSERVED_VALUATION_VERIFIED_CONDITIONAL"
        return {"cash_semantics_verified": False, "reconciliation_status": approval_block,
                "conditional_valuation": True, "snapshot_id": snapshot_id,
                "observed_cash_for_weight": str(fields[0][0]),
                "observed_buying_power": str(fields[0][1]),
                "market_values": {s: str(v) for s, v in values.items()},
                "rebuilt_total": str(rebuilt), "observation": evidence}
    return {"cash_semantics_verified": True, "reconciliation_status": "ZERO_OPEN_ORDER_CASH_PATH_VERIFIED",
            "snapshot_id": snapshot_id, "cash_for_weight": str(fields[0][0]),
            "broker_spendable_cash": str(fields[0][1]), "local_unreflected_reservations": "0",
            "local_unreflected_by_symbol": {"SMH": "0", "QQQ": "0"},
            "safe_spendable_cash": str(min(fields[0][0], fields[0][1])),
            "market_values": {s: str(v) for s, v in values.items()},
            "cash_basis": "gross_zero_open_observed", "rebuilt_total": str(rebuilt),
            "broker_total": str(fields[0][2]), "valuation_difference": str(difference),
            "observation": evidence}

"""Conservative cash and two-ETF account reconciliation for the V1 path."""

import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation


from tools.v1_state_store import ACTIVE
OPEN = set(ACTIVE)
BROKER_OPEN = {"open", "new", "pending", "pending_cancelled", "submitted", "partially_filled", "queued", "confirmed", "unconfirmed"}
OTHER_ASSETS = ("options_value", "futures_value", "event_contracts_value",
                "crypto_value", "mutual_funds_value", "fixed_income_value")


def amount(value, name):
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError(name + "_unavailable") from None
    if not result.is_finite() or result < 0:
        raise ValueError(name + "_invalid")
    return result


def _time(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else None


def normalize_cash(snapshot, local_intents=(), *, cash_basis=None,
                   buying_power_reflects_open_orders=None,
                   time_tolerance_seconds=60, max_snapshot_age_seconds=60,
                   value_tolerance="0.25"):
    """Return verified cash for weights and a separate safe spend cap, or WAIT.

    Open-order semantics are explicit evidence inputs, never guessed from a
    portfolio total. The default is deliberately unverifiable for open orders.
    """
    fingerprint = {key: snapshot.get(key) for key in (
        "cash", "buying_power", "portfolio_value", "equity_value", "positions",
        "quotes", "open_orders", "account_sync", "account_scope")}
    fingerprint["local_intents"] = list(local_intents)
    snapshot_id = hashlib.sha256(json.dumps(fingerprint, sort_keys=True, default=str).encode()).hexdigest()[:24]
    base = {"cash_semantics_verified": False, "reconciliation_status": "CASH_SEMANTICS_UNVERIFIED",
            "snapshot_id": snapshot_id,
            "source_fields": {"cash": "portfolio.cash", "spendable": "portfolio.buying_power.unleveraged_buying_power",
                              "total": "portfolio.total_value", "equity": "portfolio.equity_value"},
            "source_timestamps": {"portfolio_provider_as_of": snapshot.get("portfolio_provider_as_of"),
                                  "portfolio_received_at": (snapshot.get("account_sync") or {}).get("account_data_timestamp"),
                                  "quote_provider_as_of": {s: (snapshot.get("quotes") or {}).get(s, {}).get("timestamp")
                                                           for s in ("SMH", "QQQ")}}}

    def fail(reason):
        return dict(base, reconciliation_status=reason)

    try:
        if snapshot.get("connected") is not True or snapshot.get("positions_readable") is not True or snapshot.get("orders_readable") is not True:
            return fail("DATA_UNAVAILABLE")
        if snapshot.get("account_scope") not in (None, "ETF_ONLY"):
            return fail("ACCOUNT_SCOPE_UNVERIFIED")
        cash = amount(snapshot.get("cash"), "cash")
        buying_power = amount(snapshot.get("unleveraged_buying_power", snapshot.get("buying_power")), "unleveraged_buying_power")
        total = amount(snapshot.get("portfolio_value"), "portfolio_value")
        equity = amount(snapshot.get("equity_value"), "equity_value")
        positions = snapshot.get("positions") or {}
        if not isinstance(positions, dict):
            return fail("POSITIONS_UNAVAILABLE")
        received = _time((snapshot.get("account_sync") or {}).get("account_data_timestamp"))
        if received is None:
            return fail("ACCOUNT_TIME_UNAVAILABLE")
        age = (datetime.now(timezone.utc) - received).total_seconds()
        if age < -5 or age > max_snapshot_age_seconds:
            return fail("ACCOUNT_STALE")
        portfolio_as_of = _time(snapshot.get("portfolio_provider_as_of"))
        if portfolio_as_of is None:
            return fail("PORTFOLIO_VALUATION_TIME_UNVERIFIED")
        if abs((received - portfolio_as_of).total_seconds()) > time_tolerance_seconds:
            return fail("PORTFOLIO_VALUATION_TIME_UNALIGNED")
        values = {}
        for symbol in ("SMH", "QQQ"):
            position = positions.get(symbol)
            if not isinstance(position, dict):
                return fail("POSITION_UNAVAILABLE:" + symbol)
            if position.get("quantity", position.get("qty")) is None:
                return fail("POSITION_QUANTITY_UNAVAILABLE:" + symbol)
            qty = amount(position.get("quantity", position.get("qty")), "quantity")
            if qty == 0:
                values[symbol], as_of = Decimal(0), portfolio_as_of
            elif position.get("market_value") is not None and _time(position.get("provider_as_of")) is not None:
                values[symbol] = amount(position["market_value"], "market_value")
                as_of = _time(position.get("provider_as_of"))
            else:
                quote = (snapshot.get("quotes") or {}).get(symbol) or {}
                price = amount(quote.get("last"), "quote")
                values[symbol] = qty * price
                as_of = _time(quote.get("timestamp"))
                if quote.get("is_stale") is True:
                    return fail("QUOTE_STALE:" + symbol)
            if qty > 0 and values[symbol] <= 0:
                return fail("POSITION_VALUE_UNAVAILABLE:" + symbol)
            if as_of is None or abs((portfolio_as_of - as_of).total_seconds()) > time_tolerance_seconds:
                return fail("VALUATION_TIME_UNALIGNED:" + symbol)
        other = sum((amount(snapshot.get(key, 0), key) for key in OTHER_ASSETS), Decimal(0))
        if other != 0:
            return fail("ACCOUNT_SCOPE_OTHER_ASSETS")
        if abs(equity - sum(values.values())) > amount(value_tolerance, "value_tolerance"):
            return fail("POSITION_EQUITY_UNRECONCILED")
        orders = snapshot.get("open_orders")
        if not isinstance(orders, list):
            return fail("ORDERS_UNAVAILABLE")
        broker = {}
        for order in orders:
            if not isinstance(order, dict):
                return fail("ORDER_UNAVAILABLE")
            if order.get("account_number") and snapshot.get("account_number") and str(order["account_number"]) != str(snapshot["account_number"]):
                continue
            if str(order.get("state") or order.get("status") or "").lower() not in BROKER_OPEN:
                continue
            if str(order.get("side") or "").lower() != "buy":
                continue
            key = str(order.get("order_id") or order.get("id") or "")
            if not key:
                return fail("BROKER_ORDER_ID_UNAVAILABLE")
            remaining = order.get("remaining_notional", order.get("open_notional", order.get("reserved_amount")))
            if remaining is None:
                gross = order.get("amount", order.get("dollar_amount"))
                if gross is None:
                    return fail("BROKER_HOLD_UNAVAILABLE")
                remaining = amount(gross, "broker_order_amount") - amount(order.get("filled_amount", 0), "broker_filled")
            held = amount(remaining, "broker_hold")
            if key in broker and broker[key] != held:
                return fail("BROKER_ORDER_CONFLICT")
            broker[key] = held
        broker_held = sum(broker.values(), Decimal(0))
        if broker_held and (cash_basis not in ("gross", "net_of_open_orders")
                            or buying_power_reflects_open_orders is not True):
            return fail("CASH_SEMANTICS_UNVERIFIED")
        if not broker_held:
            cash_basis = "gross"
        cash_for_weight = cash + broker_held if cash_basis == "net_of_open_orders" else cash
        if abs(total - (equity + cash_for_weight)) > amount(value_tolerance, "value_tolerance"):
            return fail("ACCOUNT_TOTAL_UNRECONCILED")
        unreflected = Decimal(0)
        by_symbol = {"SMH": Decimal(0), "QQQ": Decimal(0)}
        for row in local_intents:
            state = str(row.get("status") or row.get("state") or "").upper()
            if state not in OPEN:
                continue
            remaining = amount(row.get("reserved_remaining", row.get("reserved_amount")), "local_reservation")
            order_id = str(row.get("broker_order_id") or row.get("order_id") or "")
            symbol = str(row.get("ticker") or row.get("symbol") or "").upper()
            if symbol not in by_symbol:
                return fail("LOCAL_SYMBOL_UNAVAILABLE")
            if state in ("SUBMITTING", "EXECUTION_UNKNOWN", "APPROVAL_STATE_UNKNOWN", "AWAITING_USER_APPROVAL", "RECONCILIATION_REQUIRED", "CANCEL_PENDING") and order_id not in broker:
                return fail("RECONCILIATION_REQUIRED")
            if order_id and order_id in broker:
                if remaining > broker[order_id] + amount(value_tolerance, "value_tolerance"):
                    return fail("LOCAL_BROKER_HOLD_CONFLICT")
                continue  # Broker spendable already reflects this order.
            if state in ("SUBMITTED", "PARTIALLY_FILLED"):
                return fail("RECONCILIATION_REQUIRED")
            unreflected += remaining
            by_symbol[symbol] += remaining
        safe = min(cash_for_weight, buying_power) - unreflected
        if safe < 0:
            safe = Decimal(0)
        return dict(base, cash_semantics_verified=True, reconciliation_status="VERIFIED",
                    cash_for_weight=str(cash_for_weight), broker_spendable_cash=str(buying_power),
                    local_unreflected_reservations=str(unreflected),
                    local_unreflected_by_symbol={key: str(value) for key, value in by_symbol.items()},
                    safe_spendable_cash=str(safe), broker_open_hold=str(broker_held),
                    market_values={key: str(value) for key, value in values.items()},
                    rebuilt_total=str(sum(values.values()) + cash_for_weight),
                    broker_total=str(total), valuation_difference=str(equity - sum(values.values())),
                    cash_basis=cash_basis)
    except ValueError as exc:
        return fail(str(exc))

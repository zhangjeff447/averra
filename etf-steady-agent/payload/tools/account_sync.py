from __future__ import annotations

import math
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

ACCOUNT_SYNC_OK = "ACCOUNT_SYNC_OK"
ACCOUNT_SYNC_STALE = "ACCOUNT_SYNC_STALE"
ACCOUNT_SYNC_FAILED = "ACCOUNT_SYNC_FAILED"
LIVE_BLOCKED = "LIVE_BLOCKED"
ACCOUNT_MAX_AGE = timedelta(minutes=5)


def _decode_json_text(value: Any) -> Any:
    """Decode MCP text blocks/envelopes without changing ordinary strings."""
    if isinstance(value, str):
        text = value.strip()
        if text and text[:1] in "[{":
            try:
                return _decode_json_text(json.loads(text))
            except (TypeError, ValueError, json.JSONDecodeError):
                # Codex/MCP may return adjacent JSON values or JSONL.  Keep
                # every complete value instead of discarding the first one or
                # failing on json.loads(..., Extra data).
                decoder = json.JSONDecoder()
                values = []
                cursor = 0
                try:
                    while cursor < len(text):
                        while cursor < len(text) and text[cursor].isspace():
                            cursor += 1
                        if cursor >= len(text):
                            break
                        parsed, cursor = decoder.raw_decode(text, cursor)
                        values.append(_decode_json_text(parsed))
                    if values:
                        return values[0] if len(values) == 1 else values
                except json.JSONDecodeError:
                    pass
                return value
        return value
    if isinstance(value, dict):
        return {key: _decode_json_text(item) for key, item in value.items()
                if key not in ("guide", "instructions", "examples")}
    if isinstance(value, list):
        return [_decode_json_text(item) for item in value]
    return value


def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if math.isfinite(parsed) and parsed >= 0 else None


def _first_number(payload: Any, keys: tuple[str, ...]) -> Optional[float]:
    if isinstance(payload, dict):
        for key in keys:
            if key in payload:
                parsed = _number(payload.get(key))
                if parsed is not None:
                    return parsed
        for value in payload.values():
            parsed = _first_number(value, keys)
            if parsed is not None:
                return parsed
    elif isinstance(payload, list):
        for value in payload:
            parsed = _first_number(value, keys)
            if parsed is not None:
                return parsed
    return None


def _timestamp(payload: Any) -> Optional[datetime]:
    if isinstance(payload, dict):
        for key in ("account_data_timestamp", "timestamp", "updated_at", "as_of", "fetched_at"):
            value = payload.get(key)
            if value:
                try:
                    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
                    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
                except ValueError:
                    pass
        for value in payload.values():
            parsed = _timestamp(value)
            if parsed is not None:
                return parsed
    elif isinstance(payload, list):
        for value in payload:
            parsed = _timestamp(value)
            if parsed is not None:
                return parsed
    return None


def _collection(payload: Any, keys: tuple[str, ...]) -> Any:
    if isinstance(payload, dict):
        for key in keys:
            value = payload.get(key)
            if isinstance(value, list):
                return value
        data = payload.get("data")
        if isinstance(data, dict):
            return _collection(data, keys)
    return payload if isinstance(payload, (dict, list)) else []


def _quotes(payload: Any) -> Dict[str, Dict[str, Any]]:
    if isinstance(payload, dict):
        results = payload.get("results")
        if not isinstance(results, list):
            for key in ("data", "result", "payload", "content"):
                nested = payload.get(key)
                found = _quotes(nested)
                if found:
                    return found
        if isinstance(results, list):
            output: Dict[str, Dict[str, Any]] = {}
            for item in results:
                if not isinstance(item, dict):
                    continue
                quote = item.get("quote") if isinstance(item.get("quote"), dict) else item
                symbol = str(quote.get("symbol") or item.get("symbol") or "").upper()
                if symbol not in {"QQQ", "SMH"}:
                    continue
                output[symbol] = {
                    "symbol": symbol,
                    "last": quote.get("last_trade_price") or quote.get("last_non_reg_trade_price") or quote.get("last"),
                    "bid": quote.get("bid_price") or quote.get("bid"),
                    "ask": quote.get("ask_price") or quote.get("ask"),
                    "source": "robinhood_mcp",
                    "timestamp": quote.get("venue_last_trade_time"),
                    "is_realtime": True,
                    "is_stale": False,
                }
            return output

    if isinstance(payload, dict):
        # Compact symbol->quote responses are also accepted.
        return {str(key).upper(): value for key, value in payload.items() if str(key).upper() in {"QQQ", "SMH"} and isinstance(value, dict)}
    return {}


def normalize_account_sync(payload: Any, *, now: Optional[datetime] = None) -> Dict[str, Any]:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    payload = _decode_json_text(payload)
    if not isinstance(payload, dict):
        return {"status": ACCOUNT_SYNC_FAILED, "reason": "account_sync_result_missing", "live_status": LIVE_BLOCKED}

    timestamp = _timestamp(payload)
    if timestamp is None:
        return {"status": ACCOUNT_SYNC_FAILED, "reason": "account_data_timestamp_missing", "live_status": LIVE_BLOCKED}
    age = current - timestamp.astimezone(timezone.utc)
    if age < timedelta(0) or age > ACCOUNT_MAX_AGE:
        return {
            "status": ACCOUNT_SYNC_STALE,
            "reason": "account_data_stale",
            "account_data_timestamp": timestamp.isoformat(),
            "live_status": LIVE_BLOCKED,
        }

    # get_portfolio is the authoritative read-only account source.  The
    # account list is only an optional fallback for fields absent there; an
    # empty get_accounts response must not invalidate a usable portfolio.
    account = payload.get("account") if isinstance(payload.get("account"), dict) else {}
    accounts = payload.get("accounts")
    buying_power = _first_number(account, ("eligible_buying_power", "buying_power", "available_buying_power", "available_funds"))
    if buying_power is None:
        buying_power = _first_number(accounts, ("eligible_buying_power", "buying_power", "available_buying_power", "available_funds"))
    cash = _first_number(account, ("cash", "cash_available_for_withdrawal", "available_cash"))
    if cash is None:
        cash = _first_number(accounts, ("cash", "cash_available_for_withdrawal", "available_cash"))
    portfolio_value = _first_number(account, ("portfolio_value", "equity", "equity_value", "total_equity", "total_value", "market_value"))
    if buying_power is None or cash is None or portfolio_value is None:
        return {
            "status": ACCOUNT_SYNC_FAILED,
            "reason": "account_numeric_fields_malformed_or_missing",
            "account_data_timestamp": timestamp.isoformat(),
            "live_status": LIVE_BLOCKED,
        }

    account_items = _collection(accounts, ("accounts",))
    if isinstance(account_items, dict):
        account_items = [account_items]
    agentic_account_confirmed = any(
        isinstance(item, dict)
        and bool(item.get("agentic_allowed"))
        and str(item.get("state") or "").lower() == "active"
        for item in account_items
    ) if isinstance(account_items, list) else None

    return {
        "status": ACCOUNT_SYNC_OK,
        "reason": None,
        "account_readable": True,
        "accounts_readable": isinstance(accounts, (dict, list)) and bool(account_items),
        "agentic_account_confirmed": agentic_account_confirmed,
        "eligible_buying_power": buying_power,
        "cash": cash,
        "portfolio_value": portfolio_value,
        "positions": _collection(payload.get("positions"), ("positions", "results")),
        "open_orders": _collection(payload.get("open_or_recent_orders"), ("orders", "open_orders", "results")),
        "quotes": _quotes(payload.get("quotes")),
        "account_data_timestamp": timestamp.isoformat(),
        "live_status": ACCOUNT_SYNC_OK,
    }


def unavailable_message(sync: Dict[str, Any]) -> str:
    reason = str(sync.get("reason") or "account_sync_failed")
    return f"ACCOUNT DATA UNAVAILABLE\nLive activation blocked\nNo order submitted\nFailure: {reason}"


def snapshot_from_account_sync(sync: Dict[str, Any]) -> Dict[str, Any]:
    ok = sync.get("status") == ACCOUNT_SYNC_OK
    return {
        "connected": ok,
        "account_readable": bool(sync.get("account_readable", False)),
        # These plural/agentic names are retained for callers of the original
        # MCP snapshot contract.
        "accounts_readable": bool(sync.get("accounts_readable", sync.get("account_readable", False))),
        "agentic_account_confirmed": sync.get("agentic_account_confirmed"),
        "eligible_buying_power": sync.get("eligible_buying_power"),
        "cash": sync.get("cash"),
        "buying_power": sync.get("eligible_buying_power"),
        "portfolio_value": sync.get("portfolio_value"),
        "positions": sync.get("positions", {}),
        "open_orders": sync.get("open_orders", []),
        "quotes": sync.get("quotes", {}),
        "account_data_timestamp": sync.get("account_data_timestamp"),
        "account_sync": sync,
        "account_sync_status": sync.get("status", ACCOUNT_SYNC_FAILED),
        "account_sync_reason": sync.get("reason"),
        "monthly_filled_buys": None,
        "daily_filled_buys": None,
        "monthly_open_buy_amount": None,
        "daily_open_buy_amount": None,
        "capabilities": {},
        "errors": [] if ok else [str(sync.get("reason") or "account_sync_failed")],
        "live_blocked_by_network": not ok,
        "error_type": sync.get("reason"),
    }


def august_budget_activation_buying_power(sync: Dict[str, Any]) -> Optional[float]:
    """Return a launch amount only for a fresh, numeric, positive sync."""
    if sync.get("status") != ACCOUNT_SYNC_OK:
        return None
    value = _number(sync.get("eligible_buying_power"))
    return value if value is not None and value > 0 else None

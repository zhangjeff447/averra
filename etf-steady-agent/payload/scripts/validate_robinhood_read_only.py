#!/usr/bin/env python3
"""User-run Robinhood MCP validator with a fail-closed read-only tool guard.

This script intentionally does not run during Codex work. Run it manually from
the repository root after reviewing the printed configuration checks.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from broker.live_safety import LiveTradingDisabled
from qqq_mcp_adapter import MCPAdapter, _InternalSnapshot
from tools.account_sync import snapshot_from_account_sync, normalize_account_sync

READ_TOOLS = {
    "get_accounts",
    "get_portfolio",
    "get_equity_positions",
    "get_equity_orders",
    "get_equity_price_book",
    "get_equity_quotes",
    "get_trade_approval_setting",
    "get_trade_approvals",
}
WRITE_TOOLS = {
    "place_equity_order", "submit_order", "cancel_equity_order", "cancel_order",
    "replace_equity_order", "replace_order", "approve_order", "place_option_order",
    "review_option_order", "exercise_option", "sell_equity_order", "short_equity_order",
    "margin_order", "place_margin_order", "sell_order", "short_order",
}
SYMBOLS = {"QQQ", "SMH"}
READ_ONLY_MCP_TOOLS = frozenset(READ_TOOLS)
FORBIDDEN_MCP_TOOLS = frozenset(WRITE_TOOLS | {
    "call_order", "put_order", "spread_order", "covered_call_order", "naked_order",
})


class ReadOnlyMCPAdapter(MCPAdapter):
    """Native read-only account batch; writes are denied before transport."""

    def __init__(self, *args, require_native_read_proof: bool = False,
                 regular_trade_quotes: bool = False, **kwargs):
        super().__init__(*args, **kwargs)
        self.require_native_read_proof = require_native_read_proof
        self.quote_tool = "get_equity_quotes" if regular_trade_quotes else "get_equity_price_book"
        self.BATCH_TOOLS = tuple(self.quote_tool if name in {"get_equity_price_book", "get_equity_quotes"}
                                 else name for name in MCPAdapter.PRODUCTION_READ_ONLY_BATCH_TOOLS)

    def _call_tool(self, tool_name: str, params: dict[str, Any], retries: int = 2,
                   timeout_seconds: int = 60) -> dict[str, Any]:
        canonical = self._canonical_tool_name(str(tool_name))
        if canonical not in READ_ONLY_MCP_TOOLS:
            raise LiveTradingDisabled(f"DRY_RUN_ONLY:broker_write_denied:{canonical}")
        return super()._call_tool(canonical, params, retries=retries, timeout_seconds=timeout_seconds)

    # Deliberately class-level so tests can replace sleep without waiting.
    SYNC_RETRY_DELAYS = (5, 15, 30)
    sleep_fn = staticmethod(time.sleep)

    BATCH_TOOLS = MCPAdapter.PRODUCTION_READ_ONLY_BATCH_TOOLS

    BATCH_PROMPT = """
Use exactly one robinhood-trading MCP session and perform this read-only sequence:
1. get_accounts
2. Use the returned agentic account identifier internally to call get_portfolio.
3. Use the same identifier to call get_equity_positions and get_equity_orders.
4. Call get_equity_price_book with exactly ["QQQ", "SMH"].
5. Read get_trade_approval_setting and the complete paginated get_trade_approvals list.
For compatibility with older prompts, get_equity_quotes with exactly ["QQQ", "SMH"]
refers to this same read-only price-book operation and must not trigger a separate call.

Do not discover tools. Do not call any other tool, including preview, order,
approval mutation, cancellation, replacement, option, margin, short, or sell tools.
write_calls_permitted=false. Never output the account identifier, credentials,
cookies, or personal identifiers.

Return exactly one JSON object and no markdown:
{"ok":true,"results":{"get_accounts":...,"get_portfolio":...,"get_equity_positions":...,"get_equity_orders":...,"get_equity_price_book":...,"get_trade_approval_setting":...,"get_trade_approvals":...}}
If an MCP session disconnects, returns HTTP 400, or times out, close that
session and retry the complete read-only batch in a fresh session.  Never
retry a write operation (writes are not part of this batch).
""".strip()

    def _read_complete_collection(self, tool, params, field):
        items, seen = [], set()
        cursor = None
        for _ in range(100):
            page = self.call_tool_exact(tool, dict(params, **({"cursor": cursor} if cursor else {})))
            nodes = [node for node in self._walk_payload_nodes(page)
                     if isinstance(node, dict) and isinstance(node.get(field), list)]
            if len(nodes) != 1 or not all(isinstance(x, dict) for x in nodes[0][field]):
                raise RuntimeError(field + "_response_incomplete")
            node = nodes[0]
            items.extend(node[field])
            cursor = node.get("next")
            if not cursor:
                return {"data": {field: items, "next": None}}
            if not isinstance(cursor, str) or cursor in seen:
                raise RuntimeError(field + "_pagination_cycle")
            seen.add(cursor)
        raise RuntimeError(field + "_pagination_incomplete")

    def _run_read_only_batch_raw(self, timeout: int = 180) -> dict[str, Any]:
        """Run the allowlisted account, order, quote, and approval reads on one MCP session."""
        if self.transport is not None:
            accounts = self.call_tool_exact("get_accounts", {})
            eligible = [item for item in self._extract_accounts_list(accounts)
                        if item.get("agentic_allowed") is True
                        and str(item.get("state") or "").lower() == "active"]
            if len(eligible) != 1:
                raise RuntimeError("agentic_account_not_unique")
            account_number = eligible[0].get("account_number")
            if not isinstance(account_number, str) or not account_number:
                raise RuntimeError("agentic_account_number_missing")
            params = {"account_number": account_number}
            results = {
                "get_accounts": accounts,
                "get_portfolio": self.call_tool_exact("get_portfolio", params),
                "get_equity_positions": self._read_complete_collection("get_equity_positions", params, "positions"),
                "get_equity_orders": self._read_complete_collection("get_equity_orders", params, "orders"),
                self.quote_tool: self.call_tool_exact(self.quote_tool, {"symbols": ["QQQ", "SMH"]}),
                "get_trade_approval_setting": self.call_tool_exact("get_trade_approval_setting", params),
                "get_trade_approvals": self._read_complete_collection("get_trade_approvals", params, "approvals"),
            }
            return {"ok": True, "direct_results": results, "returncode": 0, "stdout": "", "stderr": ""}
        if self.quote_tool == "get_equity_quotes":
            raise RuntimeError("regular_trade_quotes_require_direct_transport")
        return self._run_codex_exec_raw(self.BATCH_PROMPT, timeout=timeout)

    def _parse_batch(self, stdout: str) -> dict[str, Any] | None:
        """Parse one complete direct-MCP result envelope.

        Codex emits lifecycle events and the assistant result as separate JSON
        lines.  Only an agent_message item is eligible to contain the result;
        arbitrary nested JSON (including tool payloads) is never accepted.
        """
        events: list[dict[str, Any]] = []
        for event in self._parse_json_stream(stdout):
            if not isinstance(event, dict) or not isinstance(event.get("type"), str):
                continue
            if event["type"] in {"error", "turn.failed", "session.failed"}:
                return None
            events.append(event)
        if not events:
            return None

        assistant_results: list[Any] = []
        for event in events:
            if event.get("type") not in {"item.completed", "item.updated"}:
                continue
            item = event.get("item")
            if not isinstance(item, dict) or item.get("type") != "agent_message":
                continue
            text = item.get("text")
            if isinstance(text, str):
                assistant_results.append(text)
                continue
            content = item.get("content")
            if isinstance(content, list):
                parts = [
                    part.get("text") or part.get("value")
                    for part in content
                    if isinstance(part, dict) and isinstance(part.get("text") or part.get("value"), str)
                ]
                if parts:
                    assistant_results.append("".join(parts))
        if not assistant_results:
            return None

        required = set(self.BATCH_TOOLS)
        # An agent message is normally one JSON object, but transport wrappers
        # can append another valid JSON message or emit JSONL.  Feeding that
        # whole stream to json.loads() raises ``Extra data`` after the first
        # valid message.  Parse complete values and select the valid batch.
        for message in reversed(assistant_results):
            for result in self._parse_json_stream(message):
                if not isinstance(result, dict) or result.get("ok") is not True:
                    continue
                results = result.get("results")
                if not isinstance(results, dict):
                    continue
                # Legacy fake fixtures used the former quote tool name.
                if self.quote_tool == "get_equity_price_book" and "get_equity_price_book" not in results and "get_equity_quotes" in results:
                    results = dict(results)
                    results["get_equity_price_book"] = results["get_equity_quotes"]
                if not required.issubset(results):
                    continue
                if any(results.get(name) is None for name in required):
                    continue
                return result
        return None

    def _parse_native_batch(self, stdout: str) -> dict[str, Any] | None:
        """Accept live account data only from completed MCP tool results."""
        results: dict[str, Any] = {}
        turn_completed = False
        for event in self._parse_json_stream(stdout):
            if not isinstance(event, dict):
                continue
            if event.get("type") in ("turn.failed", "error"):
                raise RuntimeError("codex_turn_failed")
            if event.get("type") == "turn.completed":
                turn_completed = True
            if event.get("type") != "item.completed":
                continue
            item = event.get("item")
            if not isinstance(item, dict) or item.get("type") != "mcp_tool_call":
                continue
            if item.get("server") != self.server_name:
                continue
            name = str(item.get("tool") or "")
            if name not in self.BATCH_TOOLS:
                raise RuntimeError("unexpected_tool_in_read_only_batch")
            if item.get("status") != "completed" or item.get("error") or item.get("result") is None:
                raise RuntimeError("native_mcp_read_failed")
            if name in results:
                raise RuntimeError("duplicate_native_mcp_read")
            results[name] = self._unwrap_mcp_result(item["result"])
        if not turn_completed or set(results) != set(self.BATCH_TOOLS):
            return None
        return {"ok": True, "results": results}

    @staticmethod
    def _parse_json_stream(text: str) -> list[Any]:
        """Parse one JSON value, adjacent JSON values, or JSONL safely."""
        if not isinstance(text, str) or not text.strip():
            return []
        return MCPAdapter._parse_json_stream(text)

    @staticmethod
    def _is_retryable_sync_failure(*values: Any) -> bool:
        """Retry only transient network/session failures, never policy/input failures."""
        text = " ".join(str(value or "") for value in values).lower()
        non_retryable = (
            "agents.md", "policy", "permission", "forbidden", "not allowlisted",
            "invalid param", "invalid argument", "missing required", "schema",
            "unauthorized", "authentication", "account identifier",
            "model is not supported when using codex", "codex_model_unavailable", "codex_usage_limit",
            "usage limit", "out of credits", "quota exceeded",
        )
        if any(marker in text for marker in non_retryable):
            return False
        retryable = (
            "nodename nor servname", "temporary failure in name resolution",
            "name or service not known", "dns", "getaddrinfo", "network is unreachable",
            "connection reset", "connection refused", "connection aborted",
            "transport channel closed", "transport closed", "broken pipe",
            "session disconnected", "mcp disconnected", "timed out", "timeout",
            "eof", "http 502", "http 503", "http 504", "bad gateway",
            "service unavailable", "gateway timeout", "delete returned http 400",
        )
        return any(marker in text for marker in retryable)

    def _log_sync_retry(self, *, event: str, attempt: int, wait_seconds: int | None = None, reason: str = "") -> None:
        self._log_connection_error(json.dumps({
            "event": event,
            "operation": "read_only_account_sync",
            "attempt": attempt,
            "wait_seconds": wait_seconds,
            "reason": reason[:500],
            "writes_retried": False,
        }, ensure_ascii=False))

    def read_only_validation(self) -> dict[str, Any]:
        self._batch_agentic_account_number = None
        last_error = "initial_mcp_session_failed_or_incomplete"
        total_attempts = len(self.SYNC_RETRY_DELAYS) + 1
        for attempt in range(total_attempts):
            attempt_started_at = datetime.now(timezone.utc).isoformat()
            try:
                raw = self._run_read_only_batch_raw()
            except Exception as exc:
                raw = {"returncode": 1, "stdout": "", "stderr": str(exc), "error_type": type(exc).__name__}
            attempt_completed_at = datetime.now(timezone.utc).isoformat()
            stdout = str(raw.get("stdout") or "")
            direct_results = raw.get("direct_results") if self.transport is not None else None
            payload = ({"ok": True, "results": direct_results} if isinstance(direct_results, dict)
                       and set(direct_results) == set(self.BATCH_TOOLS)
                       else self._parse_native_batch(stdout) if self.require_native_read_proof
                       else self._parse_batch(stdout))
            self._log_response_boundary(
                source=f"read_only_batch_parse_attempt_{attempt + 1}",
                stdout=raw.get("stdout"), stderr=raw.get("stderr"), parsed=payload,
            )
            cleanup_text = str(raw.get("stderr") or "")
            cleanup_warning = bool(
                re.search(r"failed to delete session[\s\S]{0,200}DELETE returned HTTP 400", cleanup_text, re.IGNORECASE)
                or re.search(r"DELETE returned HTTP 400[\s\S]{0,200}failed to delete session", cleanup_text, re.IGNORECASE)
            )
            if not raw.get("timed_out") and raw.get("returncode") == 0 and payload is not None:
                batch_results = payload["results"]
                accounts = self._extract_accounts_list(batch_results["get_accounts"])
                agentic = [item for item in accounts if str(item.get("agentic_allowed") or "").lower() in ("true", "1")
                           and str(item.get("state") or "").lower() == "active"]
                selected_account = agentic[0] if len(agentic) == 1 else {}
                if len(agentic) == 1:
                    self._batch_agentic_account_number = self._first_string(
                        agentic[0], ["account_number", "account", "number"]
                    )
                account_number = self._batch_agentic_account_number
                approval_setting = None
                pending_approvals = None
                if account_number:
                    account_hash = hashlib.sha256(account_number.encode()).hexdigest()
                    setting_rows = [node["setting"] for node in self._walk_payload_nodes(
                        batch_results["get_trade_approval_setting"])
                        if isinstance(node, dict) and isinstance(node.get("setting"), dict)]
                    if len(setting_rows) == 1:
                        setting = setting_rows[0]
                        if (setting.get("account_number") == account_number
                                and type(setting.get("human_must_approve_trades")) is bool):
                            approval_setting = {
                                "source": "broker_mcp", "account_hash": account_hash,
                                "enabled": setting["human_must_approve_trades"],
                            }
                    approval_nodes = [node for node in self._walk_payload_nodes(
                        batch_results["get_trade_approvals"])
                        if isinstance(node, dict) and isinstance(node.get("approvals"), list)]
                    pending_states = {"pending", "awaiting_approval", "awaiting_user_approval",
                                      "pending_approval", "user_action_required"}
                    if len(approval_nodes) == 1:
                        approval_node = approval_nodes[0]
                        rows = approval_node["approvals"]
                        complete = not any(approval_node.get(key) for key in ("next", "nextCursor", "next_cursor"))
                        statuses_verified = complete and all(
                            isinstance(row, dict) and row.get("account_number") == account_number
                            and isinstance(row.get("approval_id"), str) and bool(row["approval_id"])
                            and str(row.get("state", "")).lower() in pending_states
                            for row in rows)
                        if statuses_verified:
                            pending_approvals = {
                                "source": "broker_mcp", "account_hash": account_hash,
                                "complete": True, "statuses_verified": True,
                                "active_count": len(rows),
                            }
                quote_result = batch_results.get(self.quote_tool, batch_results.get("get_equity_quotes"))
                return {
                    "accounts": _safe_summary(batch_results["get_accounts"]),
                    "account": _safe_summary(batch_results["get_portfolio"]),
                    "positions": _safe_summary(batch_results["get_equity_positions"]),
                    "open_or_recent_orders": _safe_summary(batch_results["get_equity_orders"]),
                    "quotes": _safe_summary(quote_result),
                    "v1_approval_setting": approval_setting,
                    "v1_pending_approvals": pending_approvals,
                    "account_data_timestamp": datetime.now(timezone.utc).isoformat(),
                    "v1_read_started_at": attempt_started_at,
                    "v1_read_completed_at": attempt_completed_at,
                    "v1_unsettled_funds": selected_account.get("unsettled_funds"),
                    "v1_pending_approval_hold": selected_account.get("pending_approval_hold"),
                    "cleanup_warning": cleanup_warning,
                    "write_calls_permitted": False,
                }

            stdout_lower = str(raw.get("stdout") or "").lower()
            if raw.get("error_type") == "codex_usage_limit":
                failure_reason = "codex_usage_limit"
            elif "model is not supported when using codex" in stdout_lower:
                failure_reason = "codex_model_unavailable"
            elif self.require_native_read_proof and self._parse_batch(stdout) is not None:
                failure_reason = "native_mcp_results_missing"
            else:
                failure_reason = "mcp_session_http_400_or_disconnected" if (
                    cleanup_warning or self._looks_like_mcp_session_failure(
                        raw.get("stderr"), raw.get("stdout"), raw.get("error_type")
                    )
                ) else "mcp_session_failed_or_incomplete"
            # Retain the long-standing public error prefix while preserving
            # the more specific reason for diagnostics and callers.
            last_error = f"initial_mcp_session_failed:{failure_reason}"
            retryable = self._is_retryable_sync_failure(
                raw.get("stderr"), raw.get("stdout"), raw.get("error_type"),
                "timeout" if raw.get("timed_out") else "",
            )
            if not retryable or attempt >= total_attempts - 1:
                self._log_sync_retry(
                    event="account_sync_failed",
                    attempt=attempt + 1,
                    reason=f"{last_error}; retryable={retryable}",
                )
                break
            wait_seconds = self.SYNC_RETRY_DELAYS[attempt]
            self._log_sync_retry(
                event="account_sync_retry_scheduled",
                attempt=attempt + 1,
                wait_seconds=wait_seconds,
                reason=last_error,
            )
            self.sleep_fn(wait_seconds)
            self._invalidate_mcp_session(last_error)
        raise RuntimeError(last_error)

    def fetch_live_account_snapshot(self) -> dict[str, Any]:
        """Expose the validated batch through the runtime adapter contract."""
        try:
            batch = self.read_only_validation()
            sync = normalize_account_sync(batch)
        except Exception as exc:
            error = str(exc).lower()
            reason = ("codex_usage_limit" if "codex_usage_limit" in error else
                      "codex_model_unavailable" if "codex_model_unavailable" in error else
                      f"read_only_sync_failed:{type(exc).__name__}")
            sync = {
                "status": "ACCOUNT_SYNC_FAILED",
                "reason": reason,
                "live_status": "LIVE_BLOCKED",
            }
            return snapshot_from_account_sync(sync)

        snapshot = _InternalSnapshot(snapshot_from_account_sync(sync))
        if sync["status"] != "ACCOUNT_SYNC_OK":
            return snapshot
        snapshot["v1_read_started_at"] = batch.get("v1_read_started_at")
        snapshot["v1_read_completed_at"] = batch.get("v1_read_completed_at")
        snapshot["v1_unsettled_funds"] = batch.get("v1_unsettled_funds")
        snapshot["pending_approval_hold"] = batch.get("v1_pending_approval_hold")
        snapshot["v1_approval_setting"] = batch.get("v1_approval_setting")
        snapshot["v1_pending_approvals"] = batch.get("v1_pending_approvals")

        # Keep broker field provenance separate from the legacy account-sync
        # aliases; V1 must not mistake equity_value for total_value.
        portfolio_nodes = [node for node in self._walk_payload_nodes(batch.get("account"))
                           if isinstance(node, dict) and "total_value" in node and "equity_value" in node]
        if len(portfolio_nodes) == 1:
            portfolio = portfolio_nodes[0]
            buying_power = portfolio.get("buying_power") if isinstance(portfolio.get("buying_power"), dict) else {}
            snapshot["v1_portfolio_fields"] = {
                key: portfolio.get(key) for key in (
                    "cash", "total_value", "equity_value", "options_value", "futures_value",
                    "event_contracts_value", "crypto_value", "mutual_funds_value",
                    "fixed_income_value", "pending_deposits")
            }
            snapshot["v1_portfolio_fields"]["unleveraged_buying_power"] = buying_power.get("unleveraged_buying_power")
            snapshot["portfolio_provider_as_of"] = next(
                (portfolio[key] for key in ("provider_as_of", "as_of") if portfolio.get(key)), None)

        account_number = getattr(self, "_batch_agentic_account_number", None)
        snapshot["agentic_account_confirmed"] = bool(account_number)
        if account_number:
            snapshot.agentic_account_number_internal = account_number
            snapshot["v1_account_hash"] = hashlib.sha256(account_number.encode()).hexdigest()

        positions_payload = batch.get("positions")
        position_lists = [node["positions"] for node in self._walk_payload_nodes(positions_payload)
                          if isinstance(node, dict) and isinstance(node.get("positions"), list)]
        positions_paginated = any(node.get(key) for node in self._walk_payload_nodes(positions_payload)
                                  if isinstance(node, dict)
                                  for key in ("next", "next_cursor", "next_page_token", "has_more"))
        positions = self._extract_items(positions_payload, ["positions"])
        snapshot["positions_readable"] = not positions_paginated and bool(position_lists) and all(isinstance(item, dict) for item in positions)
        snapshot["v1_other_equity_positions"] = []
        snapshot["positions"] = {symbol: {"symbol": symbol, "quantity": 0, "market_value": 0}
                                 for symbol in ("QQQ", "SMH")}
        if snapshot["positions_readable"]:
            for item in positions:
                symbol = str(item.get("symbol") or item.get("ticker") or "").upper()
                if symbol not in snapshot["positions"]:
                    snapshot["v1_other_equity_positions"].append(symbol or "UNKNOWN")
                    continue
                raw_quantity = item.get("quantity") if item.get("quantity") is not None else item.get("qty")
                quantity = self._safe_float(raw_quantity)
                if quantity is None or quantity < 0:
                    snapshot["positions_readable"] = False
                    break
                snapshot["positions"][symbol] = item

        quotes_by_symbol = self._extract_quote_candidates(batch.get("quotes"))
        snapshot["quotes"] = {}
        for symbol in ("QQQ", "SMH"):
            raw_quote = quotes_by_symbol.get(symbol)
            if not isinstance(raw_quote, dict) or not any(
                isinstance(raw_quote.get(key), str) and raw_quote[key].strip()
                for key in ("last_trade_at", "last_trade_timestamp", "venue_last_trade_time",
                            "venue_last_trade_timestamp", "updated_at", "timestamp", "as_of")
            ):
                continue
            quote = self._normalize_quote(symbol, raw_quote)
            if quote is not None:
                quote["received_at"] = batch.get("v1_read_completed_at")
                snapshot["quotes"][symbol] = quote
        snapshot["quotes_readable"] = all(
            isinstance(snapshot["quotes"].get(symbol), dict)
            and snapshot["quotes"][symbol].get("is_stale") is False
            for symbol in ("QQQ", "SMH")
        )

        orders_payload = batch.get("open_or_recent_orders")
        try:
            self._validate_order_collection(orders_payload)
            orders = self._extract_items(orders_payload, ["orders"])
            if not all(isinstance(item, dict) for item in orders):
                raise RuntimeError("orders_response_malformed")
        except RuntimeError:
            snapshot["orders_readable"] = False
            orders = []
        else:
            snapshot["orders_readable"] = True

        if snapshot["orders_readable"]:
            snapshot["v1_broker_orders"] = orders

        if snapshot["orders_readable"]:
            now_et = datetime.now(ZoneInfo("America/New_York")).replace(tzinfo=None)
            month_start = now_et.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            day_start = now_et.replace(hour=0, minute=0, second=0, microsecond=0)
            monthly_filled = daily_filled = monthly_open = daily_open = manual_filled = 0.0
            unattributed = 0
            invalid_amounts = False
            historical_spend: dict[str, float] = {}
            open_states = {"open", "new", "pending", "pending_cancelled", "submitted", "partially_filled", "queued", "confirmed", "unconfirmed"}
            snapshot["open_orders"] = [order for order in orders
                                       if str(order.get("state") or order.get("status") or "").lower() in open_states]
            if os.environ.get("SCORE_DYNAMIC_ALLOCATION_V1_ENABLED", "false").lower() == "true":
                normalized_open = []
                for order in snapshot["open_orders"]:
                    requested = order.get("dollar_based_amount")
                    requested = self._safe_float(requested.get("amount")) if isinstance(requested, dict) else None
                    executions = order.get("executions")
                    execution_cost = None
                    if isinstance(executions, list):
                        costs = [(self._safe_float(item.get("price")), self._safe_float(item.get("quantity")))
                                 for item in executions if isinstance(item, dict)]
                        if all(price is not None and qty is not None for price, qty in costs):
                            execution_cost = sum(price * qty for price, qty in costs)
                    filled = self._order_filled_amount(order)
                    if filled is None:
                        filled = execution_cost
                    remaining = self._order_open_amount(order)
                    if remaining is None and requested is not None:
                        remaining = max(0.0, requested - (filled or 0.0))
                    normalized_open.append(dict(order, remaining_notional=remaining, filled_amount=filled or 0))
                snapshot["open_orders"] = normalized_open
            for order in orders:
                if str(order.get("side") or "").lower() != "buy":
                    continue
                state = str(order.get("state") or order.get("status") or "").lower()
                timestamp = self._parse_iso_timestamp(order.get("last_transaction_at") or order.get("filled_at") or order.get("updated_at") or order.get("created_at"))
                order_time = timestamp.astimezone(ZoneInfo("America/New_York")).replace(tzinfo=None) if timestamp else None
                filled = self._order_filled_amount(order)
                reserved = self._order_open_amount(order)
                if not self._is_agentic_order(order):
                    if self._is_completed_drip_order(order):
                        snapshot.setdefault("external_drip_orders", []).append({
                            "order_id": order.get("id") or order.get("order_id"),
                            "symbol": order["symbol"], "filled_amount": filled,
                            "classification": "EXTERNAL_OR_MANUAL_ACTIVITY", "source": "drip",
                        })
                        continue
                    owner = str(order.get("placed_agent") or "").strip().lower()
                    if owner in {"you", "user", "manual"}:
                        if order_time is not None and order_time >= month_start:
                            manual_filled += filled or 0.0
                    elif state not in {"rejected", "failed", "cancelled", "canceled"}:
                        unattributed += 1
                    continue
                if state in {"filled", "partially_filled"} and (filled is None or order_time is None):
                    invalid_amounts = True
                if state in open_states and reserved is None:
                    invalid_amounts = True
                if state in {"filled", "partially_filled"} or (filled or 0) > 0:
                    executions = order.get("executions")
                    entries = []
                    if isinstance(executions, list) and executions:
                        for fill in executions:
                            when = self._parse_iso_timestamp(fill.get("timestamp")) if isinstance(fill, dict) else None
                            cost = self._order_filled_amount({"executions": [fill]}) if isinstance(fill, dict) else None
                            if when is None or cost is None:
                                invalid_amounts = True
                                continue
                            entries.append((when.astimezone(ZoneInfo("America/New_York")).replace(tzinfo=None), cost))
                    else:
                        entries = [(order_time, filled or 0.0)]
                    for when, cost in entries:
                        if when is not None:
                            month_key = when.strftime("%Y-%m")
                            historical_spend[month_key] = round(historical_spend.get(month_key, 0.0) + cost, 2)
                        if when is None or when >= month_start:
                            monthly_filled += cost
                        if when is None or when >= day_start:
                            daily_filled += cost
                if state in open_states:
                    monthly_open += reserved or 0.0
                    daily_open += reserved or 0.0
            snapshot.update({
                "monthly_filled_buys": monthly_filled,
                "daily_filled_buys": daily_filled,
                "monthly_open_buy_amount": monthly_open,
                "daily_open_buy_amount": daily_open,
                "agent_filled_by_month": historical_spend,
                "manual_monthly_filled_buys": manual_filled,
                "unattributed_buy_orders": unattributed,
                "budget_history_complete": not (unattributed or invalid_amounts),
            })

        snapshot["capabilities"] = {
            "order_preview": "review_equity_order" in self.config_tools,
            "place_order": "place_equity_order" in self.config_tools,
        }
        snapshot["live_blocked"] = not (
            snapshot["agentic_account_confirmed"]
            and snapshot["positions_readable"]
            and snapshot["quotes_readable"]
            and snapshot["orders_readable"]
            and snapshot.get("budget_history_complete") is True
            and snapshot["capabilities"]["order_preview"]
            and snapshot["capabilities"]["place_order"]
        )
        return snapshot


class ReadOnlyToolGuard:
    """The only call boundary used by this script."""

    def __init__(self, adapter: MCPAdapter):
        self.adapter = adapter

    def call(self, tool_name: str, params: dict[str, Any] | None = None, *, preview: bool = False) -> Any:
        name = str(tool_name).strip()
        lowered = name.lower()
        prohibited = ("option", "call", "put", "spread", "covered", "naked", "margin", "short", "sell")
        if name in WRITE_TOOLS or any(token in lowered for token in prohibited) or name.startswith(("place_", "submit_", "cancel_", "replace_", "approve_")):
            raise LiveTradingDisabled(f"blocked_before_mcp_call:{name}")
        allowed = READ_TOOLS if not preview else set()
        if name not in allowed:
            raise LiveTradingDisabled(f"tool_not_allowlisted:{name}")
        return self.adapter.call_tool_exact(name, params or {})


def _config_check() -> dict[str, Any]:
    config_path = Path.home() / ".codex" / "config.toml"
    result: dict[str, Any] = {
        "config_path": str(config_path),
        "config_exists": config_path.is_file(),
        "server_name": "robinhood-trading",
        "direct_mcp_transport": True,
        "telegram_used": False,
        "write_operations_enabled": False,
    }
    if config_path.is_file():
        try:
            import tomllib
            data = tomllib.loads(config_path.read_text(encoding="utf-8"))
            server = data.get("mcp_servers", {}).get("robinhood-trading", {})
            result["server_section_present"] = isinstance(server, dict)
            result["server_url_configured"] = bool(isinstance(server, dict) and server.get("url"))
        except Exception:
            result["server_section_present"] = False
            result["server_url_configured"] = False
    else:
        result["server_section_present"] = False
        result["server_url_configured"] = False
    return result


def _safe_summary(value: Any) -> Any:
    """Keep useful financial fields while dropping identifiers and credentials."""
    if isinstance(value, dict):
        blocked = (
            "account_number", "accountnumber", "account_id", "account_identifier", "number", "token", "cookie", "authorization", "secret", "password", "credential",
            "name", "email", "phone", "address", "identifier", "ssn", "tax", "username", "user_id",
        )
        return {str(k): _safe_summary(v) for k, v in value.items() if not any(word in str(k).lower() for word in blocked)}
    if isinstance(value, list):
        return [_safe_summary(item) for item in value]
    if isinstance(value, str):
        return re.sub(
            r"(?i)(account(?:[_ -]?number)?|token|cookie|authorization|secret|password|credential|email|phone)\s*[:=]\s*[^,;\s}\]]+",
            r"\1=[REDACTED]",
            value,
        )
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return str(value)


def _account_number(raw: Any) -> str | None:
    if isinstance(raw, dict):
        for key, value in raw.items():
            if str(key).lower() in {"account_number", "accountnumber", "account"} and value:
                return str(value)
            found = _account_number(value)
            if found:
                return found
    if isinstance(raw, list):
        for item in raw:
            found = _account_number(item)
            if found:
                return found
    return None


def dry_run() -> int:
    result = _config_check()
    result["intended_read_capabilities"] = sorted(READ_TOOLS)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["config_exists"] and result["server_section_present"] else 2


def health() -> int:
    config = _config_check()
    if not config["config_exists"] or not config["server_section_present"]:
        print(json.dumps({"status": "BLOCKED_CONFIGURATION", "config": config}, indent=2, sort_keys=True))
        return 2
    adapter = ReadOnlyMCPAdapter(workspace=str(ROOT), server_name="robinhood-trading")
    try:
        tools = adapter.discover_runtime_tools()
        names = sorted(str(tool.get("name") or tool.get("canonical_name")) for tool in tools if isinstance(tool, dict))
        print(json.dumps({"status": "HEALTHY_READ_ONLY_CAPABILITY_DISCOVERY", "capabilities": names, "write_calls_permitted": False}, indent=2))
        return 0
    except Exception as exc:
        print(json.dumps({"status": "BLOCKED_MCP", "error_type": type(exc).__name__, "write_calls_permitted": False}, indent=2))
        return 2


def sync(preview: bool = False, symbol: str | None = None, dollar_amount: str | None = None) -> int:
    config = _config_check()
    if not config["config_exists"] or not config["server_section_present"]:
        print(json.dumps({"status": "BLOCKED_CONFIGURATION", "config": config}, indent=2, sort_keys=True))
        return 2
    adapter = ReadOnlyMCPAdapter(workspace=str(ROOT), server_name="robinhood-trading")
    try:
        if preview:
            raise LiveTradingDisabled("preview_disabled_in_read_only_repair_test")
        result: dict[str, Any] = {
            "status": "READ_ONLY_SYNC_COMPLETE",
            **adapter.read_only_validation(),
        }
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except LiveTradingDisabled:
        raise
    except Exception as exc:
        print(json.dumps({"status": "BLOCKED_MCP_OR_RESPONSE", "error_type": type(exc).__name__, "write_calls_permitted": False}, indent=2))
        return 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="User-run Robinhood MCP read-only validator.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--dry-run", action="store_true")
    group.add_argument("--health", action="store_true")
    group.add_argument("--sync", action="store_true")
    parser.add_argument("--preview", action="store_true", help="With --sync, call review_equity_order only.")
    parser.add_argument("--symbol", choices=sorted(SYMBOLS), default="QQQ")
    parser.add_argument("--dollar-amount", default="50")
    args = parser.parse_args(argv)
    if args.dry_run:
        return dry_run()
    if args.health:
        return health()
    return sync(preview=args.preview, symbol=args.symbol, dollar_amount=args.dollar_amount)


if __name__ == "__main__":
    raise SystemExit("Use run_agent.py; legacy direct entry is disabled in the unified candidate.")

"""Robinhood MCP adapter backed by one direct authenticated MCP session.

This module intentionally separates two concepts:
1) Configured tools in ~/.codex/config.toml
2) Runtime-discovered tools via an actual MCP call

The runtime should prefer real MCP discovery results. Configured tools are only
used as a fallback hint when discovery is blocked.
"""

from __future__ import annotations

import copy
import json
import math
import os
import re
import signal
import shlex
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from urllib.parse import urlencode
from urllib.request import Request, urlopen

try:
    import tomllib as toml
except Exception:
    import tomli as toml  # type: ignore


class _InternalSnapshot(dict):
    """Process-local snapshot container with non-serialized internal fields."""

    pass


class MCPToolResponseError(RuntimeError):
    """Preserve an authoritative tool error body through adapter wrapping."""
    def __init__(self, response):
        super().__init__('broker_mcp_tool_error')
        payload = response.get('structuredContent')
        if not isinstance(payload, dict):
            for block in response.get('content') or []:
                if isinstance(block, dict) and block.get('type') == 'text':
                    try:
                        payload = json.loads(block.get('text', ''))
                    except (ValueError, TypeError):
                        payload = {'message': block.get('text')}
                    break
        self.mcp_response = dict(payload if isinstance(payload, dict) else response, isError=True)


class _DirectMCPTransport:
    """One authenticated Streamable HTTP MCP session.

    The transport deliberately owns the session lifecycle.  It never shells
    out to Codex (or any other process), and it only exposes JSON-RPC MCP
    operations to the adapter.
    """

    PROTOCOL_VERSION = "2025-03-26"

    def __init__(self, url: str, headers: Optional[Dict[str, str]] = None, timeout: int = 60):
        self.url = url
        self.timeout = timeout
        self.headers: Dict[str, str] = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        }
        if headers:
            self.headers.update(headers)
        self.session_id: Optional[str] = None
        self._request_id = 0
        self.initialized = False

    def _post(self, message: Dict[str, Any]) -> Any:
        request = Request(self.url, data=json.dumps(message).encode("utf-8"), headers=self.headers, method="POST")
        with urlopen(request, timeout=self.timeout) as response:
            body = response.read()
            response_headers = response.headers
        session_id = response_headers.get("Mcp-Session-Id") or response_headers.get("mcp-session-id")
        if session_id:
            self.session_id = session_id
            self.headers["Mcp-Session-Id"] = session_id
        content_type = response_headers.get("Content-Type", "")
        if "text/event-stream" in content_type:
            data = None
            for line in body.decode("utf-8", errors="replace").splitlines():
                if line.startswith("data:"):
                    try:
                        data = json.loads(line[5:].strip())
                    except json.JSONDecodeError:
                        continue
            return data
        if not body:
            return None
        return json.loads(body.decode("utf-8"))

    def _rpc(self, method: str, params: Optional[Dict[str, Any]] = None) -> Any:
        self._request_id += 1
        message = {"jsonrpc": "2.0", "id": self._request_id, "method": method}
        if params is not None:
            message["params"] = params
        result = self._post(message)
        if isinstance(result, dict) and result.get("error"):
            error = result["error"]
            raise RuntimeError(f"MCP {method} failed: {error}")
        return result.get("result") if isinstance(result, dict) else result

    def initialize(self) -> None:
        if self.initialized:
            return
        self._rpc("initialize", {
            "protocolVersion": self.PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "qqq-0dte-agent", "version": "1.0"},
        })
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
        self.headers["MCP-Protocol-Version"] = self.PROTOCOL_VERSION
        self.initialized = True

    def list_tools(self) -> List[Dict[str, Any]]:
        self.initialize()
        tools: List[Dict[str, Any]] = []
        cursor = None
        seen = set()
        while True:
            result = self._rpc("tools/list", {"cursor": cursor} if cursor else {}) or {}
            if not isinstance(result, dict) or not isinstance(result.get("tools"), list):
                raise RuntimeError("tool_discovery_incomplete")
            tools.extend(result["tools"])
            cursor = result.get("nextCursor") or result.get("next_cursor")
            if not cursor:
                return tools
            if cursor in seen or len(seen) >= 19:
                raise RuntimeError("tool_discovery_pagination_incomplete")
            seen.add(cursor)

    def call_tool(self, name: str, arguments: Dict[str, Any]) -> Any:
        self.initialize()
        result = self._rpc("tools/call", {"name": name, "arguments": arguments})
        if isinstance(result, dict) and result.get("isError"):
            raise MCPToolResponseError(result)
        # MCP tool results commonly put JSON in structuredContent or text.
        if isinstance(result, dict) and "structuredContent" in result:
            return result["structuredContent"]
        content = result.get("content") if isinstance(result, dict) else None
        if isinstance(content, list):
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    try:
                        return json.loads(item.get("text", ""))
                    except (TypeError, json.JSONDecodeError):
                        pass
        return result


class MCPAdapter:
    # Retries apply only to the read-only snapshot wrapper below.  Trade
    # submission methods never call this path.
    SNAPSHOT_RETRY_DELAYS = (5, 15, 30)
    # Robinhood order history can legitimately take several minutes even when
    # the same MCP call ultimately succeeds.  Keep this read-only call alive
    # long enough to receive the authoritative result; live execution remains
    # fail-closed until it does.
    ORDER_READ_TIMEOUT_SECONDS = 300
    sleep_fn = staticmethod(time.sleep)
    ROBINHOOD_TOOL_PREFIX = "mcp__robinhood_trading__"
    READ_ONLY_TOOLS = [
        "get_accounts",
        "get_portfolio",
        "get_equity_positions",
        "get_equity_orders",
        "get_realized_pnl",
        "get_equity_price_book",
        "get_trade_approval_setting",
        "get_trade_approvals",
    ]
    PRODUCTION_READ_ONLY_BATCH_TOOLS = (
        "get_accounts",
        "get_portfolio",
        "get_equity_positions",
        "get_equity_orders",
        "get_equity_price_book",
        "get_trade_approval_setting",
        "get_trade_approvals",
    )
    # This is the adapter's complete permitted surface.  Runtime policy still
    # decides whether preview or submission is allowed for a given operation.
    ALLOWED_MCP_TOOLS = frozenset(PRODUCTION_READ_ONLY_BATCH_TOOLS) | frozenset({
        "get_realized_pnl", "get_equity_quotes", "review_equity_order", "place_equity_order",
    })
    CODEX_MODEL = "gpt-5.6-sol"
    CODEX_REASONING_EFFORT = "medium"
    MAX_CODEX_INVOCATIONS_PER_RUN = 6

    def __init__(self, workspace: Optional[str] = None, server_name: str = "robinhood-trading", transport: Any = None, direct_transport: bool = False):
        self.workspace = workspace or os.getcwd()
        self.server_name = server_name
        self.state_dir = Path(self.workspace) / "state"
        self.logs_dir = Path(self.workspace) / "logs"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.log_path = self.state_dir / "mcp_orders.jsonl"
        self.connection_error_log = self.logs_dir / "mcp_connection_error.log"
        self.usage_safety_log = self.logs_dir / "codex_usage_safety.jsonl"
        self.tool_discovery_log = self.logs_dir / "mcp_tool_discovery.log"
        self.response_boundary_log = self.logs_dir / "mcp_response_boundary.log"
        self.config_tools = self._discover_tools_from_config()
        self.runtime_tools: Optional[List[Dict[str, Any]]] = None
        self._mcp_session_generation = 0
        self._codex_invocations_this_run = 0
        # A supplied transport is intentionally the unit-test seam. Production
        # owns one authenticated direct session for this adapter lifetime.
        self.transport = transport if transport is not None else (self._build_direct_transport() if direct_transport else None)
        self.fail_closed = True
        self.telegram_bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
        self.telegram_chat_id = os.environ.get("TELEGRAM_CHAT_ID")
        self.last_telegram_result: Optional[Dict[str, Any]] = None

    def _build_direct_transport(self) -> _DirectMCPTransport:
        cfg_path = self._codex_config_path()
        try:
            with cfg_path.open("rb") as f:
                server = (toml.load(f).get("mcp_servers", {}) or {}).get(self.server_name, {})
        except Exception as exc:
            raise RuntimeError(f"mcp_configuration_unavailable: {exc}") from exc
        url = server.get("url")
        if not isinstance(url, str) or not url.startswith("https://"):
            raise RuntimeError("mcp_configuration_unavailable: https url missing")

        headers: Dict[str, str] = {}
        configured_headers = server.get("headers")
        if isinstance(configured_headers, dict):
            for key, value in configured_headers.items():
                if isinstance(value, str):
                    headers[str(key)] = os.path.expandvars(value)
        # Preserve the same authenticated environment available to the user;
        # do not invent credentials or use browser/public quote fallbacks.
        bearer = next((os.environ.get(name) for name in (
            "ROBINHOOD_MCP_AUTH_TOKEN", "MCP_AUTH_TOKEN", "MCP_BEARER_TOKEN",
        ) if os.environ.get(name)), None)
        if bearer and "Authorization" not in headers:
            headers["Authorization"] = bearer if bearer.lower().startswith("bearer ") else f"Bearer {bearer}"
        cookie = os.environ.get("ROBINHOOD_MCP_COOKIE")
        if cookie and "Cookie" not in headers:
            headers["Cookie"] = cookie
        if "Authorization" not in headers:
            headers["Authorization"] = "Bearer " + self._codex_mcp_oauth_token(url)
        return _DirectMCPTransport(url, headers=headers)

    def _codex_mcp_oauth_token(self, url: str) -> str:
        """Use the Robinhood MCP OAuth grant, never the OpenAI auth token."""
        path = self._codex_config_path().with_name(".credentials.json")
        if os.name == 'nt':
            from windows_private import private
            if not private(path) or not private(path.parent):
                raise RuntimeError('robinhood_mcp_credentials_permissions')
        elif path.stat().st_mode & 0o077:
            raise RuntimeError("robinhood_mcp_credentials_permissions")
        credentials = json.loads(path.read_text(encoding="utf-8"))
        matches = [(key, value) for key, value in credentials.items()
                   if isinstance(value, dict) and value.get("server_name") == self.server_name
                   and value.get("server_url") == url]
        if len(matches) != 1:
            raise RuntimeError("robinhood_mcp_oauth_missing_or_ambiguous")
        key, record = matches[0]
        expires_at = record.get("expires_at")
        if not isinstance(expires_at, (int, float)):
            raise RuntimeError("robinhood_mcp_oauth_expiry_missing")
        if expires_at <= (time.time() + 300) * 1000:
            if not isinstance(record.get("refresh_token"), str) or not isinstance(record.get("client_id"), str):
                raise RuntimeError("robinhood_mcp_oauth_refresh_unavailable")
            request = Request(
                "https://api.robinhood.com/oauth2/token/",
                data=urlencode({"grant_type": "refresh_token", "refresh_token": record["refresh_token"],
                                "client_id": record["client_id"], "resource": url}).encode("utf-8"),
                headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
                method="POST",
            )
            with urlopen(request, timeout=20) as response:
                refreshed = json.load(response)
            access = refreshed.get("access_token")
            refresh = refreshed.get("refresh_token", record["refresh_token"])
            lifetime = refreshed.get("expires_in")
            if not isinstance(access, str) or not access or not isinstance(refresh, str) or not refresh \
                    or not isinstance(lifetime, (int, float)) or lifetime <= 0:
                raise RuntimeError("robinhood_mcp_oauth_refresh_invalid")
            record.update(access_token=access, refresh_token=refresh,
                          expires_at=int((time.time() + lifetime) * 1000))
            credentials[key] = record
            fd, temporary = tempfile.mkstemp(prefix=".credentials-", dir=path.parent)
            try:
                if os.name != 'nt': os.fchmod(fd, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as output:
                    json.dump(credentials, output)
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(temporary, path)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
        token = record.get("access_token")
        if not isinstance(token, str) or not token:
            raise RuntimeError("robinhood_mcp_oauth_token_missing")
        return token

    def _discover_tools_from_config(self) -> List[str]:
        cfg_path = self._codex_config_path()
        try:
            with cfg_path.open("rb") as f:
                data = toml.load(f)
        except Exception:
            return []
        tools_table = data.get("mcp_servers", {}).get(self.server_name, {}).get("tools", {})
        if isinstance(tools_table, dict):
            return list(tools_table.keys())
        return []

    @staticmethod
    def _codex_config_path() -> Path:
        codex_home = os.environ.get("CODEX_HOME")
        return (Path(codex_home) if codex_home else Path.home() / ".codex") / "config.toml"

    def _log(self, entry: Dict[str, Any]) -> None:
        try:
            with self.log_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, default=str) + "\n")
        except Exception:
            pass

    def _log_connection_error(self, error_text: str) -> None:
        timestamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        payload = {"timestamp": timestamp, "error": error_text}
        with self.connection_error_log.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")

    def _log_codex_usage(
        self,
        invocation_type: str,
        reason: str,
        *,
        codex_invoked: Optional[bool],
        **details: Any,
    ) -> None:
        from tools.ops_utils import append_jsonl
        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "scheduled_slot": os.environ.get("QQQ_AGENT_SCHEDULED_SLOT"),
            "reason": reason,
            "run_id": os.environ.get("QQQ_AGENT_RUN_ID"),
            "invocation_type": invocation_type,
            "codex_invoked": codex_invoked,
            "model": self.CODEX_MODEL,
            "reasoning_effort": self.CODEX_REASONING_EFFORT,
            **details,
        }
        append_jsonl(self.usage_safety_log, payload)

    @classmethod
    def _extract_codex_usage(cls, stdout: str) -> Dict[str, int]:
        """Extract the final Codex turn usage without logging prompt content."""
        for event in reversed(cls._parse_json_stream(stdout)):
            if not isinstance(event, dict) or event.get("type") != "turn.completed":
                continue
            usage = event.get("usage")
            if not isinstance(usage, dict):
                continue

            def _number(*names: str) -> int:
                for name in names:
                    value = usage.get(name)
                    if isinstance(value, (int, float)) and value >= 0:
                        return int(value)
                return 0

            input_units = _number("input_tokens", "input")
            cached_input_units = _number("cached_input_tokens", "cached_input")
            output_units = _number("output_tokens", "output")
            return {
                "input_units": input_units,
                "cached_input_units": cached_input_units,
                "output_units": output_units,
                "total_units": input_units + output_units,
            }
        return {}

    def _codex_invocation_authorization(self, now_utc: Optional[datetime] = None) -> tuple[bool, str]:
        """Authorize Codex only inside the claimed production decision slot."""
        from tools.scheduler_guard import evaluate_schedule

        decision = evaluate_schedule(
            now_utc or datetime.now(timezone.utc),
            env_file=Path(self.workspace) / ".env.production",
        )
        claimed_slot = os.environ.get("QQQ_AGENT_SCHEDULED_SLOT")
        run_id = os.environ.get("QQQ_AGENT_RUN_ID")
        if not decision.authorized:
            return False, decision.reason
        if not claimed_slot or claimed_slot != decision.slot:
            return False, "scheduled_slot_mismatch"
        if not run_id:
            return False, "missing_run_id"
        return True, "authorized_scheduled_window"

    @staticmethod
    def _is_retryable_snapshot_failure(snapshot: Dict[str, Any]) -> bool:
        text = " ".join(str(value or "") for value in (snapshot.get("errors") or []))
        lowered = text.lower()
        non_retryable = (
            "agents.md", "policy", "permission", "forbidden", "not allowlisted",
            "invalid param", "invalid argument", "missing required", "schema",
            "unauthorized", "authentication", "account_number_unavailable",
            "codex_usage_limit", "usage limit", "out of credits",
            "quota exceeded", "mcp_configuration_unavailable",
            "model is not supported when using codex", "codex_model_unavailable",
        )
        if any(marker in lowered for marker in non_retryable):
            return False
        retryable = (
            "nodename nor servname", "temporary failure in name resolution",
            "name or service not known", "dns", "getaddrinfo", "network is unreachable",
            "connection reset", "connection refused", "connection aborted",
            "transport channel closed", "transport closed", "broken pipe",
            "session disconnected", "mcp disconnected", "timed out", "timeout",
            "eof", "http 502", "http 503", "http 504", "bad gateway",
            "service unavailable", "gateway timeout", "delete returned http 400",
            "transient",
        )
        return any(marker in lowered for marker in retryable)

    def _log_snapshot_retry(self, *, event: str, attempt: int, wait_seconds: int | None = None, reason: str = "") -> None:
        self._log_connection_error(json.dumps({
            "event": event,
            "operation": "read_only_account_snapshot",
            "attempt": attempt,
            "wait_seconds": wait_seconds,
            "reason": reason[:500],
            "writes_retried": False,
        }, ensure_ascii=False))

    def _log_response_boundary(self, *, source: str, stdout: Any, stderr: Any, parsed: Any = None) -> None:
        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "source": source,
            "stdout_type": type(stdout).__name__,
            "stdout_len": len(stdout) if isinstance(stdout, (str, bytes)) else None,
            "stdout_boundary": "[REDACTED]",
            "stderr_type": type(stderr).__name__,
            "stderr_len": len(stderr) if isinstance(stderr, (str, bytes)) else None,
            "stderr_boundary": "[REDACTED]",
            "parsed_type": type(parsed).__name__ if parsed is not None else None,
        }
        try:
            with self.response_boundary_log.open("a", encoding="utf-8") as f:
                f.write(json.dumps(payload, ensure_ascii=False) + "\n")
        except Exception:
            pass

    @staticmethod
    def _looks_like_mcp_session_failure(*values: Any) -> bool:
        text = " ".join(str(value or "") for value in values).lower()
        return any(marker in text for marker in (
            "http 400", "returned http 400", "status code: 400",
            "session disconnected", "mcp disconnected", "connection reset",
            "broken pipe", "timed out", "timeout", "failed to delete session",
        ))

    def _invalidate_mcp_session(self, reason: str = "unknown") -> None:
        """Forget all session-derived state before establishing a new session."""
        self._mcp_session_generation += 1
        self.runtime_tools = None
        self._log_connection_error(json.dumps({
            "event": "mcp_session_reconnect",
            "generation": self._mcp_session_generation,
            "reason": reason,
        }, ensure_ascii=False))

    def _log_tool_discovery(self, *, payload_type: str, top_level_keys: List[str], parse_path: str, tool_count: int) -> None:
        timestamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        payload = {
            "timestamp": timestamp,
            "payload_type": payload_type,
            "top_level_keys": top_level_keys,
            "parse_path": parse_path,
            "tool_count": tool_count,
        }
        with self.tool_discovery_log.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")

    @staticmethod
    def _trace_params(function_name: str, tool_name: str, params: Dict[str, Any]) -> None:
        safe_params = params if isinstance(params, dict) else {}
        keys = sorted(str(k) for k in safe_params.keys())
        symbols_raw = safe_params.get("symbols")
        symbols: List[str] = []
        if isinstance(symbols_raw, list):
            symbols = [str(s).upper() for s in symbols_raw if str(s).strip()]
        elif isinstance(safe_params.get("symbol"), str) and str(safe_params.get("symbol")).strip():
            symbols = [str(safe_params.get("symbol")).upper()]
        print(
            f"[trace] function={function_name} tool={tool_name} params_keys={keys} symbols={symbols}",
            file=sys.stderr,
            flush=True,
        )

    def _tool_schema(self, tool_name: str) -> Dict[str, Any]:
        tools = self.runtime_tools if isinstance(self.runtime_tools, list) else self._discover_runtime_tools()
        for tool in tools:
            if not isinstance(tool, dict):
                continue
            name = str(tool.get("canonical_name") or tool.get("name") or "")
            if name == tool_name:
                schema = tool.get("input_schema")
                return schema if isinstance(schema, dict) else {}
        return {}

    def _required_fields(self, tool_name: str) -> List[str]:
        schema = self._tool_schema(tool_name)
        required = schema.get("required") if isinstance(schema, dict) else None
        if isinstance(required, list):
            return [str(item) for item in required if str(item).strip()]
        return []

    def _build_params_from_schema(self, tool_name: str, values: Dict[str, Any]) -> Dict[str, Any]:
        required = self._required_fields(tool_name)
        params: Dict[str, Any] = {}
        for field in required:
            if field not in values:
                continue
            val = values.get(field)
            if val is None:
                continue
            params[field] = val
        return params

    def _build_realized_pnl_params(self, account_number: Optional[str]) -> Optional[Dict[str, Any]]:
        if not account_number:
            return None

        schema = self._tool_schema("get_realized_pnl")
        required = self._required_fields("get_realized_pnl")
        params: Dict[str, Any] = {"account_number": account_number}

        properties = schema.get("properties") if isinstance(schema, dict) else None
        if not isinstance(properties, dict):
            properties = {}

        asset_field_name: Optional[str] = None
        for key in properties.keys():
            key_l = str(key).lower()
            if "asset" in key_l and "class" in key_l:
                asset_field_name = str(key)
                break

        if asset_field_name is None:
            for req in required:
                req_l = str(req).lower()
                if "asset" in req_l and "class" in req_l:
                    asset_field_name = req
                    break

        if asset_field_name is not None:
            asset_schema = properties.get(asset_field_name)
            enum_values = asset_schema.get("enum") if isinstance(asset_schema, dict) else None
            if not isinstance(enum_values, list) or not enum_values:
                return None
            allowed = [str(v) for v in enum_values if str(v).strip()]
            selected: Optional[str] = None
            for val in allowed:
                lv = val.lower()
                if any(token in lv for token in ("equity", "stock", "stocks", "etf", "equities")):
                    selected = val
                    break
            if selected is None:
                return None
            params[asset_field_name] = selected

        missing = [f for f in required if params.get(f) is None]
        if missing:
            return None
        return params

    def _walk_payload_nodes(self, payload: Any):
        stack: List[Any] = [payload]
        while stack:
            node = stack.pop()
            yield node
            if isinstance(node, dict):
                for key, value in node.items():
                    if key not in ("guide", "instructions", "examples"):
                        stack.append(value)
            elif isinstance(node, list):
                for item in node:
                    stack.append(item)
            elif isinstance(node, str):
                parsed = self._maybe_parse_json_text(node)
                if parsed is not None:
                    stack.append(parsed)

    @staticmethod
    def _top_level_keys(payload: Any) -> List[str]:
        if isinstance(payload, dict):
            return sorted(str(k) for k in payload.keys())
        return []

    def _extract_accounts_list(self, payload: Any) -> List[Dict[str, Any]]:
        accounts: List[Dict[str, Any]] = []
        for node in self._walk_payload_nodes(payload):
            if isinstance(node, dict):
                candidate = node.get("accounts")
                if isinstance(candidate, list):
                    for item in candidate:
                        if isinstance(item, dict):
                            accounts.append(item)
                if node.get("account_number") and isinstance(node, dict):
                    accounts.append(node)
        # Stable de-duplication by object id/text to avoid repeated nested captures.
        deduped: List[Dict[str, Any]] = []
        seen: set[str] = set()
        for account in accounts:
            key = json.dumps(account, sort_keys=True, ensure_ascii=False)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(account)
        return deduped

    def _log_accounts_summary(self, payload: Any, accounts: List[Dict[str, Any]]) -> None:
        keys = self._top_level_keys(payload)
        has_root_accounts = isinstance(payload, dict) and isinstance(payload.get("accounts"), list)
        has_result_accounts = (
            isinstance(payload, dict)
            and isinstance(payload.get("result"), dict)
            and isinstance(payload.get("result", {}).get("accounts"), list)
        )
        has_data_accounts = (
            isinstance(payload, dict)
            and isinstance(payload.get("data"), dict)
            and isinstance(payload.get("data", {}).get("accounts"), list)
        )
        has_content = isinstance(payload, dict) and payload.get("content") is not None
        print(
            "[accounts_summary] "
            f"top_keys={keys} has_accounts={has_root_accounts} has_result_accounts={has_result_accounts} "
            f"has_data_accounts={has_data_accounts} has_content={has_content} accounts_count={len(accounts)}",
            file=sys.stderr,
            flush=True,
        )

    def _extract_quote_candidates(self, payload: Any) -> Dict[str, Dict[str, Any]]:
        out: Dict[str, Dict[str, Any]] = {}
        for node in self._walk_payload_nodes(payload):
            if isinstance(node, dict):
                # results: [{"quote": {...}, "close": {...}}]
                if isinstance(node.get("quote"), dict):
                    quote = dict(node.get("quote") or {})
                    if not quote.get("symbol"):
                        sym = node.get("symbol") or node.get("ticker")
                        if isinstance(sym, str) and sym.strip():
                            quote["symbol"] = sym
                    symbol = str(quote.get("symbol") or quote.get("ticker") or "").upper()
                    if symbol:
                        out[symbol] = quote

                symbol = str(node.get("symbol") or node.get("ticker") or node.get("asset") or "").upper()
                has_price_hint = any(
                    node.get(key) is not None
                    for key in (
                        "last",
                        "last_trade_price",
                        "last_non_reg_trade_price",
                        "price",
                        "mark_price",
                        "close",
                        "bid_price",
                        "ask_price",
                        "bid",
                        "ask",
                        "bids",
                        "asks",
                    )
                )
                if symbol and has_price_hint:
                    out[symbol] = node

                # data: {"QQQ": {...}, "SMH": {...}}
                for key, value in node.items():
                    k = str(key).upper()
                    if k in ("QQQ", "SMH") and isinstance(value, dict):
                        quote = dict(value)
                        quote.setdefault("symbol", k)
                        out[k] = quote
        return out

    def _log_quotes_summary(self, payload: Any, quotes_by_symbol: Dict[str, Dict[str, Any]]) -> None:
        keys = self._top_level_keys(payload)

        def _has_key(node: Any, key: str) -> bool:
            if isinstance(node, dict):
                if node.get(key) is not None:
                    return True
                return any(_has_key(value, key) for value in node.values())
            if isinstance(node, list):
                return any(_has_key(value, key) for value in node)
            if isinstance(node, str):
                parsed = self._maybe_parse_json_text(node)
                return parsed is not None and _has_key(parsed, key)
            return False

        has_results = _has_key(payload, "results")
        has_quotes = _has_key(payload, "quotes")
        has_data = _has_key(payload, "data")
        has_content = _has_key(payload, "content")
        print(
            "[quotes_summary] "
            f"top_keys={keys} has_results={has_results} has_quotes={has_quotes} has_data={has_data} "
            f"has_content={has_content} symbol_count={len(quotes_by_symbol)}",
            file=sys.stderr,
            flush=True,
        )

    def _notify_telegram(self, text: str, priority: str = "normal") -> None:
        result = {"delivered": False, "reason": "credentials_missing"}
        try:
            if os.environ.get("TELEGRAM_ENABLED", "true").lower() == "false":
                result = {"delivered": False, "reason": "disabled"}
            elif self.telegram_bot_token and self.telegram_chat_id:
                import requests
                url = f"https://api.telegram.org/bot{self.telegram_bot_token}/sendMessage"
                response = requests.post(url, json={"chat_id": self.telegram_chat_id, "text": text}, timeout=10)
                payload = response.json()
                delivered = response.status_code == 200 and payload.get("ok") is True
                result = {"delivered": delivered, "reason": "sent" if delivered else "telegram_rejected",
                          "http_status": response.status_code}
        except Exception as exc:
            result = {"delivered": False, "reason": type(exc).__name__}
        self.last_telegram_result = result
        try:
            self.logs_dir.mkdir(parents=True, exist_ok=True)
            with (self.logs_dir / "telegram_delivery.jsonl").open("a", encoding="utf-8") as f:
                f.write(json.dumps({"timestamp": datetime.now(timezone.utc).isoformat(),
                                    **result}, ensure_ascii=False) + "\n")
        except OSError:
            pass
        return result

    def _extract_json_object(self, output: str) -> Dict[str, Any]:
        text = output.strip()
        if not text:
            raise RuntimeError("Empty MCP response")
        try:
            data = json.loads(text)
            if isinstance(data, dict):
                return data
        except Exception:
            pass

        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            snippet = text[start : end + 1]
            data = json.loads(snippet)
            if isinstance(data, dict):
                return data
        raise RuntimeError(f"Unable to parse MCP JSON response: {text[:300]}")

    def _collect_json_candidates(self, output: str) -> List[Any]:
        candidates: List[Any] = []
        text = output.strip()
        if not text:
            return candidates

        full = self._maybe_parse_json_text(text)
        if full is not None:
            candidates.append(full)

        for line in output.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            parsed = self._maybe_parse_json_text(stripped)
            if parsed is not None:
                candidates.append(parsed)

        return candidates

    @staticmethod
    def _parse_json_stream(text: str) -> List[Any]:
        """Parse complete adjacent JSON values (including Codex JSONL)."""
        if not isinstance(text, str) or not text.strip():
            return []
        decoder = json.JSONDecoder()
        values: List[Any] = []
        cursor = 0
        while cursor < len(text):
            while cursor < len(text) and text[cursor].isspace():
                cursor += 1
            if cursor >= len(text):
                break
            try:
                value, cursor = decoder.raw_decode(text, cursor)
            except json.JSONDecodeError:
                return []
            values.append(value)
        return values

    def _find_mcp_result_payload(self, payload: Any) -> Optional[Dict[str, Any]]:
        if isinstance(payload, dict):
            # Tool-call result shape used by _call_tool.
            if "ok" in payload and ("result" in payload or "error" in payload):
                return payload
            # Aggregated status shape used by fetch_aggregated_status.
            if "connected" in payload and "errors" in payload:
                return payload

            for key in ("result", "content", "item", "data", "payload", "response", "output", "message", "text"):
                child = payload.get(key)
                found = self._find_mcp_result_payload(child)
                if found is not None:
                    return found

            for value in payload.values():
                if isinstance(value, str):
                    parsed = self._maybe_parse_json_text(value)
                    if parsed is not None:
                        found = self._find_mcp_result_payload(parsed)
                        if found is not None:
                            return found
                elif isinstance(value, (dict, list)):
                    found = self._find_mcp_result_payload(value)
                    if found is not None:
                        return found

        elif isinstance(payload, list):
            for item in payload:
                found = self._find_mcp_result_payload(item)
                if found is not None:
                    return found

        elif isinstance(payload, str):
            parsed = self._maybe_parse_json_text(payload)
            if parsed is not None:
                return self._find_mcp_result_payload(parsed)

        return None

    def _maybe_parse_json_text(self, text: str) -> Optional[Any]:
        candidate = text.strip()
        if not candidate:
            return None
        try:
            return json.loads(candidate)
        except Exception:
            pass
        start = candidate.find("{")
        end = candidate.rfind("}")
        if start >= 0 and end > start:
            snippet = candidate[start : end + 1]
            try:
                return json.loads(snippet)
            except Exception:
                return None
        return None

    def _normalise_tools_list(self, tools: Any) -> List[Dict[str, Any]]:
        if isinstance(tools, dict):
            # Some MCP list_tools responses provide a name->schema mapping.
            tools = [{"name": key, "input_schema": value} for key, value in tools.items()]

        if not isinstance(tools, list):
            return []
        normalized: List[Dict[str, Any]] = []
        for item in tools:
            if isinstance(item, str):
                name = item
                normalized.append(
                    {
                        "name": str(name),
                        "canonical_name": self._canonical_tool_name(str(name)),
                        "description": None,
                        "input_schema": None,
                    }
                )
                continue

            if not isinstance(item, dict):
                continue

            name = item.get("name") or item.get("tool")
            if not name:
                continue
            normalized.append(
                {
                    "name": str(name),
                    "canonical_name": self._canonical_tool_name(str(name)),
                    "description": item.get("description"),
                    "input_schema": item.get("inputSchema") or item.get("input_schema") or item.get("schema"),
                }
            )
        return normalized

    def _canonical_tool_name(self, name: str) -> str:
        if name.startswith(self.ROBINHOOD_TOOL_PREFIX):
            return name[len(self.ROBINHOOD_TOOL_PREFIX) :]
        return name

    def _find_tools_in_payload(self, payload: Any, path: str = "root") -> Optional[Dict[str, Any]]:
        if isinstance(payload, dict):
            direct = payload.get("tools")
            if isinstance(direct, list):
                tools = self._normalise_tools_list(direct)
                if tools:
                    return {"tools": tools, "path": f"{path}.tools", "payload_type": type(payload).__name__, "top_level_keys": sorted(str(k) for k in payload.keys())}

            for key in ("result", "content", "item", "data", "payload", "response"):
                child = payload.get(key)
                found = self._find_tools_in_payload(child, f"{path}.{key}")
                if found is not None:
                    return found

            for key, value in payload.items():
                if isinstance(value, str):
                    parsed = self._maybe_parse_json_text(value)
                    if parsed is not None:
                        found = self._find_tools_in_payload(parsed, f"{path}.{key}")
                        if found is not None:
                            return found
                elif isinstance(value, (dict, list)):
                    found = self._find_tools_in_payload(value, f"{path}.{key}")
                    if found is not None:
                        return found

        elif isinstance(payload, list):
            tools = self._normalise_tools_list(payload)
            if tools:
                return {"tools": tools, "path": path, "payload_type": type(payload).__name__, "top_level_keys": []}
            for index, item in enumerate(payload):
                found = self._find_tools_in_payload(item, f"{path}[{index}]")
                if found is not None:
                    return found

        elif isinstance(payload, str):
            parsed = self._maybe_parse_json_text(payload)
            if parsed is not None:
                return self._find_tools_in_payload(parsed, path)

        return None

    def _summarize_discovery_failure(self, payload: Any) -> str:
        if isinstance(payload, dict):
            keys = sorted(str(k) for k in payload.keys())
            return f"type=dict top_level_keys={keys[:20]}"
        if isinstance(payload, list):
            return f"type=list len={len(payload)}"
        if isinstance(payload, str):
            preview = self._sanitize_discovery_text(
                payload.strip().replace("\n", " ")
            )[:300]
            return f"type=str preview={preview}"
        return f"type={type(payload).__name__}"

    @staticmethod
    def _sanitize_discovery_text(text: Any) -> str:
        value = str(text or "")
        value = re.sub(r"(?i)(Bearer\s+)[^\s\"']+", r"\1***", value)
        value = re.sub(r"(?i)(cookie\s*=\s*)[^\s\"']+", r"\1***", value)
        value = re.sub(
            r'(?i)(["\']?(?:authorization|token|api_key)["\']?\s*[:=]\s*["\']?)[^"\',}\s]+',
            r"\1***",
            value,
        )
        return value

    def _extract_tools_from_text_fallback(self, text: str) -> List[Dict[str, Any]]:
        if not text or "tools" not in text:
            return []

        # Try to locate a JSON-ish tools array and extract quoted names.
        match = re.search(r'"tools"\s*:\s*\[(.*?)\]', text, flags=re.DOTALL)
        if not match:
            return []

        block = match.group(1)
        names = re.findall(r'"([A-Za-z0-9_:\-\.]+)"', block)
        out: List[Dict[str, Any]] = []
        seen: set[str] = set()
        for name in names:
            if not name:
                continue
            if name in seen:
                continue
            seen.add(name)
            out.append({"name": name, "description": None, "input_schema": None})
        return out

    @staticmethod
    def _resolve_codex_executable() -> Optional[str]:
        """Resolve Codex for interactive shells and launchd environments."""
        configured = os.environ.get("CODEX_EXECUTABLE")
        # ChatGPT's bundled helper is not the Codex CLI and does not carry the
        # configured MCP authentication context.
        candidates = [configured] if configured and "ChatGPT.app" not in configured else []
        # Do not resolve a PATH alias: production must use the standalone,
        # authenticated Codex CLI explicitly.
        candidates.append("/opt/homebrew/bin/codex")
        for candidate in candidates:
            if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return candidate
        return None

    @staticmethod
    def _codex_subprocess_env() -> Dict[str, str]:
        """Pass through MCP auth/config while removing parent-session flags."""
        env = dict(os.environ)
        # Never route the child through ChatGPT.app's helper.  CODEX_HOME is
        # the user's existing Codex configuration directory; auth material is
        # owned and loaded by Codex itself and is never read by this adapter.
        env["CODEX_EXECUTABLE"] = "/opt/homebrew/bin/codex"
        env.setdefault("CODEX_HOME", str(Path.home() / ".codex"))
        # A standalone `codex exec` must initialize its own MCP client.  The
        # surrounding Codex app exports these integration/session variables;
        # retaining them makes the child try to attach to the parent app-server
        # instead of loading the configured MCP OAuth session.
        for name in (
            "CODEX_CI", "CODEX_PERMISSION_PROFILE", "CODEX_SANDBOX",
            "CODEX_SESSION_ID", "CODEX_THREAD_ID", "CODEX_MANAGED_BY_NPM",
            "CODEX_MANAGED_PACKAGE_ROOT", "CODEX_SANDBOX_NETWORK_DISABLED",
        ):
            env.pop(name, None)
        return env

    def _subprocess(self, cmd: List[str], **kwargs: Any) -> Any:
        kwargs.setdefault("env", self._codex_subprocess_env())
        return subprocess.Popen(cmd, **kwargs)

    def _run_codex_exec_raw(self, prompt: str, timeout: int = 60) -> Dict[str, Any]:
        authorized, authorization_reason = self._codex_invocation_authorization()
        if not authorized:
            self._log_codex_usage(
                "codex_exec_blocked",
                authorization_reason,
                codex_invoked=False,
            )
            raise RuntimeError(f"codex_invocation_blocked:{authorization_reason}")
        if self._codex_invocations_this_run >= self.MAX_CODEX_INVOCATIONS_PER_RUN:
            self._log_codex_usage(
                "codex_exec_blocked",
                "per_run_invocation_limit",
                codex_invoked=False,
                invocation_count=self._codex_invocations_this_run,
            )
            raise RuntimeError("codex_invocation_blocked:per_run_invocation_limit")
        codex = self._resolve_codex_executable()
        if codex is None:
            error = {"error_type": "codex_executable_not_found", "message": "Codex executable not found"}
            self._log_connection_error(json.dumps(error, ensure_ascii=False))
            return {"ok": False, **error, "stdout": "", "stderr": "", "returncode": -1}
        cmd = [
            codex, "--ask-for-approval", "never",
            "--model", self.CODEX_MODEL,
            "--config", f'model_reasoning_effort="{self.CODEX_REASONING_EFFORT}"',
            "--config", 'mcp_servers.robinhood-trading.tools.review_equity_order.approval_mode="approve"',
            "--config", 'mcp_servers.robinhood-trading.tools.place_equity_order.approval_mode="approve"',
            "exec", "--ephemeral", "--json", "--cd", self.workspace, "--skip-git-repo-check", prompt,
        ]
        cmd_text = " ".join(shlex.quote(part) for part in cmd)
        proc = self._subprocess(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        self._codex_invocations_this_run += 1
        self._log_codex_usage(
            "codex_exec",
            "authenticated_robinhood_mcp_call",
            codex_invoked=True,
            invocation_count=self._codex_invocations_this_run,
        )
        stdout_bytes = b""
        stderr_bytes = b""
        timed_out = False
        try:
            stdout_bytes, stderr_bytes = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except Exception:
                pass
            try:
                stdout_bytes, stderr_bytes = proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except Exception:
                    pass
                try:
                    stdout_bytes, stderr_bytes = proc.communicate(timeout=5)
                except Exception:
                    stdout_bytes = stdout_bytes or b""
                    stderr_bytes = stderr_bytes or b""
        stdout = stdout_bytes.decode(errors="ignore")
        stderr = stderr_bytes.decode(errors="ignore")
        returncode = proc.returncode if proc.returncode is not None else -1
        usage = self._extract_codex_usage(stdout)
        self._log_codex_usage(
            "codex_exec_completion",
            "timeout" if timed_out else ("completed" if returncode == 0 else "failed"),
            codex_invoked=None,
            invocation_count=self._codex_invocations_this_run,
            returncode=returncode,
            timed_out=timed_out,
            **usage,
        )
        # A Codex quota response is an authentication/runtime prerequisite
        # failure, not a Robinhood account-sync failure.  Detect it before
        # JSON/MCP parsing so callers can report the actionable cause and keep
        # the live-order guard closed without retrying pointlessly.
        quota_text = f"{stdout}\n{stderr}".lower()
        if any(marker in quota_text for marker in (
            "you've hit your usage limit",
            "you have hit your usage limit",
            "usage limit",
            "out of credits",
            "quota exceeded",
        )):
            result = {
                "ok": False,
                "error_type": "codex_usage_limit",
                "stdout": stdout,
                "stderr": stderr,
                "returncode": returncode,
                "command": cmd_text,
            }
            self._log_connection_error(json.dumps({
                "error_type": result["error_type"],
                "returncode": returncode,
                "message": "Codex usage limit reached; MCP discovery/calls unavailable",
            }, ensure_ascii=False))
            return result
        if "model is not supported when using codex" in quota_text:
            return {
                "ok": False,
                "error_type": "codex_model_unavailable",
                "stdout": stdout,
                "stderr": stderr,
                "returncode": returncode,
                "command": cmd_text,
            }
        if timed_out:
            result = {"ok": False, "error_type": "mcp_timeout", "stdout": stdout, "stderr": stderr, "returncode": returncode, "command": cmd_text}
            self._log_connection_error(json.dumps(result, ensure_ascii=False))
            return result
        if returncode != 0:
            result = {"ok": False, "error_type": "mcp_exec_failed", "stdout": stdout, "stderr": stderr, "returncode": returncode, "command": cmd_text}
            self._log_connection_error(stderr.strip() or stdout.strip() or f"exit={returncode}")
            return result
        return {"ok": True, "stdout": stdout, "stderr": stderr, "returncode": returncode, "command": cmd_text}

    def _run_codex_exec(self, prompt: str, timeout: int = 60) -> Dict[str, Any]:
        """Run Codex and return the tool-call JSON from its JSONL envelope.

        ``_call_tool`` has historically used this parsed compatibility seam,
        while discovery uses ``_run_codex_exec_raw`` so it can inspect the
        complete output.  Keep the seam intact for injected test adapters and
        for the no-transport production path; only live Codex output is
        accepted, never a cached quote or account snapshot.
        """
        raw = self._run_codex_exec_raw(prompt, timeout=timeout)
        error_type = str(raw.get("error_type") or "")
        if raw.get("ok") is False and error_type in {
            "codex_usage_limit", "authentication_required",
            "mcp_configuration_unavailable", "codex_model_unavailable",
        }:
            raise RuntimeError(error_type)
        events = self._parse_json_stream(str(raw.get("stdout") or ""))
        # Prefer the actual completed MCP result over the model's summary.
        # A cleanup error may be ignored only after this exact read succeeded.
        match = re.search(r"MCP 工具:\s*([a-z_]+)", prompt)
        expected_tool = match.group(1) if match else None
        native_payload = None
        native_failed = False
        for event in events:
            if not isinstance(event, dict) or event.get("type") != "item.completed":
                continue
            item = event.get("item", {})
            if not isinstance(item, dict) or item.get("type") != "mcp_tool_call":
                continue
            if item.get("server") != self.server_name or item.get("tool") != expected_tool:
                continue
            result = item.get("result")
            if item.get("status") != "completed" or item.get("error") or result is None:
                native_failed = True
                continue
            try:
                result = self._unwrap_mcp_result(result)
            except RuntimeError:
                native_failed = True
                continue
            native_payload = {"ok": True, "tool": expected_tool, "result": result, "error": None}
            # A later successful retry of the same read supersedes an earlier
            # failure. A final failed attempt still fails closed.
            native_failed = False
        if native_failed:
            raise RuntimeError("mcp_tool_result_failed")
        stderr_lines = [line.strip() for line in str(raw.get("stderr") or "").splitlines() if line.strip()]
        cleanup_only = bool(stderr_lines) and all(
            "fail to delete session" in line.lower() and "DELETE returned HTTP 400" in line
            for line in stderr_lines
        )
        completed = any(isinstance(e, dict) and e.get("type") == "turn.completed" for e in events)
        failed = any(isinstance(e, dict) and e.get("type") in ("turn.failed", "error") for e in events)
        recover_cleanup = (
            native_payload is not None and expected_tool in self.ALLOWED_MCP_TOOLS
            and expected_tool.startswith("get_") and cleanup_only and completed and not failed
            and raw.get("error_type") == "mcp_exec_failed" and not raw.get("timed_out")
            and isinstance(raw.get("returncode"), int) and raw["returncode"] > 0
        )
        if not raw.get("ok") and not recover_cleanup:
            detail = raw.get("stderr") or raw.get("error_type") or "codex_exec_failed"
            raise RuntimeError(str(detail))

        if failed:
            raise RuntimeError("codex_turn_failed")
        if native_payload is not None:
            return native_payload
        if expected_tool in {"place_equity_order", "review_equity_order"}:
            raise RuntimeError("write_requires_native_mcp_result")
        for event in events:
            payload = self._find_mcp_result_payload(event)
            if payload is not None:
                return payload

        raise RuntimeError(
            f"Unable to parse MCP JSON response: {self._sanitize_discovery_text(str(raw.get('stdout') or ''))[:300]}"
        )

    @staticmethod
    def _unwrap_mcp_result(result: Any) -> Any:
        """Decode MCP CallToolResult, never convert an error to account data."""
        if isinstance(result, list):
            return [MCPAdapter._unwrap_mcp_result(item) for item in result]
        if not isinstance(result, dict):
            return result
        if result.get("isError") is True:
            raise RuntimeError("mcp_tool_returned_error")
        if result.get("structuredContent") is not None:
            return MCPAdapter._unwrap_mcp_result(result["structuredContent"])
        content = result.get("content")
        if isinstance(content, list):
            decoded = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    try:
                        value = json.loads(block.get("text", ""))
                    except (TypeError, ValueError):
                        continue
                    if isinstance(value, (dict, list)):
                        decoded.append(MCPAdapter._unwrap_mcp_result(value))
            if len(decoded) == 1:
                return decoded[0]
            if len(decoded) > 1:
                raise RuntimeError("ambiguous_mcp_json_content")
        # Guides contain examples/instructions, never authoritative balances.
        return {key: MCPAdapter._unwrap_mcp_result(value) for key, value in result.items()
                if key not in ("guide", "instructions", "examples")}

    @staticmethod
    def _safe_float(value: Any) -> Optional[float]:
        if value is None or isinstance(value, bool):
            return None
        try:
            text = str(value).strip()
            if not text:
                return None
            number = float(text)
            return number if math.isfinite(number) else None
        except Exception:
            return None

    @staticmethod
    def _extract_items(payload: Any, preferred_keys: List[str]) -> List[Any]:
        if isinstance(payload, list):
            return payload
        if not isinstance(payload, dict):
            return []

        for key in preferred_keys:
            value = payload.get(key)
            if isinstance(value, list):
                return value
            if isinstance(value, dict):
                nested = MCPAdapter._extract_items(value, preferred_keys)
                if nested:
                    return nested

        for key in ("result", "data", "payload", "items"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
            if isinstance(value, dict):
                nested = MCPAdapter._extract_items(value, preferred_keys)
                if nested:
                    return nested
        return []

    def _first_numeric(self, payload: Any, keys: List[str]) -> Optional[float]:
        if isinstance(payload, dict):
            for key in keys:
                val = self._safe_float(payload.get(key))
                if val is not None:
                    return val
            for key in ("result", "data", "payload"):
                nested = payload.get(key)
                val = self._first_numeric(nested, keys)
                if val is not None:
                    return val
            for nested in payload.values():
                val = self._first_numeric(nested, keys)
                if val is not None:
                    return val
        elif isinstance(payload, list):
            for item in payload:
                val = self._first_numeric(item, keys)
                if val is not None:
                    return val
        return None

    def _first_bool(self, payload: Any, keys: List[str]) -> Optional[bool]:
        if isinstance(payload, dict):
            for key in keys:
                val = payload.get(key)
                if isinstance(val, bool):
                    return val
            for key in ("result", "data", "payload"):
                nested = payload.get(key)
                val = self._first_bool(nested, keys)
                if val is not None:
                    return val
        elif isinstance(payload, list):
            for item in payload:
                val = self._first_bool(item, keys)
                if val is not None:
                    return val
        return None

    def _first_string(self, payload: Any, keys: List[str]) -> Optional[str]:
        if isinstance(payload, dict):
            for key in keys:
                val = payload.get(key)
                if val is not None:
                    text = str(val).strip()
                    if text:
                        return text
            for key in ("result", "data", "payload"):
                nested = payload.get(key)
                text = self._first_string(nested, keys)
                if text:
                    return text
        elif isinstance(payload, list):
            for item in payload:
                text = self._first_string(item, keys)
                if text:
                    return text
        return None

    def _extract_symbol_payload(self, payload: Any, symbol: str) -> Optional[Dict[str, Any]]:
        target = str(symbol or "").upper()
        quotes_by_symbol = self._extract_quote_candidates(payload)
        if target in quotes_by_symbol:
            return quotes_by_symbol[target]
        return None

    def _normalize_quote(self, symbol: str, payload: Any) -> Optional[Dict[str, Any]]:
        if not isinstance(payload, dict):
            return None

        valuation_field = next((key for key in (
            "last_trade_price", "last_non_reg_trade_price", "price", "mark_price", "last", "close")
            if payload.get(key)), None)
        valuation_time = None
        if valuation_field == "last_trade_price":
            valuation_time = (payload.get("last_trade_at") or payload.get("last_trade_timestamp")
                              or payload.get("venue_last_trade_time")
                              or payload.get("venue_last_trade_timestamp"))
        elif valuation_field == "last_non_reg_trade_price":
            valuation_time = (payload.get("last_non_reg_trade_at") or payload.get("last_non_reg_trade_timestamp")
                              or payload.get("venue_last_non_reg_trade_time"))

        last = self._safe_float(
            payload.get("last_trade_price")
            or payload.get("last_non_reg_trade_price")
            or payload.get("price")
            or payload.get("mark_price")
            or payload.get("last")
            or payload.get("close")
        )
        bid = self._safe_float(payload.get("bid_price") or payload.get("bid") or payload.get("best_bid"))
        ask = self._safe_float(payload.get("ask_price") or payload.get("ask") or payload.get("best_ask"))

        is_book = "bids" in payload or "asks" in payload
        if is_book:
            # Price-book levels belong to the parent symbol, not separate quotes.
            # Do not assume the service has sorted the levels.
            def prices(levels):
                if not isinstance(levels, list):
                    return []
                values = [self._safe_float(level.get("price"))
                          for level in levels if isinstance(level, dict)]
                return [value for value in values if value is not None and value > 0]

            bids = prices(payload.get("bids"))
            asks = prices(payload.get("asks"))
            bid = max(bids) if bids else None
            ask = min(asks) if asks else None
            if bid is not None and ask is not None and bid > ask:
                return None

        if bid == 0:
            bid = None
        if ask == 0:
            ask = None

        if last is None and bid is None and ask is None:
            return None

        if last is None:
            if bid is not None and ask is not None:
                last = (bid + ask) / 2.0
            elif bid is not None:
                last = bid
            elif ask is not None:
                last = ask

        timestamp_text = None
        for key in (
            "last_trade_at", "last_trade_timestamp", "venue_last_trade_time",
            "venue_last_trade_timestamp", "updated_at", "timestamp", "as_of",
        ):
            val = payload.get(key)
            if isinstance(val, str) and val.strip():
                timestamp_text = val.strip()
                break
        timestamp_dt = self._parse_iso_timestamp(timestamp_text) if timestamp_text else None
        if is_book and timestamp_dt is None:
            # A price book with unknown freshness cannot become a live quote.
            return None
        if timestamp_dt is None:
            timestamp_dt = datetime.now(timezone.utc)
        age_seconds = (datetime.now(timezone.utc) - timestamp_dt.astimezone(timezone.utc)).total_seconds()
        is_stale = age_seconds > 60.0

        return {
            "symbol": str(symbol or "").upper(),
            "last": last,
            "bid": bid,
            "ask": ask,
            "source": "robinhood_mcp",
            "timestamp": timestamp_dt.astimezone(timezone.utc).isoformat(),
            "is_realtime": True,
            "is_stale": is_stale,
            "stale_reason": "quote_older_than_60s" if is_stale else None,
            "price_type": "last" if valuation_field in ("last_trade_price", "last_non_reg_trade_price") else "midpoint" if valuation_field is None and bid is not None and ask is not None else "unknown",
            "price_session": "regular" if valuation_field == "last_trade_price" else "non_regular" if valuation_field == "last_non_reg_trade_price" else "unknown",
            "valuation_provider_as_of": valuation_time,
        }

    def _parse_iso_timestamp(self, value: Any) -> Optional[datetime]:
        if value is None:
            return None
        text = str(value).strip()
        if not text:
            return None
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00"))
        except Exception:
            return None

    def _order_filled_amount(self, order: Dict[str, Any]) -> Optional[float]:
        executions = order.get("executions")
        if isinstance(executions, list) and executions:
            from decimal import Decimal, ROUND_HALF_UP
            try:
                total = Decimal(0)
                seen = set()
                for fill in executions:
                    if not fill.get("id") or fill["id"] in seen:
                        return None
                    seen.add(fill["id"])
                    values = [Decimal(str(fill[k])) for k in ("price", "quantity")]
                    fee = Decimal(str(fill.get("fees") or 0))
                    if any(not x.is_finite() or x < 0 for x in values + [fee]):
                        return None
                    total += (values[0] * values[1] + fee).quantize(Decimal(".01"), rounding=ROUND_HALF_UP)
                return float(total)
            except (ValueError, TypeError, KeyError, ArithmeticError):
                return None
        if order.get("cumulative_quantity") is not None:
            qty = self._safe_float(order.get("cumulative_quantity"))
            px = self._safe_float(order.get("average_price"))
            if qty == 0:
                return 0.0
            if qty is not None and qty > 0 and px is not None and px > 0:
                return round(qty * px + (self._safe_float(order.get("fees")) or 0), 2)
        for key in ("filled_amount", "filled_notional", "filled_dollars", "cumulative_notional"):
            val = self._safe_float(order.get(key))
            if val is not None:
                return max(0.0, val)

        qty = self._safe_float(order.get("filled_quantity") or order.get("executed_quantity"))
        px = self._safe_float(order.get("average_price") or order.get("filled_average_price"))
        if qty is not None and px is not None:
            return max(0.0, qty * px)
        return None

    def _order_open_amount(self, order: Dict[str, Any]) -> Optional[float]:
        for key in ("remaining_notional", "open_notional", "reserved_amount"):
            val = self._safe_float(order.get(key))
            if val is not None:
                return max(0.0, val)

        native_amount = order.get("dollar_based_amount")
        native_amount = native_amount.get("amount") if isinstance(native_amount, dict) else None
        amount = self._safe_float(native_amount or order.get("dollar_amount") or order.get("amount") or order.get("notional"))
        filled = self._order_filled_amount(order)
        if amount is not None:
            # Reserve the whole request if the broker has not reported fills.
            return max(0.0, amount - (filled or 0.0))

        qty = self._safe_float(order.get("quantity"))
        filled_qty = self._safe_float(order.get("filled_quantity")) or 0.0
        price = self._safe_float(order.get("limit_price") or order.get("price"))
        if qty is not None and price is not None:
            return max(0.0, (qty - filled_qty) * price)
        return None

    @staticmethod
    def _is_agentic_order(order: Dict[str, Any]) -> bool:
        """Count only orders Robinhood explicitly attributes to Agent Mode.

        Robinhood's ``placed_agent`` field is the API equivalent of the UI's
        ``You`` versus ``Agent`` label. Manual and unattributed orders remain
        visible in holdings/history but never consume the Agent budget.
        """
        placed_agent = str(order.get("placed_agent") or "").strip().lower()
        return placed_agent in {"agentic", "agent"}

    def _is_completed_drip_order(self, order: Dict[str, Any]) -> bool:
        """Recognize broker-labelled dividend reinvestment with dated fill proof."""
        if (str(order.get("placed_agent") or "").strip().lower() != "drip"
                or str(order.get("state") or order.get("status") or "").lower() != "filled"
                or str(order.get("side") or "").lower() != "buy"
                or order.get("symbol") not in {"QQQ", "SMH"}
                or not (order.get("id") or order.get("order_id"))):
            return False
        executions = order.get("executions")
        if not isinstance(executions, list) or not executions:
            return False
        seen = set()
        for fill in executions:
            if not isinstance(fill, dict) or not isinstance(fill.get("id"), str) or not fill["id"] or fill["id"] in seen:
                return False
            seen.add(fill["id"])
            when = self._parse_iso_timestamp(fill.get("timestamp"))
            price, quantity = self._safe_float(fill.get("price")), self._safe_float(fill.get("quantity"))
            fees = self._safe_float(fill.get("fees", 0))
            if (when is None or when.tzinfo is None
                    or (when - datetime.now(timezone.utc)).total_seconds() > 5
                    or price is None or price <= 0 or quantity is None or quantity <= 0
                    or fees is None or fees < 0
                    or str(fill.get("currency") or "USD").upper() != "USD"):
                return False
        return True

    def _validate_order_collection(self, payload):
        found = False
        for node in self._walk_payload_nodes(payload):
            if not isinstance(node, dict):
                continue
            if any(node.get(k) for k in ("next", "next_cursor", "next_page_token", "has_more")):
                raise RuntimeError("orders_history_incomplete_pagination")
            for key in ("orders", "results"):
                if isinstance(node.get(key), list):
                    found = True
                    if not all(isinstance(item, dict) for item in node[key]):
                        raise RuntimeError("orders_response_malformed")
        if not found:
            raise RuntimeError("orders_collection_missing")
        return payload

    def _build_snapshot_from_tool_calls(self) -> Dict[str, Any]:
        snapshot: Dict[str, Any] = _InternalSnapshot({
            "connected": False,
            "agentic_account_confirmed": None,
            "account_last_four": None,
            "cash": None,
            "available_cash": None,
            "buying_power": None,
            "portfolio_value": None,
            "positions": {"QQQ": None, "SMH": None},
            "open_orders": [],
            "monthly_filled_buys": None,
            "daily_filled_buys": None,
            "monthly_open_buy_amount": None,
            "daily_open_buy_amount": None,
            "quotes": {"QQQ": None, "SMH": None},
            "capabilities": {},
            "realized_pnl": None,
            "account_readable": False,
            "accounts_readable": False,
            "portfolio_readable": False,
            "positions_readable": False,
            "orders_readable": False,
            "quotes_readable": False,
            "realized_pnl_readable": False,
            "errors": [],
        })

        def _is_truthy(value: Any) -> bool:
            if isinstance(value, bool):
                return value
            return str(value).strip().lower() in {"1", "true", "yes", "y"}

        print("[read_only_snapshot] start split tool calls", file=sys.stderr, flush=True)
        # Six standalone CLI reads each require MCP startup as well as the read.
        per_read_timeout = 120.0
        deadline = time.monotonic() + 900.0
        account_fetched_at = None
        chosen = None
        call_success: Dict[str, bool] = {
            "accounts": False,
            "portfolio": False,
            "positions": False,
            "quotes": False,
            "orders": False,
            "realized_pnl": False,
        }
        payloads: Dict[str, Any] = {}

        tracked_tools = [
            "get_accounts",
            "get_portfolio",
            "get_equity_positions",
            "get_equity_orders",
            "get_equity_price_book",
            "get_realized_pnl",
        ]
        required_fields: Dict[str, List[str]] = {}
        for tool_name in tracked_tools:
            if tool_name == "get_realized_pnl" and os.environ.get("ENABLE_REALIZED_PNL", "false").lower() != "true":
                continue
            try:
                required = self._required_fields(tool_name)
            except Exception as exc:
                required = []
                snapshot["errors"].append(f"{tool_name}_schema_failed: {type(exc).__name__}: {exc}")
            required_fields[tool_name] = required
            print(f"[trace_schema] tool={tool_name} required={required}", file=sys.stderr, flush=True)
        snapshot["tool_required_fields"] = required_fields

        account_number: Optional[str] = None

        remaining = deadline - time.monotonic()
        if remaining > 0:
            per_call_timeout = min(per_read_timeout, max(1.0, remaining))
            # get_accounts is the broker's account discovery call and accepts
            # an empty object in the working direct MCP path.  Do not let a
            # stale/over-described discovered schema manufacture parameters or
            # reject this call before MCP gets a chance to answer.
            accounts_params: Dict[str, Any] = {}
            self._trace_params("read_only_snapshot", "get_accounts", accounts_params)
            try:
                payloads["accounts"] = self._call_tool("get_accounts", accounts_params, retries=0, timeout_seconds=int(per_call_timeout))
                call_success["accounts"] = True
                snapshot["account_readable"] = True
                snapshot["accounts_readable"] = True
            except Exception as exc:
                snapshot["errors"].append(f"get_accounts_failed: {exc}")
        else:
            snapshot["errors"].append("get_accounts_failed: total_read_only_timeout")

        accounts_payload = payloads.get("accounts")
        if call_success.get("accounts"):
            accounts = self._extract_accounts_list(accounts_payload)
            self._log_accounts_summary(accounts_payload, accounts)
            agentic_accounts = [
                acc
                for acc in accounts
                if _is_truthy(acc.get("agentic_allowed")) and str(acc.get("state") or "").lower() == "active"
            ]
            if agentic_accounts:
                chosen = agentic_accounts[0]
                account_number = self._first_string(chosen, ["account_number", "account", "number"])
                snapshot["agentic_account_confirmed"] = True
                if account_number:
                    setattr(snapshot, "agentic_account_number_internal", account_number)
                    digits = "".join(ch for ch in account_number if ch.isdigit())
                    masked = digits[-4:] if len(digits) >= 4 else account_number[-4:]
                    print(f"[accounts_summary] selected_account_last4={masked}", file=sys.stderr, flush=True)
            else:
                snapshot["agentic_account_confirmed"] = False
                snapshot["errors"].append("agentic_account_not_found")

            if account_number and snapshot.get("account_last_four") is None:
                digits = "".join(ch for ch in account_number if ch.isdigit())
                snapshot["account_last_four"] = digits[-4:] if len(digits) >= 4 else account_number[-4:]

            # Never borrow capital from the default/margin account in the list.
            accounts_cash = self._first_numeric(chosen, ["cash", "available_cash", "cash_available_for_withdrawal"])
            accounts_bp = self._first_numeric(chosen, ["buying_power", "account_buying_power"])
            accounts_available_cash = self._first_numeric(chosen, ["available_cash", "cash", "cash_available_for_withdrawal"])
            if accounts_cash is not None:
                snapshot["cash"] = accounts_cash
            if accounts_bp is not None:
                snapshot["buying_power"] = accounts_bp
            if accounts_available_cash is not None:
                snapshot["available_cash"] = accounts_available_cash

        phase2_calls = [
            ("positions", "get_equity_positions"),
            ("orders", "get_equity_orders"),
            ("realized_pnl", "get_realized_pnl"),
            # Read balance last so slow order reconciliation cannot age it out.
            ("portfolio", "get_portfolio"),
        ]

        for slot, tool_name in phase2_calls:
            if tool_name == "get_realized_pnl" and os.environ.get("ENABLE_REALIZED_PNL", "false").lower() != "true":
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                snapshot["errors"].append(f"{tool_name}_failed: total_read_only_timeout")
                continue

            # Portfolio is usable without an account identifier on some broker
            # sessions.  Try it independently so an accounts failure does not
            # erase valid portfolio/capital data.  Other account-scoped tools
            # remain fail-closed when no identifier is available.
            if not account_number and (tool_name != "get_portfolio" or call_success.get("accounts")):
                snapshot["errors"].append(f"{tool_name}_failed: account_number_unavailable")
                continue

            if tool_name == "get_realized_pnl":
                params = self._build_realized_pnl_params(account_number)
                if params is None:
                    snapshot["realized_pnl_readable"] = False
                    snapshot["errors"].append("realized_pnl_unsupported_or_invalid_schema")
                    continue
                missing_required = [f for f in required_fields.get(tool_name, []) if params.get(f) is None]
            else:
                values = {"account_number": account_number}
                params = {"account_number": account_number} if account_number else {}
                schema_params = self._build_params_from_schema(tool_name, values)
                params.update(schema_params)
                missing_required = [f for f in required_fields.get(tool_name, []) if f not in params or params.get(f) is None]
            if missing_required:
                if tool_name == "get_realized_pnl":
                    snapshot["realized_pnl_readable"] = False
                    snapshot["errors"].append("realized_pnl_unsupported_or_invalid_schema")
                else:
                    snapshot["errors"].append(f"{tool_name}_failed: missing_required_params={missing_required}")
                continue

            self._trace_params("read_only_snapshot", tool_name, params)
            try:
                if tool_name == "get_equity_orders":
                    # The broker's order-history endpoint is much slower than
                    # balances/quotes. A successful direct call has been
                    # observed beyond the old 45-second cap. Reserve time for
                    # portfolio and quotes, but do not manufacture a timeout
                    # before Robinhood returns the order ledger.
                    order_deadline = deadline - 2 * per_read_timeout
                    order_remaining = max(1.0, order_deadline - time.monotonic())
                    order_timeout = int(min(self.ORDER_READ_TIMEOUT_SECONDS, order_remaining))
                    payloads[slot] = self._read_orders_with_retry(
                        params, order_timeout, deadline=order_deadline
                    )
                    self._validate_order_collection(payloads[slot])
                else:
                    timeout = int(min(per_read_timeout, max(1.0, remaining)))
                    payloads[slot] = self._call_tool(tool_name, params, retries=0, timeout_seconds=timeout)
                call_success[slot] = True
                if slot == "portfolio":
                    account_fetched_at = datetime.now(timezone.utc).isoformat()
            except Exception as exc:
                snapshot["errors"].append(f"{tool_name}_failed: {exc}")

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            snapshot["errors"].append("get_equity_quotes_failed: total_read_only_timeout")
        else:
            quote_symbols = ["QQQ", "SMH"]
            quote_params = {"symbols": quote_symbols}
            self._trace_params("read_only_snapshot", "get_equity_price_book", quote_params)
            try:
                if type(self)._call_tool is not MCPAdapter._call_tool:
                    payloads["quotes"] = self._call_tool("get_equity_quotes", {"symbols": quote_symbols})
                else:
                    payloads["quotes"] = self._call_tool(
                        "get_equity_price_book", quote_params, retries=0,
                        timeout_seconds=int(min(per_read_timeout, max(1.0, remaining))),
                    )
                call_success["quotes"] = True
            except Exception as exc:
                snapshot["errors"].append(f"get_equity_quotes_failed: {exc}")

        snapshot["connected"] = bool(call_success.get("accounts") or call_success.get("portfolio") or call_success.get("positions"))
        if not snapshot["connected"]:
            snapshot["live_blocked_by_network"] = True
            snapshot["error_type"] = "mcp_error"

        portfolio_payload = payloads.get("portfolio")
        if call_success.get("portfolio"):
            snapshot["portfolio_readable"] = True
            portfolio_cash = self._first_numeric(portfolio_payload, ["cash", "available_cash", "cash_available_for_withdrawal"])
            portfolio_bp = self._first_numeric(portfolio_payload, ["buying_power", "account_buying_power", "day_trading_buying_power"])
            portfolio_value = self._first_numeric(portfolio_payload, ["portfolio_value", "equity", "total_equity", "market_value"])
            # Portfolio is authoritative for account cash and buying power;
            # get_accounts values above are only a fallback when portfolio
            # omits a field.
            if portfolio_cash is not None:
                snapshot["cash"] = portfolio_cash
            if portfolio_cash is not None:
                snapshot["available_cash"] = portfolio_cash
            if portfolio_bp is not None:
                snapshot["buying_power"] = portfolio_bp
            if portfolio_value is not None:
                snapshot["portfolio_value"] = portfolio_value

        positions_payload = payloads.get("positions")
        if call_success.get("positions"):
            snapshot["positions_readable"] = True
            items = self._extract_items(positions_payload, ["positions"])
            for item in items:
                if not isinstance(item, dict):
                    continue
                symbol = str(item.get("symbol") or item.get("ticker") or "").upper()
                if symbol in ("QQQ", "SMH"):
                    snapshot["positions"][symbol] = item

        quotes_payload = payloads.get("quotes")
        if call_success.get("quotes"):
            quotes_by_symbol = self._extract_quote_candidates(quotes_payload)
            self._log_quotes_summary(quotes_payload, quotes_by_symbol)
            quote_success_count = 0
            for symbol in ("QQQ", "SMH"):
                quote_payload = quotes_by_symbol.get(symbol) or self._extract_symbol_payload(quotes_payload, symbol)
                quote = self._normalize_quote(symbol, quote_payload)
                if quote is not None:
                    snapshot["quotes"][symbol] = quote
                    quote_success_count += 1
            snapshot["quotes_readable"] = quote_success_count == 2
            if quote_success_count != 2:
                missing = []
                for symbol in ("QQQ", "SMH"):
                    if snapshot["quotes"].get(symbol) is not None:
                        continue
                    book = quotes_by_symbol.get(symbol)
                    if isinstance(book, dict) and ("bids" in book or "asks" in book):
                        empty = book.get("bids") == [] and book.get("asks") == []
                        reason = "empty_price_book" if empty else "invalid_price_book"
                        missing.append(f"{reason}:{symbol}")
                snapshot["errors"].append("get_equity_quotes_failed: " + (",".join(missing) or "missing_symbol_quotes"))

        orders_payload = payloads.get("orders")
        if call_success.get("orders"):
            snapshot["orders_readable"] = True
            items = self._extract_items(orders_payload, ["orders"])
            orders = [item for item in items if isinstance(item, dict)]
            snapshot["order_history"] = orders
            snapshot["open_orders"] = [order for order in orders if str(
                order.get("state") or order.get("status") or ""
            ).lower() in {"open", "pending", "submitted", "partially_filled", "queued", "confirmed", "unconfirmed"}]

            from zoneinfo import ZoneInfo
            now_utc = datetime.now(ZoneInfo("America/New_York")).replace(tzinfo=None)
            month_start = now_utc.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            day_start = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)

            historical_spend = {}
            manual_filled = 0.0
            unattributed_orders = 0
            invalid_agent_amounts = False
            monthly_filled = 0.0
            daily_filled = 0.0
            monthly_open = 0.0
            daily_open = 0.0

            for order in orders:
                side = str(order.get("side") or "").lower()
                if side != "buy":
                    continue
                state = str(order.get("state") or order.get("status") or "").lower()
                ts = self._parse_iso_timestamp(order.get("filled_at") or order.get("updated_at") or order.get("created_at"))
                ts_naive = ts.astimezone(ZoneInfo("America/New_York")).replace(tzinfo=None) if ts is not None else None

                if not self._is_agentic_order(order):
                    if self._is_completed_drip_order(order):
                        snapshot.setdefault("external_drip_orders", []).append({
                            "order_id": order.get("id") or order.get("order_id"),
                            "symbol": order["symbol"], "filled_amount": self._order_filled_amount(order),
                            "classification": "EXTERNAL_OR_MANUAL_ACTIVITY", "source": "drip",
                        })
                        continue
                    owner = str(order.get("placed_agent") or "").strip().lower()
                    if owner in {"you", "user", "manual"}:
                        if ts_naive is not None and ts_naive >= month_start:
                            manual_filled += self._order_filled_amount(order) or 0.0
                    elif state not in {"rejected", "failed", "cancelled", "canceled"}:
                        unattributed_orders += 1
                    continue

                filled_amount = self._order_filled_amount(order) or 0.0
                open_amount = self._order_open_amount(order) or 0.0
                if (state in {"filled", "partially_filled"} and
                    (self._order_filled_amount(order) is None or ts_naive is None)):
                    invalid_agent_amounts = True
                if (state in {"open", "pending", "submitted", "partially_filled", "queued", "confirmed", "unconfirmed"}
                        and self._order_open_amount(order) is None):
                    invalid_agent_amounts = True

                if state in ("filled", "partially_filled") or filled_amount > 0:
                    if ts_naive is not None:
                        key = ts_naive.strftime("%Y-%m")
                        historical_spend[key] = historical_spend.get(key, 0.0) + filled_amount
                    if ts_naive is None or ts_naive >= month_start:
                        monthly_filled += filled_amount
                    if ts_naive is None or ts_naive >= day_start:
                        daily_filled += filled_amount

                if state in ("open", "pending", "submitted", "partially_filled", "queued", "confirmed", "unconfirmed"):
                    # Reservations do not expire simply because the date changes.
                    monthly_open += open_amount
                    daily_open += open_amount

            snapshot["monthly_filled_buys"] = monthly_filled
            snapshot["agent_filled_by_month"] = historical_spend
            snapshot["manual_monthly_filled_buys"] = manual_filled
            snapshot["unattributed_buy_orders"] = unattributed_orders
            snapshot["budget_history_complete"] = not (unattributed_orders or invalid_agent_amounts)
            snapshot["daily_filled_buys"] = daily_filled
            snapshot["monthly_open_buy_amount"] = monthly_open
            snapshot["daily_open_buy_amount"] = daily_open

        pnl_payload = payloads.get("realized_pnl")
        if call_success.get("realized_pnl"):
            snapshot["realized_pnl_readable"] = True
            pnl_value = self._first_numeric(pnl_payload, ["realized_pnl", "pnl", "amount", "value"])
            if pnl_value is not None:
                snapshot["realized_pnl"] = pnl_value

        # The production consumer checks account_sync, not just connected.
        # Normalize actual scoped results; do not manufacture a success flag.
        from tools.account_sync import normalize_account_sync, ACCOUNT_SYNC_OK
        sync = normalize_account_sync({
            "accounts": {"accounts": [chosen] if chosen is not None else []},
            "account": payloads.get("portfolio") if call_success["portfolio"] else {},
            "positions": payloads.get("positions"),
            "open_or_recent_orders": payloads.get("orders"),
            "quotes": snapshot["quotes"],
            "account_data_timestamp": account_fetched_at,
        })
        if not call_success["portfolio"]:
            quota_failure = next((str(error) for error in snapshot["errors"]
                                  if "codex_usage_limit" in str(error).lower()
                                  or "you've hit your usage limit" in str(error).lower()
                                  or "usage limit" in str(error).lower()), None)
            sync = {
                "status": "ACCOUNT_SYNC_FAILED",
                "reason": "codex_usage_limit" if quota_failure else "portfolio_read_failed",
                "live_status": "LIVE_BLOCKED",
            }
            if quota_failure:
                snapshot["error_type"] = "codex_usage_limit"
        snapshot["account_sync"] = sync
        snapshot["account_sync_status"] = sync["status"]
        snapshot["account_sync_reason"] = sync.get("reason")
        snapshot["account_data_timestamp"] = sync.get("account_data_timestamp")
        snapshot["account_readable"] = sync["status"] == ACCOUNT_SYNC_OK
        snapshot["eligible_buying_power"] = sync.get("eligible_buying_power")
        if sync["status"] == ACCOUNT_SYNC_OK:
            snapshot["buying_power"] = sync["eligible_buying_power"]
            snapshot["cash"] = sync["cash"]
            snapshot["portfolio_value"] = sync["portfolio_value"]

        print(
            "[read_only_snapshot] split calls done "
            f"connected={snapshot.get('connected')} cash={snapshot.get('cash')} "
            f"orders_readable={snapshot.get('orders_readable')} quotes_readable={snapshot.get('quotes_readable')} "
            f"errors_count={len(snapshot.get('errors') or [])}",
            file=sys.stderr,
            flush=True,
        )
        if sync["status"] == ACCOUNT_SYNC_OK and not snapshot.get("orders_readable"):
            snapshot["account_sync_status"] = "ACCOUNT_SYNC_PARTIAL"
            snapshot["account_sync_reason"] = "orders_unreadable"
            snapshot["account_sync"] = {**sync, "status": "ACCOUNT_SYNC_PARTIAL", "reason": "orders_unreadable", "live_status": "LIVE_BLOCKED"}
        from tools.robinhood_runtime import _is_regular_market_time
        market_open = _is_regular_market_time(datetime.now(timezone.utc))
        snapshot["market_session"] = "regular" if market_open else "outside_regular"
        snapshot["quote_status"] = ("AVAILABLE" if snapshot["quotes_readable"] else
                                    "UNAVAILABLE" if market_open else "UNAVAILABLE_OUTSIDE_REGULAR_HOURS")
        snapshot["live_blocked"] = bool(
            not market_open or snapshot["account_sync_status"] != ACCOUNT_SYNC_OK
            or not snapshot["quotes_readable"] or not snapshot["positions_readable"]
        )
        return snapshot

    def _build_direct_primary_snapshot(self) -> Dict[str, Any]:
        """Read the two authoritative fields on one direct MCP session.

        Portfolio and price book are deliberately first.  Diagnostics such as
        accounts, positions, and orders are optional and can never turn a
        successful primary read into a disconnected snapshot.
        """
        snapshot: Dict[str, Any] = _InternalSnapshot({
            "connected": False, "cash": None, "available_cash": None,
            "buying_power": None, "portfolio_value": None,
            "positions": {"QQQ": None, "SMH": None}, "open_orders": [],
            "quotes": {"QQQ": None, "SMH": None}, "capabilities": {},
            "monthly_filled_buys": None, "daily_filled_buys": None,
            "monthly_open_buy_amount": None, "daily_open_buy_amount": None,
            "account_readable": False, "accounts_readable": False,
            "portfolio_readable": False, "positions_readable": False,
            "orders_readable": False, "quotes_readable": False,
            "realized_pnl_readable": False, "errors": [],
        })
        portfolio = None
        quotes = None
        try:
            portfolio = self._call_tool("get_portfolio", {}, retries=0, timeout_seconds=60)
            snapshot["portfolio_readable"] = True
            snapshot["account_readable"] = True
            snapshot["cash"] = self._first_numeric(
                portfolio, ["cash", "available_cash", "cash_available_for_withdrawal"]
            )
            snapshot["available_cash"] = snapshot["cash"]
            snapshot["buying_power"] = self._first_numeric(
                portfolio, ["buying_power", "account_buying_power", "day_trading_buying_power"]
            )
            snapshot["portfolio_value"] = self._first_numeric(
                portfolio, ["portfolio_value", "total_value", "equity", "total_equity", "market_value"]
            )
        except Exception as exc:
            snapshot["errors"].append(f"get_portfolio_failed: {exc}")

        try:
            quotes = self._call_tool("get_equity_price_book", {"symbols": ["QQQ", "SMH"]}, retries=0, timeout_seconds=60)
            by_symbol = self._extract_quote_candidates(quotes)
            for symbol in ("QQQ", "SMH"):
                quote = self._normalize_quote(symbol, by_symbol.get(symbol) or self._extract_symbol_payload(quotes, symbol))
                if quote is not None:
                    snapshot["quotes"][symbol] = quote
            snapshot["quotes_readable"] = all(snapshot["quotes"].values())
        except Exception as exc:
            snapshot["errors"].append(f"get_equity_price_book_failed: {exc}")

        snapshot["connected"] = bool(snapshot["portfolio_readable"] and snapshot["quotes_readable"])
        if not snapshot["connected"]:
            snapshot["live_blocked_by_network"] = True
            snapshot["error_type"] = "mcp_error"
        print(
            "[read_only_snapshot] direct primary calls done "
            f"connected={snapshot['connected']} cash={snapshot['cash']} "
            f"quotes_readable={snapshot['quotes_readable']}",
            file=sys.stderr, flush=True,
        )
        return snapshot

    def list_tools(self) -> List[str]:
        tools = self._discover_runtime_tools()
        if not tools:
            raise RuntimeError("tool_discovery_failed: no runtime tools discovered")
        names = []
        for t in tools:
            if not isinstance(t, dict):
                continue
            canonical = t.get("canonical_name") or t.get("name")
            if canonical:
                names.append(str(canonical))

        deduped: List[str] = []
        seen = set()
        for name in names:
            if name in seen:
                continue
            seen.add(name)
            deduped.append(name)
        return deduped

    def discover_runtime_tools(self) -> List[Dict[str, Any]]:
        return self._discover_runtime_tools()

    def get_accounts(self) -> Any:
        params: Dict[str, Any] = {}
        self._trace_params("get_accounts", "get_accounts", params)
        return self._call_tool("get_accounts", params)

    def get_portfolio(self, account_number: Optional[str] = None) -> Any:
        values = {"account_number": account_number}
        params = self._build_params_from_schema("get_portfolio", values)
        self._trace_params("get_portfolio", "get_portfolio", params)
        return self._call_tool("get_portfolio", params)

    def get_equity_positions(self, account_number: Optional[str] = None) -> Any:
        values = {"account_number": account_number}
        params = self._build_params_from_schema("get_equity_positions", values)
        self._trace_params("get_equity_positions", "get_equity_positions", params)
        return self._call_tool("get_equity_positions", params)

    def get_equity_orders(self, account_number: Optional[str] = None) -> Any:
        values = {"account_number": account_number}
        params = self._build_params_from_schema("get_equity_orders", values)
        self._trace_params("get_equity_orders", "get_equity_orders", params)
        return self._call_tool("get_equity_orders", params)

    def get_realized_pnl(self, account_number: Optional[str] = None) -> Any:
        values = {"account_number": account_number}
        params = self._build_params_from_schema("get_realized_pnl", values)
        self._trace_params("get_realized_pnl", "get_realized_pnl", params)
        return self._call_tool("get_realized_pnl", params)

    def get_equity_quotes(self, symbols: List[str]) -> Any:
        return self.get_equity_price_book(symbols)

    def get_equity_price_book(self, symbols: List[str]) -> Any:
        clean_symbols = [str(s).upper() for s in symbols if str(s).strip()]
        if not clean_symbols:
            raise RuntimeError("quote_symbols_missing")
        params = {"symbols": clean_symbols}
        self._trace_params("get_equity_price_book", "get_equity_price_book", params)
        return self._call_tool("get_equity_price_book", params)

    def call_tool_exact(self, tool_name: str, params: Dict[str, Any]) -> Any:
        return self._call_tool(tool_name, params)


    def _read_orders_with_retry(self, params, timeout_seconds, deadline=None):
        """Bounded retries for reads only; never resubmit an order."""
        for attempt in range(4):
            remaining = deadline - time.monotonic() if deadline is not None else timeout_seconds
            if remaining <= 0:
                raise RuntimeError("orders_read_deadline_exceeded")
            try:
                return self._call_tool("get_equity_orders", params, retries=0,
                                       timeout_seconds=max(1, int(min(timeout_seconds, remaining))))
            except Exception as exc:
                if attempt == 3 or not self._is_retryable_snapshot_failure({"errors": [str(exc)]}):
                    raise
                self._log_snapshot_retry(event="orders_read_retry", attempt=attempt + 1,
                                         wait_seconds=self.SNAPSHOT_RETRY_DELAYS[attempt], reason=type(exc).__name__)
                delay = self.SNAPSHOT_RETRY_DELAYS[attempt]
                if deadline is not None and time.monotonic() + delay >= deadline:
                    raise RuntimeError("orders_read_deadline_exceeded") from exc
                self.sleep_fn(delay)
                schemas = self.runtime_tools
                self._invalidate_mcp_session("orders_read_retry")
                self.runtime_tools = schemas

    def fetch_live_account_snapshot(self) -> Dict[str, Any]:
        # Keep this adapter API as the detailed, per-tool snapshot path.  The
        # production account-sync validator uses its own one-session batch
        # adapter; callers of this long-standing method still rely on all of
        # the per-field readability and account-confirmation fields below.
        if isinstance(self.transport, _DirectMCPTransport):
            return self._build_direct_primary_snapshot()
        total_attempts = len(self.SNAPSHOT_RETRY_DELAYS) + 1
        last_snapshot: Dict[str, Any] = {}
        for attempt in range(total_attempts):
            last_snapshot = self._build_snapshot_from_tool_calls()
            if (last_snapshot.get("account_sync_reason") == "orders_unreadable"
                    or last_snapshot.get("orders_readable") is False):
                return last_snapshot  # Order-specific retry budget is already exhausted.
            sync_status = last_snapshot.get("account_sync_status")
            if sync_status == "ACCOUNT_SYNC_OK" and last_snapshot.get("orders_readable") is not False:
                return last_snapshot
            retryable = self._is_retryable_snapshot_failure(last_snapshot)
            if not retryable or attempt >= total_attempts - 1:
                self._log_snapshot_retry(
                    event="account_sync_failed",
                    attempt=attempt + 1,
                    reason=f"status={sync_status}; retryable={retryable}; errors={last_snapshot.get('errors')}",
                )
                return last_snapshot
            wait_seconds = self.SNAPSHOT_RETRY_DELAYS[attempt]
            self._log_snapshot_retry(
                event="account_sync_retry_scheduled",
                attempt=attempt + 1,
                wait_seconds=wait_seconds,
                reason=str(last_snapshot.get("errors") or "account_sync_failed"),
            )
            self.sleep_fn(wait_seconds)
            self._invalidate_mcp_session("account_sync_retry")
        return last_snapshot

    # Production transport is the single direct Streamable HTTP MCP session.
    def _discover_runtime_tools(self) -> List[Dict[str, Any]]:
        if self.transport is not None:
            try:
                tools = self.transport.list_tools()
                self.runtime_tools = self._normalise_tools_list(tools)
            except Exception as exc:
                self.runtime_tools = []
                self._log_connection_error(f"injected_mcp_tool_discovery_failed: {type(exc).__name__}: {exc}")
                raise RuntimeError(f"tool_discovery_failed: {exc}") from exc
            self._log_tool_discovery(payload_type=type(tools).__name__, top_level_keys=[], parse_path="injected_transport", tool_count=len(self.runtime_tools))
            if not self.runtime_tools:
                raise RuntimeError("tool_discovery_failed: no runtime tools discovered")
            return self.runtime_tools

        prompt = (
            "只执行 robinhood-trading 的 list_tools 发现，不得调用任何 MCP 业务工具。\n"
            "返回 robinhood-trading 的完整工具名称列表，不得筛选、总结、改写或省略。\n"
            "stdout 只输出可解析的 JSON 或 Codex envelope，且结果中必须包含 tools 数组。"
        )
        raw = self._run_codex_exec_raw(prompt, timeout=120)
        stdout = str(raw.get("stdout") or "")
        candidates = self._collect_json_candidates(stdout)
        for candidate in candidates:
            found = self._find_tools_in_payload(candidate)
            if found is not None:
                self.runtime_tools = found["tools"]
                self._log_tool_discovery(payload_type=found.get("payload_type", type(candidate).__name__), top_level_keys=found.get("top_level_keys", []), parse_path=found.get("path", "unknown"), tool_count=len(self.runtime_tools))
                return self.runtime_tools
        text_tools = self._extract_tools_from_text_fallback(stdout)
        if text_tools:
            self.runtime_tools = self._normalise_tools_list(text_tools)
            self._log_tool_discovery(payload_type="str", top_level_keys=[], parse_path="text_tools_fallback", tool_count=len(self.runtime_tools))
            return self.runtime_tools
        self.runtime_tools = []
        summary = self._summarize_discovery_failure(stdout or raw)
        self._log_tool_discovery(payload_type=type(stdout or raw).__name__, top_level_keys=[], parse_path="unparsed", tool_count=0)
        raise RuntimeError(f"tool_discovery_failed: command={raw.get('command')}; raw={summary}")

    def _call_tool(self, tool_name: str, params: Dict[str, Any], retries: int = 2, timeout_seconds: int = 60) -> Dict[str, Any]:
        canonical = self._canonical_tool_name(str(tool_name))
        # A timed-out submission may already exist at the broker. Never retry it.
        if canonical == "place_equity_order":
            retries = 0
        if canonical not in self.ALLOWED_MCP_TOOLS:
            raise RuntimeError(f"tool_not_allowlisted:{canonical}")
        if self.transport is not None:
            try:
                result = self.transport.call_tool(canonical, copy.deepcopy(params if isinstance(params, dict) else {}))
            except Exception as exc:
                self._log_connection_error(f"injected_mcp_tool_call_failed:{canonical}: {type(exc).__name__}: {exc}")
                raise RuntimeError(f"Failed MCP tool call {canonical}: {exc}") from exc
            result = self._unwrap_mcp_result(result)
            return result if isinstance(result, dict) else {"result": result}

        frozen_params = copy.deepcopy(params if isinstance(params, dict) else {})
        prompt = (
            "你是严格的 MCP 机器接口调用器。\n"
            f"请调用 robinhood-trading MCP 工具: {canonical}\n"
            f"参数 JSON: {json.dumps(frozen_params, ensure_ascii=False)}\n"
            "调用后，stdout 只输出一个 JSON 对象，格式为 {\"ok\": true, \"tool\": \"<tool_name>\", \"result\": <原始返回>, \"error\": null}。"
        )
        last_error: Optional[Exception] = None
        for attempt in range(retries + 1):
            try:
                payload = self._run_codex_exec(prompt, timeout=timeout_seconds)
                if payload.get("ok") is False:
                    raise RuntimeError(str(payload.get("error") or "unknown MCP tool error"))
                result = self._unwrap_mcp_result(payload.get("result"))
                return result if isinstance(result, dict) else {"result": result}
            except Exception as exc:
                last_error = exc
                if self._is_retryable_snapshot_failure({"errors": [str(exc)]}) and attempt < retries:
                    time.sleep(1.0 * (attempt + 1))
                    continue
                break
        raise RuntimeError(f"Failed MCP tool call {canonical}: {last_error}")

    def fetch_aggregated_status(self) -> Dict[str, Any]:
        """Use the direct MCP snapshot for callers of the aggregate API."""
        return self.fetch_live_account_snapshot()


if __name__ == "__main__":
    raise SystemExit("Use run_agent.py; legacy direct entry is disabled in the unified candidate.")

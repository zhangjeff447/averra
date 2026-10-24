import copy
import hashlib
import inspect
import json
import os
import sys
import tempfile
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from qqq_mcp_adapter import MCPAdapter
from strategies.qqq_smh_agent.strategy_agent import StrategyAgent
from tools.quote_provider import QuoteProvider
from tools.account_sync import ACCOUNT_SYNC_OK, unavailable_message


def _default_production_adapter(**legacy_options):
    from config.unified import load_config
    from broker.unified import UnifiedBrokerAdapter
    config, _ = load_config()
    return UnifiedBrokerAdapter(config=config, workspace=ROOT)

TRADING_MODE = os.environ.get("TRADING_MODE", "read_only").strip().lower()
ALLOW_LIVE_ORDERS = os.environ.get("ALLOW_LIVE_ORDERS", "false").strip().lower() == "true"


def _positive_env_float(name: str, default: float) -> float:
    """Read a positive numeric safety limit from the local environment."""
    try:
        value = float(os.environ.get(name, str(default)))
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid_limit:" + name) from exc
    import math
    if not math.isfinite(value) or value < 0:
        raise ValueError("invalid_limit:" + name)
    return value

ALLOWED_SYMBOLS = ["QQQ", "SMH"]
OPTION_TOOL_NAMES = {
    "review_option_order",
    "place_option_order",
    "cancel_option_order",
    "exercise_option",
}
MONTHLY_LIMIT = _positive_env_float("MONTHLY_BUY_LIMIT", 300.0)
DAILY_LIMIT = _positive_env_float("DAILY_BUY_LIMIT", min(100.0, MONTHLY_LIMIT))
MIN_ORDER_AMOUNT = _positive_env_float("MIN_ORDER_AMOUNT", 0.0)
MAX_PREVIEW_AMOUNT = _positive_env_float("MAX_ORDER_AMOUNT", min(100.0, MONTHLY_LIMIT))
ET = ZoneInfo("America/New_York")


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _safe_float(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        import math
        number = float(value)
        return number if math.isfinite(number) else None
    except Exception:
        return None


def _parse_time(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value)
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _observed_date(day: date) -> date:
    if day.weekday() == 5:
        return day - timedelta(days=1)
    if day.weekday() == 6:
        return day + timedelta(days=1)
    return day


def _nth_weekday(year: int, month: int, weekday: int, nth: int) -> date:
    day = date(year, month, 1)
    while day.weekday() != weekday:
        day += timedelta(days=1)
    return day + timedelta(days=7 * (nth - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    day = date(year + 1, 1, 1) - timedelta(days=1) if month == 12 else date(year, month + 1, 1) - timedelta(days=1)
    while day.weekday() != weekday:
        day -= timedelta(days=1)
    return day


def _easter_date(year: int) -> date:
    a, b, c = year % 19, year // 100, year % 100
    d, e, f = b // 4, b % 4, (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    return date(year, month, ((h + l - 7 * m + 114) % 31) + 1)


def _market_holidays(year: int) -> set[date]:
    return {
        _observed_date(date(year, 1, 1)),
        _nth_weekday(year, 1, 0, 3),
        _nth_weekday(year, 2, 0, 3),
        _easter_date(year) - timedelta(days=2),
        _last_weekday(year, 5, 0),
        _observed_date(date(year, 6, 19)),
        _observed_date(date(year, 7, 4)),
        _nth_weekday(year, 9, 0, 1),
        _nth_weekday(year, 11, 3, 4),
        _observed_date(date(year, 12, 25)),
    }


def _early_close_days(year: int) -> set[date]:
    thanksgiving = _nth_weekday(year, 11, 3, 4)
    candidates = {thanksgiving + timedelta(days=1)}
    for day in (date(year, 7, 3), date(year, 12, 24)):
        if day.weekday() < 5 and day not in _market_holidays(year):
            candidates.add(day)
    return candidates


def _is_regular_market_time(now_utc: datetime) -> bool:
    now_et = now_utc.astimezone(ET)
    trading_day = now_et.date()
    if now_et.weekday() >= 5 or trading_day in _market_holidays(trading_day.year):
        return False
    close = time(13, 0) if trading_day in _early_close_days(trading_day.year) else time(16, 0)
    wall_time = now_et.time().replace(tzinfo=None)
    return time(9, 30) <= wall_time < close


def _find_numeric(payload: Any, keys: List[str]) -> Optional[float]:
    if isinstance(payload, dict):
        for key in keys:
            if key in payload:
                val = _safe_float(payload.get(key))
                if val is not None:
                    return val
        for value in payload.values():
            found = _find_numeric(value, keys)
            if found is not None:
                return found
    elif isinstance(payload, list):
        for item in payload:
            found = _find_numeric(item, keys)
            if found is not None:
                return found
    return None


def _extract_list(payload: Any, keys: List[str]) -> List[Dict[str, Any]]:
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if isinstance(payload, dict):
        for key in keys:
            val = payload.get(key)
            if isinstance(val, list):
                return [x for x in val if isinstance(x, dict)]
    return []


def _mask_last_four(value: str) -> str:
    digits = "".join([c for c in value if c.isdigit()])
    return digits[-4:] if len(digits) >= 4 else ""


@dataclass
class LiveExecutionState:
    executed_decision_ids: List[str]
    last_buy_time_by_symbol: Dict[str, str]
    known_open_order_ids: List[str]
    monthly_spent_cache: Dict[str, float]
    daily_spent_cache: Dict[str, float]
    buy_executions_by_symbol: Dict[str, Dict[str, Any]]
    trading_halted: bool
    halt_reason: str
    pending_submission: Optional[Dict[str, Any]] = None

    @classmethod
    def empty(cls) -> "LiveExecutionState":
        return cls(
            executed_decision_ids=[],
            last_buy_time_by_symbol={},
            known_open_order_ids=[],
            monthly_spent_cache={},
            daily_spent_cache={},
            buy_executions_by_symbol={},
            trading_halted=False,
            halt_reason="",
        )

    @classmethod
    def load(cls, path: Path) -> "LiveExecutionState":
        backup_path = path.with_name("live_execution_state.backup.json")

        def _read_state_file(target: Path) -> Optional["LiveExecutionState"]:
            if not target.exists():
                return None
            try:
                raw = json.loads(target.read_text(encoding="utf-8"))
                return cls(
                    executed_decision_ids=list(raw.get("executed_decision_ids", [])),
                    last_buy_time_by_symbol=dict(raw.get("last_buy_time_by_symbol", {})),
                    known_open_order_ids=list(raw.get("known_open_order_ids", [])),
                    monthly_spent_cache=dict(raw.get("monthly_spent_cache", {})),
                    daily_spent_cache=dict(raw.get("daily_spent_cache", {})),
                    buy_executions_by_symbol=dict(raw.get("buy_executions_by_symbol", {})),
                    trading_halted=bool(raw.get("trading_halted", False)),
                    halt_reason=str(raw.get("halt_reason", "") or ""),
                    pending_submission=raw.get("pending_submission"),
                )
            except Exception:
                return None

        primary = _read_state_file(path)
        if primary is not None:
            return primary

        backup = _read_state_file(backup_path)
        if backup is not None:
            return backup

        state = cls.empty()
        if path.exists() or backup_path.exists():
            state.trading_halted = True
            state.halt_reason = "execution_state_corrupt"
        return state

    def save(self, path: Path) -> None:
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
        backup_path = path.with_name("live_execution_state.backup.json")
        payload = json.dumps(
            {
                "executed_decision_ids": self.executed_decision_ids,
                "last_buy_time_by_symbol": self.last_buy_time_by_symbol,
                "known_open_order_ids": self.known_open_order_ids,
                "monthly_spent_cache": self.monthly_spent_cache,
                "daily_spent_cache": self.daily_spent_cache,
                "buy_executions_by_symbol": self.buy_executions_by_symbol,
                "trading_halted": self.trading_halted,
                "halt_reason": self.halt_reason,
                "pending_submission": self.pending_submission,
            },
            ensure_ascii=False,
            indent=2,
        )

        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix="live_state_", suffix=".tmp", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as tmp_file:
                tmp_file.write(payload)
                tmp_file.flush()
                os.fsync(tmp_file.fileno())
            Path(tmp_name).replace(path)
            if path.exists():
                backup_path.write_text(payload, encoding="utf-8")
        finally:
            if os.path.exists(tmp_name):
                os.remove(tmp_name)


class TradingRuntime:
    def __init__(self, mode=None, allow_live_orders=None, adapter=None, strategy=None,
                 quote_provider=None, state_path=None, runtime_log_path=None,
                 history_provider=None, volatility_provider=None):
        from config.unified import load_config
        from broker.unified import UnifiedBrokerAdapter
        from tools.daily_history import DailyHistory
        from tools.volatility_context import VolatilityProvider
        from tools.v1_state_store import V1StateStore
        config, _ = load_config()
        if ((mode is not None and mode != config.trading_mode)
                or (allow_live_orders is not None and allow_live_orders != config.allow_live_orders)):
            raise ValueError("runtime_constructor_mode_conflict")
        if adapter is not None and (not isinstance(adapter, UnifiedBrokerAdapter)
                or adapter.operation_mode != config.operation_mode
                or adapter.transport.mode != config.operation_mode):
            raise ValueError("injected_adapter_mode_unverified")
        self.approval_mode = config.approval_mode
        self.mode, self.operation_mode = config.trading_mode, config.operation_mode
        self.allow_live_orders = config.allow_live_orders
        self.v1_submission_approved = config.v1_submission_approved
        self.monthly_limit, self.daily_limit = config.monthly_buy_limit, config.daily_buy_limit
        self.cash_regime_v2_enabled = getattr(config, 'cash_regime_v2_enabled', False)
        self.cash_regime_policy = getattr(config, 'cash_regime_policy', None)
        self.cash_regime_policy_hash = getattr(config, 'cash_regime_policy_hash', None)
        self.min_order, self.max_order = config.min_order_amount, config.max_order_amount
        self.score_dynamic_v1_enabled, self.restricted_no_open_order_mode = True, True
        self.adapter = adapter or _default_production_adapter()
        self.strategy = strategy or StrategyAgent()
        self.history_provider = history_provider or DailyHistory()
        self.volatility_provider = volatility_provider or VolatilityProvider()
        self.quote_provider = quote_provider or QuoteProvider(adapter=self.adapter, cache_dir=ROOT / 'state/quote_cache')
        self.state_path = state_path or ROOT / "state/live_execution_state.json"
        self.runtime_log_path = runtime_log_path or ROOT / "logs/trading_runtime.jsonl"
        if not self.state_path.resolve().is_relative_to(ROOT / 'state'):
            raise ValueError("runtime_state_outside_candidate")
        self.state = LiveExecutionState.load(self.state_path)
        self.v1_store = V1StateStore(self.state_path.with_name("v1_state.sqlite"))
        self._last_mcp_error_fingerprint, self._last_mcp_error_at = None, None

    def _trading_date(self, now_utc: Optional[datetime] = None) -> str:
        current = now_utc or _now_utc()
        return current.astimezone(ET).date().isoformat()

    def _buy_already_executed_today(self, symbol: str, now_utc: Optional[datetime] = None) -> bool:
        record = self.state.buy_executions_by_symbol.get(symbol)
        return isinstance(record, dict) and record.get("trading_date") == self._trading_date(now_utc)

    @staticmethod
    def _find_order_value(payload: Any, keys: set[str]) -> Any:
        if isinstance(payload, dict):
            for key in keys:
                if key in payload and payload[key] is not None:
                    return payload[key]
            for value in payload.values():
                found = TradingRuntime._find_order_value(value, keys)
                if found is not None:
                    return found
        elif isinstance(payload, list):
            for value in payload:
                found = TradingRuntime._find_order_value(value, keys)
                if found is not None:
                    return found
        return None

    def _record_buy_execution(
        self,
        symbol: str,
        placed: Any,
        status_result: Any,
        params: Dict[str, Any],
    ) -> None:
        status = self._find_order_value(status_result, {"status", "state"})
        if str(status or "").strip().lower() not in {"filled", "partially_filled", "partially-filled"}:
            return
        order_id = self._find_order_value(status_result, {"order_id", "id"})
        if order_id is None:
            order_id = self._find_order_value(placed, {"order_id", "id"})
        filled_quantity = self._find_order_value(
            status_result, {"filled_quantity", "executed_quantity", "filled_qty", "filledQuantity"}
        )
        if filled_quantity is None:
            filled_quantity = self._find_order_value(placed, {"filled_quantity", "executed_quantity", "filled_qty", "filledQuantity"})
        filled_amount = self._find_order_value(
            status_result, {"filled_amount", "executed_notional", "filled_notional", "filledAmount"}
        )
        if filled_amount is None:
            filled_amount = self._find_order_value(placed, {"filled_amount", "executed_notional", "filled_notional", "filledAmount"})

        self.state.buy_executions_by_symbol[symbol] = {
            "symbol": symbol,
            "trading_date": self._trading_date(),
            "order_id": str(order_id) if order_id is not None else None,
            "filled_quantity": _safe_float(filled_quantity),
            "filled_amount": _safe_float(filled_amount),
        }

    def notify(self, message: str, priority: str = "normal", *, display: Optional[str] = None) -> None:
        self.runtime_log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.runtime_log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"timestamp": _now_utc().isoformat(), "priority": priority, "message": message}, ensure_ascii=False) + "\n")
        from tools.telegram_templates import compact_message, with_scores
        text = display if display is not None else compact_message(message)
        if text is None:
            return
        text = with_scores(text, getattr(self, "_notification_decision", None))
        self.adapter._notify_telegram(text, priority=priority)

    def _log_mcp_connection_errors(self, errors: List[str]) -> None:
        if not errors:
            return

        # Emit one summary + one final error per cycle, and suppress rapid duplicates.
        normalized: List[str] = []
        for err in errors:
            text = str(err).strip()
            if not text:
                continue
            if text not in normalized:
                normalized.append(text)
        if not normalized:
            return

        summary_text = normalized[0]
        final_text = normalized[-1]
        fingerprint_src = f"{summary_text}\n---\n{final_text}"
        fingerprint = hashlib.sha256(fingerprint_src.encode("utf-8")).hexdigest()[:16]
        now = _now_utc()
        if (
            self._last_mcp_error_fingerprint == fingerprint
            and self._last_mcp_error_at is not None
            and (now - self._last_mcp_error_at) < timedelta(seconds=60)
        ):
            return

        self._last_mcp_error_fingerprint = fingerprint
        self._last_mcp_error_at = now

        path = Path("logs/mcp_connection_error.log")
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(
                json.dumps(
                    {
                        "timestamp": now.isoformat(),
                        "message": "mcp_connection_error_summary",
                        "error_count": len(errors),
                        "unique_error_count": len(normalized),
                        "detail": summary_text,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
            if final_text != summary_text:
                f.write(
                    json.dumps(
                        {
                            "timestamp": now.isoformat(),
                            "message": "mcp_connection_error_final",
                            "detail": final_text,
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )

    def _extract_accounts(self, raw: Any) -> List[Dict[str, Any]]:
        return _extract_list(raw, ["accounts", "result"])

    def _extract_positions(self, raw: Any) -> List[Dict[str, Any]]:
        return _extract_list(raw, ["positions", "equity_positions", "result"])

    def _extract_orders(self, raw: Any) -> List[Dict[str, Any]]:
        return _extract_list(raw, ["orders", "equity_orders", "result"])

    def _extract_symbol(self, item: Dict[str, Any]) -> Optional[str]:
        symbol = item.get("symbol") or item.get("ticker")
        if not symbol and isinstance(item.get("instrument"), dict):
            symbol = item["instrument"].get("symbol")
        if not symbol:
            return None
        return str(symbol).upper()

    def _extract_account_last_four(self, account: Dict[str, Any]) -> str:
        for key in ("account_number", "number", "account", "account_id", "id"):
            if key in account:
                last_four = _mask_last_four(str(account.get(key)))
                if last_four:
                    return last_four
        return ""

    def _is_agentic_account(self, account: Dict[str, Any]) -> Optional[bool]:
        for key in ("is_agentic", "agentic", "agentic_account"):
            if key in account:
                return bool(account.get(key))
        kind = str(account.get("account_type") or account.get("type") or "").lower()
        if "agentic" in kind:
            return True
        if kind in ("ira", "retirement", "margin", "cash"):
            return False
        return None

    def _extract_cash(self, portfolio: Dict[str, Any]) -> Optional[float]:
        for key in ("cash", "cash_balance", "available_cash"):
            val = _safe_float(portfolio.get(key))
            if val is not None:
                return val
        return None

    def _extract_buying_power(self, portfolio: Dict[str, Any]) -> Optional[float]:
        for key in ("buying_power", "available_funds", "margin_buying_power"):
            val = _safe_float(portfolio.get(key))
            if val is not None:
                return val
        return None

    def _extract_portfolio_value(self, portfolio: Dict[str, Any]) -> Optional[float]:
        for key in ("equity", "portfolio_value", "market_value", "total_equity"):
            val = _safe_float(portfolio.get(key))
            if val is not None:
                return val
        return None

    def _order_side(self, order: Dict[str, Any]) -> str:
        return str(order.get("side") or "").lower()

    def _order_state(self, order: Dict[str, Any]) -> str:
        return str(order.get("state") or order.get("status") or "").lower()

    def _order_time(self, order: Dict[str, Any]) -> Optional[datetime]:
        for key in ("updated_at", "filled_at", "submitted_at", "created_at", "date"):
            if key in order:
                dt = _parse_time(order.get(key))
                if dt is not None:
                    return dt
        return None

    def _filled_amount(self, order: Dict[str, Any]) -> Optional[float]:
        for key in (
            "filled_notional",
            "executed_notional",
            "filled_amount",
            "filled_dollars",
            "cumulative_notional",
        ):
            val = _safe_float(order.get(key))
            if val is not None:
                return max(0.0, val)

        qty = _safe_float(order.get("filled_quantity"))
        if qty is None:
            qty = _safe_float(order.get("executed_quantity"))
        px = _safe_float(order.get("average_price"))
        if px is None:
            px = _safe_float(order.get("filled_average_price"))
        if qty is not None and px is not None:
            return max(0.0, qty * px)
        return None

    def _open_reserved_amount(self, order: Dict[str, Any]) -> Optional[float]:
        for key in ("remaining_notional", "open_notional", "reserved_amount"):
            val = _safe_float(order.get(key))
            if val is not None:
                return max(0.0, val)

        amount = _safe_float(order.get("amount"))
        filled = self._filled_amount(order)
        if amount is not None and filled is not None:
            return max(0.0, amount - filled)

        qty = _safe_float(order.get("quantity"))
        filled_qty = _safe_float(order.get("filled_quantity")) or 0.0
        price = _safe_float(order.get("limit_price"))
        if price is None:
            price = _safe_float(order.get("price"))
        if qty is not None and price is not None:
            return max(0.0, (qty - filled_qty) * price)
        return None

    def _is_open_state(self, state: str) -> bool:
        return state in ("open", "pending", "submitted", "partially_filled", "queued")

    def _is_filled_state(self, state: str) -> bool:
        return state in ("filled", "partially_filled")

    def _month_day_windows(self) -> Tuple[datetime, datetime]:
        now_et = _now_utc().astimezone(ET)
        month_start = now_et.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        day_start = now_et.replace(hour=0, minute=0, second=0, microsecond=0)
        return month_start, day_start

    def _pick_capability(self, tools: Optional[List[Dict[str, Any]]], names: List[str]) -> Optional[bool]:
        if tools is None or len(tools) == 0:
            return None
        available = {str(t.get("name")) for t in tools if isinstance(t, dict) and t.get("name")}
        return any(name in available for name in names)

    def fetch_live_account_snapshot(self) -> Dict[str, Any]:
        snapshot = self.adapter.fetch_live_account_snapshot()
        if not isinstance(snapshot, dict):
            snapshot = {
                "connected": False,
                "agentic_account_confirmed": None,
                "account_last_four": None,
                "cash": None,
                "buying_power": None,
                "portfolio_value": None,
                "positions": {"QQQ": None, "SMH": None},
                "open_orders": [],
                "monthly_filled_buys": None,
                "daily_filled_buys": None,
                "quotes": {"QQQ": None, "SMH": None},
                "capabilities": {},
                "errors": ["invalid_snapshot_type"],
                "live_blocked_by_network": True,
                "error_type": "mcp_invalid_snapshot",
            }

        defaults = {
            "connected": False,
            "agentic_account_confirmed": None,
            "account_last_four": None,
            "cash": None,
            "buying_power": None,
            "portfolio_value": None,
            "positions": {"QQQ": None, "SMH": None},
            "open_orders": [],
            "monthly_filled_buys": None,
            "daily_filled_buys": None,
            "quotes": {"QQQ": None, "SMH": None},
            "capabilities": {},
            "errors": [],
        }
        for key, value in defaults.items():
            if key not in snapshot:
                snapshot[key] = value

        if snapshot.get("connected") is False:
            snapshot["live_blocked_by_network"] = True
            # The account batch already failed; per-symbol quote retries would
            # start more Codex sessions without making the account tradeable.
            snapshot["quotes_readable"] = False

        # Decision layer depends on QuoteProvider only.
        # Always provide quote dictionaries to avoid READ_ONLY failing on quote gaps.
        quotes = snapshot.get("quotes") if isinstance(snapshot.get("quotes"), dict) else {}
        snapshot["quotes"] = quotes

        qqq_existing = quotes.get("QQQ") if isinstance(quotes.get("QQQ"), dict) else None
        smh_existing = quotes.get("SMH") if isinstance(quotes.get("SMH"), dict) else None
        existing_robinhood_pair = (
            isinstance(qqq_existing, dict)
            and isinstance(smh_existing, dict)
            and str(qqq_existing.get("source") or "") == "robinhood_mcp"
            and str(smh_existing.get("source") or "") == "robinhood_mcp"
            and _safe_float(qqq_existing.get("last")) is not None
            and _safe_float(smh_existing.get("last")) is not None
        )
        if existing_robinhood_pair and hasattr(self.quote_provider, "quote_tool_selected"):
            self.quote_provider.quote_tool_selected = {"name": "get_equity_quotes"}

        for symbol in ALLOWED_SYMBOLS:
            existing_quote = quotes.get(symbol) if isinstance(quotes.get(symbol), dict) else None
            existing_is_robinhood = (
                isinstance(existing_quote, dict)
                and str(existing_quote.get("source") or "") == "robinhood_mcp"
                and _safe_float(existing_quote.get("last")) is not None
            )
            if existing_is_robinhood:
                continue

            # A completed snapshot already attempted this quote pair. Do not
            # start another CLI session per symbol after a known empty book.
            if snapshot.get("quotes_readable") is False:
                snapshot["quotes"][symbol] = {
                    "symbol": symbol, "last": None, "bid": None, "ask": None,
                    "source": "mcp_unavailable", "timestamp": _now_utc().isoformat(),
                    "is_realtime": False, "is_stale": True,
                    "stale_reason": snapshot.get("quote_status", "mcp_unavailable"),
                }
                continue

            quote = self.quote_provider.get_quote(symbol)
            if not isinstance(quote, dict):
                quote = {
                    "symbol": symbol,
                    "last": None,
                    "bid": None,
                    "ask": None,
                    "source": "unavailable",
                    "timestamp": _now_utc().isoformat(),
                }
            if existing_is_robinhood and str(quote.get("source") or "") != "robinhood_mcp":
                continue
            snapshot["quotes"][symbol] = quote

        errs = snapshot.get("errors", [])
        if snapshot.get("live_blocked_by_network") and isinstance(errs, list) and errs:
            self._log_mcp_connection_errors([str(e) for e in errs])

        if getattr(self, "score_dynamic_v1_enabled", False):
            if (snapshot.get("orders_readable") is True
                    and snapshot.get("agentic_account_confirmed") is True
                    and self.v1_store.initialized()
                    and self.v1_store.account_matches_hash(snapshot.get("v1_account_hash"))):
                try:
                    from tools.v1_execution import reconcile_orders
                    self.v1_store.recover_uncertain()
                    reconciliation = reconcile_orders(self.v1_store, snapshot.get("v1_broker_orders"),
                        complete_history=snapshot.get("budget_history_complete") is True)
                    snapshot['intent_reconciliation'] = reconciliation
                    if reconciliation['status'] != 'RECONCILED' or self.v1_store.open_intents():
                        snapshot['live_blocked'] = True
                        snapshot['execution_block_reason'] = 'BLOCKED_BY_UNRESOLVED_INTENT' if self.v1_store.open_intents() else 'RECONCILIATION_REQUIRED'

                    budget_now = _now_utc()
                    if getattr(self, 'cash_regime_v2_enabled', False):
                        from tools.cash_regime_state import verified_policy
                        if not verified_policy(self.v1_store, self.cash_regime_policy_hash):
                            raise ValueError('cash_policy_migration_required')
                    else:
                        self.v1_store.credit_through(budget_now.astimezone(ET).strftime("%Y-%m"), self.monthly_limit)
                    broker_spend = snapshot.get("agent_filled_by_month")
                    if not self.v1_store.matches_broker_spend(broker_spend):
                        raise ValueError("v1_broker_ledger_spend_mismatch")
                    snapshot.update(self.v1_store.month_snapshot(budget_now))
                    snapshot["budget_readable"] = self.v1_store.halt_reason() is None
                except ValueError as exc:
                    snapshot["budget_readable"] = False
                    snapshot["live_blocked"] = True
                    snapshot.setdefault("errors", []).append("v1_monthly_credit_failed:" + str(exc))
            else:
                snapshot["budget_readable"] = False
                snapshot["live_blocked"] = True
                snapshot.setdefault("errors", []).append("v1_ledger_account_or_orders_unavailable")
        return snapshot

    def fetch_restricted_observed_snapshot(self, *, exclude_intent_id=None) -> Dict[str, Any]:
        """Two bounded account reads; only the isolated V1 opt-in uses this."""
        from tools.v1_observed_snapshot import observe_position_quote_snapshot

        before = self.fetch_live_account_snapshot()
        after = self.fetch_live_account_snapshot()
        active = [row for row in self.v1_store.open_intents()
                  if row["intent_id"] != exclude_intent_id]
        from tools.approval_compatibility import COMPATIBILITY_MODE
        mode = getattr(self, 'approval_mode', COMPATIBILITY_MODE)
        result = observe_position_quote_snapshot(before, after, active, approval_mode=mode)
        # Valuation for strategy presentation is independent of the order fence.
        # Actual submission still uses result, which rejects unresolved intents.
        strategy_valuation = observe_position_quote_snapshot(before, after, (), approval_mode=mode)
        if strategy_valuation.get('cash_semantics_verified') is True:
            from decimal import Decimal
            by_symbol = {symbol: sum((Decimal(row['reserved_remaining']) for row in active if row['ticker'] == symbol), Decimal('0')) for symbol in ('SMH', 'QQQ')}
            held = sum(by_symbol.values())
            strategy_valuation['local_unreflected_reservations'] = str(held)
            strategy_valuation['local_unreflected_by_symbol'] = {s: str(v) for s,v in by_symbol.items()}
            strategy_valuation['safe_spendable_cash'] = str(max(Decimal('0'), Decimal(strategy_valuation['safe_spendable_cash']) - held))
            before['strategy_cash_normalization'] = strategy_valuation
        before["cash_normalization"] = result
        before["observed_position_quote_snapshot_v1"] = result["observation"]
        return before

    def _build_holdings(self, snapshot: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
        holdings: Dict[str, Dict[str, Any]] = {}
        positions = snapshot.get("positions", {})
        for symbol in ALLOWED_SYMBOLS:
            pos = positions.get(symbol, {}) if isinstance(positions, dict) else {}
            if not isinstance(pos, dict):
                pos = {}
            qty = _safe_float(pos.get("quantity") or pos.get("qty")) or 0.0
            mv = _safe_float(pos.get("market_value") or pos.get("value"))
            if (mv is None or mv <= 0) and qty:
                q = snapshot.get("quotes", {}).get(symbol, {})
                price = _safe_float(q.get("last") or q.get("ask"))
                if price is None or price <= 0:
                    raise ValueError("holding_value_unavailable:" + symbol)
                mv = qty * price
            mv = mv or 0.0
            holdings[symbol] = {"quantity": qty, "market_value": mv}
        return holdings

    def _build_closes(self, snapshot: Dict[str, Any]) -> Dict[str, List[float]]:
        return {symbol: self.history_provider.get_closes(symbol, now=_now_utc())
                for symbol in ALLOWED_SYMBOLS}

    def prepare_cash_regime(self, snapshot, *, closes=None, exclude_intent_id=None):
        """Advance each completed date once, then expose the persistent cash gate."""
        if not getattr(self, 'cash_regime_v2_enabled', False):
            return snapshot
        from decimal import Decimal
        from strategies.qqq_smh_agent.cash_regime_v2 import observations, advance, add_cash_capacity
        from tools.cash_regime_state import load, save, context, verified_policy
        if not verified_policy(self.v1_store, self.cash_regime_policy_hash):
            raise ValueError('cash_policy_migration_required')
        cash = snapshot.get('cash_normalization') or {}
        if cash.get('cash_semantics_verified') is not True:
            raise ValueError('regime_cash_unverified')
        closes = closes or self._build_closes(snapshot)
        volatility = {name:self.history_provider.get_closes('^' + name, now=_now_utc()) for name in ('VIX','VXN')}
        dates = self.history_provider.completed_dates
        common = sorted(set.intersection(*(set(dates[s]) for s in ('SMH','QQQ','^VIX','^VXN'))))
        if any(dates[s][-1] != common[-1] for s in dates):
            raise ValueError('regime_history_unaligned')
        aligned = {}
        for symbol, values in dict(closes, **{'^'+k:v for k,v in volatility.items()}).items():
            by_date = dict(zip(dates[symbol],values))
            aligned[symbol] = [by_date[d] for d in common]
        state = load(self.v1_store)
        obs = observations(common, {s:aligned[s] for s in ('SMH','QQQ')},
                           {s:aligned['^'+s] for s in ('VIX','VXN')}, self.cash_regime_policy)
        for item in obs:
            if not state or item['date'] >= state['last_date']:
                previous_date = state.get('last_date') if state else None
                smh_clear_count = state.get('smh_clear_count', 0) if state else 0
                state = advance(state, item, self.cash_regime_policy)
                if item['date'] != previous_date:
                    clear = item['symbols']['SMH']['strong'] and not any(item['volatility_pressure'].values())
                    state['smh_clear_count'] = smh_clear_count + 1 if clear else 0
        if not state or state['last_date'] != common[-1]:
            raise ValueError('regime_observation_stale')
        if self.cash_regime_policy.get('smh_policy_version'):
            from strategies.qqq_smh_agent.smh_long_term import features
            from strategies.qqq_smh_agent.indicators import rsi_from_closes
            state['smh_feature'] = features(dates['SMH'], closes['SMH'], rsi_from_closes(closes['SMH']))
        values = {s:Decimal(cash['market_values'][s]) for s in ('SMH','QQQ')}
        total = sum(values.values()) + Decimal(cash['cash_for_weight'])
        gaps = {s:max(Decimal('0'),total * w - values[s]) for s,w in (('SMH',Decimal('.8')),('QQQ',Decimal('.2')))}
        fills = self.v1_store.db.execute('SELECT COALESCE(SUM(cents),0) FROM fills').fetchone()[0]
        state = add_cash_capacity(state,int(Decimal(cash['cash_for_weight'])*100),fills,gaps,self.cash_regime_policy)
        save(self.v1_store,state)
        prepared = copy.copy(snapshot)
        prepared['cash_regime_v2'] = context(self.v1_store,self.cash_regime_policy,exclude_intent_id)
        return prepared

    def build_strategy_decision(self, snapshot: Dict[str, Any]) -> Dict[str, Any]:
        try:
            if snapshot.get("connected") is not True or _safe_float(snapshot.get("cash")) is None:
                raise ValueError("account_data_unavailable")
            if snapshot.get("positions_readable") is False:
                raise ValueError("positions_unavailable")
            holdings = self._build_holdings(snapshot)
            closes = self._build_closes(snapshot)
        except (RuntimeError, ValueError) as exc:
            return {"data_ready": False, "decisions": {
                symbol: {"symbol": symbol, "action": "WAIT", "decision_type": "WAIT",
                         "amount": 0.0, "reason": [str(exc)]} for symbol in ALLOWED_SYMBOLS}}
        cash = _safe_float(snapshot.get("cash")) or 0.0
        decision = self.strategy.evaluate_signals(holdings, cash, closes,
            volatility_context=snapshot.get("volatility_context"))
        execution_block = snapshot.get('execution_block_reason')
        active = self.v1_store.open_intents()
        if active:
            execution_block = 'BLOCKED_BY_UNRESOLVED_INTENT'
            decision['unresolved_intent'] = self.v1_store.audit_report(active[0]['intent_id'])
        decision['execution_state'] = execution_block or 'NOT_SUBMITTED'
        decision['approval_mode'] = getattr(self, 'approval_mode', 'APPROVAL_SAFE_COMPATIBILITY_MODE')
        if snapshot.get('budget_readable') is not True:
            decision.update(data_ready=True, allocation_data_ready=False, overall='WAIT')
            for item in decision.get('decisions', {}).values():
                item.update(action='WAIT', amount=0, reason=['budget_reconciliation_failed'], execution_state=execution_block or 'BUDGET_UNVERIFIED')
            return decision
        decision['budget'] = {'monthly_limit': snapshot.get('monthly_budget_limit'),
            'spent': snapshot.get('monthly_filled_buys'),
            'remaining': float(snapshot['monthly_budget_limit']) - float(snapshot['monthly_filled_buys'])}
        from strategies.qqq_smh_agent.score_dynamic_v1 import plan, select_one_for_existing_executor
        from tools.v1_execution import V1ExecutionService
        snapshot = V1ExecutionService(self, self.v1_store, self.adapter)._normalize(snapshot)
        if snapshot.get('strategy_cash_normalization'):
            snapshot = copy.copy(snapshot)
            snapshot['cash_normalization'] = snapshot['strategy_cash_normalization']
        decision["score_received_at"] = _now_utc().isoformat()
        scores = {symbol: (item.get("metadata") or {}).get("signal_score")
                  for symbol, item in decision.get("decisions", {}).items()}
        if getattr(self, 'cash_regime_v2_enabled', False):
            try:
                snapshot = self.prepare_cash_regime(snapshot, closes=closes)
                decision['funding_mode'] = 'ACCOUNT_CASH'
                decision['cash_regime_v2'] = snapshot['cash_regime_v2']
                decision['cash_funding'] = {'available':snapshot['cash_normalization']['safe_spendable_cash'],
                                          'reserved':self.v1_store.summary()['reserved']}
            except (ValueError, RuntimeError, KeyError) as exc:
                decision.update(data_ready=True,allocation_data_ready=False,overall='WAIT')
                for item in decision['decisions'].values():
                    item.update(action='WAIT',amount=0,reason=['REGIME_DATA_UNAVAILABLE:'+str(exc)])
                return decision
        v1 = plan(dict(snapshot, monthly_new_allowance=self.monthly_limit), scores,
                  daily_limit=self.daily_limit, per_order_limit=self.max_order,
                  pending_submission=self.state.pending_submission,
                  run_id=os.environ.get("QQQ_AGENT_RUN_ID"))
        observation = snapshot.get("cash_normalization") or {}
        diagnostic = None
        if observation.get("conditional_valuation") is True:
            from scripts.validate_v1_restricted_read_only import calculate_plan_diagnostic
            diagnostic = calculate_plan_diagnostic(snapshot, observation, scores,
                {k: snapshot[k] for k in ("monthly_budget_limit", "monthly_filled_buys", "monthly_new_allowance")},
                {"monthly_new_allowance": self.monthly_limit, "daily_limit": self.daily_limit,
                 "per_order_limit": self.max_order}, state=self.state, store=self.v1_store)
            decision["plan_diagnostic"] = diagnostic
        # Apply each ETF's own daily gate before the shared one-order selector.
        # A symbol at its own daily limit must not consume the cycle's selection
        # and prevent the other eligible ETF from being considered.
        gate_reasons = {}
        eligible_symbols = set()
        gate_time = _now_utc().isoformat()
        for symbol, proposal in v1.get('etfs', {}).items():
            if proposal.get('decision') != 'BUY':
                continue
            if symbol == 'QQQ':
                reason = self.v1_store.qqq_buy_gate(proposal.get('final_score'), now=gate_time)
            elif symbol == 'SMH':
                reason = self.v1_store.smh_buy_gate(now=gate_time,
                    last_buy_time=self.state.last_buy_time_by_symbol.get(symbol))
            else:
                reason = 'SYMBOL_NOT_ALLOWED'
            if reason:
                gate_reasons[symbol] = reason
            else:
                eligible_symbols.add(symbol)
        selected = (select_one_for_existing_executor(v1, eligible_symbols=eligible_symbols)
                    if v1.get("overall") == "BUY" else None)
        for symbol, item in decision.get("decisions", {}).items():
            proposal = v1.get("etfs", {}).get(symbol, {})
            amount = proposal.get("final_permitted_amount", "0.00")
            item['execution_state'] = (execution_block or gate_reasons.get(symbol)
                                       or proposal.get('execution_state', 'NOT_SUBMITTED'))
            item["action"] = proposal.get("decision", "WAIT")
            item["decision_type"] = item["action"]
            item["amount"] = float(amount)
            item["reason"] = [proposal.get("reason", v1.get("reason", "DATA_UNAVAILABLE"))]
            if diagnostic and diagnostic.get("PLAN_CALCULATION_STATUS") == "COMPLETE":
                conditional = diagnostic["plan"]["etfs"][symbol]
                item["reason"] = [conditional.get("calculation_reason", item["reason"][0])]
                item["conditional_preapproval_amount"] = conditional["conditional_preapproval_amount"]
                item["execution_eligibility"] = diagnostic["EXECUTION_ELIGIBILITY"]
            item.setdefault("metadata", {})["score_dynamic_v1"] = proposal
            item["metadata"]["strategy_version"] = v1["policy_version"]
            if proposal.get('smh_research_policy'):
                item['metadata']['signal_score'] = float(proposal['final_score'])
                item['score'] = float(proposal['final_score'])
                item['buy_score'] = float(proposal['final_score'])
            item["selected_for_execution"] = bool(selected and selected["symbol"] == symbol)
        decision["overall"] = v1["overall"]
        decision["allocation_data_ready"] = "total_assets" in v1 or bool(diagnostic and diagnostic.get("PLAN_CALCULATION_STATUS") == "COMPLETE")
        decision["data_ready"] = True
        decision["score_dynamic_v1"] = v1
        return decision
    def _decision_id(self, decision: Dict[str, Any]) -> str:
        now_et = _now_utc().astimezone(ET).date().isoformat()
        symbol = str(decision.get("symbol") or "").upper()
        decision_type = str(decision.get("decision_type") or decision.get("action") or "")
        amount = float(decision.get("amount") or 0.0)
        metadata = decision.get("metadata") if isinstance(decision.get("metadata"), dict) else {}
        strategy_version = str(metadata.get("strategy_version") or os.environ.get("STRATEGY_VERSION", "v1"))
        payload = f"{now_et}|{symbol}|{decision_type}|{amount:.2f}|{strategy_version}"
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]

    def _halt(self, reason: str) -> None:
        self.state.trading_halted = True
        self.state.halt_reason = reason
        self.state.save(self.state_path)
        self.notify(f"系统进入 halted 状态: {reason}", priority="high")

    def _risk_check(self, decision: Dict[str, Any], snapshot: Dict[str, Any]) -> Tuple[bool, str]:
        if snapshot.get("connected") is not True:
            return False, "account_disconnected"
        if not bool(snapshot.get("agentic_account_confirmed", False)):
            return False, "non_agentic_account"
        if snapshot.get("orders_readable") is False:
            return False, "orders_unreadable"
        if snapshot.get("budget_readable") is False:
            return False, "budget_reconciliation_failed"

        action = str(decision.get("action") or "").upper()
        symbol = str(decision.get("symbol") or "").upper()
        decision_type = str(decision.get("decision_type") or "").lower()
        metadata = decision.get("metadata") if isinstance(decision.get("metadata"), dict) else {}
        amount = _safe_float(decision.get("amount"))
        side_meta = str(metadata.get("side") or "").strip().lower()

        if side_meta in {"sell_short", "short"}:
            return False, "short_selling_prohibited"
        if bool(metadata.get("margin", False)) or bool(metadata.get("use_margin", False)):
            return False, "margin_borrowing_prohibited"

        if symbol not in ALLOWED_SYMBOLS:
            return False, "symbol_not_allowed"
        if "option" in decision_type or "spread" in decision_type:
            return False, "options_prohibited"
        if str(metadata.get("instrument_type") or "").strip().lower() == "option":
            return False, "options_prohibited"

        decision_id = self._decision_id(decision)
        if decision_id in self.state.executed_decision_ids:
            return False, "duplicate_decision_id"

        if action != "BUY":
            return False, "buy_only_policy"

        if amount is None or round(amount, 2) <= 0 or amount < self.min_order:
            return False, "amount_below_min"

        cash = _safe_float(snapshot.get("cash"))
        if cash is None:
            return False, "cash_unavailable"

        monthly_filled = _safe_float(snapshot.get("monthly_filled_buys"))
        daily_filled = _safe_float(snapshot.get("daily_filled_buys"))
        monthly_open = _safe_float(snapshot.get("monthly_open_buy_amount"))
        daily_open = _safe_float(snapshot.get("daily_open_buy_amount"))
        if None in (monthly_filled, daily_filled, monthly_open, daily_open):
            return False, "order_history_or_open_orders_unavailable"

        if not getattr(self, 'cash_regime_v2_enabled', False) and monthly_filled + monthly_open + amount > float(snapshot.get("monthly_budget_limit", self.monthly_limit)):
            return False, "monthly_limit_exceeded"
        if daily_filled + daily_open + amount > self.daily_limit:
            return False, "daily_limit_exceeded"
        if amount > cash:
            return False, "insufficient_cash"

        for order in snapshot.get("open_orders", []):
            if self._extract_symbol(order) == symbol and self._order_side(order) == "buy":
                return False, "symbol_has_open_buy_order"

        if symbol == 'QQQ':
            score = (metadata.get('score_dynamic_v1') or {}).get('final_score', metadata.get('signal_score'))
            reason = self.v1_store.qqq_buy_gate(score, now=_now_utc().isoformat())
            if reason:
                return False, reason
        if symbol == 'SMH':
            reason = self.v1_store.smh_buy_gate(now=_now_utc().isoformat(),
                last_buy_time=self.state.last_buy_time_by_symbol.get(symbol))
            if reason:
                return False, reason

        return True, "ok"

    def _build_order_params(
        self,
        decision: Dict[str, Any],
        quote: Dict[str, Any],
        snapshot: Dict[str, Any],
        capabilities: Dict[str, Any],
    ) -> Tuple[Optional[Dict[str, Any]], str]:
        symbol = str(decision.get("symbol") or "").upper()
        action = str(decision.get("action") or "").upper()
        amount = float(decision.get("amount") or 0.0)
        _ = quote
        _ = capabilities

        if action == "BUY":
            cash = _safe_float(snapshot.get("cash"))
            monthly_filled = _safe_float(snapshot.get("monthly_filled_buys"))
            daily_filled = _safe_float(snapshot.get("daily_filled_buys"))
            monthly_open = _safe_float(snapshot.get("monthly_open_buy_amount"))
            daily_open = _safe_float(snapshot.get("daily_open_buy_amount"))
            if None in (cash, monthly_filled, daily_filled, monthly_open, daily_open):
                return None, "insufficient_budget_context"

            monthly_remaining = max(0.0, float(snapshot.get("monthly_budget_limit", self.monthly_limit)) - float(monthly_filled) - float(monthly_open))
            if getattr(self, 'cash_regime_v2_enabled', False):
                monthly_remaining = cash
            daily_remaining = max(0.0, self.daily_limit - float(daily_filled) - float(daily_open))
            capped_amount = max(
                0.0,
                min(
                    float(amount),
                    float(cash),
                    monthly_remaining,
                    daily_remaining,
                    self.max_order,
                ),
            )
            if round(capped_amount, 2) <= 0 or capped_amount < self.min_order:
                return None, "amount_below_min"
        else:
            return None, "buy_only_policy"

        params = {
            "symbol": symbol,
            "side": action.lower(),
            "type": "market",
            "dollar_amount": f"{capped_amount:.2f}",
            "market_hours": "regular_hours",
            "time_in_force": "gfd",
        }
        account_number = getattr(snapshot, "agentic_account_number_internal", None)
        if isinstance(account_number, str) and account_number.strip():
            params["account_number"] = account_number.strip()
        return params, "ok"

    def _extract_preview_metrics(self, preview: Any) -> Dict[str, Any]:
        data = preview.get("data") if isinstance(preview, dict) else None
        quote = data.get("quote_data") if isinstance(data, dict) else None
        ask = _safe_float(quote.get("ask_price")) if isinstance(quote, dict) else None
        last = _safe_float(quote.get("last_trade_price")) if isinstance(quote, dict) else None
        estimated_price = (ask if ask is not None and ask > 0 else last if last is not None and last > 0
                           else _find_numeric(preview, ["estimated_price", "price", "limit_price", "estimated_fill_price"]))
        estimated_cost = _find_numeric(preview, ["estimated_cost", "estimated_amount", "cost", "total", "amount"])
        if estimated_cost is None and isinstance(data, dict):
            estimated_cost = _safe_float(data.get("dollar_amount"))
        source = preview if isinstance(preview, dict) else {}
        preview_ts = _parse_time(
            source.get("timestamp") or source.get("preview_timestamp")
            or source.get("updated_at") or source.get("created_at")
            or (quote.get("venue_ask_time") if isinstance(quote, dict) else None)
            or (quote.get("venue_last_trade_time") if isinstance(quote, dict) else None)
        )
        if preview_ts is None:
            preview_ts = _now_utc()
        return {
            "estimated_price": estimated_price,
            "estimated_cost": estimated_cost,
            "preview_timestamp": preview_ts,
        }

    def _is_quote_fresh_for_live(self, quote: Dict[str, Any]) -> bool:
        source = str(quote.get("source") or "")
        is_realtime = bool(quote.get("is_realtime", False))
        ts = _parse_time(quote.get("timestamp"))
        has_price = (_safe_float(quote.get("ask")) is not None) or (_safe_float(quote.get("last")) is not None)
        if source != "robinhood_mcp" or not is_realtime or ts is None or not has_price:
            return False
        age = _now_utc() - ts
        return timedelta(0) <= age <= timedelta(seconds=60) and not quote.get("is_stale", False)

    def _is_preview_fresh_for_live(self, preview_meta: Dict[str, Any]) -> bool:
        ts = preview_meta.get("preview_timestamp")
        if not isinstance(ts, datetime):
            return False
        age = _now_utc() - ts
        return timedelta(0) <= age <= timedelta(seconds=60)

    def execute_decision(self, *args, **kwargs):
        raise RuntimeError("LEGACY_EXECUTION_DISABLED_USE_V1_INTENTS")

    def run_read_only(self) -> Dict[str, Any]:
        self._notification_decision = None
        self.notify(f"系统启动: mode={self.mode}")
        snapshot = (self.fetch_restricted_observed_snapshot()
                    if getattr(self, "restricted_no_open_order_mode", False)
                    else self.fetch_live_account_snapshot())
        try:
            volatility_context = self.volatility_provider.fetch(now=_now_utc())
        except Exception as exc:
            from tools.volatility_context import unavailable_context
            volatility_context = unavailable_context("fetch_failed:" + type(exc).__name__)
        snapshot["volatility_context"] = volatility_context
        account_sync = snapshot.get("account_sync") if isinstance(snapshot.get("account_sync"), dict) else {}
        if account_sync.get("status") != ACCOUNT_SYNC_OK:
            self.notify(unavailable_message(account_sync))
        else:
            self.notify(
                "ACCOUNT SYNC OK: "
                f"eligible_buying_power={account_sync.get('eligible_buying_power')}, "
                f"cash={account_sync.get('cash')}, "
                f"portfolio_value={account_sync.get('portfolio_value')}"
            )
        decision = self.build_strategy_decision(snapshot)
        self._notification_decision = decision
        from tools.telegram_templates import format_decision
        self.notify(f"Decision: {decision}", display=format_decision(decision))
        return {
            "mode": "read_only",
            "snapshot": snapshot,
            "decision": decision,
            "live_blocked_by_network": snapshot.get("live_blocked_by_network", False),
        }

    def run_preview(self, *args, **kwargs):
        raise RuntimeError("LEGACY_PREVIEW_DISABLED_USE_READ_ONLY_VALIDATION")

    def _v1_execution_guard(self, snapshot, decision=None):
        from tools.v1_execution import approval_block_reason
        if self.mode != "live" or not self.allow_live_orders or not self.v1_submission_approved:
            return "V1_SUBMISSION_NOT_APPROVED"
        identity = getattr(snapshot, "agentic_account_number_internal", None)
        if (not identity or hashlib.sha256(identity.encode()).hexdigest() != snapshot.get("v1_account_hash")
                or not self.v1_store.account_matches_hash(snapshot.get("v1_account_hash"))):
            return "ACCOUNT_BINDING_UNVERIFIED"
        if self.state.trading_halted or self.state.pending_submission:
            return self.state.halt_reason or "EXECUTION_UNKNOWN"
        if not _is_regular_market_time(_now_utc()):
            return "WAIT_FOR_MARKET_OPEN"
        if decision and decision['symbol'] != 'QQQ' and self._buy_already_executed_today(decision["symbol"], _now_utc()):
            return "ALREADY_BOUGHT_TODAY"
        if (getattr(self.adapter, "last_telegram_result", None) or {}).get("delivered") is not True:
            return "DECISION_NOTIFICATION_UNCONFIRMED"
        from tools.approval_compatibility import COMPATIBILITY_MODE
        return approval_block_reason(snapshot, mode=getattr(self, 'approval_mode', COMPATIBILITY_MODE))

    def _v1_preview_guard(self, preview, params, snapshot, quote):
        data = preview.get("data") if isinstance(preview, dict) else None
        if not isinstance(data, dict) or preview.get("isError") or preview.get("ok") is False:
            return "preview_rejected"
        if any(str(data.get(k, "")).lower() != str(params[k]).lower() for k in ("symbol", "side", "type")):
            return "preview_does_not_match_order"
        if _safe_float(data.get("dollar_amount")) != _safe_float(params["dollar_amount"]):
            return "preview_does_not_match_order"
        if data.get("order_checks") != {}:
            return "broker_pretrade_alert"
        pq = data.get("quote_data")
        if not isinstance(pq, dict) or pq.get("state") != "active" or pq.get("has_traded") is not True:
            return "preview_quote_unavailable"
        meta = self._extract_preview_metrics(preview)
        cost = _safe_float(meta.get("estimated_cost"))
        if cost is None or cost <= 0 or cost > float(params["dollar_amount"]):
            return "preview_cost_unverified_or_exceeds_reservation"
        if not self._is_quote_fresh_for_live(quote) or not self._is_preview_fresh_for_live(meta):
            return "preview_or_quote_stale"
        if _safe_float(quote.get("ask")) is None and _safe_float(meta.get("estimated_price")) is None:
            return "no_ask_without_valid_preview"
        caps = snapshot.get("capabilities") or {}
        if caps.get("order_preview") is not True or caps.get("place_order") is not True:
            return "order_capability_unverified"
        return None

    def _v1_notify_preview(self, preview, params):
        data = preview["data"]
        quote = data["quote_data"]
        display = f"订单预览：{params['symbol']} BUY market ${params['dollar_amount']}"
        for name in ("bid_price", "ask_price", "last_trade_price", "updated_at"):
            if quote.get(name) is not None:
                display += f"\n{name}: {quote[name]}"
        disclosure = data.get("market_data_disclosure")
        if isinstance(disclosure, str) and disclosure:
            display += "\n" + disclosure
        self.notify("V1 order preview", display=display)
        return (getattr(self.adapter, "last_telegram_result", None) or {}).get("delivered") is True

    def score_fresh_snapshot(self, snapshot):
        """Re-evaluate the same scoring rules before refreshing a planned order."""
        volatility = self.volatility_provider.fetch(now=_now_utc())
        decision = self.strategy.evaluate_signals(self._build_holdings(snapshot), float(snapshot['cash']),
            self._build_closes(snapshot), volatility_context=volatility)
        scores = {symbol: (item.get('metadata') or {}).get('signal_score')
                  for symbol, item in decision.get('decisions', {}).items()}
        if set(scores) != {'QQQ', 'SMH'} or any(value is None for value in scores.values()):
            raise ValueError('fresh_score_unverified')
        return scores

    def run_live(self, base=None):
        from config.unified import LIVE_TRADING
        if self.operation_mode != LIVE_TRADING or not self.allow_live_orders or not self.v1_submission_approved:
            return {"live": {"status": "blocked", "reason": "READ_ONLY_VALIDATION"}}
        from tools.v1_execution import V1ExecutionService
        from tools.telegram_templates import format_v1_execution
        base = base if base is not None else self.run_read_only()
        result = V1ExecutionService(self, self.v1_store, self.adapter, submission_authorized=True).run(base)
        self.notify("V1 execution: " + str(result.get("status")), display=format_v1_execution(result))
        return {"mode": "live", "snapshot": base["snapshot"], "decision": base.get("decision"), "live": result}

    def run(self) -> Dict[str, Any]:
        if self.mode == "read_only":
            return self.run_read_only()
        if self.mode == "preview":
            return self.run_preview()
        if self.mode == "live":
            return self.run_live()
        return {"status": "error", "reason": f"unknown_mode:{self.mode}"}


def main() -> None:
    runtime = TradingRuntime()
    result = runtime.run()
    print(json.dumps(result, ensure_ascii=False, default=str))


if __name__ == "__main__":
    raise SystemExit("Use run_agent.py; legacy direct entry is disabled in the unified candidate.")

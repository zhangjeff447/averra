import hashlib
import json
from dataclasses import dataclass
from typing import Dict, Optional
from zoneinfo import ZoneInfo

from tools.ops_utils import ProductionConfigError


VALID_MODES = {"read_only", "preview", "live"}


@dataclass
class ProductionConfig:
    trading_mode: str
    allow_live_orders: bool
    monthly_buy_limit: float
    daily_buy_limit: float
    min_order_amount: float
    max_order_amount: float
    timezone: str
    telegram_enabled: bool
    log_level: str
    live_blocked_by_network: bool
    env_source: Optional[str]
    raw_env: Dict[str, str]
    score_dynamic_v1_enabled: bool = False
    v1_submission_approved: bool = False
    restricted_no_open_order_mode: bool = False
    effective_policy_hash: str = ""


V1_POLICY_KEYS = (
    "TRADING_MODE", "ALLOW_LIVE_ORDERS", "QQQ_TARGET_WEIGHT", "SMH_TARGET_WEIGHT",
    "MONTHLY_BUY_LIMIT", "DAILY_BUY_LIMIT", "MIN_ORDER_AMOUNT", "MAX_ORDER_AMOUNT",
    "TIMEZONE", "SCORE_DYNAMIC_ALLOCATION_V1_ENABLED", "V1_SUBMISSION_APPROVED",
    "RESTRICTED_NO_OPEN_ORDER_MODE",
)


def _to_bool(value: str, key: str) -> bool:
    v = value.strip().lower()
    if v in ("1", "true", "yes", "on"):
        return True
    if v in ("0", "false", "no", "off"):
        return False
    raise ProductionConfigError(f"invalid_bool:{key}")


def _to_float(value: str, key: str) -> float:
    try:
        import math
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("nonfinite")
        return number
    except Exception as exc:
        raise ProductionConfigError(f"invalid_number:{key}") from exc


def _validate_values(env):
    """Internal value validation; no environment or file loading."""
    source = 'config/unified_policy.json'
    v1_enabled = _to_bool(env.get('SCORE_DYNAMIC_ALLOCATION_V1_ENABLED', 'false'), 'SCORE_DYNAMIC_ALLOCATION_V1_ENABLED')
    v1_approved = _to_bool(env.get('V1_SUBMISSION_APPROVED', 'false'), 'V1_SUBMISSION_APPROVED')
    restricted = _to_bool(env.get('RESTRICTED_NO_OPEN_ORDER_MODE', 'false'), 'RESTRICTED_NO_OPEN_ORDER_MODE')
    policy_values = {key: env.get(key) for key in V1_POLICY_KEYS}
    policy_values["SCORE_DYNAMIC_ALLOCATION_V1_ENABLED"] = v1_enabled
    policy_values["RESTRICTED_NO_OPEN_ORDER_MODE"] = restricted
    policy_hash = hashlib.sha256(json.dumps(policy_values, sort_keys=True).encode()).hexdigest()

    required = [
        "TRADING_MODE",
        "ALLOW_LIVE_ORDERS",
        "MONTHLY_BUY_LIMIT",
        "DAILY_BUY_LIMIT",
        "MIN_ORDER_AMOUNT",
        "MAX_ORDER_AMOUNT",
        "TIMEZONE",
        "TELEGRAM_ENABLED",
        "LOG_LEVEL",
    ]

    missing = [k for k in required if k not in env or str(env.get(k)).strip() == ""]
    if missing:
        raise ProductionConfigError(f"missing_required:{','.join(missing)}")

    mode = env["TRADING_MODE"].strip().lower()
    if mode not in VALID_MODES:
        raise ProductionConfigError("invalid_trading_mode")

    allow_live_orders = _to_bool(env["ALLOW_LIVE_ORDERS"], "ALLOW_LIVE_ORDERS")
    if mode == "live" and not allow_live_orders:
        raise ProductionConfigError("live_mode_requires_allow_live_orders_true")

    monthly = _to_float(env["MONTHLY_BUY_LIMIT"], "MONTHLY_BUY_LIMIT")
    daily = _to_float(env["DAILY_BUY_LIMIT"], "DAILY_BUY_LIMIT")
    min_amount = _to_float(env["MIN_ORDER_AMOUNT"], "MIN_ORDER_AMOUNT")
    max_amount = _to_float(env["MAX_ORDER_AMOUNT"], "MAX_ORDER_AMOUNT")

    if monthly < 0 or daily < 0 or min_amount < 0 or max_amount < 0:
        raise ProductionConfigError("negative_limits_not_allowed")

    if max_amount < min_amount:
        raise ProductionConfigError("max_order_amount_below_min")
    if v1_enabled:
        if min_amount != 0:
            raise ProductionConfigError("v1_strategy_minimum_must_be_zero")
        qqq = _to_float(env.get("QQQ_TARGET_WEIGHT", ""), "QQQ_TARGET_WEIGHT")
        smh = _to_float(env.get("SMH_TARGET_WEIGHT", ""), "SMH_TARGET_WEIGHT")
        if abs(qqq - 0.20) > 1e-9 or abs(smh - 0.80) > 1e-9:
            raise ProductionConfigError("v1_target_weights_conflict")

    timezone = env["TIMEZONE"].strip()
    try:
        ZoneInfo(timezone)
    except Exception as exc:
        raise ProductionConfigError("invalid_timezone") from exc

    telegram_enabled = _to_bool(env["TELEGRAM_ENABLED"], "TELEGRAM_ENABLED")
    log_level = env["LOG_LEVEL"].strip().upper()
    # Kept as a backwards-compatible field for callers that deserialize the
    # config, but it is not a source of runtime health.  Network blocking is
    # determined from the current MCP read in run_live_cycle.py.
    live_blocked = _to_bool(env.get("LIVE_BLOCKED_BY_NETWORK", "false"), "LIVE_BLOCKED_BY_NETWORK")

    return ProductionConfig(
        trading_mode=mode,
        allow_live_orders=allow_live_orders,
        monthly_buy_limit=monthly,
        daily_buy_limit=daily,
        min_order_amount=min_amount,
        max_order_amount=max_amount,
        timezone=timezone,
        telegram_enabled=telegram_enabled,
        log_level=log_level,
        live_blocked_by_network=live_blocked,
        env_source=source,
        raw_env=env,
        score_dynamic_v1_enabled=v1_enabled,
        v1_submission_approved=v1_approved,
        restricted_no_open_order_mode=restricted,
        effective_policy_hash=policy_hash,
    )

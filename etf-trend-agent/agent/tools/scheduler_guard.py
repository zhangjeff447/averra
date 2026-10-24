"""Purely local schedule gate for the production live cycle.

This module deliberately has no imports from the broker, MCP, Codex, model,
strategy, or notification layers.  It is safe to evaluate on every scheduler
wakeup without consuming network or AI resources.
"""

import os
from dataclasses import dataclass
from datetime import datetime, time
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from tools.ops_utils import (
    current_slot,
    is_regular_market_hours,
    is_trading_day,
    load_env_file,
)


SLOTS = (time(10, 5), time(12, 30), time(15, 30))
WINDOW_MINUTES = 5
DEFAULT_TIMEZONE = "America/New_York"


@dataclass(frozen=True)
class ScheduleDecision:
    authorized: bool
    local_now: datetime
    timezone_name: str
    slot: Optional[str]
    reason: str


def evaluate_schedule(now_utc: datetime, *, env_file: Optional[Path] = None) -> ScheduleDecision:
    """Return authorization using only local time/configuration data."""
    env = load_env_file(env_file) if env_file is not None else {}
    timezone_name = str(os.environ.get("TIMEZONE") or env.get("TIMEZONE") or DEFAULT_TIMEZONE)
    try:
        timezone = ZoneInfo(timezone_name)
    except Exception:
        timezone_name = DEFAULT_TIMEZONE
        timezone = ZoneInfo(timezone_name)

    local_now = now_utc.astimezone(timezone)
    if not is_trading_day(local_now):
        return ScheduleDecision(False, local_now, timezone_name, None, "not_trading_day")
    if not is_regular_market_hours(local_now):
        return ScheduleDecision(False, local_now, timezone_name, None, "outside_regular_market_hours")

    slot = current_slot(local_now, list(SLOTS), window_minutes=WINDOW_MINUTES)
    if slot is None:
        return ScheduleDecision(False, local_now, timezone_name, None, "outside_scheduled_window")
    return ScheduleDecision(True, local_now, timezone_name, slot, "authorized_scheduled_window")

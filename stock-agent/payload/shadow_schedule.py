"""Local market-session policy for the ongoing, close-only stock monitor."""
from datetime import date, timedelta

from market_regime import market_session

START_DATE = date(2026, 9, 29)


def authorized_day(day):
    try:
        value = date.fromisoformat(day)
    except (TypeError, ValueError):
        return False
    return value >= START_DATE and value.isoformat() == day and market_session(value)


def shadow_day_number(day):
    if not authorized_day(day):
        raise ValueError("UNAUTHORIZED_SHADOW_DATE")
    value, end, number = START_DATE, date.fromisoformat(day), 1
    while value <= end:
        number += int(market_session(value))
        value += timedelta(days=1)
    return number

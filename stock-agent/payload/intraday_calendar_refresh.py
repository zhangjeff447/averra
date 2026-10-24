"""Bounded official-calendar recheck; a failed fetch never manufactures verification."""
from __future__ import annotations

import re
import sqlite3
import urllib.request
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
from zoneinfo import ZoneInfo

from intraday_calendar import record_refresh_failure
from market_regime import market_session

ET = ZoneInfo("America/New_York")


class _Text(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)


def official_text(url):
    request = urllib.request.Request(url, headers={"User-Agent": "AverraStockAgent/1.0 official-calendar-check"})
    with urllib.request.urlopen(request, timeout=12) as response:
        raw = response.read(2_000_001)
    if len(raw) > 2_000_000:
        raise ValueError("CALENDAR_SOURCE_TOO_LARGE")
    parser = _Text()
    parser.feed(raw.decode("utf-8", errors="replace"))
    return re.sub(r"\s+", " ", " ".join(parser.parts)).lower()


def _time_forms(clock):
    hour, minute = map(int, clock.split(":"))
    twelve = hour % 12 or 12
    meridian = "am" if hour < 12 else "pm"
    return (clock.lower(), f"{twelve}:{minute:02d} {meridian}",
            f"{twelve}:{minute:02d} {meridian[0]}.m.", f"{twelve}:{minute:02d}{meridian}")


def _has_explicit_fact(text, day, clock, name):
    date_forms = (f"{day.strftime('%B').lower()} {day.day}", f"{day.day} {day.strftime('%B').lower()}")
    terms = name.lower().split()
    for form in date_forms:
        for match in re.finditer(re.escape(form), text):
            context = text[max(0, match.start()-100):match.end()+600]
            if any(form in context for form in _time_forms(clock)) and all(term in context for term in terms[:2]):
                return True
    return False


def refresh_due(db: sqlite3.Connection, now=None, fetch=official_text):
    """Recheck weekly and on the prior trading day, at most once per source/day."""
    now = now or datetime.now(timezone.utc)
    today = now.astimezone(ET).date()
    tomorrow = today + timedelta(days=1)
    while not market_session(tomorrow):
        tomorrow += timedelta(days=1)
    rows = db.execute("SELECT event_id,event_name,event_date,scheduled_time_et,source,source_url,verified_at "
                      "FROM official_calendar_events WHERE calendar_status='VERIFIED' AND event_date>=?",
                      (today.isoformat(),)).fetchall()
    report = {"attempted_sources": 0, "verified_events": 0, "unverified_events": 0, "failures": {}}
    by_url = {}
    for row in rows:
        verified = datetime.fromisoformat(row[6])
        weekly = now - verified >= timedelta(days=7)
        prior_day = row[2] == tomorrow.isoformat() and verified.astimezone(ET).date() < today
        if weekly or prior_day:
            by_url.setdefault(row[5], []).append(row)
    sessions_by_url = {}
    for session_day, kind, close, url, verified in db.execute(
            "SELECT trading_date,session_type,close_time_et,source_url,verified_at FROM official_market_sessions WHERE trading_date>=?",
            (today.isoformat(),)):
        if (now - datetime.fromisoformat(verified) >= timedelta(days=7) or
                session_day == tomorrow.isoformat() and datetime.fromisoformat(verified).astimezone(ET).date() < today):
            by_url.setdefault(url, [])
            sessions_by_url.setdefault(url, []).append((session_day, kind, close))
    for authority, url, verified in db.execute(
            "SELECT authority,source,verified_at FROM intraday_calendar_coverage"):
        if now - datetime.fromisoformat(verified) >= timedelta(days=7):
            by_url.setdefault(url, [])
    for url, events in by_url.items():
        source = events[0][4] if events else db.execute(
            "SELECT authority FROM intraday_calendar_coverage WHERE source=?", (url,)).fetchone()[0]
        attempted = db.execute("SELECT last_attempt_at FROM official_calendar_fetches WHERE source_url=?", (url,)).fetchone()
        if attempted and attempted[0] and datetime.fromisoformat(attempted[0]).astimezone(ET).date() == today:
            continue
        report["attempted_sources"] += 1
        try:
            content = fetch(url)
        except Exception as exc:
            record_refresh_failure(db, source, type(exc).__name__, now)
            with db:
                db.execute("INSERT OR REPLACE INTO official_calendar_fetches VALUES(?,?,'FAILED')",
                           (url, now.isoformat()))
            report["failures"][source] = type(exc).__name__
            continue
        with db:
            db.execute("INSERT OR REPLACE INTO official_calendar_fetches VALUES(?,?,'FETCHED')",
                       (url, now.isoformat()))
            db.execute("UPDATE intraday_calendar_coverage SET last_attempt_at=?,last_error=NULL WHERE authority=?",
                       (now.isoformat(), source))
            for session_day, kind, close in sessions_by_url.get(url, []):
                day = date.fromisoformat(session_day)
                date_text = f"{day.strftime('%B').lower()} {day.day}, {day.year}"
                found = False
                for match in re.finditer(re.escape(date_text), content):
                    context = content[max(0, match.start()-180):match.end()+120]
                    if kind == "EARLY_CLOSE":
                        found |= "close early" in context and "1:00 p.m." in context
                    else:
                        found |= "thanksgiving" in context or "christmas" in context
                if found:
                    db.execute("UPDATE official_market_sessions SET verified_at=? WHERE trading_date=?",
                               (now.isoformat(), session_day))
                else:
                    report["unverified_events"] += 1
            if not events:
                continue  # fetch alone cannot assert unrelated authority event facts
            for event_id, name, day_str, clock, _source, _url, _verified in events:
                if _has_explicit_fact(content, date.fromisoformat(day_str), clock, name):
                    db.execute("UPDATE official_calendar_events SET retrieved_at=?,verified_at=? WHERE event_id=?",
                               (now.isoformat(), now.isoformat(), event_id))
                    report["verified_events"] += 1
                else:
                    old = db.execute("SELECT revision FROM official_calendar_events WHERE event_id=?", (event_id,)).fetchone()
                    rev = old[0] + 1
                    db.execute("UPDATE official_calendar_events SET calendar_status='UNVERIFIED',revision=? WHERE event_id=?", (rev, event_id))
                    db.execute("INSERT INTO official_calendar_revisions VALUES(?,?,?,?)",
                               (event_id, rev, now.isoformat(), '{"reason":"OFFICIAL_FACT_NOT_FOUND","calendar_status":"UNVERIFIED"}'))
                    report["unverified_events"] += 1
    return report

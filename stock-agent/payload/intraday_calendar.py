"""Official, point-in-time macro calendar and intraday check schedule."""
from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, time, timedelta, timezone
from urllib.parse import urlparse

from market_regime import market_session, previous_session

HOSTS = {"BLS": "bls.gov", "BEA": "bea.gov", "FED": "federalreserve.gov", "NYSE": "nyse.com"}
BLS = {"CPI", "NFP", "EMPLOYMENT_SITUATION", "PPI", "JOLTS"}
BEA = {"PCE", "GDP", "PERSONAL_INCOME_OUTLAYS"}
FED = {"FOMC_DECISION", "FOMC_STATEMENT", "FED_CHAIR_PRESS_CONFERENCE"}


def init_calendar(db: sqlite3.Connection) -> None:
    db.executescript("""CREATE TABLE IF NOT EXISTS official_calendar_events (
      event_id TEXT PRIMARY KEY, event_name TEXT NOT NULL, event_type TEXT NOT NULL,
      event_date TEXT NOT NULL, scheduled_time_et TEXT, source TEXT NOT NULL,
      source_url TEXT NOT NULL, retrieved_at TEXT NOT NULL, verified_at TEXT,
      impact_level TEXT NOT NULL, calendar_status TEXT NOT NULL, revision INTEGER NOT NULL);
    CREATE TABLE IF NOT EXISTS official_calendar_revisions (
      event_id TEXT NOT NULL, revision INTEGER NOT NULL, recorded_at TEXT NOT NULL,
      record_json TEXT NOT NULL, PRIMARY KEY(event_id,revision));
    CREATE TABLE IF NOT EXISTS official_market_sessions (
      trading_date TEXT PRIMARY KEY, session_type TEXT NOT NULL, close_time_et TEXT,
      source_url TEXT NOT NULL, verified_at TEXT NOT NULL, revision INTEGER NOT NULL);
    CREATE TABLE IF NOT EXISTS official_market_session_revisions (
      trading_date TEXT NOT NULL, revision INTEGER NOT NULL, recorded_at TEXT NOT NULL,
      record_json TEXT NOT NULL, PRIMARY KEY(trading_date,revision));
    CREATE TABLE IF NOT EXISTS intraday_calendar_coverage (
      authority TEXT PRIMARY KEY, verified_through TEXT NOT NULL, source TEXT NOT NULL,
      verified_at TEXT NOT NULL, last_attempt_at TEXT, last_error TEXT);
    CREATE TABLE IF NOT EXISTS official_calendar_fetches (
      source_url TEXT PRIMARY KEY, last_attempt_at TEXT NOT NULL, last_status TEXT NOT NULL);""")
    columns = {row[1] for row in db.execute("PRAGMA table_info(intraday_calendar_coverage)")}
    for name in ("last_attempt_at", "last_error"):
        if name not in columns:
            db.execute(f"ALTER TABLE intraday_calendar_coverage ADD COLUMN {name} TEXT")


def _official(url, authority):
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    expected = HOSTS[authority]
    return parsed.scheme == "https" and (host == expected or host.endswith("." + expected))


def _authority(event_type):
    return "BLS" if event_type in BLS else "BEA" if event_type in BEA else "FED" if event_type in FED else None


def import_events(db, records, cfg, now=None):
    """Import supplied official facts; missing explicit time stays UNVERIFIED."""
    stamp = (now or datetime.now(timezone.utc)).isoformat()
    count = 0
    with db:
        for item in records:
            kind = item.get("event_type", item.get("event", "")).upper()
            authority = _authority(kind)
            url = item.get("source_url", item.get("source", ""))
            if authority is None or not _official(url, authority):
                raise ValueError("UNVERIFIED_MACRO_SOURCE")
            day = date.fromisoformat(item["date"]).isoformat()
            clock = item.get("scheduled_time_et")
            if clock is not None:
                parsed = time.fromisoformat(clock)
                if parsed.tzinfo or parsed.second or parsed.microsecond:
                    raise ValueError("INVALID_OFFICIAL_EVENT_TIME")
                clock = parsed.strftime("%H:%M")
            impact = item["impact_level"].upper()
            if impact not in {"HIGH", "MATERIAL"}:
                raise ValueError("INVALID_EVENT_IMPACT")
            event_id = item.get("event_id") or f"{authority}:{kind}:{day}"
            if not event_id.replace(":", "").replace("-", "").replace("_", "").isalnum():
                raise ValueError("INVALID_EVENT_ID")
            status = "VERIFIED" if clock else "UNVERIFIED"
            name = item.get("event_name", kind.replace("_", " ").title())
            old = db.execute("SELECT event_date,scheduled_time_et,source_url,calendar_status,revision "
                             "FROM official_calendar_events WHERE event_id=?", (event_id,)).fetchone()
            changed = not old or old[:4] != (day, clock, url, status)
            revision = (old[4] + 1) if old and changed else (old[4] if old else 1)
            record = dict(event_id=event_id, event_name=name, event_type=kind, date=day,
                          scheduled_time_et=clock, source=authority, source_url=url,
                          retrieved_at=stamp, verified_at=stamp if clock else None,
                          impact_level=impact, calendar_status=status, revision=revision)
            db.execute("""INSERT INTO official_calendar_events VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
              ON CONFLICT(event_id) DO UPDATE SET event_name=excluded.event_name,
              event_type=excluded.event_type,event_date=excluded.event_date,
              scheduled_time_et=excluded.scheduled_time_et,source=excluded.source,
              source_url=excluded.source_url,retrieved_at=excluded.retrieved_at,
              verified_at=excluded.verified_at,impact_level=excluded.impact_level,
              calendar_status=excluded.calendar_status,revision=excluded.revision""",
              (event_id, name, kind, day, clock, authority, url, stamp,
               record["verified_at"], impact, status, revision))
            if changed:
                db.execute("INSERT INTO official_calendar_revisions VALUES(?,?,?,?)",
                           (event_id, revision, stamp, json.dumps(record, sort_keys=True)))
            count += 1
    return count


def import_market_sessions(db, records, now=None):
    stamp = (now or datetime.now(timezone.utc)).isoformat()
    with db:
        for item in records:
            day = date.fromisoformat(item["date"]).isoformat()
            kind = item["session_type"].upper()
            close = item.get("close_time_et")
            url = item["source_url"]
            if not _official(url, "NYSE") or kind not in {"HOLIDAY", "EARLY_CLOSE"}:
                raise ValueError("UNVERIFIED_NYSE_SESSION")
            if (kind == "EARLY_CLOSE" and (not close or time.fromisoformat(close).strftime("%H:%M") != close)) or (kind == "HOLIDAY" and close):
                raise ValueError("INVALID_NYSE_CLOSE")
            old = db.execute("SELECT session_type,close_time_et,source_url,revision FROM official_market_sessions WHERE trading_date=?", (day,)).fetchone()
            changed = not old or old[:3] != (kind, close, url)
            rev = old[3] + 1 if old and changed else old[3] if old else 1
            db.execute("INSERT OR REPLACE INTO official_market_sessions VALUES(?,?,?,?,?,?)",
                       (day, kind, close, url, stamp, rev))
            if changed:
                db.execute("INSERT INTO official_market_session_revisions VALUES(?,?,?,?)",
                           (day, rev, stamp, json.dumps(item, sort_keys=True)))
    return len(records)


def set_coverage(db, authority, through, source, cfg, now=None):
    authority = authority.upper()
    if authority not in HOSTS or not _official(source, authority):
        raise ValueError("UNVERIFIED_CALENDAR_COVERAGE")
    date.fromisoformat(through)
    stamp = (now or datetime.now(timezone.utc)).isoformat()
    with db:
        db.execute("INSERT OR REPLACE INTO intraday_calendar_coverage VALUES(?,?,?,?,?,NULL)",
                   (authority, through, source, stamp, stamp))


def record_refresh_failure(db, authority, error, now=None):
    stamp = (now or datetime.now(timezone.utc)).isoformat()
    with db:
        db.execute("UPDATE intraday_calendar_coverage SET last_attempt_at=?,last_error=? WHERE authority=?",
                   (stamp, str(error)[:200], authority))


def calendar_ready(db, day, cfg, now=None):
    now = now or datetime.now(timezone.utc)
    rows = {r[0]: r for r in db.execute("SELECT authority,verified_through,verified_at,last_attempt_at,last_error FROM intraday_calendar_coverage")}
    for authority in HOSTS:
        row = rows.get(authority)
        if not row or row[1] < day.isoformat():
            return False
        stamp = datetime.fromisoformat(row[2])
        if stamp.tzinfo is None or stamp > now or now - stamp > timedelta(days=cfg["calendar_max_age_days"]):
            return False
        if row[4] and row[3] and datetime.fromisoformat(row[3]) > stamp:
            return False
    return True


def events_for_day(db, day, cfg=None, now=None):
    now = now or datetime.now(timezone.utc)
    result = []
    for row in db.execute("SELECT event_id,event_name,event_type,event_date,scheduled_time_et,source_url,impact_level,verified_at,source "
                          "FROM official_calendar_events WHERE event_date=? AND calendar_status='VERIFIED' ORDER BY scheduled_time_et,event_type",
                          (day.isoformat(),)):
        if cfg:
            verified = datetime.fromisoformat(row[7])
            coverage = db.execute("SELECT last_attempt_at,last_error FROM intraday_calendar_coverage WHERE authority=?", (row[8],)).fetchone()
            if coverage and coverage[1] and coverage[0] and datetime.fromisoformat(coverage[0]) > verified:
                continue
            prior = previous_session(day)
            if verified.astimezone(__import__('zoneinfo').ZoneInfo('America/New_York')).date() < prior:
                continue  # prior-session revalidation required for event-specific checks
            if now - verified > timedelta(days=cfg["calendar_max_age_days"]):
                continue
        result.append(dict(event_id=row[0], event_name=row[1], event=row[2], date=row[3],
                           scheduled_time_et=row[4], source=row[5], impact_level=row[6]))
    return result


def market_day(db, day, cfg=None, now=None):
    row = db.execute("SELECT session_type,close_time_et,verified_at FROM official_market_sessions WHERE trading_date=?",
                     (day.isoformat(),)).fetchone()
    if row:
        return dict(session_type=row[0], close_time_et=row[1])
    return None


def schedule(day, events, cfg, session=None):
    if not market_session(day) or session and session["session_type"] == "HOLIDAY":
        return []
    close = (session or {}).get("close_time_et") or "16:00"
    windows = {t: {"time_et": t, "kind": "NORMAL", "events": [], "merged": 0}
               for t in cfg["normal_checks_et"] if t < close}
    allowed = set(cfg.get("event_schedule_types", BLS | BEA | FED))
    def minutes(clock):
        hour, minute = map(int, clock.split(":"))
        return hour * 60 + minute
    def clock(value):
        return f"{value // 60:02d}:{value % 60:02d}"
    def add(slot, kind, code):
        if not (minutes("09:30") <= slot < minutes(close)):
            return
        nearby = next((k for k in windows if abs(minutes(k) - slot) <= cfg.get("event_dedup_minutes", 5)), None)
        key = nearby or clock(slot)
        if key not in windows:
            windows[key] = {"time_et": key, "kind": kind, "events": [], "merged": 0}
        else:
            windows[key]["merged"] += 1
            if windows[key]["kind"] == "NORMAL":
                windows[key]["kind"] = kind
        if code not in windows[key]["events"]:
            windows[key]["events"].append(code)
    for event in events:
        code = event["event"].upper()
        if code not in allowed or event.get("impact_level") not in {"HIGH", "MATERIAL"}:
            continue
        at = minutes(event["scheduled_time_et"])
        if code in {"FOMC_DECISION", "FOMC_STATEMENT"}:
            add(at - cfg.get("fomc_pre_offset_minutes", 5), "PRE_EVENT_CHECK", code)
            add(at + cfg.get("fomc_first_post_offset_minutes", 10), "FIRST_REACTION_CHECK", code)
            add(at + cfg.get("fomc_second_post_offset_minutes", 35), "POST_FOMC_REPRICING_CHECK", code)
            add(minutes(close) - cfg.get("fomc_final_risk_before_close_minutes", 15), "FINAL_FOMC_RISK_CHECK", code)
        elif code == "FED_CHAIR_PRESS_CONFERENCE":
            add(at + cfg.get("fomc_press_post_offset_minutes", 5), "POST_FOMC_REPRICING_CHECK", code)
        elif event["scheduled_time_et"] == "08:30":
            add(minutes("09:35"), "MACRO_OPENING_CHECK", code)
        else:
            add(at - cfg.get("pre_event_offset_minutes", 5), "PRE_EVENT_CHECK", code)
            add(at + cfg.get("first_post_event_offset_minutes", 10), "POST_EVENT_CHECK", code)
            if event["scheduled_time_et"] != "10:00":
                add(at + cfg.get("second_post_event_offset_minutes", 35), "POST_EVENT_CHECK", code)
    if close != "16:00":
        preclose = cfg["early_close_preclose_et"]
        if preclose < close:
            add(minutes(preclose), "EARLY_PRE_CLOSE_RISK_CHECK", "NYSE_EARLY_CLOSE")
    return [windows[t] for t in sorted(windows)]

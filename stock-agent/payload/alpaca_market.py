#!/usr/bin/env python3
"""One-day Alpaca historical SIP import. Market Data endpoint only; no orders."""
from __future__ import annotations

import argparse
import json
import math
import os
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from quant_screen import ROOT
from market_regime import market_session
from shadow_day1_gate import BENCHMARKS, DAY

API = "https://data.alpaca.markets/v2/stocks/bars"
SOURCE = "ALPACA_SIP"
ET = ZoneInfo("America/New_York")


def daily_request_window(day, now=None):
    """Historical SIP only: never request a future/recent end timestamp."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("TIMEZONE_AWARE_NOW_REQUIRED")
    requested_day = date.fromisoformat(day)
    if not market_session(requested_day):
        raise ValueError("ALPACA_NON_TRADING_DATE")
    close = datetime.fromisoformat(day + "T16:00:00").replace(tzinfo=ET)
    end = close + timedelta(minutes=1)
    if now < end.astimezone(timezone.utc) + timedelta(minutes=15):
        raise RuntimeError("ALPACA_DATA_INCOMPLETE_DAILY_SIP_DELAY")
    start = datetime.fromisoformat(day + "T00:00:00").replace(tzinfo=ET)
    return start.isoformat(), end.isoformat()


def credentials(path=ROOT / ".env"):
    values = {key: os.environ.get(key) for key in ("ALPACA_API_KEY", "ALPACA_API_SECRET")}
    if path.exists():
        for line in path.read_text().splitlines():
            if "=" not in line or line.lstrip().startswith("#"):
                continue
            key, value = line.split("=", 1)
            if key in values and not values[key]:
                values[key] = value.strip().strip("\"'")
    if not all(values.values()):
        raise ValueError("ALPACA_CREDENTIALS_MISSING")
    return values


class AlpacaData:
    def __init__(self, auth, opener=None, sleeper=time.sleep):
        self.auth = auth
        self.opener = opener or urllib.request.urlopen
        self.sleeper = sleeper
        self.requests = 0

    def bars(self, symbols, timeframe, start, end):
        if not symbols or timeframe not in ("1Day", "5Min"):
            raise ValueError("INVALID_MARKET_DATA_REQUEST")
        result = {symbol: [] for symbol in symbols}
        page = None
        seen = set()
        while True:
            query = {"symbols": ",".join(symbols), "timeframe": timeframe,
                     "start": start, "end": end, "feed": "sip",
                     "adjustment": "split", "limit": 10000}
            if page:
                query["page_token"] = page
            url = API + "?" + urllib.parse.urlencode(query)
            request = urllib.request.Request(url, headers={
                "APCA-API-KEY-ID": self.auth["ALPACA_API_KEY"],
                "APCA-API-SECRET-KEY": self.auth["ALPACA_API_SECRET"],
                "Accept": "application/json"})
            for attempt, delay in enumerate((0, 5, 15, 30)):
                if delay:
                    self.sleeper(delay)
                self.requests += 1
                try:
                    with self.opener(request, timeout=35) as response:
                        payload = json.load(response)
                    break
                except urllib.error.HTTPError as exc:
                    if exc.code in (401, 403):
                        raise RuntimeError(f"ALPACA_AUTH_OR_FEED_DENIED_HTTP_{exc.code}") from None
                    if exc.code not in (429, 500, 502, 503, 504) or attempt == 3:
                        raise RuntimeError(f"ALPACA_HTTP_{exc.code}") from None
                except (urllib.error.URLError, TimeoutError):
                    if attempt == 3:
                        raise RuntimeError("ALPACA_NETWORK_ERROR") from None
            for symbol, rows in payload.get("bars", {}).items():
                if symbol in result:
                    result[symbol].extend(rows)
            page = payload.get("next_page_token")
            if not page:
                return result
            if page in seen:
                raise RuntimeError("ALPACA_PAGINATION_LOOP")
            seen.add(page)


def valid_daily(bar, day):
    try:
        values = [float(bar[key]) for key in ("o", "h", "l", "c", "v")]
        o, h, l, c, v = values
        return (bar["t"][:10] == day and all(math.isfinite(x) for x in values)
                and min(o, h, l, c) > 0 and l <= min(o, c) <= max(o, c) <= h
                and v >= 0 and v == int(v))
    except (KeyError, TypeError, ValueError, OverflowError):
        return False


def sync(day=DAY, db_path=ROOT / "data" / "market_data.db", client=None,
         *, preserve_existing=False):
    start, end = daily_request_window(day)
    client = client or AlpacaData(credentials())
    started = time.monotonic()
    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(db_path) as db:
        db.row_factory = sqlite3.Row
        names = [r[0] for r in db.execute("SELECT ticker FROM securities WHERE is_active=1 ORDER BY ticker")]
        names = list(dict.fromkeys([*BENCHMARKS, *names]))
        stored = db.execute("""SELECT ticker,open,high,low,close,volume,source
            FROM daily_bars WHERE date=?""", (day,)).fetchall()
        existing = {r[0] for r in stored if r[0] in names and
                    (preserve_existing or r[6] == SOURCE) and valid_daily(
                        dict(t=day, o=r[1], h=r[2], l=r[3], c=r[4], v=r[5]), day)}
        stored_sources = {r[6] for r in stored if r[0] in existing}
        pending = [name for name in names if name not in existing]
        rows = []
        missing = []
        bad = []
        for offset in range(0, len(pending), 100):
            batch = pending[offset:offset + 100]
            payload = client.bars(batch, "1Day", start, end)
            for symbol in batch:
                day_bars = [b for b in payload[symbol] if b.get("t", "")[:10] == day]
                if not day_bars:
                    missing.append(symbol)
                elif len(day_bars) != 1 or not valid_daily(day_bars[0], day):
                    bad.append(symbol)
                else:
                    b = day_bars[0]
                    rows.append((symbol, day, b["o"], b["h"], b["l"], b["c"], int(b["v"]),
                                 b["c"], SOURCE, now, "OK"))
        complete_benchmarks = all(name in existing or any(row[0] == name for row in rows)
                                  for name in BENCHMARKS)
        baseline = db.execute("""SELECT date,COUNT(*) FROM daily_bars WHERE date<?
            GROUP BY date ORDER BY date DESC LIMIT 1""", (day,)).fetchone()
        prior_day, prior = (baseline[0], baseline[1]) if baseline else (None, 0)
        coverage = (len(existing) + len(rows)) / prior if prior else 0
        if bad or not complete_benchmarks or coverage < 0.95:
            raise RuntimeError(json.dumps({"status": "ALPACA_DATA_INCOMPLETE",
                                           "bad": bad, "missing_benchmarks": sorted(set(BENCHMARKS) & set(missing)),
                                           "coverage_vs_prior": round(coverage, 4),
                                           "request_count": client.requests}))
        with db:
            db.executemany("""INSERT INTO daily_bars
                (ticker,date,open,high,low,close,volume,adjusted_close,source,updated_at,quality_status)
                VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(ticker,date) DO UPDATE SET
                open=excluded.open,high=excluded.high,low=excluded.low,close=excluded.close,
                volume=excluded.volume,adjusted_close=excluded.adjusted_close,source=excluded.source,
                updated_at=excluded.updated_at,quality_status=excluded.quality_status""", rows)
            db.execute("""INSERT INTO market_sync_days
                (date,source,fetched_at,downloaded_rows,accepted_rows,rejected_rows)
                VALUES(?,?,?,?,?,?) ON CONFLICT(date) DO UPDATE SET
                source=excluded.source,fetched_at=excluded.fetched_at,
                downloaded_rows=excluded.downloaded_rows,accepted_rows=excluded.accepted_rows,
                rejected_rows=excluded.rejected_rows""",
                (day, "MIXED" if stored_sources - {SOURCE} else SOURCE,
                 now, len(rows), len(rows), 0))
            details = {"feed": "sip", "timeframe": "1Day", "adjustment": "split",
                       "coverage_baseline_date": prior_day,
                       "missing_symbols": missing, "skipped_existing": len(existing),
                       "preserved_existing_sources": preserve_existing,
                       "stored_sources": sorted(stored_sources | ({SOURCE} if rows else set()))}
            db.execute("""INSERT INTO market_sync_runs
                (started_at,ended_at,asof,source,status,universe_count,api_requests,
                 downloaded_rows,updated_symbols,skipped_symbols,missing_symbols,elapsed_seconds,details_json)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (now, datetime.now(timezone.utc).isoformat(), day, SOURCE, "COMPLETE", len(names),
                 client.requests, len(rows), len(rows), len(existing), len(missing),
                 round(time.monotonic() - started, 3), json.dumps(details)))
        return {"status": "COMPLETE", "source": SOURCE, "feed": "sip", "date": day,
                "universe": len(names), "requests": client.requests, "rows": len(rows),
                "skipped_existing": len(existing), "missing_count": len(missing),
                "missing_benchmarks": [], "coverage_vs_prior": round(coverage, 4),
                "elapsed_seconds": round(time.monotonic() - started, 3)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--asof", default=DAY)
    args = parser.parse_args()
    print(json.dumps(sync(args.asof), indent=2))

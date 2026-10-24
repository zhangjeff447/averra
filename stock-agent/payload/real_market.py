#!/usr/bin/env python3
"""Incremental real US daily market-data import. No broker, Telegram or LLM calls."""
from __future__ import annotations

import argparse
import json
import logging
from logging.handlers import RotatingFileHandler
import math
import os
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from quant_screen import ROOT, init_db
from market_regime import previous_session


API = "https://api.massive.com"
BENCHMARKS = ("SPY", "QQQ")
LOG = logging.getLogger("real_market")


def setup_logging():
    if not LOG.handlers:
        handler = RotatingFileHandler(ROOT / "data" / "real_market.log", maxBytes=2_000_000, backupCount=2)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        LOG.addHandler(handler)
    LOG.setLevel(logging.INFO)


class MarketDataError(RuntimeError):
    def __init__(self, code, detail=""):
        super().__init__(f"{code}: {detail}")
        self.code = code


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def load_key(path):
    key = os.environ.get("MASSIVE_API_KEY")
    if not key and path.exists():
        for line in path.read_text().splitlines():
            if line.startswith("MASSIVE_API_KEY="):
                key = line.split("=", 1)[1].strip().strip("\"'")
                break
    if not key:
        raise MarketDataError("AUTH_ERROR", "MASSIVE_API_KEY missing from environment/.env")
    return key


class MassiveClient:
    def __init__(self, key, spacing=13, opener=None, sleeper=time.sleep):
        self.key, self.spacing = key, spacing
        self.opener = opener or urllib.request.urlopen
        self.sleeper = sleeper
        self.next_at = 0.0
        self.request_count = 0

    def get(self, path, query=None):
        url = path if path.startswith("https://api.massive.com/") else API + path
        if query:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(query)
        for attempt, delay in enumerate((0, 5, 15, 30)):
            if delay:
                self.sleeper(delay)
            wait = self.next_at - time.monotonic()
            if wait > 0:
                self.sleeper(wait)
            self.next_at = time.monotonic() + self.spacing
            req = urllib.request.Request(url, headers={"Authorization": "Bearer " + self.key,
                                                       "Accept": "application/json",
                                                       "User-Agent": "AverraStockAgent/1.0"})
            self.request_count += 1
            try:
                with self.opener(req, timeout=35) as response:
                    payload = json.load(response)
                if payload.get("status") not in ("OK", "DELAYED", "SUCCESS"):
                    raise MarketDataError("MISSING_DATA", str(payload.get("status")))
                return payload
            except urllib.error.HTTPError as exc:
                if exc.code in (401, 403):
                    code = "AUTH_ERROR"
                elif exc.code == 429:
                    code = "RATE_LIMIT"
                elif exc.code == 404:
                    code = "INVALID_SYMBOL" if "/ticker/" in url else "MISSING_DATA"
                else:
                    code = "NETWORK_ERROR"
                if code in ("AUTH_ERROR", "INVALID_SYMBOL", "MISSING_DATA") or attempt == 3:
                    raise MarketDataError(code, f"HTTP {exc.code}") from exc
                LOG.warning("%s HTTP %s retry in %ss", code, exc.code, (5, 15, 30)[attempt])
            except (urllib.error.URLError, TimeoutError) as exc:
                if attempt == 3:
                    raise MarketDataError("NETWORK_ERROR", type(exc).__name__) from exc
                LOG.warning("NETWORK_ERROR retry in %ss", (5, 15, 30)[attempt])
        raise AssertionError("unreachable")


def tickers(client, exchange_mics):
    url = "/v3/reference/tickers"
    params = {"market": "stocks", "locale": "us", "type": "CS", "active": "true", "limit": 1000}
    records = {}
    seen_urls = set()
    while url:
        if url in seen_urls:
            raise MarketDataError("MISSING_DATA", "ticker pagination loop")
        seen_urls.add(url)
        payload = client.get(url, params)
        for row in payload.get("results") or []:
            symbol = row.get("ticker")
            exchange = exchange_mics.get(row.get("primary_exchange"))
            if not (symbol and exchange and row.get("active") is True and row.get("type") == "CS"
                    and row.get("locale") == "us" and row.get("market") == "stocks"):
                continue
            records[symbol] = (symbol, "COMMON_STOCK", "UNKNOWN", exchange, 0, 0, 0, 0,
                               row.get("name"), "CS", row.get("cik"), "MASSIVE", utc_now(), "PENDING")
        url, params = payload.get("next_url"), None
    if len(records) < 1000:
        raise MarketDataError("MISSING_DATA", "US common-stock master unexpectedly small")
    return records


def store_tickers(db, records):
    sql = """INSERT INTO securities(ticker,asset_type,country,exchange,is_etf,is_otc,is_preferred,is_warrant,
        company_name,security_type,cik,source,updated_at,data_quality_status,is_active)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,1) ON CONFLICT(ticker) DO UPDATE SET
        asset_type=excluded.asset_type,
        country=CASE WHEN securities.sector_source='NASDAQ_SCREENER' THEN securities.country ELSE excluded.country END,
        exchange=excluded.exchange,
        company_name=excluded.company_name,security_type=excluded.security_type,cik=excluded.cik,
        source=excluded.source,updated_at=excluded.updated_at,is_active=1"""
    with db:
        db.execute("UPDATE securities SET is_active=0 WHERE source='MASSIVE'")
        db.executemany(sql, records.values())


def parse_bar(row, day, allowed):
    symbol = row.get("T")
    if symbol not in allowed:
        return None
    try:
        o, h, l, c, v = (float(row[name]) for name in ("o", "h", "l", "c", "v"))
    except (KeyError, TypeError, ValueError) as exc:
        raise MarketDataError("MISSING_DATA", f"{symbol} invalid OHLCV") from exc
    if not all(math.isfinite(x) for x in (o, h, l, c, v)) or min(o, h, l, c) <= 0 or v < 0:
        raise MarketDataError("MISSING_DATA", f"{symbol} NaN/Inf/nonpositive")
    if l > min(o, c) or h < max(o, c) or h < l or h > 1_000_000:
        raise MarketDataError("MISSING_DATA", f"{symbol} extreme/inconsistent price")
    return (symbol, day, o, h, l, c, int(v), c, "MASSIVE", utc_now(), "VALID")


def store_grouped(db, day, rows, allowed):
    accepted, rejected, seen = [], Counter(), set()
    for row in rows:
        symbol = row.get("T")
        if symbol not in allowed:
            continue
        if symbol in seen:
            rejected["duplicate_bar"] += 1
            continue
        seen.add(symbol)
        try:
            bar = parse_bar(row, day, allowed)
            if bar:
                accepted.append(bar)
        except MarketDataError as exc:
            rejected[exc.code] += 1
    if not any(bar[0] == "SPY" for bar in accepted) or not any(bar[0] == "QQQ" for bar in accepted):
        raise MarketDataError("MISSING_DATA", f"{day} SPY/QQQ absent")
    with db:
        db.executemany("""INSERT INTO daily_bars(ticker,date,open,high,low,close,volume,adjusted_close,source,updated_at,quality_status)
            VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(ticker,date) DO UPDATE SET
            open=excluded.open,high=excluded.high,low=excluded.low,close=excluded.close,
            volume=excluded.volume,adjusted_close=excluded.adjusted_close,source=excluded.source,
            updated_at=excluded.updated_at,quality_status=excluded.quality_status""", accepted)
        db.execute("INSERT INTO market_sync_days VALUES(?,?,?,?,?,?) ON CONFLICT(date) DO UPDATE SET "
                   "fetched_at=excluded.fetched_at,downloaded_rows=excluded.downloaded_rows,"
                   "accepted_rows=excluded.accepted_rows,rejected_rows=excluded.rejected_rows",
                   (day, "MASSIVE", utc_now(), len(rows), len(accepted), sum(rejected.values())))
    return len(accepted), rejected


def trading_days(client, asof, sessions):
    start = (date.fromisoformat(asof) - timedelta(days=math.ceil(sessions * 1.7))).isoformat()
    payload = client.get(f"/v2/aggs/ticker/SPY/range/1/day/{start}/{asof}",
                         {"adjusted": "true", "sort": "asc", "limit": 50000})
    days = [datetime.fromtimestamp(row["t"] / 1000, ZoneInfo("America/New_York")).date().isoformat()
            for row in payload.get("results") or []]
    if len(days) < sessions:
        raise MarketDataError("MISSING_DATA", f"only {len(days)} SPY sessions available; need {sessions}")
    return days[-sessions:]


def record_splits(db, client, start, asof, allowed):
    """Keep the action ledger; initial grouped bars are already split-adjusted."""
    url = "/stocks/v1/splits"
    params = {"execution_date.gte": start, "execution_date.lte": asof, "limit": 5000}
    affected = set()
    while url:
        payload = client.get(url, params)
        for item in payload.get("results") or []:
            symbol = item.get("ticker")
            if symbol not in allowed:
                continue
            affected.add(symbol)
            with db:
                db.execute("INSERT OR IGNORE INTO corporate_actions VALUES(?,?,?,?,?,?)",
                           (item.get("id") or f"{symbol}:{item.get('execution_date')}:split", symbol,
                            item.get("execution_date"), item.get("adjustment_type") or "split",
                            "MASSIVE", json.dumps(item, sort_keys=True)))
        url, params = payload.get("next_url"), None
    return affected


def refresh_recent_splits(db, client, first_new_day, asof, history_start, allowed):
    """Re-fetch only affected tickers so old adjusted bars reflect new splits."""
    affected = record_splits(db, client, first_new_day, asof, allowed)
    refreshed = 0
    for symbol in sorted(affected):
        payload = client.get(f"/v2/aggs/ticker/{urllib.parse.quote(symbol, safe='')}/range/1/day/{history_start}/{asof}",
                             {"adjusted": "true", "sort": "asc", "limit": 50000})
        new_bars = []
        for item in payload.get("results") or []:
            day = datetime.fromtimestamp(item["t"] / 1000, ZoneInfo("America/New_York")).date().isoformat()
            try:
                bar = parse_bar({**item, "T": symbol}, day, allowed)
            except MarketDataError:
                continue
            if bar:
                new_bars.append(bar)
        if not new_bars:
            continue
        with db:
            db.executemany("""INSERT INTO daily_bars(ticker,date,open,high,low,close,volume,adjusted_close,source,updated_at,quality_status)
                VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(ticker,date) DO UPDATE SET
                open=excluded.open,high=excluded.high,low=excluded.low,close=excluded.close,volume=excluded.volume,
                adjusted_close=excluded.adjusted_close,updated_at=excluded.updated_at,quality_status=excluded.quality_status""", new_bars)
        refreshed += 1
    return refreshed


def sync(db, client, asof, cfg, progress=None):
    started = time.monotonic()
    LOG.info("scan_start asof=%s source=MASSIVE", asof)
    cur = db.execute("INSERT INTO market_sync_runs(started_at,asof,source,status) VALUES(?,?,?,?)",
                     (utc_now(), asof, "MASSIVE", "RUNNING"))
    run_id = cur.lastrowid
    db.commit()
    stats = Counter({"downloaded_rows": 0, "accepted_rows": 0, "rejected_rows": 0,
                     "updated_days": 0, "updated_symbols": 0, "split_refreshed_symbols": 0,
                     "missing_symbols": 0, "skipped_symbols": 0})
    try:
        records = tickers(client, cfg["exchange_mics"])
        store_tickers(db, records)
        allowed = set(records) | set(BENCHMARKS) | set(cfg["sector_etfs"])
        days = trading_days(client, asof, cfg["history_sessions"])
        if days[-1] != asof:
            raise MarketDataError("STALE_DATA", f"latest completed SPY session is {days[-1]}, requested {asof}")
        done = {row[0] for row in db.execute("SELECT date FROM market_sync_days WHERE date>=? AND date<=?", (days[0], days[-1]))}
        prior_latest = max(done) if done else None
        new_days = []
        stats["universe_count"] = len(records)
        stats["skipped_days"] = len(done & set(days))
        for number, day in enumerate(days, 1):
            if day in done:
                continue
            payload = client.get(f"/v2/aggs/grouped/locale/us/market/stocks/{day}",
                                 {"adjusted": "true", "include_otc": "false"})
            rows = payload.get("results") or []
            if len(rows) < 1000:
                raise MarketDataError("MISSING_DATA", f"{day} grouped market unexpectedly small")
            count, rejected = store_grouped(db, day, rows, allowed)
            stats["downloaded_rows"] += len(rows)
            stats["accepted_rows"] += count
            stats["rejected_rows"] += sum(rejected.values())
            stats["updated_days"] += 1
            new_days.append(day)
            LOG.info("daily_import date=%s downloaded=%s accepted=%s rejected=%s", day, len(rows), count, sum(rejected.values()))
            if progress:
                progress(number, len(days), day, count)
        if new_days and not db.execute("SELECT 1 FROM corporate_actions LIMIT 1").fetchone():
            stats["split_events_symbols"] = len(record_splits(db, client, days[0], asof, allowed))
        if prior_latest and new_days and max(new_days) > prior_latest:
            stats["split_refreshed_symbols"] = refresh_recent_splits(
                db, client, min(day for day in new_days if day > prior_latest), asof, days[0], allowed)
        latest = days[-1]
        with db:
            db.execute("""UPDATE securities SET last_trading_date=(SELECT MAX(date) FROM daily_bars WHERE ticker=securities.ticker),
                data_quality_status=CASE WHEN (SELECT COUNT(*) FROM daily_bars WHERE ticker=securities.ticker AND date>=?)<252
                    THEN 'INSUFFICIENT_HISTORY' WHEN (SELECT MAX(date) FROM daily_bars WHERE ticker=securities.ticker)<>?
                    THEN 'STALE_DATA' ELSE 'VALID' END WHERE source='MASSIVE'""", (days[0], latest))
        stats["missing_symbols"] = db.execute("SELECT COUNT(*) FROM securities WHERE source='MASSIVE' AND last_trading_date IS NULL").fetchone()[0]
        if new_days:
            placeholders = ",".join("?" for _ in new_days)
            stats["updated_symbols"] = db.execute(
                f"""SELECT COUNT(DISTINCT b.ticker) FROM daily_bars b JOIN securities s ON s.ticker=b.ticker
                    WHERE b.date IN ({placeholders}) AND b.source='MASSIVE' AND s.source='MASSIVE'""",
                new_days).fetchone()[0]
        stats["updated_symbols"] = max(stats["updated_symbols"], stats["split_refreshed_symbols"])
        stats["skipped_symbols"] = stats["universe_count"] - stats["updated_symbols"]
        status = "COMPLETE"
    except Exception as exc:
        status = "REAL DATA FAILED"
        stats["error_code"] = (exc.code if isinstance(exc, MarketDataError) else
                               "DATABASE_ERROR" if isinstance(exc, sqlite3.Error) else type(exc).__name__)
        LOG.exception("%s", status)
        raise
    finally:
        stats["api_requests"] = client.request_count
        stats["elapsed_seconds"] = round(time.monotonic() - started, 2)
        with db:
            db.execute("""UPDATE market_sync_runs SET ended_at=?,status=?,universe_count=?,api_requests=?,
                downloaded_rows=?,updated_symbols=?,skipped_symbols=?,missing_symbols=?,elapsed_seconds=?,details_json=?
                WHERE id=?""", (utc_now(), status, stats["universe_count"], stats["api_requests"],
                stats["downloaded_rows"], stats["updated_symbols"], stats["skipped_symbols"],
                stats["missing_symbols"], stats["elapsed_seconds"], json.dumps(dict(stats)), run_id))
        LOG.info("scan_end run=%s status=%s asof=%s metrics=%s", run_id, status, asof, json.dumps(dict(stats)))
    return run_id, dict(stats)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=ROOT / "data" / "market_data.db")
    parser.add_argument("--config", type=Path, default=ROOT / "real_market_config.json")
    parser.add_argument("--asof", default=previous_session(datetime.now(ZoneInfo("America/New_York")).date()).isoformat())
    args = parser.parse_args()
    cfg = json.loads(args.config.read_text())
    args.db.parent.mkdir(parents=True, exist_ok=True)
    setup_logging()
    client = MassiveClient(load_key(ROOT / ".env"), cfg["free_plan_min_request_spacing_seconds"])
    with sqlite3.connect(args.db, timeout=30) as db:
        db.row_factory = sqlite3.Row
        init_db(db)
        try:
            run_id, stats = sync(db, client, args.asof, cfg,
                                 progress=lambda n, total, day, count: print(f"{n}/{total} {day} accepted={count}", flush=True))
            print(json.dumps({"run_id": run_id, "status": "COMPLETE", **stats}, indent=2))
        except Exception as exc:
            raise SystemExit(f"REAL DATA FAILED: {type(exc).__name__}: {exc}") from exc


if __name__ == "__main__":
    main()

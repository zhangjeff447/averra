"""SEC JSON API cache, identity checks, and point-in-time XBRL selection."""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
import urllib.error
import urllib.request
import zlib
from datetime import date, datetime, time as clock_time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from request_diagnostics import RequestAudit


LOG = logging.getLogger("phase3_sec")
SEC_BASE = "https://data.sec.gov"
ET = ZoneInfo("America/New_York")


class SECError(RuntimeError):
    def __init__(self, kind, message):
        super().__init__(f"{kind}: {message}")
        self.kind = kind


def load_user_agent(env_path):
    for line in Path(env_path).read_text().splitlines() if Path(env_path).exists() else []:
        if line.startswith("SEC_USER_AGENT="):
            value = line.split("=", 1)[1].strip().strip('"\'')
            if "@" in value and " " in value:
                return value
    raise SECError("CONFIGURATION", "SEC_USER_AGENT with organization/name and contact email is required in .env")


def next_business_day(day):
    value = date.fromisoformat(day) + timedelta(days=1)
    while value.weekday() >= 5:
        value += timedelta(days=1)
    return value.isoformat()


def availability_date(filed, accepted, cutoff):
    """Unknown acceptance time gets a deliberately conservative next-business-day lag."""
    if accepted:
        try:
            stamp = datetime.fromisoformat(accepted.replace("Z", "+00:00"))
            if stamp.tzinfo:
                local = stamp.astimezone(ET)
                if local.time() <= clock_time.fromisoformat(cutoff):
                    return max(filed, local.date().isoformat())
                return next_business_day(local.date().isoformat())
        except ValueError:
            pass
    return next_business_day(filed)


def accession_acceptance(submissions):
    recent = submissions.get("filings", {}).get("recent", {})
    return dict(zip(recent.get("accessionNumber", []), recent.get("acceptanceDateTime", [])))


def validate_identity(cik, ticker, company_name, submissions, facts, cfg):
    if int(submissions.get("cik", -1)) != int(cik) or int(facts.get("cik", -1)) != int(cik):
        raise SECError("IDENTITY_MISMATCH", "CIK mismatch")
    tickers = {str(t).upper().replace("-", ".") for t in submissions.get("tickers", [])}
    if ticker.upper().replace("-", ".") not in tickers:
        raise SECError("IDENTITY_MISMATCH", "ticker absent from SEC CIK identity")
    def words(name):
        return {w for w in re.findall(r"[a-z0-9]+", (name or "").lower()) if len(w) > 2 and w not in {"inc", "corp", "class", "common", "stock", "company", "holdings", "limited", "plc"}}
    a, b = words(company_name), words(facts.get("entityName"))
    if a and b and len(a & b) / min(len(a), len(b)) < cfg["minimum_company_name_overlap"]:
        raise SECError("IDENTITY_MISMATCH", "company name conflicts with SEC entity")


class SECClient:
    def __init__(self, user_agent, cfg, opener=None, sleeper=time.sleep, now=None, audit=None):
        self.user_agent = user_agent
        self.cfg = cfg
        self.opener = opener or urllib.request.urlopen
        self.sleeper = sleeper
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.last_request = 0.0
        self.api_requests = 0
        self.cache_hits = 0
        self.audit = audit or RequestAudit(enabled=opener is None)

    def request(self, path):
        for attempt, delay in enumerate((0, *self.cfg["retry_seconds"])):
            if delay:
                self.sleeper(delay)
            spacing = self.cfg["minimum_request_spacing_seconds"] - (time.monotonic() - self.last_request)
            if spacing > 0:
                self.sleeper(spacing)
            request = urllib.request.Request(SEC_BASE + path, headers={
                "User-Agent": self.user_agent, "Accept": "application/json", "Accept-Encoding": "gzip"})
            self.api_requests += 1
            self.last_request = time.monotonic()
            started = time.monotonic()
            failure, status, size = None, None, None
            try:
                with self.opener(request, timeout=30) as response:
                    status = getattr(response, "status", 200)
                    body = response.read()
                    size = len(body)
                    if response.headers.get("Content-Encoding") == "gzip":
                        import gzip
                        body = gzip.decompress(body)
                    return json.loads(body)
            except urllib.error.HTTPError as exc:
                failure, status = exc, exc.code
                exc.close()
                if exc.code in (401, 403):
                    raise SECError("ACCESS_DENIED", f"SEC HTTP {exc.code}") from exc
                if exc.code == 404:
                    raise SECError("MISSING_DATA", "SEC record unavailable") from exc
                if exc.code != 429 and exc.code < 500:
                    raise SECError("HTTP_ERROR", f"SEC HTTP {exc.code}") from exc
                if attempt == len(self.cfg["retry_seconds"]):
                    raise SECError("RATE_LIMIT" if exc.code == 429 else "NETWORK_ERROR", f"SEC HTTP {exc.code}") from exc
            except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
                failure = exc
                if attempt == len(self.cfg["retry_seconds"]):
                    raise SECError("NETWORK_ERROR", type(exc).__name__) from exc
            finally:
                retry = failure is not None and attempt < len(self.cfg["retry_seconds"]) and (
                    not isinstance(failure, urllib.error.HTTPError) or status == 429 or status >= 500)
                self.audit.record("SEC_COMPANYFACTS" if "companyfacts" in path else "SEC_SUBMISSIONS",
                    request.full_url, attempt, started, error=failure, status=status,
                    response_bytes=size,
                    retry_delay_seconds=self.cfg["retry_seconds"][attempt] if retry else 0)
        raise AssertionError("unreachable")

    def cached(self, db, cik, endpoint, path, *, force=False, allow_stale=False):
        row = db.execute("SELECT * FROM sec_cache WHERE cik=? AND endpoint=?", (cik, endpoint)).fetchone()
        if row and not force:
            fetched = datetime.fromisoformat(row["fetched_at"])
            fresh = (self.now() - fetched).total_seconds() < self.cfg["cache_ttl_hours"] * 3600
            if fresh and row["cache_status"] == "MISSING":
                self.cache_hits += 1
                raise SECError("MISSING_DATA", "SEC record unavailable (cached)")
            if allow_stale or fresh:
                self.cache_hits += 1
                return json.loads(zlib.decompress(row["payload"])), "HIT"
        try:
            payload = self.request(path)
        except SECError as exc:
            if exc.kind == "MISSING_DATA":
                with db:
                    db.execute("""INSERT INTO sec_cache VALUES(?,?,?,?,?,?,?) ON CONFLICT(cik,endpoint)
                        DO UPDATE SET payload=excluded.payload,fetched_at=excluded.fetched_at,
                        cache_status=excluded.cache_status""",
                        (cik, endpoint, zlib.compress(b"{}"), self.now().isoformat(),
                         None, "SEC_EDGAR", "MISSING"))
            raise
        encoded = zlib.compress(json.dumps(payload, separators=(",", ":")).encode())
        last_filing = None
        if endpoint == "submissions":
            dates = payload.get("filings", {}).get("recent", {}).get("filingDate", [])
            last_filing = max(dates) if dates else None
        with db:
            db.execute("""INSERT INTO sec_cache VALUES(?,?,?,?,?,?,?) ON CONFLICT(cik,endpoint)
                DO UPDATE SET payload=excluded.payload,fetched_at=excluded.fetched_at,
                last_filing=excluded.last_filing,cache_status=excluded.cache_status""",
                (cik, endpoint, encoded, self.now().isoformat(), last_filing, "SEC_EDGAR", "FRESH"))
        return payload, "FETCHED"

    def company(self, db, cik):
        cik = str(int(cik)).zfill(10)
        submissions, _ = self.cached(db, cik, "submissions", f"/submissions/CIK{cik}.json")
        recent = submissions.get("filings", {}).get("recent", {})
        newest = max((f"{day}|{accn}" for day, form, accn in zip(recent.get("filingDate", []),
                       recent.get("form", []), recent.get("accessionNumber", []))
                      if form in self.cfg["forms"]), default=None)
        fact_row = db.execute("SELECT last_filing,cache_status FROM sec_cache WHERE cik=? AND endpoint='companyfacts'", (cik,)).fetchone()
        needs_refresh = bool(fact_row and fact_row["cache_status"] != "MISSING" and newest and newest != fact_row[0])
        facts, _ = self.cached(db, cik, "companyfacts", f"/api/xbrl/companyfacts/CIK{cik}.json",
                               force=needs_refresh, allow_stale=bool(fact_row) and fact_row["cache_status"] != "MISSING" and not needs_refresh)
        if needs_refresh or fact_row is None:
            with db:
                db.execute("UPDATE sec_cache SET last_filing=? WHERE cik=? AND endpoint='companyfacts'", (newest, cik))
        return submissions, facts


def extract_facts(facts, submissions, asof, cfg):
    """Select original facts known by asof; never let a later restatement rewrite history."""
    accepted = accession_acceptance(submissions)
    by_metric = {}
    forms = set(cfg["source"]["forms"])
    for metric, aliases in cfg["aliases"].items():
        unit = cfg.get("units", {}).get(metric, "USD")
        selected = {}
        seen_same_filing = {}
        for alias_priority, alias in enumerate(aliases):
            taxonomy, tag = alias.split(":", 1)
            entries = facts.get("facts", {}).get(taxonomy, {}).get(tag, {}).get("units", {}).get(unit, [])
            for item in entries:
                if item.get("form") not in forms or "val" not in item or "end" not in item or "filed" not in item:
                    continue
                available = availability_date(item["filed"], accepted.get(item.get("accn")), cfg["source"]["scan_cutoff_et"])
                if available > asof:
                    continue
                start = item.get("start")
                identity = (alias, start, item["end"], item.get("accn"))
                prior_value = seen_same_filing.get(identity)
                if prior_value is not None and abs(prior_value - item["val"]) > max(1e-8, abs(prior_value) * 1e-6):
                    raise SECError("DATA_CONFLICT", f"conflicting {metric} facts in one accession")
                seen_same_filing[identity] = item["val"]
                period_days = (date.fromisoformat(item["end"]) - date.fromisoformat(start)).days + 1 if start else None
                if period_days is not None:
                    if cfg["quality"]["minimum_quarter_days"] <= period_days <= cfg["quality"]["maximum_quarter_days"]:
                        period = "QUARTERLY"
                    elif cfg["quality"]["minimum_annual_days"] <= period_days <= cfg["quality"]["maximum_annual_days"]:
                        period = "ANNUAL"
                    else:
                        period = "YTD"
                else:
                    period = "INSTANT"
                key = (start, item["end"], period)
                candidate = {"value": item["val"], "unit": unit, "start": start, "end": item["end"],
                             "period": period, "fy": item.get("fy"), "fp": item.get("fp"),
                             "filed": item["filed"], "available_date": available,
                             "accepted_at": accepted.get(item.get("accn")), "accession": item.get("accn"),
                             "form": item["form"], "concept": alias, "source": "SEC_COMPANYFACTS",
                             "data_quality": "RESTATED_OR_AMENDED" if item["form"].endswith("/A") else "AS_FILED"}
                rank = (available, item["filed"], -alias_priority, item.get("accn") or "")
                if key not in selected or rank > selected[key][0]:
                    selected[key] = (rank, candidate)
        by_metric[metric] = sorted((value for _, value in selected.values()), key=lambda x: (x["end"], x["period"], x["filed"]))
    return by_metric

"""Phase 4A real-source collector. Local parsing only; no LLM or broker access."""
from __future__ import annotations

import hashlib
import html
import json
import re
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from email.utils import parsedate_to_datetime
import xml.etree.ElementTree as ET

from phase4_evidence import compact, utc_now
from phase4_evidence import headline_category
from request_diagnostics import RequestAudit

LABEL_ORDER = {"LOW": 0, "MEDIUM": 1, "HIGH": 2, "MATERIAL": 3}
SEC_FORMS = {"8-K", "8-K/A", "10-Q", "10-Q/A", "10-K", "10-K/A", "6-K", "20-F"}
ITEM_TITLES = {
    "1.01": "Material Definitive Agreement", "1.02": "Termination of Material Agreement",
    "2.01": "Acquisition or Disposition", "2.02": "Results of Operations",
    "2.05": "Exit or Disposal", "2.06": "Material Impairment",
    "4.01": "Auditor Change", "4.02": "Non-reliance or Restatement",
    "5.02": "Director or Executive Change", "7.01": "Regulation FD",
    "8.01": "Other Events", "9.01": "Financial Statements and Exhibits",
}


class TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "ix:header"}:
            self.hidden += 1
        elif tag in {"p", "div", "br", "tr", "li", "h1", "h2", "h3"}:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style", "ix:header"} and self.hidden:
            self.hidden -= 1
        elif tag in {"p", "div", "tr", "li"}:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def document_text(body):
    decoded = body.decode("utf-8", "replace") if isinstance(body, bytes) else body
    if "<" in decoded and ">" in decoded:
        parser = TextExtractor()
        parser.feed(decoded)
        decoded = "".join(parser.parts)
    return re.sub(r"[ \t]+", " ", re.sub(r"\n\s*\n+", "\n", html.unescape(decoded))).strip()


def accepted_before(value, cutoff):
    if not value:
        return False
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        limit = datetime.fromisoformat(cutoff.replace("Z", "+00:00"))
        return stamp.tzinfo is not None and limit.tzinfo is not None and stamp <= limit
    except ValueError:
        return False


def ir_snapshot_eligible(retrieved_at, cutoff, historical_asof):
    return not historical_asof or accepted_before(retrieved_at, cutoff)


def select_filings(submissions, cutoff, cfg):
    recent = submissions.get("filings", {}).get("recent", {})
    limit = datetime.fromisoformat(cutoff.replace("Z", "+00:00")).date()
    start = limit - timedelta(days=cfg["window_days"])
    selected, baseline = [], None
    forms = recent.get("form", [])
    for i, form in enumerate(forms):
        if form not in SEC_FORMS:
            continue
        def field(name):
            values = recent.get(name, [])
            return values[i] if i < len(values) else None
        day, accepted = field("filingDate"), field("acceptanceDateTime")
        if not day or not accepted_before(accepted, cutoff):
            continue
        row = {"form": form, "filing_date": day, "accepted_at": accepted,
               "accession": field("accessionNumber"), "primary_document": field("primaryDocument"),
               "items": field("items") or ""}
        if form.startswith(("10-Q", "10-K")) and baseline is None:
            baseline = row
        if datetime.fromisoformat(day).date() >= start:
            selected.append(row)
    selected = [r for r in selected if r["form"].startswith("8-K")][:cfg["max_8k_filings"]] + [
        r for r in selected if not r["form"].startswith("8-K")][:cfg["max_periodic_filings"]]
    if baseline and baseline not in selected:
        selected.append(baseline)
    return selected


def sec_url(cik, accession, name):
    if not accession or not name or name.startswith("/") or ".." in name.split("/"):
        raise ValueError("invalid SEC filing path")
    return (f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/"
            f"{accession.replace('-', '')}/{urllib.parse.quote(name, safe='/.-_')}")


def extract_8k_items(text, filing, url, cfg):
    matches = list(re.finditer(r"(?im)^\s*item\s+([1-9]\.\d{2})\s*[:.\-–]?\s*([^\n]{0,120})", text))
    events = []
    for index, match in enumerate(matches):
        code = match.group(1)
        if code not in cfg["materiality_by_item"] and not code.startswith("3."):
            continue
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        section = re.sub(r"\s+", " ", text[match.end():end]).strip()
        if len(section) < 35:
            continue  # Table of contents, not a substantive section.
        title = match.group(2).strip() or ITEM_TITLES.get(code, f"Item {code}")
        kind = {"2.02": "EARNINGS", "4.01": "AUDITOR_CHANGE", "4.02": "RESTATEMENT",
                "2.01": "ACQUISITION", "5.02": "EXECUTIVE_CHANGE", "1.01": "MAJOR_CONTRACT"}.get(code, "SEC_MATERIAL_EVENT")
        materiality = cfg["materiality_by_item"].get(code, "HIGH")
        if filing["form"].endswith("/A"):
            kind, materiality = "FILING_AMENDMENT", "MEDIUM"
        events.append(event(filing, kind, materiality, title,
                            section[:1500] if code == "2.01" else section[:550],
                            url, item=code, confidence="MEDIUM"))
    return events


PERIODIC_PATTERNS = [
    ("MANAGEMENT_DISCUSSION", r"(?im)^\s*item\s+2\.?\s+management.{0,100}discussion"),
    ("RISK_FACTORS", r"(?im)^\s*item\s+1a\.?\s+risk factors"),
    ("LEGAL_PROCEEDINGS", r"(?im)^\s*item\s+1\.?\s+legal proceedings"),
    ("LIQUIDITY", r"(?i)\bliquidity and capital resources\b"),
    ("GOING_CONCERN", r"(?i)\bsubstantial doubt.{0,100}going concern\b"),
    ("DEBT", r"(?i)\b(long.term debt|credit agreement|debt covenants)\b"),
    ("CUSTOMER_CONCENTRATION", r"(?i)\b(customer concentration|major customer)\b"),
    ("SHARE_ISSUANCE", r"(?i)\b(issuance of common stock|at.the.market offering)\b"),
    ("MATERIAL_COMMITMENT", r"(?i)\b(material commitments|contractual obligations)\b"),
]


def extract_periodic(text, filing, url, cfg):
    events = []
    for kind, pattern in PERIODIC_PATTERNS:
        matches = list(re.finditer(pattern, text))
        if not matches:
            continue
        match = matches[-1] if kind in {"MANAGEMENT_DISCUSSION", "RISK_FACTORS", "LEGAL_PROCEEDINGS"} else matches[0]
        excerpt = re.sub(r"\s+", " ", text[match.start():match.start() + 650]).strip()
        if len(excerpt) < 45:
            continue
        if re.search(r"(?i)item\s+\d+[a-z]?\.?\s+.{0,80}\s+\d+\s+item\s+\d+", excerpt[:250]):
            continue  # Table of contents, not a substantive section.
        label = "HIGH" if kind == "GOING_CONCERN" else "MEDIUM"
        events.append(event(filing, kind, label, kind.replace("_", " ").title(), excerpt[:500],
                            url, confidence="LOW"))
    return events


def event(filing, kind, materiality, title, excerpt, url, *, item=None, confidence="MEDIUM", source_type="SEC_CONTENT"):
    return {"form": filing["form"], "filing_date": filing["filing_date"],
            "accepted_at": filing["accepted_at"], "accession": filing["accession"], "item": item,
            "event_type": kind, "materiality": materiality, "section_title": title,
            "verified_facts": [excerpt] if excerpt else [], "extraction_confidence": confidence,
            "source_type": source_type, "sources": [{"url": url, "source_type": source_type,
                                                     "published_at": filing["accepted_at"]}],
            "date": filing["filing_date"]}


def exhibit_links(index_html, base_url):
    links = []
    for match in re.finditer(r'(?is)<tr[^>]*>(.*?)</tr>', index_html):
        row = match.group(1)
        label = re.search(r"EX[- ]?99\.[12]", document_text(row), re.I)
        href = re.search(r'href=["\']([^"\']+)["\']', row, re.I)
        if label and href:
            link = urllib.parse.urljoin(base_url, html.unescape(href.group(1)))
            if urllib.parse.urlparse(link).hostname == "www.sec.gov":
                links.append((label.group(0).upper().replace(" ", "-"), link))
    return list(dict.fromkeys(links))


def extract_release(text, filing, url, cfg, source_type="SEC_EXHIBIT", title=None):
    lead = (title or text[:800]).lower()
    if source_type == "SEC_EXHIBIT" and re.search(
            r"(?i)(financial statements|balance sheets|statements of operations)", text[:1000]):
        kind = "FINANCIAL_STATEMENT_EXHIBIT"
    elif re.search(r"\b(to report|to announce|will report)\b", lead):
        kind = "EARNINGS_SCHEDULE"
    elif re.search(r"\b(reports|announces).{0,60}(quarter|year).{0,30}(results|earnings)|financial results", lead):
        kind = "EARNINGS"
    elif re.search(r"guidance|financial outlook", lead):
        kind = "GUIDANCE"
    elif re.search(r"acquir|merger", lead):
        kind = "ACQUISITION"
    elif re.search(r"strategic partnership|strategic agreement|strategic venture", lead):
        kind = "STRATEGIC_PARTNERSHIP"
    elif re.search(r"appoint|depart|resign|board of directors", lead):
        kind = "EXECUTIVE_CHANGE"
    else:
        kind = "OFFICIAL_RELEASE"
    label = cfg["materiality_by_event"].get(kind, "MEDIUM")
    facts = []
    for pattern, tag in ((r"(?i)\b(total )?revenue\s+(?:was|of|totaled|increased to)\s+\$?[\d,.]+\s*(?:million|billion)?", "ACTUAL_RESULT"),
                         (r"(?i)\b(?:diluted )?(?:earnings|net income) per share\s+(?:was|of)\s+\$?[\d,.]+", "ACTUAL_RESULT"),
                         (r"(?i)\b(?:expects|guides|forecasts|projects)\s+.{0,100}(?:revenue|margin|earnings)\s+.{0,80}", "MANAGEMENT_GUIDANCE")):
        for found in list(re.finditer(pattern, text[:100000]))[:3]:
            if tag == "ACTUAL_RESULT" and re.search(r"(?i)\b(expects|guides|forecasts|projects)\b",
                                                    text[max(0, found.start() - 30):found.start()]):
                continue
            facts.append(f"{tag}: {re.sub(r'\s+', ' ', found.group(0)).strip()}")
    result = event(filing, kind, label, kind.replace("_", " ").title(), "", url,
                   confidence="MEDIUM" if facts else "LOW", source_type=source_type)
    if title:
        facts.insert(0, f"OFFICIAL_HEADLINE: {title[:250]}")
        # The first article paragraph is evidence of what the company said, not independent verification.
        positions = [match.start() for match in re.finditer(re.escape(title), text[:30000], re.I)]
        if positions:
            excerpt = re.sub(r"\s+", " ", text[positions[-1] + len(title):positions[-1] + len(title) + 700]).strip()
            if excerpt:
                facts.append(f"OFFICIAL_SOURCE_EXCERPT: {excerpt[:550]}")
    elif kind == "FINANCIAL_STATEMENT_EXHIBIT":
        facts.append(f"SEC_EXHIBIT_EXCERPT: {re.sub(r'\s+', ' ', text[:300]).strip()}")
    result["verified_facts"] = facts[:6]
    if not facts and kind in {"EARNINGS", "GUIDANCE", "OFFICIAL_RELEASE", "EARNINGS_SCHEDULE"}:
        result["materiality"] = "LOW"
    if kind == "FINANCIAL_STATEMENT_EXHIBIT":
        result["materiality"] = "LOW"
        result["extraction_confidence"] = "LOW"
    return result


class DocumentClient:
    def __init__(self, db, user_agent, cfg, opener=None, sleeper=time.sleep, audit=None):
        self.db, self.user_agent, self.cfg = db, user_agent, cfg
        self.opener, self.sleeper = opener or urllib.request.urlopen, sleeper
        self.requests = self.cache_hits = 0
        self.last_request = 0.0
        self.failures = []
        self.audit = audit or RequestAudit(enabled=opener is None)

    def get(self, ticker, url, source_type, *, form=None, accession=None, published_at=None,
            max_age_hours=None):
        cached = self.db.execute("SELECT body_zlib,retrieved_at FROM evidence_source_documents WHERE url=?", (url,)).fetchone()
        if cached:
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(cached[1])).total_seconds() / 3600
            if max_age_hours is None or age < max_age_hours:
                self.cache_hits += 1
                return zlib.decompress(cached[0])
        host = (urllib.parse.urlparse(url).hostname or "").lower()
        if urllib.parse.urlparse(url).scheme != "https" or host not in {"www.sec.gov", "data.sec.gov"} and \
                host not in self.cfg.get("verified_ir_hosts", {}).get(ticker, []):
            self._error(ticker, source_type, url, "UNVERIFIED_SOURCE")
            return None
        for attempt, delay in enumerate((0, *self.cfg["retry_seconds"])):
            if delay:
                self.sleeper(delay)
            spacing = self.cfg["minimum_request_spacing_seconds"] - (time.monotonic() - self.last_request)
            if spacing > 0:
                self.sleeper(spacing)
            request = urllib.request.Request(url, headers={"User-Agent": self.user_agent,
                                                         "Accept": "text/html,application/json,application/xml"})
            self.requests += 1
            self.last_request = time.monotonic()
            started = time.monotonic()
            failure, status, size = None, None, None
            try:
                with self.opener(request, timeout=self.cfg["timeout_seconds"]) as response:
                    status = getattr(response, "status", 200)
                    final_host = (urllib.parse.urlparse(response.geturl()).hostname or "").lower()
                    if final_host != host:
                        raise ValueError("unexpected redirect host")
                    body = response.read(self.cfg["max_document_bytes"] + 1)
                    size = len(body)
                    if len(body) > self.cfg["max_document_bytes"]:
                        raise ValueError("document exceeds size limit")
                    content_type = response.headers.get("Content-Type", "")
                with self.db:
                    self.db.execute("""INSERT INTO evidence_source_documents
                        (url,ticker,source_type,form,accession,published_at,retrieved_at,content_type,body_zlib,sha256)
                        VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(url) DO UPDATE SET
                        retrieved_at=excluded.retrieved_at,body_zlib=excluded.body_zlib,
                        sha256=excluded.sha256,content_type=excluded.content_type""",
                        (url, ticker, source_type, form, accession, published_at, utc_now(), content_type,
                         zlib.compress(body), hashlib.sha256(body).hexdigest()))
                return body
            except urllib.error.HTTPError as exc:
                failure, status = exc, exc.code
                kind = "HTTP_429" if exc.code == 429 else "HTTP_5XX" if exc.code >= 500 else f"HTTP_{exc.code}"
                retry = exc.code == 429 or exc.code >= 500
                exc.close()
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                failure = exc
                kind, retry = type(exc).__name__, True
            except ValueError as exc:
                failure = exc
                kind, retry = str(exc), False
            finally:
                retrying = failure is not None and attempt < len(self.cfg["retry_seconds"]) and (
                    not isinstance(failure, (ValueError, urllib.error.HTTPError)) or
                    isinstance(failure, urllib.error.HTTPError) and (status == 429 or status >= 500))
                self.audit.record(source_type, url, attempt, started, error=failure, status=status,
                    response_bytes=size,
                    retry_delay_seconds=self.cfg["retry_seconds"][attempt] if retrying else 0)
            if not retry or attempt == len(self.cfg["retry_seconds"]):
                self._error(ticker, source_type, url, kind)
                return None
        return None

    def _error(self, ticker, source, url, kind):
        row = self.db.execute("SELECT retrieved_at FROM evidence_source_documents WHERE url=?", (url,)).fetchone()
        with self.db:
            self.db.execute("""INSERT INTO evidence_source_errors
                (ticker,source,request_url,error_class,occurred_at,last_success_at)
                VALUES(?,?,?,?,?,?)""", (ticker, source, url, kind, utc_now(), row[0] if row else None))
        self.failures.append({"source": source, "url": url, "error_class": kind,
                              "last_success_at": row[0] if row else None})


def merge_events(events, ticker):
    merged = {}
    for entry in events:
        kind = entry["event_type"]
        group_kind = "EARNINGS" if kind in {"EARNINGS", "GUIDANCE"} else kind
        identity = "" if group_kind == "EARNINGS" else ":" + (
            entry.get("accession") or re.sub(r"\W+", "-", entry["section_title"].lower())[:80])
        group = f"{ticker}:{entry['date']}:{group_kind}{identity}"
        entry = {**entry, "duplicate_group": group}
        if group not in merged:
            merged[group] = entry
            continue
        old = merged[group]
        sources = old["sources"] + [s for s in entry["sources"] if s not in old["sources"]]
        facts = old["verified_facts"] + [s for s in entry["verified_facts"] if s not in old["verified_facts"]]
        winner = max((old, entry), key=lambda e: (LABEL_ORDER[e["materiality"]],
                                                  e["extraction_confidence"] != "LOW"))
        merged[group] = {**winner, "sources": sources, "verified_facts": facts[:8]}
    return sorted(merged.values(), key=lambda e: (-LABEL_ORDER[e["materiality"]], e["date"], e["event_type"]))


def coverage(events, sec_content, ir_available, sec_partial=False, ir_partial=False):
    def grade(yes, partial=False):
        return "AVAILABLE" if yes else "PARTIAL" if partial else "UNAVAILABLE"
    values = {"SEC_CONTENT": grade(sec_content, sec_partial),
              "IR": grade(ir_available and not ir_partial, ir_partial),
              "EARNINGS": grade(any(e["event_type"] == "EARNINGS" and e["verified_facts"] for e in events)),
              "GUIDANCE": grade(any("MANAGEMENT_GUIDANCE" in fact for e in events
                                    for fact in e["verified_facts"])),
              "MATERIAL_EVENTS": grade(any(e["materiality"] in {"HIGH", "MATERIAL"} and
                                           e["verified_facts"] for e in events))}
    pct = round(100 * sum(1 if value == "AVAILABLE" else .5 if value == "PARTIAL" else 0
                          for value in values.values()) / len(values))
    confidence = "HIGH" if pct >= 80 else "MEDIUM" if pct >= 60 else "LOW" if pct >= 40 else "INSUFFICIENT"
    if not ir_available and confidence == "HIGH":
        confidence = "MEDIUM"
    return values, pct, confidence


def packet_for_evidence(ticker, cutoff, events, source_coverage, pct, confidence, failures, cfg):
    important = [e for e in events if e["materiality"] in {"MATERIAL", "HIGH"}]
    lower = [e for e in events if e["materiality"] not in {"MATERIAL", "HIGH"}]
    packet = {"ticker": ticker, "evidence_asof": cutoff, "coverage": source_coverage,
              "coverage_pct": pct, "confidence": confidence,
              "recent_material_events": important, "other_events": lower,
              "recent_earnings": [e["duplicate_group"] for e in events if e["event_type"] == "EARNINGS"],
              "guidance": [e["duplicate_group"] for e in events if e["event_type"] == "GUIDANCE" or any(
                  "MANAGEMENT_GUIDANCE" in f for f in e["verified_facts"])],
              "material_sec_events": [e["duplicate_group"] for e in important if
                                      e["source_type"].startswith("SEC")],
              "key_risk_evidence": [e["duplicate_group"] for e in events if e["event_type"] in {
                  "RESTATEMENT", "AUDITOR_CHANGE", "RISK_FACTORS", "GOING_CONCERN", "LEGAL_PROCEEDINGS"}],
              "unresolved": failures}
    # Keep all material events; trim only lower-value entries from the model-facing packet.
    while len(compact(packet).encode()) > 4 * cfg["packet_target_tokens"] and packet["other_events"]:
        packet["other_events"].pop()
    return packet


def ir_feed_entries(raw):
    root = ET.fromstring(raw)
    entries = []
    for item in root.findall(".//item"):
        title = item.findtext("title") or ""
        published = item.findtext("pubDate") or ""
        try:
            parsed = parsedate_to_datetime(published)
            if parsed.tzinfo is None:
                continue
            stamp = parsed.astimezone(timezone.utc).isoformat()
        except (TypeError, ValueError):
            continue
        kind, _ = headline_category(title)
        entries.append({"title": title, "published_at": stamp,
                        "url": item.findtext("link") or "", "kind": kind})
    return entries


def select_ir_entries(entries, cutoff, cfg):
    """Filter by availability, deduplicate URLs, then prioritize dated material news."""
    day = datetime.fromisoformat(cutoff.replace("Z", "+00:00")).date()
    unique = {}
    for entry in entries:
        if not accepted_before(entry["published_at"], cutoff):
            continue
        if (day - datetime.fromisoformat(entry["published_at"]).date()).days > cfg["window_days"]:
            continue
        unique.setdefault(entry["url"], entry)
    def priority(entry):
        label = cfg["materiality_by_event"].get(entry["kind"], "LOW")
        headline_priority = headline_category(entry["title"])[1]
        if re.search(r"(?i)auditor|restatement|fraud|regulatory|offering|financing|major contract", entry["title"]):
            headline_priority = 5
        return max(LABEL_ORDER[label] + 2, headline_priority), entry["published_at"]
    return sorted(unique.values(), key=priority, reverse=True)


def verified_ir_config(ticker, company_name, cfg, cik=None):
    entry = cfg.get("verified_ir", {}).get(ticker)
    if not entry:
        return None
    if entry.get("cik") and (cik is None or str(cik).zfill(10) != entry["cik"]):
        return None
    def words(value):
        return {word for word in re.findall(r"[a-z0-9]+", value.lower())
                if word not in {"inc", "corp", "corporation", "company", "platforms", "limited", "the"}}
    expected, actual = words(entry.get("company_name", "")), words(company_name)
    if not expected or not actual or not expected & actual:
        return None
    hosts = entry.get("hosts", [])
    if not hosts or not all(re.fullmatch(r"[a-z0-9.-]+", host) for host in hosts):
        return None
    return entry


def collect_ticker(market, fundamental, research, ticker, cutoff, cfg, user_agent, opener=None,
                   sleeper=time.sleep, historical_asof=False):
    """Collect only this ticker; failures are returned, never converted to mock evidence."""
    started = time.monotonic()
    security = market.execute("SELECT cik,company_name FROM securities WHERE ticker=?", (ticker,)).fetchone()
    if not security or not security[0]:
        raise ValueError(f"SEC identity unavailable for {ticker}")
    cik, company_name = str(int(security[0])).zfill(10), security[1]
    ir = verified_ir_config(ticker, company_name, cfg, cik)
    runtime_cfg = {**cfg, "verified_ir_hosts": {ticker: ir["hosts"] if ir else []}}
    client = DocumentClient(research, user_agent, runtime_cfg, opener=opener, sleeper=sleeper)
    submissions_url = f"https://data.sec.gov/submissions/CIK{cik}.json"
    raw = client.get(ticker, submissions_url, "SEC_SUBMISSIONS",
                     max_age_hours=0 if cfg.get("intraday_force_refresh") else 12)
    stale_submissions = False
    if raw is None:
        old = fundamental.execute("""SELECT payload FROM sec_cache
            WHERE cik=? AND endpoint='submissions' AND cache_status='FRESH'""", (cik,)).fetchone()
        if old:
            raw, stale_submissions = zlib.decompress(old[0]), True
    submissions = json.loads(raw) if raw else {}
    listed = {str(s).upper().replace("-", ".") for s in submissions.get("tickers", [])}
    if ticker.upper().replace("-", ".") not in listed or int(submissions.get("cik", -1)) != int(cik):
        submissions = {}
        client.failures.append({"source": "SEC_SUBMISSIONS", "error_class": "IDENTITY_MISMATCH"})
    filings = select_filings(submissions, cutoff, cfg)
    events, filing_results = [], []
    content_count = exhibit_count = 0
    for filing in filings:
        try:
            main_url = sec_url(cik, filing["accession"], filing["primary_document"])
        except ValueError:
            client.failures.append({"source": "SEC_CONTENT", "error_class": "INVALID_DOCUMENT_PATH",
                                    "accession": filing["accession"]})
            continue
        body = client.get(ticker, main_url, "SEC_CONTENT", form=filing["form"],
                          accession=filing["accession"], published_at=filing["accepted_at"])
        found = []
        if body:
            content_count += 1
            text = document_text(body)
            found = extract_8k_items(text, filing, main_url, cfg) if filing["form"].startswith("8-K") else (
                extract_periodic(text, filing, main_url, cfg))
            events.extend(found)
        filing_results.append({**filing, "primary_url": main_url, "content_status": "AVAILABLE" if body else "UNAVAILABLE",
                               "extracted_event_count": len(found)})
        if not filing["form"].startswith("8-K"):
            continue
        index_url = sec_url(cik, filing["accession"], filing["accession"] + "-index.html")
        index_body = client.get(ticker, index_url, "SEC_INDEX", form=filing["form"],
                                accession=filing["accession"], published_at=filing["accepted_at"])
        if not index_body:
            continue
        for exhibit_type, exhibit_url in exhibit_links(index_body.decode("utf-8", "replace"), index_url)[
                :cfg["max_exhibits_per_filing"]]:
            body = client.get(ticker, exhibit_url, exhibit_type, form=filing["form"],
                              accession=filing["accession"], published_at=filing["accepted_at"])
            if body:
                exhibit_count += 1
                release = extract_release(document_text(body), filing, exhibit_url, cfg)
                release["exhibit_type"] = exhibit_type
                events.append(release)
    ir_sources = []
    ir_deferred = []
    ir_attempted = 0
    ir_seen = set()
    ir_limit = cfg.get("max_ir_releases_per_ticker", 6)
    if not isinstance(ir_limit, int) or isinstance(ir_limit, bool) or ir_limit < 1:
        raise ValueError("INVALID_IR_RELEASE_LIMIT")
    if ir:
        for feed_url in ir.get("feeds", []):
            raw = client.get(ticker, feed_url, "COMPANY_IR_FEED",
                             max_age_hours=0 if cfg.get("intraday_force_refresh") else 12)
            if not raw:
                continue
            try:
                entries = ir_feed_entries(raw)
            except ET.ParseError:
                client._error(ticker, "COMPANY_IR_FEED", feed_url, "INVALID_XML")
                continue
            for entry in select_ir_entries(entries, cutoff, cfg):
                day = entry["published_at"][:10]
                url = entry["url"]
                if url in ir_seen:
                    continue
                ir_seen.add(url)
                if (urllib.parse.urlparse(url).hostname or "").lower() not in ir["hosts"]:
                    client._error(ticker, "COMPANY_IR", url, "IR_DOMAIN_MISMATCH")
                    continue
                if ir_attempted >= ir_limit:
                    ir_deferred.append({"url": url, "title": entry["title"],
                                        "published_at": entry["published_at"],
                                        "reason": "IR_COLLECTION_LIMIT"})
                    continue
                ir_attempted += 1
                body = client.get(ticker, url, "COMPANY_IR", published_at=entry["published_at"])
                if not body:
                    continue
                snapshot = research.execute("SELECT retrieved_at FROM evidence_source_documents WHERE url=?",
                                            (url,)).fetchone()
                if not ir_snapshot_eligible(snapshot[0] if snapshot else None, cutoff, historical_asof):
                    client.failures.append({"source": "COMPANY_IR", "url": url,
                                            "error_class": "IR_SNAPSHOT_POST_ASOF"})
                    continue
                ir_sources.append(url)
                filing = {"form": "IR", "filing_date": day, "accepted_at": entry["published_at"],
                          "accession": None}
                release = extract_release(document_text(body), filing, url, cfg,
                                          source_type="COMPANY_IR", title=entry["title"])
                release["section_title"] = entry["title"][:180]
                events.append(release)
    events = merge_events(events, ticker)
    for entry in events:
        for source in entry["sources"]:
            row = research.execute("SELECT retrieved_at FROM evidence_source_documents WHERE url=?",
                                   (source["url"],)).fetchone()
            source["retrieved_at"] = row[0] if row else None
            source["source_quality"] = "OFFICIAL_SEC" if source["source_type"].startswith("SEC") else "VERIFIED_COMPANY_IR"
    values, pct, confidence = coverage(events, content_count > 0, bool(ir_sources),
                                       sec_partial=bool(filings) and not content_count or stale_submissions,
                                       ir_partial=bool(ir_deferred))
    if not ir_sources:
        client.failures.append({"source": "COMPANY_IR", "error_class": "IR_UNAVAILABLE"})
    packet = packet_for_evidence(ticker, cutoff, events, values, pct, confidence, client.failures, cfg)
    packet.update({"filings_found": filing_results, "relevant_exhibits": exhibit_count,
                   "ir_collection": {"attempted_releases": ir_attempted,
                                     "max_releases": ir_limit, "deferred": ir_deferred},
                   "ir_sources": ir_sources, "deduplicated_event_count": len(events),
                   "source_status": "STALE_SUBMISSIONS_FALLBACK" if stale_submissions else "CURRENT_OR_CACHED",
                   "omitted_low_event_count": 0})
    while len(compact(packet).encode()) > 4 * cfg["packet_target_tokens"] and packet["other_events"]:
        packet["other_events"].pop()
        packet["omitted_low_event_count"] += 1
    visible = {entry["duplicate_group"] for entry in packet["recent_material_events"] + packet["other_events"]}
    for category in ("recent_earnings", "guidance", "material_sec_events", "key_risk_evidence"):
        packet[category] = [group for group in packet[category] if group in visible]
    packet["omitted_low_event_count"] = len(events) - len(visible)
    source_hash = hashlib.sha256(compact([(e["duplicate_group"], e["sources"]) for e in events]).encode()).hexdigest()
    with research:
        research.executemany("""INSERT OR REPLACE INTO evidence_events_v2
            (ticker,evidence_asof,duplicate_group,event_json,created_at) VALUES(?,?,?,?,?)""",
            [(ticker, cutoff, entry["duplicate_group"], compact(entry), utc_now()) for entry in events])
        research.execute("""INSERT OR REPLACE INTO evidence_packets_v2 VALUES(?,?,?,?,?,?,?)""",
                         (ticker, cutoff, source_hash, compact(packet), pct, confidence, utc_now()))
    return {"ticker": ticker, "packet": packet, "coverage_pct": pct, "confidence": confidence,
            "estimated_packet_tokens": (len(compact(packet).encode()) + 3) // 4,
            "network_requests": client.requests, "cache_hits": client.cache_hits,
            "runtime_seconds": round(time.monotonic() - started, 2), "network_failures": client.failures}

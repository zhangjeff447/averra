#!/usr/bin/env python3
"""Enrich Massive's CS master using Nasdaq's public stock-screener JSON feed."""
from __future__ import annotations

import argparse
import json
import sqlite3
import time
import urllib.error
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from quant_screen import ROOT, init_db


URL = "https://api.nasdaq.com/api/screener/stocks?tableonly=true&limit=10000&download=true"
SECTOR_NAMES = {
    "Technology": "Technology", "Finance": "Financials", "Health Care": "Healthcare",
    "Industrials": "Industrials", "Consumer Discretionary": "Consumer Discretionary",
    "Consumer Staples": "Consumer Staples", "Energy": "Energy", "Utilities": "Utilities",
    "Basic Materials": "Materials", "Real Estate": "Real Estate",
    "Telecommunications": "Communication Services",
}


def fetch_rows(opener=None, sleeper=time.sleep, metrics=None):
    opener = opener or urllib.request.urlopen
    for attempt, delay in enumerate((0, 5, 15, 30)):
        if delay:
            sleeper(delay)
        try:
            req = urllib.request.Request(URL, headers={"Accept": "application/json", "User-Agent": "Mozilla/5.0",
                                                       "Origin": "https://www.nasdaq.com"})
            if metrics is not None:
                metrics["api_requests"] = metrics.get("api_requests", 0) + 1
            with opener(req, timeout=30) as response:
                payload = json.load(response)
            rows = payload.get("data", {}).get("rows") or []
            if len(rows) < 3000:
                raise ValueError("Nasdaq stock-screener response unexpectedly small")
            return rows
        except (urllib.error.URLError, TimeoutError) as exc:
            if attempt == 3:
                raise RuntimeError("SECTOR_METADATA_FAILED") from exc
    raise AssertionError("unreachable")


def enrich(db, rows, config):
    by_symbol = {row["symbol"].replace("/", "."): row for row in rows if row.get("symbol")}
    now = datetime.now(timezone.utc).isoformat()
    changes = []
    counts = Counter()
    for row in db.execute("SELECT ticker FROM securities WHERE source='MASSIVE'"):
        symbol = row[0]
        item = by_symbol.get(symbol)
        if not item:
            counts["unmatched"] += 1
            changes.append(("UNKNOWN", None, None, None, None, "NASDAQ_SCREENER", now, symbol))
            continue
        country = item.get("country") or "UNKNOWN"
        verified = country == "United States"
        sector = SECTOR_NAMES.get(item.get("sector")) if verified else None
        industry = (item.get("industry") or None) if verified else None
        if verified:
            counts["us_verified"] += 1
        else:
            counts["foreign_or_unknown"] += 1
        if sector:
            counts["sector_classified"] += 1
        else:
            counts["sector_unknown"] += 1
        changes.append(("US" if verified else "NON_US", country, sector, industry,
                        config["sector_to_etf"].get(sector), "NASDAQ_SCREENER", now, symbol))
    with db:
        db.executemany("""UPDATE securities SET country=?,issuer_country=?,sector=?,industry=?,sector_etf=?,
            sector_source=?,updated_at=? WHERE ticker=?""", changes)
    return dict(counts)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=ROOT / "data" / "market_data.db")
    parser.add_argument("--config", type=Path, default=ROOT / "real_market_config.json")
    args = parser.parse_args()
    cfg = json.loads(args.config.read_text())
    rows = fetch_rows()
    with sqlite3.connect(args.db) as db:
        init_db(db)
        stats = enrich(db, rows, cfg)
    print(json.dumps({"source": "NASDAQ_SCREENER", "downloaded_rows": len(rows), **stats}, indent=2))


if __name__ == "__main__":
    main()

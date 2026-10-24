#!/usr/bin/env python3
"""Offline US common-stock universe and daily OHLCV quant screen. No AI or network."""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
import sqlite3
from collections import Counter
from datetime import date
from pathlib import Path

from analysis import indicators, security_ok


ROOT = Path(__file__).resolve().parent
MASTER_FIELDS = ("ticker", "asset_type", "country", "exchange", "is_etf", "is_otc", "is_preferred", "is_warrant")
BAR_FIELDS = ("ticker", "date", "open", "high", "low", "close", "volume")
TICKER = re.compile(r"^[A-Z][A-Z0-9.]{0,9}$")
UPSERT_BAR = """INSERT INTO daily_bars(ticker,date,open,high,low,close,volume)
VALUES(?,?,?,?,?,?,?) ON CONFLICT(ticker,date) DO UPDATE SET
open=excluded.open, high=excluded.high, low=excluded.low,
close=excluded.close, volume=excluded.volume"""


def init_db(db):
    db.executescript((ROOT / "market_schema.sql").read_text())
    upgrades = {
        "securities": {
            "is_active": "INTEGER NOT NULL DEFAULT 1",
            "sector_etf": "TEXT", "company_name": "TEXT", "security_type": "TEXT",
            "sector": "TEXT", "industry": "TEXT", "cik": "TEXT",
            "sic_code": "TEXT", "source": "TEXT", "updated_at": "TEXT",
            "last_trading_date": "TEXT", "data_quality_status": "TEXT",
            "issuer_country": "TEXT", "sector_source": "TEXT",
            "raw_sector": "TEXT", "normalized_sector": "TEXT",
            "mapped_sector_etf": "TEXT", "mapping_confidence": "TEXT",
            "mapping_source": "TEXT",
        },
        "daily_bars": {
            "adjusted_close": "REAL", "source": "TEXT", "updated_at": "TEXT",
            "quality_status": "TEXT",
        },
    }
    for table, additions in upgrades.items():
        columns = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
        for name, kind in additions.items():
            if name not in columns:
                db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {kind}")


def csv_rows(path, required):
    with Path(path).open(newline="", encoding="utf-8-sig") as stream:
        reader = csv.DictReader(stream)
        missing = set(required) - set(reader.fieldnames or ())
        if missing:
            raise ValueError(f"{path}: missing columns {', '.join(sorted(missing))}")
        for number, row in enumerate(reader, 2):
            if None in row:
                raise ValueError(f"{path}:{number}: extra CSV columns")
            yield number, row


def flag(value):
    value = (value or "").strip().lower()
    if value not in {"0", "1", "false", "true", "no", "yes"}:
        raise ValueError("classification flags must be explicit 0/1, false/true, or no/yes")
    return int(value in {"1", "true", "yes"})


def ticker(value):
    value = (value or "").strip().upper()
    if not TICKER.fullmatch(value):
        raise ValueError(f"invalid ticker {value!r}")
    return value


def import_master(db, path):
    records = []
    seen = set()
    for number, row in csv_rows(path, MASTER_FIELDS):
        try:
            symbol = ticker(row["ticker"])
            if symbol in seen:
                raise ValueError(f"duplicate ticker {symbol}")
            seen.add(symbol)
            sector_etf = (row.get("sector_etf") or "").strip().upper() or None
            if sector_etf:
                ticker(sector_etf)
            records.append((symbol, row["asset_type"].strip().upper(), row["country"].strip().upper(),
                            row["exchange"].strip().upper(), *(flag(row[name]) for name in MASTER_FIELDS[4:]), sector_etf))
        except (AttributeError, ValueError) as exc:
            raise ValueError(f"{path}:{number}: {exc}") from exc
    if not records:
        raise ValueError(f"{path}: empty security master")
    with db:
        db.execute("DELETE FROM securities")
        db.executemany("INSERT INTO securities(ticker,asset_type,country,exchange,is_etf,is_otc,is_preferred,is_warrant,sector_etf) VALUES(?,?,?,?,?,?,?,?,?)", records)
    return len(records)


def parse_bar(row):
    symbol = ticker(row["ticker"])
    day = date.fromisoformat(row["date"].strip()).isoformat()
    prices = [float(row[key]) for key in ("open", "high", "low", "close")]
    volume = int(row["volume"])
    if not all(math.isfinite(p) and p > 0 for p in prices) or volume < 0:
        raise ValueError("OHLC must be finite and positive; volume must be nonnegative")
    opened, high, low, close = prices
    if low > min(opened, close) or high < max(opened, close) or high < low:
        raise ValueError("inconsistent OHLC range")
    return symbol, day, opened, high, low, close, volume


def import_bars(db, path):
    count = 0
    batch = []
    with db:
        for number, row in csv_rows(path, BAR_FIELDS):
            try:
                batch.append(parse_bar(row))
            except (TypeError, ValueError, AttributeError) as exc:
                raise ValueError(f"{path}:{number}: {exc}") from exc
            count += 1
            if len(batch) == 1000:
                db.executemany(UPSERT_BAR, batch)
                batch.clear()
        if batch:
            db.executemany(UPSERT_BAR, batch)
    if not count:
        raise ValueError(f"{path}: empty daily OHLCV file")
    return count


def rejection_reason(stock):
    if "is_active" in stock.keys() and not stock["is_active"]:
        return "inactive_security"
    if stock["asset_type"] != "COMMON_STOCK":
        return "not_common_stock"
    if stock["country"] != "US":
        return "not_us"
    if stock["exchange"] not in {"NYSE", "NASDAQ", "AMEX"}:
        return "unsupported_exchange"
    for name in ("is_etf", "is_otc", "is_preferred", "is_warrant"):
        if stock[name]:
            return name
    return None


def quant_rank(metrics):
    momentum = max(-50, min(35, metrics["return20"] * 100 + metrics["return60"] * 50))
    trend = 5 * (metrics["close"] > metrics["sma20"]) + 5 * (metrics["sma20"] > metrics["sma50"])
    trend += 5 * (metrics["sma50"] > metrics["sma200"])
    participation = max(0, min(5, (metrics["rvol"] - 1) * 5))
    return round(max(0, min(100, 50 + momentum + trend + participation)), 2)


def screen(db, asof, top=100, min_price=3, min_dollar_volume=2_000_000):
    asof = date.fromisoformat(asof).isoformat()
    if top < 1:
        raise ValueError("top must be positive")
    rejects = Counter()
    candidates = []
    master_count = 0
    common_count = 0
    for stock in db.execute("SELECT * FROM securities ORDER BY ticker"):
        master_count += 1
        reason = rejection_reason(stock)
        if reason:
            rejects[reason] += 1
            continue
        if not security_ok(dict(stock)):
            raise AssertionError("universe classification mismatch")
        common_count += 1
        rows = db.execute("SELECT date,open,high,low,close,volume FROM daily_bars WHERE ticker=? AND date<=? ORDER BY date DESC LIMIT 200",
                          (stock["ticker"], asof)).fetchall()
        if len(rows) < 200 or rows[0]["date"] != asof:
            rejects["missing_200_current_bars"] += 1
            continue
        bars = [dict(row) for row in reversed(rows)]
        average_dollar_volume = sum(bar["close"] * bar["volume"] for bar in bars[-20:]) / 20
        if bars[-1]["close"] < min_price or bars[-1]["volume"] == 0 or average_dollar_volume < min_dollar_volume:
            rejects["price_or_liquidity"] += 1
            continue
        try:
            metrics = indicators(bars)
        except (ValueError, ZeroDivisionError, OverflowError, TypeError):
            rejects["indicator_error"] += 1
            continue
        candidates.append({"ticker": stock["ticker"], "exchange": stock["exchange"],
                           "quant_rank": quant_rank(metrics), "close": metrics["close"],
                           "avg_dollar_volume_20d": round(average_dollar_volume, 2),
                           "return20": round(metrics["return20"], 6), "return60": round(metrics["return60"], 6),
                           "rvol": round(metrics["rvol"], 3), "sma20": round(metrics["sma20"], 4),
                           "sma50": round(metrics["sma50"], 4), "sma200": round(metrics["sma200"], 4),
                           "rsi14": round(metrics["rsi"], 2), "atr14": round(metrics["atr14"], 4)})
    if not master_count:
        raise ValueError("No security master loaded; import a complete master before screening")
    candidates.sort(key=lambda row: (-row["quant_rank"], -row["avg_dollar_volume_20d"], row["ticker"]))
    return {"asof": asof, "method": "offline_daily_ohlcv_v1", "scope": "supplied_master_only",
            "master_count": master_count,
            "us_common_listed_count": common_count, "screened_count": len(candidates),
            "rejected": dict(sorted(rejects.items())), "top": top, "candidates": candidates[:top]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--master", type=Path, help="Complete security-master CSV; replaces previous master")
    parser.add_argument("--bars", type=Path, help="Historical or incremental daily OHLCV CSV")
    parser.add_argument("--asof", required=True, help="Trading date YYYY-MM-DD")
    parser.add_argument("--db", type=Path, default=ROOT / "data" / "market_data.db")
    parser.add_argument("--output", type=Path, default=ROOT / "data" / "top100.json")
    parser.add_argument("--top", type=int, default=100)
    args = parser.parse_args()
    args.db.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(args.db) as db:
        db.row_factory = sqlite3.Row
        init_db(db)
        imported_master = import_master(db, args.master) if args.master else 0
        imported_bars = import_bars(db, args.bars) if args.bars else 0
        result = screen(db, args.asof, args.top)
    result["imported_master_rows"] = imported_master
    result["imported_bar_rows"] = imported_bars
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_name(args.output.name + ".tmp")
    temporary.write_text(json.dumps(result, indent=2) + "\n")
    os.replace(temporary, args.output)
    print(f"{result['asof']}: {result['us_common_listed_count']} US common listed; "
          f"{result['screened_count']} passed daily data/liquidity; "
          f"{len(result['candidates'])} saved to {args.output}")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Create an explicitly synthetic Phase 1 database for Phase 2 integration checks."""
import argparse
import sqlite3
from pathlib import Path

from mock_data import bars
from quant_screen import ROOT, init_db, screen


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=ROOT / "data" / "phase2_mock.db")
    args = parser.parse_args()
    if args.db.exists():
        parser.error(f"Refusing to overwrite existing file: {args.db}")
    args.db.parent.mkdir(parents=True, exist_ok=True)
    series = {
        "SPY": bars(400, 0.0005), "QQQ": bars(350, 0.0007, 0.3),
        "XLK": bars(180, 0.0008, 0.5), "XLF": bars(35, 0.0004, 1.0),
    }
    with sqlite3.connect(args.db) as db:
        db.row_factory = sqlite3.Row
        init_db(db)
        for n in range(120):
            ticker = f"MOCKQ{n + 1:03d}"
            sector = "XLK" if n % 2 else "XLF"
            series[ticker] = bars(20 + n * 1.5, 0.0002 + n * 0.000014, n * 0.23,
                                  volume=600_000 + n * 8_000)
            db.execute("INSERT INTO securities(ticker,asset_type,country,exchange,is_etf,is_otc,is_preferred,is_warrant,sector_etf) VALUES(?,?,?,?,?,?,?,?,?)",
                       (ticker, "COMMON_STOCK", "US", "NASDAQ" if sector == "XLK" else "NYSE",
                        0, 0, 0, 0, sector))
        for symbol, values in series.items():
            db.executemany("INSERT INTO daily_bars(ticker,date,open,high,low,close,volume) VALUES(?,?,?,?,?,?,?)",
                           [(symbol, b["date"], b["open"], b["high"], b["low"], b["close"], b["volume"])
                            for b in values])
        db.commit()
        asof = series["SPY"][-1]["date"]
        phase1 = screen(db, asof, top=100)
    print(f"SYNTHETIC ONLY | asof={asof} | Phase 1 pass={phase1['screened_count']} | database={args.db}")


if __name__ == "__main__":
    main()

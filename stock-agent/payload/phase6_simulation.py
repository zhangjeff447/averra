#!/usr/bin/env python3
"""Disposable ticker-only holdings scenario; no API, broker or production DB."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from portfolio import Portfolio
from stock_agent import command


def run():
    with tempfile.TemporaryDirectory(prefix="averra-holdings-") as temp:
        book = Portfolio(Path(temp) / "simulation.db")
        try:
            snap = {"stocks": [{"ticker": "AMD", "asset_type": "COMMON_STOCK", "country": "US",
                                "exchange": "NASDAQ", "is_active": True}]}
            bought = command(book, "/bought AMD", snap, update_id=1, source="SIMULATION")
            tracked_after_buy = [r[0] for r in book.db.execute(
                "SELECT ticker FROM tracked_holdings WHERE status='HELD'")]
            sold = command(book, "/sold AMD", snap, update_id=2, source="SIMULATION")
            return {"buy": bought, "tracked_after_buy": tracked_after_buy, "sell": sold,
                    "holding_events": book.db.execute("SELECT COUNT(*) FROM holding_events").fetchone()[0],
                    "tracked_after_sell": book.db.execute(
                        "SELECT COUNT(*) FROM tracked_holdings WHERE status='HELD'").fetchone()[0],
                    "simulation_database_deleted_on_exit": True,
                    "api_calls": 0, "ai_tokens": 0, "broker_connections": 0}
        finally:
            book.close()


if __name__ == "__main__":
    print(json.dumps(run(), indent=2))

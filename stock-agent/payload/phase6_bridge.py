"""Read frozen Phase 5/2 SQLite outputs into Phase 6; never refresh upstream data."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


def _readonly(path):
    return sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)


def from_entry_databases(entry_path, market_path):
    entry = _readonly(entry_path)
    market = _readonly(market_path)
    try:
        run = entry.execute("SELECT id,asof FROM entry_runs WHERE status='COMPLETE' ORDER BY id DESC LIMIT 1").fetchone()
        if not run:
            raise ValueError("No completed Phase 5 run")
        run_id, asof = run
        quant_run = market.execute("SELECT id FROM quant_runs WHERE asof<=? ORDER BY asof DESC,id DESC LIMIT 1", (asof,)).fetchone()
        if not quant_run:
            raise ValueError("No point-in-time Phase 2 risk scores")
        rows = entry.execute("""SELECT ticker,entry_price,entry_score,entry_state,chase_risk,regime,
            timeframes_json,research_reference_json FROM entry_decisions WHERE run_id=?""", (run_id,)).fetchall()
        price_at = datetime.fromisoformat(asof + "T16:00:00").replace(tzinfo=ZoneInfo("America/New_York")).isoformat()
        candidates = []
        daily_returns = {}
        for ticker, price, score, state, chase, regime, frames_json, research_json in rows:
            research = json.loads(research_json)
            if research.get("research_status") != "COMPLETE":
                continue
            security = market.execute("""SELECT asset_type,country,exchange,is_etf,is_otc,is_preferred,is_warrant
                FROM securities WHERE ticker=?""", (ticker,)).fetchone()
            risk_row = market.execute("SELECT risk_score,features_json FROM quant_scores WHERE run_id=? AND ticker=?",
                                      (quant_run[0], ticker)).fetchone()
            if not security or not risk_row or not price or not research.get("sector"):
                continue
            asset_type, country, exchange, is_etf, is_otc, is_preferred, is_warrant = security
            if asset_type != "COMMON_STOCK" or country != "US" or exchange not in {"NYSE", "NASDAQ", "AMEX"} or any((is_etf, is_otc, is_preferred, is_warrant)):
                continue
            features = json.loads(risk_row[1])
            frames = json.loads(frames_json)
            daily = frames.get("daily") or {}
            bars = market.execute("SELECT date,close FROM daily_bars WHERE ticker=? AND date<=? ORDER BY date DESC LIMIT 60",
                                  (ticker, asof)).fetchall()[::-1]
            daily_returns[ticker] = {date: close / previous - 1 for (date, close), (_, previous) in zip(bars[1:], bars[:-1]) if previous > 0}
            candidates.append({"ticker": ticker, "sector": research["sector"], "asset_type": asset_type,
                               "country": country, "exchange": exchange, "is_etf": bool(is_etf),
                               "is_otc": bool(is_otc), "is_preferred": bool(is_preferred),
                               "is_warrant": bool(is_warrant), "conviction": research.get("conviction_score"),
                               "entry": score, "risk_score": risk_row[0], "state": state,
                               "chase_risk": chase, "regime": regime, "price": price, "price_at": price_at,
                               "atr_pct": (daily.get("atr_pct") or 0) * 100,
                               "realized_vol_pct": (features.get("realized_vol20") or 0) * 100,
                               "drawdown_pct": abs(features.get("max_drawdown120") or 0) * 100,
                               "source_entry_run_id": run_id, "simulation": False})
        if not candidates:
            raise ValueError("No validated Phase 5 common-stock candidates")
        common_dates = set.intersection(*(set(daily_returns[c["ticker"]]) for c in candidates))
        for c in candidates:
            c["returns"] = [daily_returns[c["ticker"]][d] for d in sorted(common_dates)[-40:]]
        benchmarks = {}
        for symbol in ("SPY", "QQQ"):
            row = market.execute("SELECT close FROM daily_bars WHERE ticker=? AND date=?", (symbol, asof)).fetchone()
            benchmarks[symbol.lower() + "_close"] = row[0] if row else None
        return {"asof": price_at, "candidates": candidates, **benchmarks}
    finally:
        entry.close()
        market.close()

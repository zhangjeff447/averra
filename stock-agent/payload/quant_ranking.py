#!/usr/bin/env python3
"""Phase 2: deterministic, offline quant ranking over Phase 1 SQLite data."""
from __future__ import annotations

import argparse
import json
import logging
from logging.handlers import RotatingFileHandler
import math
import os
import sqlite3
import statistics
import time
from collections import Counter
from datetime import date, datetime, time as clock_time, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from analysis import ema_series, indicators
from quant_screen import ROOT, init_db, rejection_reason


LOG = logging.getLogger("quant_ranking")
COMPONENTS = ("momentum", "relative_strength", "technical", "volume", "risk")


class DataQualityError(ValueError):
    pass


def load_config(path):
    cfg = json.loads(Path(path).read_text())
    weights = cfg["component_weights"]
    if set(weights) != set(COMPONENTS) or any(w < 0 for w in weights.values()) or not math.isclose(sum(weights.values()), 1):
        raise ValueError("component weights must be nonnegative and sum to 1")
    for component in COMPONENTS:
        rules = cfg["features"][component]
        if not rules or not any(rule["enabled"] and rule["weight"] > 0 for rule in rules.values()):
            raise ValueError(f"{component} has no enabled positive-weight features")
        for name, rule in rules.items():
            if rule["weight"] < 0:
                raise ValueError(f"{name}: negative weight")
            if rule.get("mode") == "target":
                if rule["radius"] <= 0:
                    raise ValueError(f"{name}: radius must be positive")
            elif rule["low"] >= rule["high"] or rule.get("direction", "higher") not in {"higher", "lower"}:
                raise ValueError(f"{name}: invalid normalization range/direction")
    params = cfg["feature_parameters"]
    for name, value in params.items():
        if not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            raise ValueError(f"{name}: must be positive and finite")
        if name.endswith("_days") and not isinstance(value, int):
            raise ValueError(f"{name}: must be an integer")
    if params["abnormal_rvol_low"] >= params["abnormal_rvol_high"]:
        raise ValueError("abnormal RVOL limits out of order")
    return cfg


def fetch_bars(db, ticker, asof, limit=252):
    rows = db.execute("SELECT date,open,high,low,close,volume FROM daily_bars WHERE ticker=? AND date<=? ORDER BY date DESC LIMIT ?",
                      (ticker, asof, limit)).fetchall()
    return [dict(row) for row in reversed(rows)]


def validate_bars(bars, asof, cfg, minimum=252, allowed_large_moves=()):
    if len(bars) < minimum:
        raise DataQualityError("insufficient_history")
    if bars[-1]["date"] != asof:
        raise DataQualityError("stale_market_data")
    if (date.fromisoformat(asof) - date.fromisoformat(bars[0]["date"])).days > cfg["maximum_calendar_span_days"]:
        raise DataQualityError("missing_bars")
    previous = None
    for bar in bars:
        try:
            day = date.fromisoformat(bar["date"])
            values = [float(bar[k]) for k in ("open", "high", "low", "close")]
            volume = float(bar["volume"])
        except (TypeError, ValueError, KeyError) as exc:
            raise DataQualityError("invalid_ohlcv") from exc
        if not all(math.isfinite(v) and v > 0 for v in values) or not math.isfinite(volume) or volume < 0:
            raise DataQualityError("nan_inf_or_invalid_ohlcv")
        opened, high, low, close = values
        if high > cfg["maximum_price"] or low > min(opened, close) or high < max(opened, close):
            raise DataQualityError("extreme_or_invalid_price")
        if previous:
            gap = (day - previous[0]).days
            if gap <= 0 or gap > cfg["maximum_gap_days"]:
                raise DataQualityError("missing_bars")
            if abs(close / previous[1] - 1) > cfg["maximum_single_day_return"] and bar["date"] not in allowed_large_moves:
                raise DataQualityError("possible_split_or_extreme_price")
        previous = (day, close)


def returns(closes, n):
    return closes[-1] / closes[-n - 1] - 1


def moving_features(bars, params):
    i = indicators(bars)
    closes = [b["close"] for b in bars]
    e12, e26 = ema_series(closes, params["macd_fast"]), ema_series(closes, params["macd_slow"])
    macd = [a - b for a, b in zip(e12, e26)]
    signal = ema_series(macd, params["macd_signal"])
    hist = [a - b for a, b in zip(macd, signal)]
    close = closes[-1]
    period = params["macd_slope_days"]
    slope = hist[-1] - hist[-period - 1]
    acceleration = slope - (hist[-period - 1] - hist[-2 * period - 1])
    out = {
        "macd_line": macd[-1], "macd_signal": signal[-1], "macd_hist": hist[-1],
        "macd_hist_pct": hist[-1] / close, "macd_hist_slope": slope,
        "macd_hist_slope_pct": slope / close, "macd_hist_acceleration": acceleration,
        "macd_hist_acceleration_pct": acceleration / close, "macd_above_zero": int(macd[-1] > 0),
        "rsi14": i["rsi"], "kdj_k": i["kdj_k"], "kdj_d": i["kdj_d"],
        "kdj_j": i["kdj_j"], "kdj_spread": i["kdj_k"] - i["kdj_d"],
        "ema9": i["ema9"], "ema21": i["ema21"], "sma20": i["sma20"],
        "sma50": i["sma50"], "sma200": i["sma200"], "adx14": i["adx14"],
        "bollinger_upper": i["bollinger_upper"], "bollinger_lower": i["bollinger_lower"],
        "bollinger_width": (i["bollinger_upper"] - i["bollinger_lower"]) / i["sma20"],
        "atr14": i["atr14"], "atr_pct": i["atr14"] / close,
    }
    for key in ("ema9", "ema21", "sma20", "sma50", "sma200"):
        out[f"distance_{key}"] = close / out[key] - 1
    return out


def relative_features(stock, benchmarks, sector_symbol, params):
    closes = [b["close"] for b in stock]
    out = {}
    for name, symbol in (("spy", "SPY"), ("qqq", "QQQ"), ("sector", sector_symbol)):
        if not symbol or symbol not in benchmarks:
            out.update({f"rs_{name}_{n}d": None for n in (1, 5, 20, 60)})
            out[f"rs_{name}_trend"] = None
            out[f"rs_{name}_acceleration"] = None
            continue
        other = benchmarks[symbol]
        if [b["date"] for b in stock[-61:]] != [b["date"] for b in other[-61:]]:
            if name != "sector":
                raise DataQualityError("benchmark_calendar_mismatch")
            out.update({f"rs_{name}_{n}d": None for n in (1, 5, 20, 60)})
            out[f"rs_{name}_trend"] = None
            out[f"rs_{name}_acceleration"] = None
            continue
        other_closes = [b["close"] for b in other]
        for n in (1, 5, 20, 60):
            out[f"rs_{name}_{n}d"] = returns(closes, n) - returns(other_closes, n)
        out[f"rs_{name}_trend"] = out[f"rs_{name}_5d"] - out[f"rs_{name}_20d"] / params["relative_trend_divisor"]
        out[f"rs_{name}_acceleration"] = out[f"rs_{name}_1d"] - out[f"rs_{name}_5d"] / params["relative_acceleration_divisor"]
    return out


def raw_features(bars, benchmarks, sector_symbol, params):
    closes = [b["close"] for b in bars]
    volumes = [b["volume"] for b in bars]
    technical = moving_features(bars, params)
    recent_returns = [closes[j] / closes[j - 1] - 1 for j in range(1, len(closes))]
    recent_gaps = [abs(bars[j]["open"] / closes[j - 1] - 1) for j in range(1, len(bars))]
    drawdown_days = params["risk_drawdown_days"]
    running_peak = closes[-drawdown_days - 1]
    drawdown = 0.0
    for close in closes[-drawdown_days:]:
        running_peak = max(running_peak, close)
        drawdown = min(drawdown, close / running_peak - 1)
    short_days = params["risk_vol_short_days"]
    long_days = params["risk_vol_long_days"]
    annualizer = math.sqrt(params["annualization_sessions"])
    vol20 = statistics.pstdev(recent_returns[-short_days:]) * annualizer
    vol60 = statistics.pstdev(recent_returns[-long_days:]) * annualizer
    average_days = params["volume_average_days"]
    avg_volume = statistics.mean(volumes[-average_days:])
    rvol = volumes[-1] / statistics.mean(volumes[-average_days - 1:-1])
    ret1 = returns(closes, 1)
    out = {
        "open": bars[-1]["open"], "high": bars[-1]["high"], "low": bars[-1]["low"],
        "close": closes[-1], "return1": ret1, "return20": returns(closes, 20),
        "return60": returns(closes, 60), "return120": returns(closes, 120),
        "momentum_acceleration": returns(closes, 20) - returns(closes, 60) / params["momentum_acceleration_divisor"],
        "trend_persistence": sum(closes[j] > statistics.mean(closes[j - 19:j + 1])
                                 for j in range(len(closes) - params["trend_persistence_days"], len(closes))) / params["trend_persistence_days"],
        "distance_52w_high": closes[-1] / max(b["high"] for b in bars[-params["high_lookback_days"]:]) - 1,
        "trend_alignment": sum((closes[-1] > technical["ema9"], technical["ema9"] > technical["ema21"],
                                technical["ema21"] > technical["sma50"], technical["sma50"] > technical["sma200"])) / 4,
        "current_volume": volumes[-1], "average_volume20": avg_volume, "rvol": rvol,
        "volume_trend": statistics.mean(volumes[-params["volume_trend_days"]:]) / avg_volume - 1,
        "price_volume_confirmation": math.copysign(max(0, rvol - 1), ret1) if ret1 else 0,
        "breakout_volume": rvol if closes[-1] > max(b["high"] for b in bars[-params["volume_breakout_days"] - 1:-1]) else 0,
        "abnormal_volume": int(rvol > params["abnormal_rvol_high"] or rvol < params["abnormal_rvol_low"]),
        "realized_vol20": vol20, "realized_vol60": vol60,
        "max_drawdown120": drawdown,
        "downside_vol60": math.sqrt(statistics.mean(min(0, r) ** 2 for r in recent_returns[-long_days:])) * annualizer,
        "avg_abs_gap60": statistics.mean(recent_gaps[-params["risk_gap_days"]:]),
        "max_abs_gap60": max(recent_gaps[-params["risk_gap_days"]:]),
        "abnormal_volatility": vol20 / vol60 if vol60 else 0,
    }
    out.update(technical)
    out.update(relative_features(bars, benchmarks, sector_symbol, params))
    for name, value in out.items():
        if value is not None and (not isinstance(value, (int, float)) or not math.isfinite(value)):
            raise DataQualityError(f"nan_inf_feature:{name}")
    return out


def feature_score(value, rule):
    if rule.get("mode") == "target":
        return max(0, 100 * (1 - abs(value - rule["target"]) / rule["radius"]))
    normalized = max(0, min(1, (value - rule["low"]) / (rule["high"] - rule["low"])))
    if rule.get("direction") == "lower":
        normalized = 1 - normalized
    return normalized * 100


def component_score(features, rules):
    weighted = total = 0.0
    for name, rule in rules.items():
        if name not in features:
            raise ValueError(f"unknown configured feature: {name}")
        if not rule["enabled"] or rule["weight"] == 0 or features[name] is None:
            continue
        weighted += feature_score(features[name], rule) * rule["weight"]
        total += rule["weight"]
    if total == 0:
        raise DataQualityError("no_usable_scoring_features")
    return round(weighted / total, 4)


def previous_runs(db, asof):
    ids = [row["id"] for row in db.execute(
        "SELECT MAX(id) id FROM quant_runs WHERE asof<? GROUP BY asof ORDER BY asof DESC LIMIT 5", (asof,))]
    return [{row["ticker"]: dict(row) for row in db.execute(
        "SELECT ticker,rank,quant_score FROM quant_scores WHERE run_id=?", (run_id,))} for run_id in ids]


def memory_values(ticker, rank, score, previous, cfg):
    old1 = previous[0].get(ticker) if len(previous) >= 1 else None
    old2 = previous[1].get(ticker) if len(previous) >= 2 else None
    old5 = previous[4].get(ticker) if len(previous) >= 5 else None
    rank1 = old1["rank"] - rank if old1 else None
    rank5 = old5["rank"] - rank if old5 else None
    score1 = round(score - old1["quant_score"], 4) if old1 else None
    score5 = round(score - old5["quant_score"], 4) if old5 else None
    m = cfg["memory"]
    momentum = None if old1 is None else round(
        score1 * m["score_1d_weight"] + rank1 * m["rank_1d_weight"]
        + (score5 or 0) * m["score_5d_weight"] + (rank5 or 0) * m["rank_5d_weight"], 4)
    return {"rank_change_1d": rank1, "rank_change_5d": rank5,
            "score_change_1d": score1, "score_change_5d": score5,
            "ranking_momentum": momentum,
            "consecutive_improvement": bool(old1 and old2 and rank < old1["rank"] < old2["rank"]
                                            and score > old1["quant_score"] > old2["quant_score"]),
            "sudden_deterioration": bool(old1 and (rank1 <= -m["sudden_rank_drop"]
                                                 or score1 <= -m["sudden_score_drop"]))}


def scan(db, asof, cfg, *, data_mode="live", top=100, phase26_cfg=None):
    started = time.monotonic()
    asof = date.fromisoformat(asof).isoformat()
    if data_mode not in {"live", "historical", "mock"} or top < 1:
        raise ValueError("invalid data mode or top count")
    if data_mode == "live":
        et_now = datetime.now(ZoneInfo("America/New_York"))
        age = (et_now.date() - date.fromisoformat(asof)).days
        if not 0 <= age <= cfg["quality"]["maximum_live_age_days"]:
            raise DataQualityError("stale_market_data")
        if age == 0 and et_now.time() < clock_time.fromisoformat(cfg["quality"]["daily_bar_ready_time_et"]):
            raise DataQualityError("daily_bar_not_final")
    benchmarks = {}
    for symbol in ("SPY", "QQQ"):
        bars = fetch_bars(db, symbol, asof)
        validate_bars(bars, asof, cfg["quality"], minimum=252)
        benchmarks[symbol] = bars
    previous = previous_runs(db, asof)
    scored, failed, action_checks = [], [], []
    eligible_count = 0
    missing_sectors = set()
    params = cfg["feature_parameters"]
    history_needed = max(cfg["quality"]["minimum_bars"], params["high_lookback_days"],
                         params["risk_drawdown_days"] + 1, params["trend_persistence_days"] + 20,
                         params["risk_vol_long_days"] + 1, params["risk_gap_days"] + 1,
                         params["volume_average_days"] + 1, params["volume_breakout_days"] + 1,
                         2 * params["macd_slope_days"] + 1, 200)
    for stock in db.execute("SELECT * FROM securities ORDER BY ticker"):
        if rejection_reason(stock):
            continue
        eligible_count += 1
        symbol = stock["ticker"]
        try:
            bars = fetch_bars(db, symbol, asof, limit=history_needed)
            allowed_moves = set()
            if phase26_cfg and len(bars) >= history_needed and bars[-1]["date"] == asof:
                from phase26_actions import inspect_jumps
                bars, checks, allowed_moves = inspect_jumps(db, symbol, bars, phase26_cfg["corporate_actions"])
                action_checks.extend(checks)
            validate_bars(bars, asof, cfg["quality"], minimum=history_needed, allowed_large_moves=allowed_moves)
            if [b["date"] for b in bars[-252:]] != [b["date"] for b in benchmarks["SPY"][-252:]]:
                raise DataQualityError("missing_bars")
            avg_dollar_volume = statistics.mean(b["close"] * b["volume"] for b in bars[-20:])
            if (bars[-1]["close"] < cfg["quality"]["phase1_min_price"] or bars[-1]["volume"] == 0
                    or avg_dollar_volume < cfg["quality"]["phase1_min_dollar_volume"]):
                raise DataQualityError("phase1_price_or_liquidity")
            if phase26_cfg:
                from phase26_sector import sector_benchmark
                sector_symbol, sector_trace = sector_benchmark(stock, phase26_cfg["sector_mapping"])
            else:
                sector_symbol, sector_trace = stock["sector_etf"], {}
            if sector_symbol and sector_symbol not in benchmarks and sector_symbol not in missing_sectors:
                sector_bars = fetch_bars(db, sector_symbol, asof)
                try:
                    validate_bars(sector_bars, asof, cfg["quality"], minimum=61)
                    benchmarks[sector_symbol] = sector_bars
                except DataQualityError:
                    missing_sectors.add(sector_symbol)
                    LOG.warning("Sector ETF data unavailable: %s", sector_symbol)
            features = raw_features(bars, benchmarks, sector_symbol, cfg["feature_parameters"])
            features["avg_dollar_volume_20d"] = avg_dollar_volume
            features["sector_etf"] = sector_symbol
            features.update(sector_trace)
            if phase26_cfg and (sector_symbol not in benchmarks or features["rs_sector_20d"] is None):
                features["sector_rs_used"] = "NO"
                features["sector_rs_weight"] = 0.0
                features["fallback_benchmark"] = phase26_cfg["sector_mapping"]["fallback_benchmark"]
            if phase26_cfg and features["sector_rs_used"] == "YES":
                rs_rules = cfg["features"]["relative_strength"]
                total_rs_weight = sum(rule["weight"] for name, rule in rs_rules.items()
                                      if rule["enabled"] and features.get(name) is not None)
                sector_rs_weight = sum(rule["weight"] for name, rule in rs_rules.items()
                                       if name.startswith("rs_sector_") and rule["enabled"] and features.get(name) is not None)
                features["sector_rs_weight"] = round(sector_rs_weight / total_rs_weight, 6)
            components = {name: component_score(features, cfg["features"][name]) for name in COMPONENTS}
            score = round(sum(components[name] * cfg["component_weights"][name] for name in COMPONENTS), 4)
            scored.append({"ticker": symbol, "quant_score": score,
                           **{f"{name}_score": components[name] for name in COMPONENTS}, "features": features})
        except (DataQualityError, ValueError, ZeroDivisionError, OverflowError, TypeError) as exc:
            reason = str(exc) if isinstance(exc, DataQualityError) else f"calculation_error:{type(exc).__name__}"
            failed.append((symbol, reason))
            LOG.warning("%s skipped %s: %s", "DATA_QUALITY" if isinstance(exc, DataQualityError) else "CALCULATION",
                        symbol, reason)
        except Exception as exc:
            failed.append((symbol, f"unexpected:{type(exc).__name__}"))
            LOG.exception("UNEXPECTED per-stock failure for %s", symbol)
    scored.sort(key=lambda r: (-r["quant_score"], r["ticker"]))
    for rank, row in enumerate(scored, 1):
        row["rank"] = rank
        row["memory"] = memory_values(row["ticker"], rank, row["quant_score"], previous, cfg)
    def persist():
        with db:
            cur = db.execute("INSERT INTO quant_runs(scanned_at,asof,config_json,eligible_count,scored_count,failed_count,elapsed_ms,data_mode) VALUES(?,?,?,?,?,?,?,?)",
                             (datetime.now(timezone.utc).isoformat(), asof, json.dumps(cfg, sort_keys=True),
                              eligible_count, len(scored), len(failed), 0, data_mode))
            run_id = cur.lastrowid
            db.executemany("INSERT INTO quant_scores VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                           [(run_id, row["ticker"], row["rank"], row["quant_score"],
                             *(row[f"{name}_score"] for name in COMPONENTS),
                             json.dumps(row["features"], sort_keys=True), json.dumps(row["memory"], sort_keys=True))
                            for row in scored])
            db.executemany("INSERT INTO quant_failures VALUES(?,?,?)", [(run_id, ticker, reason) for ticker, reason in failed])
            elapsed_ms = round((time.monotonic() - started) * 1000)
            db.execute("UPDATE quant_runs SET elapsed_ms=? WHERE id=?", (elapsed_ms, run_id))
        return run_id, elapsed_ms
    for delay in (0, 5, 15, 30):
        if delay:
            LOG.warning("Transient SQLite lock; retry in %ss", delay)
            time.sleep(delay)
        try:
            run_id, elapsed_ms = persist()
            break
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower() and "busy" not in str(exc).lower():
                raise
    else:
        raise sqlite3.OperationalError("SQLite remained locked after retries")
    if phase26_cfg:
        from phase26_actions import persist_checks
        persist_checks(db, action_checks)
    LOG.info("run=%s asof=%s mode=%s eligible=%s scored=%s failed=%s elapsed_ms=%s",
             run_id, asof, data_mode, eligible_count, len(scored), len(failed), elapsed_ms)
    return {"run_id": run_id, "asof": asof, "scanned_at": datetime.now(timezone.utc).isoformat(),
            "data_mode": data_mode, "eligible_count": eligible_count, "scored_count": len(scored),
            "failed_count": len(failed), "failure_reasons": dict(sorted(Counter(reason for _, reason in failed).items())),
            "elapsed_ms": elapsed_ms, "top": scored[:top]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--asof", help="Ranking date YYYY-MM-DD")
    parser.add_argument("--mode", choices=("live", "historical", "mock"), default="live")
    parser.add_argument("--db", type=Path, default=ROOT / "data" / "market_data.db")
    parser.add_argument("--config", type=Path, default=ROOT / "quant_config.json")
    parser.add_argument("--output", type=Path, default=ROOT / "data" / "quant_top100.json")
    parser.add_argument("--top", type=int, default=100)
    parser.add_argument("--show", metavar="TICKER", help="Show latest saved raw features for a ticker")
    parser.add_argument("--history", metavar="TICKER", help="Show compact saved rank/score history")
    args = parser.parse_args()
    if not args.asof and not (args.show or args.history):
        parser.error("--asof required for a scan")
    if args.asof and (args.show or args.history):
        parser.error("choose a scan or a read-only lookup")
    args.db.parent.mkdir(parents=True, exist_ok=True)
    log_path = ROOT / "data" / "quant_rank.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(log_path, maxBytes=2_000_000, backupCount=2)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    LOG.addHandler(handler)
    LOG.setLevel(logging.INFO)
    with sqlite3.connect(args.db, timeout=30) as db:
        db.row_factory = sqlite3.Row
        init_db(db)
        if args.show:
            row = db.execute("SELECT s.* FROM quant_scores s JOIN quant_runs r ON r.id=s.run_id WHERE s.ticker=? ORDER BY r.asof DESC,r.id DESC LIMIT 1",
                             (args.show.upper(),)).fetchone()
            print(json.dumps({**dict(row), "features": json.loads(row["features_json"]),
                              "memory": json.loads(row["memory_json"])} if row else {}, indent=2))
            return
        if args.history:
            rows = db.execute("SELECT r.asof,s.rank,s.quant_score,s.memory_json FROM quant_scores s JOIN quant_runs r ON r.id=s.run_id WHERE s.ticker=? ORDER BY r.asof,r.id", (args.history.upper(),)).fetchall()
            print(json.dumps([{**dict(row), "memory": json.loads(row["memory_json"])} for row in rows], indent=2))
            return
        result = scan(db, args.asof, load_config(args.config), data_mode=args.mode, top=args.top)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_name(args.output.name + ".tmp")
    temporary.write_text(json.dumps(result, indent=2) + "\n")
    os.replace(temporary, args.output)
    print(f"run {result['run_id']}: {result['scored_count']}/{result['eligible_count']} scored; "
          f"{result['failed_count']} failed; {result['elapsed_ms']} ms; Top {len(result['top'])} -> {args.output}")


if __name__ == "__main__":
    main()

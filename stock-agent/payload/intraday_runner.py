#!/usr/bin/env python3
"""Independent, monitor-only intraday check. No trading API or BUY_NOW."""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import urllib.request
from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo

from intraday_calendar import calendar_ready, events_for_day, init_calendar, market_day, schedule
from intraday_monitor import (ET, IEXBars, NEGATIVE_TYPES, SEVERE_TYPES, assess,
                              daily_context, material_evidence, scoped_universe)
from intraday_state import (begin_check, daily_totals, fail_check, finish_check, init_state,
                            reserve_alert, send_reserved)
from market_regime import previous_session
from quant_screen import ROOT
from stock_agent import load_env

STATE_DB = ROOT / "data" / "intraday_monitor.db"
CONFIG = ROOT / "intraday_config.json"


def config():
    return json.loads(CONFIG.read_text())


def connect(path=STATE_DB):
    db = sqlite3.connect(path, timeout=30)
    init_state(db)
    init_calendar(db)
    return db


def restore_close_entry_plans(day):
    """Recover missing next-session references from an already completed close run.

    This is a schema/data repair, not a new Entry decision or market refresh.
    A recommendation never becomes a confirmed holding here.
    """
    from phase5_entry import init_db as init_entry, load_config as entry_config
    from price_plan import buy_plan

    entry_path = ROOT / "data" / "entry_data.db"
    if not entry_path.exists():
        raise FileNotFoundError("ENTRY_DATA_DB_MISSING")
    prior = previous_session(day).isoformat()
    folder = ROOT / "data" / "shadow_daily" / prior
    entry_file, final_file = folder / "entry.json", folder / "final.json"
    with sqlite3.connect(entry_path, timeout=30) as entry:
        init_entry(entry)
        if not entry_file.exists() or not final_file.exists():
            return 0
        saved = json.loads(entry_file.read_text())
        final = json.loads(final_file.read_text())
        if (saved.get("trading_date") != prior or saved.get("target_session") != day.isoformat()
                or final.get("status") != "PIPELINE_COMPLETE"
                or final.get("market_data_as_of") != prior
                or final.get("target_session") != day.isoformat()):
            return 0
        run_id = saved.get("run_id")
        run = entry.execute("SELECT asof,status FROM entry_runs WHERE id=?", (run_id,)).fetchone()
        if not run or run[0] != prior or run[1] != "COMPLETE":
            return 0
        thresholds = entry_config()["thresholds"]
        inserted = 0
        with entry:
            for result in saved.get("results", []):
                if result.get("entry_state") != "BUY_TOMORROW_CANDIDATE":
                    continue
                ticker = result.get("ticker")
                decision = entry.execute("SELECT entry_state FROM entry_decisions WHERE run_id=? AND ticker=?",
                                         (run_id, ticker)).fetchone()
                if not decision or decision[0] != "BUY_TOMORROW_CANDIDATE":
                    continue
                if entry.execute("SELECT 1 FROM entry_reference_plans WHERE run_id=? AND ticker=?",
                                 (run_id, ticker)).fetchone():
                    continue
                plan = result.get("buy_plan") or buy_plan(result, thresholds)
                if (not plan or plan.get("status") != "CONDITIONAL"
                        or plan.get("generated_from_trading_date") != prior
                        or plan.get("target_trading_date") != day.isoformat()):
                    continue
                plan = {**plan, "recovery_source": "SAVED_COMPLETED_CLOSE_ENTRY"}
                inserted += entry.execute(
                    "INSERT OR IGNORE INTO entry_reference_plans VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (run_id, ticker, prior, day.isoformat(), plan["reference_generated_at"],
                     plan["buy_zone_low"], plan["buy_zone_high"], plan["do_not_chase_above"],
                     plan.get("nearest_support"), plan.get("nearest_resistance"),
                     plan.get("chase_risk"), json.dumps(plan, sort_keys=True))).rowcount
        return inserted


def verified_evidence(ticker, cutoff, last_seen):
    """Fresh SEC/verified IR Phase 4A collection; errors never invent an event."""
    from phase4_2_evidence import collect_ticker
    from phase3_sec import load_user_agent

    cfg = json.loads((ROOT / "phase4_2_config.json").read_text())
    cfg["intraday_force_refresh"] = True
    with sqlite3.connect(f"file:{ROOT / 'data' / 'market_data.db'}?mode=ro", uri=True) as market, \
         sqlite3.connect(f"file:{ROOT / 'data' / 'fundamental_data.db'}?mode=ro", uri=True) as fundamental, \
         sqlite3.connect(ROOT / "data" / "research_data.db") as research:
        result = collect_ticker(market, fundamental, research, ticker, cutoff.isoformat(), cfg,
                                load_user_agent(ROOT / ".env"))
    # A failed SEC/IR refresh is not treated as absence of new material events.
    failures = result["network_failures"]
    if any(item.get("source") == "SEC_SUBMISSIONS" for item in failures):
        return [], result["network_requests"], "EVIDENCE_UNAVAILABLE"
    events = []
    for item in result["packet"]["recent_material_events"]:
        official = any(s.get("source_quality") in {"OFFICIAL_SEC", "VERIFIED_COMPANY_IR"}
                       for s in item.get("sources", []))
        kind = item.get("event_type", "")
        facts = " ".join([item.get("section_title", ""), *item.get("verified_facts", [])]).lower()
        risk_type = ("RESTATEMENT" if item.get("item") == "4.02" else
                     "AUDITOR_ISSUE" if item.get("item") == "4.01" else
                     "GUIDANCE_CUT" if re.search(r"(cut|lower|reduce|withdraw).{0,35}(guidance|outlook|forecast)", facts) else
                     "CONTRACT_TERMINATION" if re.search(r"(terminat|cancel).{0,35}(material|major).{0,25}(agreement|contract)", facts) else
                     "REGULATORY_FAILURE" if re.search(r"(fda|regulator).{0,45}(reject|denial|complete response letter)", facts) else
                     kind)
        events.append({"evidence_id": item["duplicate_group"], "materiality": item["materiality"],
                       "official": official, "event_type": risk_type,
                       "accepted_at": item.get("accepted_at"),
                       "headline": item.get("section_title", "Material company event"),
                       "source": item.get("sources", [{}])[0].get("url")})
    return material_evidence(events, last_seen), result["network_requests"], "OK" if not failures else "PARTIAL"


def prior_check_time(db, ticker, day):
    row = db.execute("SELECT recorded_at FROM intraday_observations WHERE ticker=? "
                     "ORDER BY recorded_at DESC LIMIT 1", (ticker,)).fetchone()
    if row:
        return datetime.fromisoformat(row[0])
    # On the first check, detect only events since the prior session close.
    return datetime.combine(previous_session(day), time(16), ET).astimezone(timezone.utc)


def previous_state(db, ticker, kind):
    row = db.execute("SELECT details_json FROM intraday_observations WHERE ticker=? "
                     "AND status='OK' ORDER BY recorded_at DESC LIMIT 1", (ticker,)).fetchone()
    return json.loads(row[0]).get(kind) if row else None


def benchmark_snapshot(market, bars_by_ticker, day, now, cfg):
    from intraday_monitor import valid_bars
    result = {}
    for ticker in ("SPY", "QQQ"):
        bars = bars_by_ticker.get(ticker, [])
        context = daily_context(market, ticker, day)
        if not context or not valid_bars(bars, day, now, cfg):
            return None
        price = bars[-1]["c"]
        result[ticker] = {"previous_close": context["previous_close"], "price": price,
                          "change_pct": round((price / context["previous_close"] - 1) * 100, 3)}
    return result


def market_volatility(benchmark, cfg):
    move = max(abs(item["change_pct"]) for item in benchmark.values())
    if move >= cfg["market_shock_pct_extreme"]:
        return "EXTREME_VOLATILITY"
    if move >= cfg["market_shock_pct_high"]:
        return "HIGH_VOLATILITY"
    if move >= cfg["market_shock_pct_elevated"]:
        return "ELEVATED"
    return "NORMAL"


def fresh_volatility_indices(day, now, cfg):
    """Optional last-known Yahoo context; stale prior-close values are not intraday."""
    path = ROOT / "data" / "market_regime.json"
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text())
        answer = {}
        for name in ("vix", "vxn"):
            item = payload.get(name) or {}
            stamp = datetime.fromisoformat(item["timestamp"].replace("Z", "+00:00"))
            if (stamp.astimezone(ET).date() == day and item.get("status") == "FRESH" and
                    0 <= (now - stamp).total_seconds() / 60 <= cfg["max_bar_age_minutes"]):
                answer[name.upper()] = {"value": item["value"], "asof": stamp.isoformat(),
                                        "source": item.get("source", "YAHOO")}
        return answer
    except (KeyError, TypeError, ValueError, OSError):
        return {}


def alert_text(item, role, window, events, *, scope=None, cfg=None, benchmark=None):
    state = item.get("position_state") or item.get("plan_state") or "MATERIAL_EVENT"
    labels = {"BUY_PLAN_INVALIDATED": "昨日买入计划已失效",
              "WAIT_FOR_REASSESSMENT": "买入计划需重新评估",
              "HOLD_TIGHT_ALERT": "持仓风险升高",
              "TRIM_REVIEW": "检查是否需要保护利润 / 减仓",
              "REDUCE_CANDIDATE": "减仓候选，需人工复核",
              "THESIS_REVIEW": "持仓逻辑需重新评估",
              "EXIT_CANDIDATE": "卖出候选，需人工复核",
              "POSITION_NORMAL": "持仓出现新的重要信息",
              "ENTRY_PLAN_VALID": "买入计划出现新的重要信息",
              "MATERIAL_EVENT": "出现新的重要信息"}
    regimes = {"NORMAL": "正常", "ELEVATED": "偏高", "HIGH_VOLATILITY": "高波动",
               "EXTREME_VOLATILITY": "极端波动"}
    role_label = "已确认持仓" if role == "TRACKED" else "未确认买入计划" if role == "ENTRY_PLAN" else "观察股票"
    lines = [f"AVERRA STOCKS｜盘中提醒", f"{item['ticker']} · {window} ET｜{role_label}",
             f"结论：{labels.get(state, '需要重新评估')}（{state}）"]
    reasons = []
    plan = (scope or {}).get("plan") or {}
    zones = (scope or {}).get("reference_zones") or {}
    negative = [e for e in events if e.get("event_type") in NEGATIVE_TYPES]
    severe = [e for e in negative if e.get("event_type") in SEVERE_TYPES]
    if state == "BUY_PLAN_INVALIDATED":
        if negative:
            reasons.append("发现重大负面公司事件")
        if item["market_regime"] == "EXTREME_VOLATILITY":
            reasons.append("波动达到极端级别")
        if benchmark and cfg and any(benchmark.get(s, {}).get("change_pct", 0) <= -cfg["market_shock_pct_high"] for s in ("SPY", "QQQ")):
            reasons.append("大盘显著下跌")
        ceiling = plan.get("do_not_chase_above")
        if ceiling is not None and item["price"] > ceiling:
            reasons.append(f"现价 ${item['price']:.2f} 高于不追价上限 ${ceiling:.2f}")
        support = plan.get("nearest_support")
        if support is not None and cfg and plan.get("atr") and item["price"] < support - cfg["structure_break_atr"] * plan["atr"]:
            reasons.append(f"价格跌破支撑位 ${support:.2f} 的容许区间")
    elif role == "TRACKED":
        if severe:
            reasons.append("发现可能破坏持仓逻辑的重大事件")
        elif negative:
            reasons.append("发现重大负面公司事件")
        if state == "EXIT_CANDIDATE" and zones.get("trend_failure_low") is not None and item["price"] < zones["trend_failure_low"]:
            reasons.append(f"跌破趋势失效线 ${zones['trend_failure_low']:.2f}")
        if state == "TRIM_REVIEW" and zones.get("profit_protection_low") is not None and item["price"] < zones["profit_protection_low"]:
            reasons.append(f"跌破利润保护线 ${zones['profit_protection_low']:.2f}")
        if not reasons:
            reasons.append("价格波动或市场风险触及持仓复核条件")
    elif events:
        reasons.append("出现新的重要事件，原买入判断需要重新核对")
    if state == "BUY_PLAN_INVALIDATED" and not reasons:
        reasons.append("原买入计划的风险条件已触发；等待重新评估")
    if reasons:
        lines.append("原因：" + "；".join(reasons))
    lines += [f"行情：${item['price']:.2f}｜开盘跳空 {item['opening_gap_pct']:+.2f}%｜较昨收变动 {item['move_atr']:+.2f} ATR",
              f"VWAP（日内成交量加权均价）：{'$' + format(item['vwap'], '.2f') if item['vwap'] is not None else '暂无'}｜波动：{regimes.get(item['market_regime'], item['market_regime'])}"]
    if events:
        lines.append("重要事件：")
        for event in events:
            lines.append(f"· {event.get('headline', '未提供标题')} [{event['evidence_id']}] ({event.get('event_type', 'EVENT')})")
    if state == "BUY_PLAN_INVALIDATED":
        lines.append("现在：不要按昨日区间追买；这不是卖出提醒。等待收盘后重新扫描。")
    elif item.get("positive_event_review"):
        lines.append("现在：等待重新评估；利好事件不等于盘中立即买入（BUY_NOW 关闭）。")
    elif role == "TRACKED":
        lines.append("现在：复核持仓与风险；这是候选提醒，不是自动卖出指令。")
    else:
        lines.append("现在：等待重新评估；不是盘中买入指令。")
    asof = datetime.fromisoformat(item["asof"].replace("Z", "+00:00")).astimezone(ET)
    lines.append(f"数据：IEX，更新于 {asof:%m/%d %H:%M} ET；非全市场 SIP。未执行交易。")
    return "\n".join(lines)


def macro_alert_text(window, event_names, benchmark, previous, current):
    event_labels = {"NFP": "美国非农就业(NFP)", "CPI": "消费者物价指数(CPI)",
                    "PPI": "生产者物价指数(PPI)", "JOLTS": "职位空缺(JOLTS)",
                    "PCE": "个人消费支出物价(PCE)", "FOMC_DECISION": "美联储利率决定(FOMC)",
                    "FOMC_STATEMENT": "美联储声明(FOMC)"}
    events = ", ".join(event_labels.get(name.strip(), name.strip()) for name in event_names.split(","))
    regimes = {"NORMAL": "正常", "ELEVATED": "偏高", "HIGH_VOLATILITY": "高波动",
               "EXTREME_VOLATILITY": "极端波动"}
    return "\n".join(["AVERRA STOCKS｜大盘波动提醒",
                      f"时间：{window} ET｜事件：{events}",
                      f"市场波动：{regimes.get(previous, previous)} → {regimes.get(current, current)}",
                      f"SPY {benchmark['SPY']['change_pct']:+.2f}%｜QQQ {benchmark['QQQ']['change_pct']:+.2f}%",
                      "现在：留意持仓风险和原买入计划；这不是自动买卖指令。",
                      "数据：IEX，非全市场 SIP。未执行交易。"])


def telegram_sender(message):
    token = os.environ.get("STOCK_TELEGRAM_BOT_TOKEN") or os.environ.get("STOCK_TELEGRAM_TOKEN")
    chat_id = os.environ.get("STOCK_TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        raise ValueError("TELEGRAM_CREDENTIALS_MISSING")
    body = json.dumps({"chat_id": chat_id, "text": message}).encode()
    request = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage",
                                     data=body, headers={"Content-Type": "application/json"})
    # Intentionally no automatic retry: a timeout can hide a successful send.
    with urllib.request.urlopen(request, timeout=20) as response:
        result = json.load(response)
    if not result.get("ok"):
        raise ValueError("TELEGRAM_SEND_UNCONFIRMED")
    return result["result"]["message_id"]


def run_window(now, cfg, db, *, iex, evidence_collector=verified_evidence,
               sender=telegram_sender, watch=(), research_refresh=None):
    local = now.astimezone(ET)
    day = local.date()
    if cfg.get("enabled") is not True or day.isoformat() < cfg.get("activation_date", "0001-01-01"):
        return {"status": "DISABLED"}
    verified_events = events_for_day(db, day, cfg, now)
    windows = {w["time_et"]: w for w in schedule(day, verified_events, cfg, market_day(db, day, cfg, now))}
    window = local.strftime("%H:%M")
    if window not in windows:
        return {"status": "NO_SCHEDULED_CHECK"}
    calendar_status = "VERIFIED" if calendar_ready(db, day, cfg, now) else "CALENDAR_EVENT_SCHEDULING_DEGRADED"
    started = begin_check(db, day.isoformat(), window, windows[window]["kind"])
    if started != "STARTED":
        return {"status": started}
    observations, stats = [], dict(tickers_checked=0, market_state="DATA_UNAVAILABLE",
                                event_state="NONE", alerts_generated=0, iex_requests=0,
                                evidence_requests=0, luna_calls=0, luna_input_tokens=0,
                                luna_output_tokens=0, luna_cost_usd=0,
                                luna_unknown_cost_usd=0, calendar_status=calendar_status,
                                scheduled_checks=len(windows), merged_checks=sum(w["merged"] for w in windows.values()))
    try:
        restore_close_entry_plans(day)
        with sqlite3.connect(f"file:{ROOT / 'data' / 'stock_agent.db'}?mode=ro", uri=True) as book, \
             sqlite3.connect(f"file:{ROOT / 'data' / 'entry_data.db'}?mode=ro", uri=True) as entry, \
             sqlite3.connect(f"file:{ROOT / 'data' / 'market_data.db'}?mode=ro", uri=True) as market:
            scope = scoped_universe(book, entry, day, watch=watch, max_watch=cfg["max_watch"])
            benchmarks = iex.fetch(["SPY", "QQQ"], day, now, cfg)
            benchmark = benchmark_snapshot(market, benchmarks, day, now, cfg)
            if benchmark is None:
                stats["market_state"] = "DATA_UNAVAILABLE"
                for ticker in scope:
                    observations.append({"ticker": ticker, "role": "TRACKED" if scope[ticker]["held"] else "ENTRY_PLAN",
                                         "status": "DATA_UNAVAILABLE", "source": "IEX"})
            else:
                stats["market_state"] = market_volatility(benchmark, cfg)
                for symbol in ("SPY", "QQQ"):
                    observations.append({"ticker": symbol, "role": "BENCHMARK", "status": "OK",
                                         "source": "IEX", "state": stats["market_state"],
                                         "asof": benchmarks[symbol][-1]["t"], **benchmark[symbol]})
                for symbol, item in fresh_volatility_indices(day, now, cfg).items():
                    observations.append({"ticker": symbol, "role": "VOL_INDEX", "status": "OK",
                                         "state": None, **item})
                prior_market = db.execute("SELECT market_state FROM intraday_checks WHERE trading_date=? "
                                          "AND check_window<? AND status='COMPLETE' ORDER BY check_window DESC LIMIT 1",
                                          (day.isoformat(), window)).fetchone()
                if (stats["market_state"] in {"HIGH_VOLATILITY", "EXTREME_VOLATILITY"} and
                        (not prior_market or prior_market[0] != stats["market_state"])):
                    event_names = ", ".join(windows[window]["events"]) or "Market move"
                    market_text = macro_alert_text(window, event_names, benchmark,
                                                   prior_market[0] if prior_market else "UNKNOWN",
                                                   stats["market_state"])
                    alert_id = reserve_alert(db, day.isoformat(), window, "MARKET", event_names,
                                             stats["market_state"], market_text)
                    if alert_id:
                        stats["alerts_generated"] += 1
                        send_reserved(db, alert_id, sender)
                for ticker, role in scope.items():
                    try:
                        bars = iex.fetch([ticker], day, now, cfg).get(ticker, [])
                        context = daily_context(market, ticker, day)
                        previous = prior_check_time(db, ticker, day)
                        try:
                            events, request_count, event_status = evidence_collector(ticker, now, previous) if role["held"] or role["plan"] else ([], 0, "NOT_SCOPED")
                        except Exception as exc:
                            events, request_count, event_status = [], 0, "EVIDENCE_UNAVAILABLE"
                        stats["evidence_requests"] += request_count
                        if event_status != "OK":
                            stats["event_state"] = event_status
                        if events:
                            evidence_key = ",".join(sorted(e["evidence_id"] for e in events))
                            with db:
                                fresh = db.execute("""INSERT OR IGNORE INTO intraday_research_queue
                                  (ticker,evidence_key,first_seen_at,status)
                                  VALUES(?,?,?,'LUNA_REFRESH_REQUIRED') RETURNING ticker""",
                                  (ticker, evidence_key, now.isoformat())).fetchone()
                            if fresh:
                                stats["event_state"] = "LUNA_REFRESH_REQUIRED"
                                if research_refresh is not None:
                                    # Only new material evidence enters the Initial tier.
                                    try:
                                        result = research_refresh(ticker, events)
                                        if result.get("model") != "gpt-5.6-luna" or result.get("web_search") or result.get("sol_calls", 0):
                                            raise ValueError("INTRADAY_AI_POLICY_VIOLATION")
                                        stats["luna_calls"] += result.get("calls", 0)
                                        stats["luna_input_tokens"] += result.get("input_tokens", 0)
                                        stats["luna_output_tokens"] += result.get("output_tokens", 0)
                                        stats["luna_cost_usd"] += result.get("cost_usd", 0)
                                        stats["luna_unknown_cost_usd"] += result.get("unknown_cost_usd", 0)
                                        status = "DEEP_RESEARCH_PENDING" if result.get("escalate") else result.get("status", "COMPLETE")
                                    except Exception:
                                        # Preserve the deterministic material-event alert even
                                        # if optional AI research fails or is uncertain.
                                        result, status = {}, "FAILED_UNKNOWN"
                                    stats["event_state"] = status
                                    with db:
                                        db.execute("""UPDATE intraday_research_queue SET status=?,input_tokens=?,
                                          output_tokens=?,cost_usd=? WHERE ticker=? AND evidence_key=?""",
                                          (status, result.get("input_tokens"), result.get("output_tokens"),
                                           result.get("cost_usd"), ticker, evidence_key))
                        item = assess(ticker, role, bars, context, benchmark, events, now, day, cfg)
                        item["role"] = "TRACKED" if role["held"] else "ENTRY_PLAN" if role["plan"] else "WATCH"
                        item["state"] = item.get("position_state") or item.get("plan_state")
                        observations.append(item)
                        if item["status"] != "OK":
                            continue
                        meaningful = item["state"] in {"HOLD_TIGHT_ALERT", "TRIM_REVIEW", "REDUCE_CANDIDATE",
                                                        "THESIS_REVIEW", "EXIT_CANDIDATE", "BUY_PLAN_INVALIDATED"}
                        changed = item["state"] != previous_state(db, ticker, "state")
                        if (meaningful and changed) or events:
                            event_key = ",".join(sorted(e["evidence_id"] for e in events)) if events else item["state"]
                            text = alert_text(item, item["role"], window, events,
                                              scope=role, cfg=cfg, benchmark=benchmark)
                            alert_state = ("POSITIVE_EVENT_REVIEW" if item.get("positive_event_review")
                                           and not meaningful else item["state"] or "MATERIAL_EVENT")
                            alert_id = reserve_alert(db, day.isoformat(), window, ticker, event_key,
                                                     alert_state, text)
                            if alert_id:
                                stats["alerts_generated"] += 1
                                send_reserved(db, alert_id, sender)
                    except Exception as exc:
                        observations.append({"ticker": ticker, "role": "TRACKED" if role["held"] else "ENTRY_PLAN",
                                             "status": "DATA_UNAVAILABLE", "source": "IEX",
                                             "error_type": type(exc).__name__})
            stats["tickers_checked"] = len(scope)
            stats["iex_requests"] = iex.requests
        finish_check(db, day.isoformat(), window, observations=observations, stats=stats)
        try:
            write_daily_report(db, day, cfg, now)
            stats["daily_report_status"] = "SAVED"
        except (OSError, sqlite3.Error) as exc:
            stats["daily_report_status"] = "FAILED_" + type(exc).__name__
        return {"status": "COMPLETE", **stats}
    except Exception as exc:
        fail_check(db, day.isoformat(), window, exc)
        return {"status": "FAILED", "error_type": type(exc).__name__}


def write_daily_report(db, day, cfg, now):
    """Local-only report; never sends routine check-complete Telegram messages."""
    from intraday_state import daily_totals
    windows = schedule(day, events_for_day(db, day, cfg, now), cfg, market_day(db, day, cfg, now))
    report = {"date": day.isoformat(), "generated_at": now.isoformat(),
              "scheduled_checks": len(windows),
              "merged_deduplicated_checks": sum(w["merged"] for w in windows),
              **daily_totals(db, day.isoformat()),
              "material_events_found": db.execute("SELECT COUNT(*) FROM intraday_research_queue WHERE substr(first_seen_at,1,10)=?", (day.isoformat(),)).fetchone()[0],
              "entry_plans_invalidated": db.execute("SELECT COUNT(*) FROM intraday_alerts WHERE trading_date=? AND state='BUY_PLAN_INVALIDATED'", (day.isoformat(),)).fetchone()[0],
              "position_alerts": db.execute("SELECT COUNT(*) FROM intraday_alerts WHERE trading_date=? AND ticker!='MARKET' AND state!='BUY_PLAN_INVALIDATED'", (day.isoformat(),)).fetchone()[0],
              "calendar_status": "VERIFIED" if calendar_ready(db, day, cfg, now) else "CALENDAR_EVENT_SCHEDULING_DEGRADED"}
    path = ROOT / "data" / "intraday_reports"
    path.mkdir(parents=True, exist_ok=True)
    (path / f"{day.isoformat()}.json").write_text(json.dumps(report, indent=2, sort_keys=True))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--plan", action="store_true", help="Read-only schedule for today")
    parser.add_argument("--run", action="store_true", help="Execute only the current configured check")
    args = parser.parse_args()
    cfg = config()
    now = datetime.now(timezone.utc)
    db = connect()
    try:
        if args.plan:
            print(json.dumps({"enabled": cfg.get("enabled", False),
                              "schedule": schedule(now.astimezone(ET).date(),
                                                   events_for_day(db, now.astimezone(ET).date(), cfg, now), cfg,
                                                   market_day(db, now.astimezone(ET).date(), cfg, now))}))
        elif args.run:
            if cfg.get("enabled") and now.astimezone(ET).date().isoformat() >= cfg.get("activation_date", "0001-01-01"):
                from intraday_calendar_refresh import refresh_due
                refresh_due(db, now)
            load_env(ROOT / ".env")
            source = IEXBars(os.environ.get("ALPACA_API_KEY"), os.environ.get("ALPACA_API_SECRET"))
            from intraday_luna import IntradayLunaRefresh
            print(json.dumps(run_window(now, cfg, db, iex=source,
                                        research_refresh=IntradayLunaRefresh() if cfg.get("enabled") else None)))
        else:
            parser.error("select --plan or --run")
    finally:
        db.close()


if __name__ == "__main__":
    main()

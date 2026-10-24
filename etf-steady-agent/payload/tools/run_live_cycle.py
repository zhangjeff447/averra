import json
import os
import re
import sys
import traceback
from datetime import datetime, time, timezone
from pathlib import Path
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools.ops_utils import (
    ProductionConfigError,
    acquire_lock,
    append_jsonl,
    append_text_log,
    atomic_write_json,
    clear_dedup_alert,
    ensure_log_files,
    is_telegram_configured,
    load_json,
    load_env_file,
    release_lock,
    should_send_dedup_alert,
)
from tools.scheduler_guard import SLOTS, evaluate_schedule
TradingRuntime = None  # Imported only after the local schedule guard passes.
ACCOUNT_SYNC_OK = "ACCOUNT_SYNC_OK"

# Authorized decision slots: 10:05, 12:30, 15:30 in the configured timezone.
STATE_DIR = Path("state")
LOG_DIR = Path("logs")
RUNTIME_STATE_PATH = STATE_DIR / "live_execution_state.json"
RUNTIME_STATE_BACKUP_PATH = STATE_DIR / "live_execution_state.backup.json"
CYCLE_STATE_PATH = STATE_DIR / "live_cycle_state.json"
CYCLE_STATE_BACKUP_PATH = STATE_DIR / "live_cycle_state.backup.json"
LOCK_PATH = STATE_DIR / "live_cycle.lock"
ALERT_KEY_NETWORK = "mcp_network_blocked"
LABEL = "com.qqqsmh.agent"
USAGE_LOG_PATH = LOG_DIR / "codex_usage_safety.jsonl"
TELEGRAM_ENV_KEYS = ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")
REPORT_ONLY_MAX_DAILY_CHARS = 3400


def _report_only_enabled():
    from config.unified import load_config
    return load_config()[1]['REPORT_ONLY_ENABLED'] == 'true'


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _usage_event(**record: Any) -> None:
    payload = {"timestamp": _now_utc().isoformat(), "codex_invoked": False, **record}
    try:
        append_jsonl(USAGE_LOG_PATH, payload)
    except OSError:
        pass


def _non_retryable_codex_error(value: Any) -> bool:
    text = str(value or "").lower()
    return any(marker in text for marker in (
        "codex_usage_limit", "you've hit your usage limit",
        "you have hit your usage limit", "codex usage limit reached",
        "authentication required", "authentication-required",
        "unauthorized", "mcp_configuration_unavailable",
        "model is not supported when using codex", "codex_model_unavailable",
    ))


def _load_selected_shell_env(path: Path) -> Dict[str, str]:
    """Read only Telegram values from a shell profile without executing it."""
    if not path.exists():
        return {}
    selected: Dict[str, str] = {}
    key_pattern = "|".join(re.escape(key) for key in TELEGRAM_ENV_KEYS)
    pattern = re.compile(rf"^\s*(?:export\s+)?({key_pattern})\s*=\s*(.*?)\s*$")
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        match = pattern.match(raw_line)
        if not match:
            continue
        value = match.group(2).strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if value:
            selected[match.group(1)] = value
    return selected


def _load_cycle_state(log_paths: Dict[str, Path]) -> Dict[str, Any]:
    payload, err = load_json(CYCLE_STATE_PATH)
    if payload is not None:
        payload.setdefault("last_run_slots", {})
        payload.setdefault("alert_dedup", {})
        return payload

    backup_payload, backup_err = load_json(CYCLE_STATE_BACKUP_PATH)
    if backup_payload is not None:
        backup_payload.setdefault("last_run_slots", {})
        backup_payload.setdefault("alert_dedup", {})
        append_text_log(log_paths["errors"], f"Cycle state restored from backup due to primary read error: {err}")
        return backup_payload

    append_text_log(log_paths["errors"], f"Cycle state unreadable and no backup available: primary={err}, backup={backup_err}")
    return {"last_run_slots": {}, "alert_dedup": {}}


def _save_cycle_state(state: Dict[str, Any]) -> None:
    atomic_write_json(CYCLE_STATE_PATH, state, backup_path=CYCLE_STATE_BACKUP_PATH)


def _json_print(payload: Dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, default=str))


def _run_read_only_snapshot() -> int:
    """Verify the exact production snapshot path without execution stages."""
    runtime_class = TradingRuntime
    if runtime_class is None:
        from tools.robinhood_runtime import TradingRuntime as runtime_class
    runtime = runtime_class(mode="read_only", allow_live_orders=False)
    # This diagnostic must not evaluate a strategy or emit Telegram messages.
    snapshot = runtime.fetch_live_account_snapshot()
    if not isinstance(snapshot, dict):
        snapshot = {"errors": ["invalid_snapshot_type"]}
    _json_print({
        "status": "read_only_snapshot",
        "command_path": "authenticated_direct_mcp_transport",
        "mode": "read_only",
        "account_readable": snapshot.get("account_readable"),
        "orders_readable": snapshot.get("orders_readable"),
        "positions_readable": snapshot.get("positions_readable"),
        "quotes_readable": snapshot.get("quotes_readable"),
        "market_session": snapshot.get("market_session"),
        "quote_status": snapshot.get("quote_status"),
        "live_blocked": snapshot.get("live_blocked", True),
        "monthly_filled_buys": snapshot.get("monthly_filled_buys"),
        "monthly_open_buy_amount": snapshot.get("monthly_open_buy_amount"),
        "manual_monthly_filled_buys": snapshot.get("manual_monthly_filled_buys"),
        "unattributed_buy_orders": snapshot.get("unattributed_buy_orders"),
        "budget_readable": snapshot.get("budget_readable"),
        "monthly_budget_limit": snapshot.get("monthly_budget_limit"),
        "budget_remaining_before_open_orders": snapshot.get("budget_remaining_before_open_orders"),
        "buying_power": snapshot.get("buying_power"),
        "cash": snapshot.get("cash"),
        "quotes": {
            symbol: snapshot.get("quotes", {}).get(symbol)
            for symbol in ("QQQ", "SMH")
            if isinstance(snapshot.get("quotes"), dict)
        },
        "connected": snapshot.get("connected"),
        "live_blocked_by_network": snapshot.get("live_blocked_by_network"),
        "errors": snapshot.get("errors", []),
        "account_sync_status": snapshot.get("account_sync_status"),
        "account_sync_reason": snapshot.get("account_sync_reason"),
        "agentic_account_confirmed": snapshot.get("agentic_account_confirmed"),
    })
    sync_ok = snapshot.get("account_sync_status") == ACCOUNT_SYNC_OK
    quotes = snapshot.get("quotes") or {}
    fresh_quotes = all(
        isinstance(quotes.get(s), dict)
        and quotes[s].get("source") == "robinhood_mcp"
        and quotes[s].get("is_realtime") is True
        and quotes[s].get("is_stale") is False
        for s in ("QQQ", "SMH")
    )
    outside_regular = snapshot.get("market_session") == "outside_regular"
    blocking_errors = [e for e in snapshot.get("errors", []) if not (
        outside_regular and str(e).startswith("get_equity_quotes_failed: empty_price_book:")
    )]
    return 0 if (sync_ok and (fresh_quotes or outside_regular) and snapshot.get("agentic_account_confirmed") is True
                 and snapshot.get("orders_readable") is True and snapshot.get("positions_readable") is True
                 and not blocking_errors) else 1


def _safe_mode(config_mode: str) -> str:
    # Force fail-closed defaults for production deployment.
    return config_mode if config_mode in ("read_only", "preview", "live") else "read_only"


def _notify_telegram(env, message, *, enabled=True, decision=None):
    from tools.telegram_delivery import deliver
    from tools.telegram_templates import with_scores
    result = deliver(with_scores(message, decision))
    return result.get('delivered') is True


def _build_execution_record(
    *,
    run_id: str,
    mode: str,
    symbol: str,
    decision_id: Optional[str],
    action: str,
    amount: float,
    risk_result: str,
    order_status: str,
    error_type: Optional[str],
) -> Dict[str, Any]:
    return {
        "timestamp": _now_utc().isoformat(),
        "run_id": run_id,
        "mode": mode,
        "symbol": symbol,
        "decision_id": decision_id,
        "action": action,
        "amount": amount,
        "risk_result": risk_result,
        "order_status": order_status,
        "error_type": error_type,
    }


def _select_first_decision(decision_block: Dict[str, Any]) -> Dict[str, Any]:
    decisions = decision_block.get("decisions", {}) if isinstance(decision_block, dict) else {}
    if not isinstance(decisions, dict):
        return {}
    selected = [(symbol, item) for symbol, item in decisions.items()
                if isinstance(item, dict) and item.get("selected_for_execution") is True]
    for symbol, item in (selected or list(decisions.items())):
        if not isinstance(item, dict):
            continue
        merged = dict(item)
        merged["symbol"] = symbol
        return merged
    return {}


def _safe_halt(runtime: Optional[Any], reason: str, log_paths: Dict[str, Path]) -> None:
    if runtime is None:
        return
    try:
        runtime.state.trading_halted = True
        runtime.state.halt_reason = reason
        runtime.state.save(RUNTIME_STATE_PATH)
        append_text_log(log_paths["errors"], f"Runtime set to halted: {reason}")
    except Exception as exc:
        append_text_log(log_paths["errors"], f"Failed to persist halted state: {exc}")


def _send_daily_summary_if_due(
    *,
    env: Dict[str, str],
    now_et: datetime,
    mode: str,
    snapshot: Dict[str, Any],
    decision: Dict[str, Any],
    risk_triggered: bool,
    has_open_orders: bool,
    halted: bool,
    allow_live: bool,
    cycle_state: Dict[str, Any],
    log_paths: Dict[str, Path],
) -> None:
    if now_et.time() < time(15, 15):
        return

    day_key = now_et.date().isoformat()
    if (cycle_state.get("daily_summary_sent_date") == day_key
            or cycle_state.get("daily_summary_attempted_date") == day_key):
        return
    cycle_state["daily_summary_attempted_date"] = day_key
    _save_cycle_state(cycle_state)  # Claim before sending; never retry an uncertain delivery.

    def with_report_only_section(message, *, account_ready):
        if not _report_only_enabled():
            return message
        from tools.report_only import append_to
        combined, _ = append_to(message, snapshot, now_et)
        if len(combined) > REPORT_ONLY_MAX_DAILY_CHARS:
            try:
                append_text_log(log_paths["errors"], "Report-only column skipped: daily message length limit.")
            except OSError:
                pass
            return message
        return combined

    if snapshot.get("live_blocked_by_network"):
        sync = snapshot.get("account_sync") if isinstance(snapshot.get("account_sync"), dict) else {}
        from tools.account_sync import unavailable_message
        msg = unavailable_message(sync)
        delivered = _notify_telegram(env, with_report_only_section(msg, account_ready=False), enabled=True, decision=decision)
        cycle_state["daily_summary_delivery"] = "confirmed" if delivered else "unconfirmed"
        if delivered:
            cycle_state["daily_summary_sent_date"] = day_key
        append_text_log(log_paths["runtime"], "Daily summary attempted (network blocked concise mode).")
        return

    sync = snapshot.get("account_sync") if isinstance(snapshot.get("account_sync"), dict) else {}
    if sync.get("status") != ACCOUNT_SYNC_OK:
        from tools.account_sync import unavailable_message
        msg = unavailable_message(sync)
        delivered = _notify_telegram(env, with_report_only_section(msg, account_ready=False), enabled=True, decision=decision)
        cycle_state["daily_summary_delivery"] = "confirmed" if delivered else "unconfirmed"
        if delivered:
            cycle_state["daily_summary_sent_date"] = day_key
        append_text_log(log_paths["runtime"], "Daily summary attempted (account data unavailable).")
        return
    from tools.telegram_templates import format_daily_summary
    msg = format_daily_summary(
        day_key, mode, snapshot, decision, risk_triggered=risk_triggered,
        has_open_orders=has_open_orders, halted=halted, allow_live=allow_live,
        funding_mode=env.get("FUNDING_MODE"),
    )
    delivered = _notify_telegram(env, with_report_only_section(msg, account_ready=True), enabled=True, decision=decision)
    cycle_state["daily_summary_delivery"] = "confirmed" if delivered else "unconfirmed"
    if delivered:
        cycle_state["daily_summary_sent_date"] = day_key
    append_text_log(log_paths["runtime"], "Daily summary attempted; inspect daily_summary_delivery for confirmation.")


def main() -> int:
    if "--read-only-snapshot" in sys.argv[1:]:
        return _run_read_only_snapshot()
    run_id = _now_utc().strftime("%Y%m%dT%H%M%S%fZ")
    schedule = evaluate_schedule(_now_utc(), env_file=None)
    now_local = schedule.local_now
    slot = schedule.slot
    timezone_name = schedule.timezone_name
    if not schedule.authorized:
        _usage_event(
            status="skipped", reason=schedule.reason,
            scheduled_slot=None, run_id=run_id, invocation_type=None,
            local_time=now_local.isoformat(), timezone=timezone_name,
        )
        _json_print({
            "status": "skipped", "reason": schedule.reason,
            "codex_invoked": False, "time_local": now_local.isoformat(),
            "timezone": timezone_name, "run_id": run_id,
        })
        return 0

    log_paths = ensure_log_files(LOG_DIR)
    lock = acquire_lock(LOCK_PATH)
    if lock is None:
        append_text_log(log_paths["runtime"], "Skipped cycle: lock busy (another cycle running).")
        _usage_event(status="skipped", reason="cycle_lock_busy", scheduled_slot=slot,
                     run_id=run_id, invocation_type=None)
        _json_print({"status": "skipped", "reason": "cycle_lock_busy", "codex_invoked": False})
        return 0

    runtime: Optional[Any] = None
    cycle_state: Dict[str, Any] = {"last_run_slots": {}, "alert_dedup": {}}

    try:
        cycle_state = _load_cycle_state(log_paths)
        from config.unified import load_config
        config, _ = load_config()
        env = dict(os.environ)
        mode = _safe_mode(config.trading_mode)

        if mode == "live" and not config.allow_live_orders:
            raise ProductionConfigError("live_mode_disallowed_by_allow_live_orders")

        timezone = ZoneInfo(config.timezone)
        now_et = _now_utc().astimezone(timezone)

        day_key = now_et.date().isoformat()
        day_slots = cycle_state.setdefault("last_run_slots", {}).setdefault(day_key, [])
        if slot in day_slots:
            append_text_log(log_paths["runtime"], f"Skipped cycle: slot already executed ({day_key} {slot}).")
            _json_print({"status": "skipped", "reason": "slot_already_executed", "slot": slot, "date": day_key, "run_id": run_id})
            return 0

        if cycle_state.get("codex_disabled_date") == day_key:
            _usage_event(status="skipped", reason="codex_disabled_for_day",
                         scheduled_slot=slot, run_id=run_id, invocation_type=None)
            _json_print({"status": "skipped", "reason": "codex_disabled_for_day",
                         "codex_invoked": False, "slot": slot, "date": day_key})
            return 0

        day_slots.append(slot)
        cycle_state["last_slot_claim"] = {"date": day_key, "slot": slot, "run_id": run_id}
        _save_cycle_state(cycle_state)

        os.environ["QQQ_AGENT_SCHEDULED_SLOT"] = slot
        os.environ["QQQ_AGENT_RUN_ID"] = run_id
        _usage_event(status="authorized", reason="scheduled_market_decision",
                     scheduled_slot=slot, run_id=run_id,
                     invocation_type="authenticated_direct_mcp_transport",
                     codex_invoked=False)

        runtime_class = TradingRuntime
        if runtime_class is None:
            from tools.robinhood_runtime import TradingRuntime as runtime_class
        runtime = runtime_class(
            mode=mode,
            allow_live_orders=config.allow_live_orders,
            state_path=RUNTIME_STATE_PATH,
            runtime_log_path=log_paths["runtime"],
        )

        if runtime.state.trading_halted:
            msg = f"Cycle blocked by halted state: {runtime.state.halt_reason}"
            append_text_log(log_paths["errors"], msg)
            _notify_telegram(env, msg, enabled=True)
            _json_print({"status": "halted", "reason": runtime.state.halt_reason, "run_id": run_id})
            return 1

        # Run READ_ONLY first for all modes; this is the safe source of truth for
        # connection health and local strategy decisions.
        read_only_result = runtime.run_read_only()
        snapshot = read_only_result.get("snapshot", {}) if isinstance(read_only_result, dict) else {}
        decision_block = read_only_result.get("decision", {}) if isinstance(read_only_result, dict) else {}
        selected = _select_first_decision(decision_block)

        codex_failure_text = " ".join(str(value) for value in (
            snapshot.get("error_type"), snapshot.get("account_sync_reason"),
            snapshot.get("errors"),
        ))
        if _non_retryable_codex_error(codex_failure_text):
            cycle_state["codex_disabled_date"] = day_key
            cycle_state["codex_disabled_reason"] = "codex_non_retryable"
            _save_cycle_state(cycle_state)
            _usage_event(status="disabled", reason="codex_non_retryable",
                         scheduled_slot=slot, run_id=run_id,
                         invocation_type="authenticated_direct_mcp_transport",
                         codex_invoked=False)
            reason = str(snapshot.get("account_sync_reason") or snapshot.get("error_type") or "codex_non_retryable")
            _notify_telegram(
                env,
                f"QQQ/SMH {slot}: Codex/MCP 不可用（{reason}）。账户未同步；本轮未预览或下单。",
                enabled=is_telegram_configured(env),
                decision=decision_block,
            )
            _json_print({"status": "disabled", "reason": "codex_non_retryable",
                         "slot": slot, "date": day_key, "run_id": run_id})
            return 0

        if mode == "live":
            telegram_result = getattr(runtime.adapter, "last_telegram_result", None)
            if not isinstance(telegram_result, dict) or telegram_result.get("delivered") is not True:
                raise ProductionConfigError("live_mode_requires_successful_telegram_decision_delivery")

        action = str(selected.get("action") or "WAIT")
        symbol = str(selected.get("symbol") or "")
        amount = float(selected.get("amount") or 0.0)
        decision_id = runtime._decision_id(selected) if symbol else None

        append_jsonl(
            log_paths["decisions"],
            {
                "timestamp": _now_utc().isoformat(),
                "run_id": run_id,
                "mode": mode,
                "decision": selected,
                "decision_block": decision_block,
                "live_blocked_by_network": snapshot.get("live_blocked_by_network", False),
            },
        )

        # This is deliberately derived only from this cycle's live MCP read.
        # Persisted/env state must never keep a recovered network permanently
        # blocked (or clear a current transport failure).
        network_blocked = snapshot.get("live_blocked_by_network") is True
        data_blocked = bool(snapshot.get("live_blocked", False))
        live_blocked = network_blocked or data_blocked
        order_status = "read_only"
        risk_result = "ok"
        error_type = snapshot.get("error_type")
        exec_result: Dict[str, Any] = {"status": "read_only"}

        if network_blocked:
            order_status = "network_blocked"
            risk_result = "network_blocked"
            key = f"{ALERT_KEY_NETWORK}:{error_type or 'unknown'}"
            if should_send_dedup_alert(cycle_state, key, _now_utc(), cooldown_hours=6):
                _notify_telegram(env, f"MCP连接异常: {error_type or 'unknown_error'}，系统保持READ_ONLY。", enabled=True, decision=decision_block)
            append_text_log(log_paths["runtime"], "Network blocked; skipped preview/place by policy.")
        elif data_blocked:
            order_status = "account_data_blocked"
            risk_result = str(snapshot.get("error_type") or "account_data_unavailable")
            append_text_log(log_paths["runtime"], "Account data unavailable; skipped preview/place by policy.")
        else:
            recovered = clear_dedup_alert(cycle_state, f"{ALERT_KEY_NETWORK}:{error_type or 'unknown'}")
            if recovered:
                _notify_telegram(env, "MCP连接已恢复。", enabled=True, decision=decision_block)

            if mode == "preview":
                exec_result = runtime.run_preview(base=read_only_result)
                preview = exec_result.get("preview", {}) if isinstance(exec_result, dict) else {}
                order_status = str(preview.get("status") or "preview")
                if order_status in ("rejected", "unsupported", "halted"):
                    risk_result = str(preview.get("reason") or order_status)
            elif mode == "live":
                if not config.allow_live_orders:
                    order_status = "blocked"
                    risk_result = "live_gate_not_satisfied"
                    exec_result = {"status": "blocked", "reason": "allow_live_orders_false"}
                else:
                    exec_result = runtime.run_live(base=read_only_result)
                    live = exec_result.get("live", {}) if isinstance(exec_result, dict) else {}
                    order_status = str(live.get("status") or "live")
                    if order_status in ("rejected", "unsupported", "halted", "blocked"):
                        risk_result = str(live.get("reason") or order_status)

        append_jsonl(
            log_paths["execution"],
            _build_execution_record(
                run_id=run_id,
                mode=mode,
                symbol=symbol,
                decision_id=decision_id,
                action=action,
                amount=amount,
                risk_result=risk_result,
                order_status=order_status,
                error_type=str(error_type) if error_type else None,
            ),
        )

        cycle_state["last_cycle_time"] = _now_utc().isoformat()
        cycle_state["last_cycle_mode"] = mode
        cycle_state["last_cycle_result"] = order_status
        cycle_state["last_mcp_connected"] = bool(snapshot.get("connected"))
        cycle_state["last_live_blocked_by_network"] = bool(snapshot.get("live_blocked_by_network", False))
        if bool(snapshot.get("connected")):
            cycle_state["last_successful_read_only"] = _now_utc().isoformat()
        if mode == "preview" and order_status.startswith("preview"):
            cycle_state["last_preview"] = _now_utc().isoformat()

        _save_cycle_state(cycle_state)

        summary_msg = (
            f"Cycle {run_id}: mode={mode}, status={order_status}, "
            f"mcp_connected={snapshot.get('connected')}, blocked={live_blocked}"
        )
        _notify_telegram(env, summary_msg, enabled=is_telegram_configured(env), decision=decision_block)

        _send_daily_summary_if_due(
            env=env,
            now_et=now_et,
            mode=mode,
            snapshot=snapshot,
            decision=decision_block,
            risk_triggered=risk_result != "ok",
            has_open_orders=bool(snapshot.get("open_orders")),
            halted=runtime.state.trading_halted,
            allow_live=config.allow_live_orders,
            cycle_state=cycle_state,
            log_paths=log_paths,
        )
        _save_cycle_state(cycle_state)

        _json_print(
            {
                "status": "ok",
                "run_id": run_id,
                "slot": slot,
                "date": day_key,
                "mode": mode,
                "order_status": order_status,
                "live_blocked_by_network": live_blocked,
                "result": exec_result,
            }
        )
        return 0

    except ProductionConfigError as exc:
        cycle_state["codex_disabled_date"] = now_local.date().isoformat()
        cycle_state["codex_disabled_reason"] = "permanent_configuration_error"
        _save_cycle_state(cycle_state)
        append_text_log(log_paths["errors"], f"Production config error: {exc}")
        _json_print({"status": "failed", "error_type": "config_error", "error": str(exc), "run_id": run_id})
        return 2
    except Exception as exc:
        tb = traceback.format_exc()
        append_text_log(log_paths["errors"], f"Unhandled exception: {exc}\n{tb}")
        append_jsonl(
            log_paths["execution"],
            _build_execution_record(
                run_id=run_id,
                mode=(runtime.mode if runtime else "unknown"),
                symbol="",
                decision_id=None,
                action="ERROR",
                amount=0.0,
                risk_result="exception",
                order_status="failed",
                error_type="unhandled_exception",
            ),
        )
        if _non_retryable_codex_error(exc):
            cycle_state["codex_disabled_date"] = now_local.date().isoformat()
            cycle_state["codex_disabled_reason"] = "codex_non_retryable"
            _save_cycle_state(cycle_state)
            _usage_event(status="disabled", reason="codex_non_retryable",
                         scheduled_slot=slot, run_id=run_id,
                         invocation_type="authenticated_direct_mcp_transport",
                         codex_invoked=False)
        elif runtime is not None and runtime.mode == "live" and not isinstance(exc, OSError):
            _safe_halt(runtime, f"severe_unhandled_exception:{exc}", log_paths)
        _json_print({"status": "failed", "error_type": "unhandled_exception", "error": str(exc), "run_id": run_id})
        return 3
    finally:
        release_lock(lock)


if __name__ == "__main__":
    raise SystemExit("Use run_agent.py; legacy direct entry is disabled in the unified candidate.")

"""One real read-only run through the sole future production entry."""
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run():
    from config.unified import load_config, READ_ONLY_VALIDATION
    from tools.robinhood_runtime import TradingRuntime, _is_regular_market_time
    from tools.report_only import append_to
    from tools.telegram_templates import format_daily_summary, format_decision
    from scripts.validate_robinhood_read_only import READ_ONLY_MCP_TOOLS
    config, effective = load_config()
    now = datetime.now(timezone.utc)
    if config.operation_mode != READ_ONLY_VALIDATION:
        raise RuntimeError('validation_requires_read_only_mode')
    if not _is_regular_market_time(now):
        raise RuntimeError('normal_trading_session_required')
    runtime = TradingRuntime()
    try:
        base = runtime.run_read_only()
        snapshot, decision = base['snapshot'], base['decision']
        diagnostic = decision.get('plan_diagnostic')
        candidate = ((diagnostic or {}).get('conditional_candidate') or
                     (diagnostic or {}).get('execution_candidate'))
        from strategies.qqq_smh_agent.score_dynamic_v1 import select_one_for_existing_executor
        if diagnostic is None:
            candidate = select_one_for_existing_executor(decision.get('score_dynamic_v1') or {})
        risk = 'NO_BUY_CANDIDATE'
        if candidate:
            item = dict(decision['decisions'][candidate['symbol']], symbol=candidate['symbol'],
                        action='BUY', amount=candidate['amount'])
            _, risk = runtime._risk_check(item, snapshot)
        trade_fingerprint = hashlib.sha256(json.dumps({'decision': decision,
            'budget': runtime.v1_store.summary()}, sort_keys=True, default=str).encode()).hexdigest()
        message = format_daily_summary(now.date().isoformat(), config.operation_mode, snapshot, decision,
            risk_triggered=False, has_open_orders=bool(snapshot.get('open_orders')), halted=runtime.state.trading_halted,
            allow_live=False, funding_mode=effective.get("FUNDING_MODE"))
        daily_preview, report = append_to(message, snapshot, now)
        after = hashlib.sha256(json.dumps({'decision': decision,
            'budget': runtime.v1_store.summary()}, sort_keys=True, default=str).encode()).hexdigest()
        if trade_fingerprint != after:
            raise RuntimeError('report_only_changed_trading_state')
        checks = {'normal_trading_session': True, 'agentic_account': snapshot.get('agentic_account_confirmed') is True,
            'account_matches_ledger': runtime.v1_store.account_matches_hash(snapshot.get('v1_account_hash')),
            'positions_complete': snapshot.get('positions_readable') is True,
            'orders_complete': snapshot.get('orders_readable') is True and snapshot.get('budget_history_complete') is True,
            'quotes_fresh': snapshot.get('quotes_readable') is True,
            'real_history_inputs': all(s in runtime.history_provider.audit_metadata for s in ('SMH', 'QQQ')),
            'budget_reconciled': snapshot.get('budget_readable') is True,
            'plan_calculated': decision.get('data_ready') is True and decision.get('allocation_data_ready') is True,
            'smh_signal_verified': not config.cash_regime_v2_enabled or (
                decision.get('score_dynamic_v1', {}).get('etfs', {}).get('SMH', {}).get('reason') != 'WAIT_SMH_SIGNAL_UNVERIFIED'
                and decision.get('cash_regime_v2', {}).get('smh_feature') is not None),
            'report_only_isolated': trade_fingerprint == after,
            'broker_writes': not any(n not in READ_ONLY_MCP_TOOLS for n in runtime.adapter.transport.calls)}
        result = {'validation': 'PASS' if all(checks.values()) else 'BLOCKED', 'time': now.isoformat(),
            'entrypoint': 'run_agent.py --validate-read-only', 'mode': config.operation_mode,
            'approval_state': 'PLATFORM_APPROVAL_VISIBILITY_LIMITATION', 'approval_mode': config.approval_mode, 'execution_state': decision.get('execution_state'), 'checks': checks,
            'policy_hash': effective['policy_hash'], 'account_hash': snapshot.get('v1_account_hash'),
            'read_calls': runtime.adapter.transport.calls, 'broker_writes': 0, 'telegram_sends': 0,
            'cash': snapshot.get('cash'), 'buying_power': snapshot.get('buying_power'),
            'positions': snapshot.get('positions'), 'quotes': snapshot.get('quotes'),
            'open_orders_count': len(snapshot.get('open_orders') or []),
            'broker_orders': snapshot.get('v1_broker_orders'),
            'budget': runtime.v1_store.summary(), 'history': runtime.history_provider.audit_metadata,
            'decision': decision, 'conditional_candidate': candidate, 'risk_check': risk,
            'cash_observation': snapshot.get('cash_normalization'),
            'report_only': report, 'errors': snapshot.get('errors')}
        output = ROOT / 'evidence'
        output.mkdir(exist_ok=True)
        (output / 'real_read_only_validation.json').write_text(json.dumps(result, indent=2, default=str, ensure_ascii=False))
        (output / 'telegram_decision_preview.txt').write_text(format_decision(decision))
        (output / 'telegram_daily_preview.txt').write_text(daily_preview)
        print(json.dumps({'validation': result['validation'], 'checks': checks, 'broker_writes': 0,
                          'approval_state': result['approval_state']}, ensure_ascii=False))
        return 0 if result['validation'] == 'PASS' else 2
    finally:
        runtime.v1_store.db.close()

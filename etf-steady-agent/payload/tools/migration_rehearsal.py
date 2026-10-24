"""Migration and forward-ledger rollback rehearsal on private copies only."""
import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def run(legacy_root, validation, output):
    from tools.v1_state_store import V1StateStore
    from tools.v1_execution import reconcile_orders
    from broker.unified import ModeTransport
    from broker.live_safety import LiveTradingDisabled
    source = Path(legacy_root) / 'state'
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    official = [source / name for name in ('agent_budget_ledger.json', 'live_execution_state.json', 'live_cycle_state.json')]
    before = {p.name: sha(p) for p in official}
    for path in official:
        (output / path.name).write_bytes(path.read_bytes())
    if before != {p.name: sha(p) for p in official}:
        raise RuntimeError('official_snapshot_changed_during_copy')
    state = json.loads((output / 'live_execution_state.json').read_text())
    rows = validation.get('broker_orders')
    if validation['validation'] != 'PASS' or not isinstance(rows, list):
        raise RuntimeError('complete_real_read_only_snapshot_required')
    if state.get('pending_submission'):
        raise RuntimeError('legacy_submission_unresolved')
    if validation['open_orders_count']:
        raise RuntimeError('broker_open_orders_require_reconciliation')
    store = V1StateStore(output / 'unified.sqlite')
    store.initialize_from_legacy(output / 'agent_budget_ledger.json', verified_account_hash=validation['account_hash'])
    store.credit_through(datetime.now(timezone.utc).strftime('%Y-%m'), 300)
    reconciled = reconcile_orders(store, rows, complete_history=True)
    if reconciled['status'] != 'RECONCILED' or store.summary() != validation['budget']:
        raise RuntimeError('copied_budget_or_orders_do_not_reconcile')
    baseline = store.summary()
    # A separate paper fork exercises post-snapshot credit/fill/unknown, not fabricated real events.
    paper_path = output / 'paper-forward-ledger.sqlite'
    with sqlite3.connect(paper_path) as destination:
        store.db.backup(destination)
    paper = V1StateStore(paper_path)
    paper.credit_through('2026-11', 300)
    intent = paper.reserve(decision_id='PAPER-delta-fill', run_id='PAPER-delta-fill', snapshot_id='PAPER',
        ticker='SMH', requested_amount='60.00', score='90', base_amount='60.00', multiplier='1',
        policy_version='score_dynamic_v1', safe_cash='130', daily_limit='100', broker_daily_filled='0',
        broker_daily_open='0', local_unmatched='0', max_order='100')
    paper.mark_submitting(intent['intent_id'])
    paper.mark_submitted(intent['intent_id'], 'PAPER-order-fill')
    fill = {'id': 'PAPER-order-fill', 'ref_id': intent['ref_id'], 'symbol': 'SMH', 'side': 'buy',
        'state': 'filled', 'cumulative_quantity': '.15', 'executions': [
            {'id': 'PAPER-fill-1', 'price': '400', 'quantity': '.15', 'timestamp': datetime.now(timezone.utc).isoformat()}]}
    for _ in range(2):
        if reconcile_orders(paper, [fill], complete_history=True)['status'] != 'RECONCILED':
            raise RuntimeError('paper_delta_fill_failed')
    unknown = paper.reserve(decision_id='PAPER-unknown', run_id='PAPER-unknown', snapshot_id='PAPER',
        ticker='QQQ', requested_amount='20.00', score='90', base_amount='20.00', multiplier='1',
        policy_version='score_dynamic_v1', safe_cash='70', daily_limit='100', broker_daily_filled='60',
        broker_daily_open='0', local_unmatched='0', max_order='100')
    paper.mark_submitting(unknown['intent_id'])
    paper.mark_unknown(unknown['intent_id'], 'PAPER-network-timeout')
    empty = reconcile_orders(paper, [], complete_history=True)
    assert empty['status'] == 'RECONCILIATION_REQUIRED'
    assert paper.get(unknown['intent_id'])['status'] == 'RECONCILIATION_REQUIRED'
    pre_rollback = paper.summary()
    ledger_hash = sha(paper_path)
    root = Path(__file__).resolve().parents[1]
    installation = output / 'simulated_install'
    installation.mkdir()
    (installation / 'entry.py').write_bytes((root / 'run_agent.py').read_bytes())
    (installation / 'agent.plist').write_bytes((root / 'deployment/com.qqqsmh.agent.review.plist').read_bytes())
    policy = json.loads((root / 'config/unified_policy.json').read_text())
    hypothetical = dict(policy, AGENT_MODE='LIVE_TRADING', TRADING_MODE='live', ALLOW_LIVE_ORDERS='true',
                        V1_SUBMISSION_APPROVED='true', FINAL_DEPLOYMENT_AUTHORIZED='false')
    (installation / 'hypothetical-live-policy.json').write_text(json.dumps(hypothetical))
    # Code/launchd selection rolls back; the forward ledger is neither restored nor edited.
    (output / 'rollback-selection.json').write_text(json.dumps({'selected_code': 'LEGACY_ARCHIVE',
        'operation_mode': 'READ_ONLY_VALIDATION', 'ledger': str(paper_path),
        'financial_snapshot_restore': False, 'unknown_must_remain': True}))
    (installation / 'entry.py').write_bytes((Path(legacy_root) / 'tools/run_live_cycle.py').read_bytes())
    installed_plist = Path.home() / 'Library/LaunchAgents/com.qqqsmh.agent.plist'
    (installation / 'agent.plist').write_bytes(installed_plist.read_bytes())
    (installation / 'operation-policy.json').write_text(json.dumps(policy))
    assert sha(paper_path) == ledger_hash and paper.summary() == pre_rollback
    class NoBroker:
        calls = 0
        def call_tool(self, *args):
            self.calls += 1
            raise AssertionError('no real transport in rehearsal')
    broker = NoBroker()
    guarded = ModeTransport(broker, 'READ_ONLY_VALIDATION', False)
    try:
        guarded.call_tool('place_equity_order', {'symbol': 'SMH', 'side': 'buy'})
    except LiveTradingDisabled:
        pass
    assert broker.calls == 0
    after = {p.name: sha(p) for p in official}
    assert before == after
    result = {'status': 'PASS', 'scope': 'CONSISTENT FORMAL COPY + SEPARATE SYNTHETIC PAPER DELTAS',
        'legacy_stop': 'SIMULATED_FENCE_ONLY; service not stopped', 'official_hashes_before': before,
        'official_hashes_after': after, 'real_copy_budget': baseline,
        'real_account_positions_cash_orders_checks': validation['checks'],
        'paper_new_credit': '300.00', 'paper_fill': '60.00', 'paper_duplicate_fill_idempotent': True,
        'paper_unknown_reserved': '20.00', 'empty_list_does_not_clear_unknown': True,
        'paper_rollback_ledger_preserved': True, 'paper_forward_ledger': pre_rollback,
        'paper_code_and_plist_rollback': 'COPIED CODE/PLIST RESTORED ONLY INSIDE simulated_install; not executed or loaded',
        'live_flag_simulation': 'WRITE_REJECTED_BEFORE_TRANSPORT', 'real_broker_writes': 0,
        'new_launchagent_loaded': False, 'legacy_service_stopped': False,
        'single_executor_cutover': 'SIMULATED same launchd label; authorization required to install',
        'read_only_start_and_telegram_preview': 'real_read_only_validation.json and local previews'}
    (output / 'result.json').write_text(json.dumps(result, indent=2))
    paper.db.close()
    store.db.close()
    return result

"""Public desktop entry: configuration and fresh account validation only."""
import argparse,hashlib,json,os,sqlite3,tempfile
from pathlib import Path

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--check-config',action='store_true')
    parser.add_argument('--validate-read-only',action='store_true')
    parser.add_argument('--live',action='store_true')
    args=parser.parse_args()
    from etf.cycle import POLICY
    if args.live or not (args.check_config ^ args.validate_read_only):
        print(json.dumps(dict(status='BLOCKED',reason='public_download_read_only')));return 2
    if args.check_config:
        print(json.dumps(dict(POLICY,mode='READ_ONLY_VALIDATION',symbols=['SMH','QQQ','SGOV'],
            order_permissions={'SMH':['BUY'],'QQQ':['BUY'],'SGOV':['BUY','SELL']},
            live_execution_available=False,website_preferences_applied=False,
            trading_llm_calls=0),indent=2));return 0
    from etf.broker import NativeAdapter,Broker
    from etf.funds import normalize
    from etf.journal import Journal,clone_legacy
    from etf.reconciliation import validate_history
    from qqq_mcp_adapter import MCPAdapter
    config=MCPAdapter._codex_config_path()
    with tempfile.TemporaryDirectory(prefix='etf-v3-read-only-') as temp:
        adapter=NativeAdapter(temp,config,live=False)
        accounts=adapter._unwrap_mcp_result(adapter._call_tool('get_accounts',{},retries=0))['data']['accounts']
        eligible=[a for a in accounts if a.get('agentic_allowed') is True and a.get('state')=='active']
        ledger=Path('state/v1_state.sqlite');expected=None
        if ledger.exists():
            with sqlite3.connect('file:'+str(ledger.resolve())+'?mode=ro',uri=True) as db:
                tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                for table in ['meta','etf_meta']:
                    if table in tables:
                        row=db.execute('SELECT value FROM '+table+" WHERE key='account_hash'").fetchone()
                        if row:expected=json.loads(row[0]) if table=='etf_meta' else row[0]
            clone_legacy(ledger,Path(temp)/'account.sqlite')
        if expected:
            eligible=[a for a in eligible if hashlib.sha256(str(a.get('account_number')).encode()).hexdigest()==expected]
        if len(eligible)!=1:raise ValueError('select_and_verify_one_agentic_account_locally')
        expected=expected or hashlib.sha256(str(eligible[0]['account_number']).encode()).hexdigest()
        journal=Journal(Path(temp)/'account.sqlite')
        try:
            broker=Broker(adapter,expected_account_hash=expected);raw=broker.read()
            journal.reconcile(raw['orders'],complete=raw['complete_orders'])
            history=validate_history(journal,raw)
            if journal.halt_reason() or journal.active() or journal.legacy_active():raise ValueError('unresolved_or_halted_account')
            if raw['pending_approvals']:raise ValueError('pending_broker_approval')
            snapshot=normalize(raw)
            print(json.dumps(dict(status='READ_ONLY_ACCOUNT_VERIFIED',strategy='etf_cycle_v3',
                targets=POLICY['targets'],safe_cash=snapshot['safe_cash'],net_assets=snapshot['net_assets'],
                weights={s:str(float(snapshot['values'][s])/float(snapshot['net_assets'])) if float(snapshot['net_assets'])>0 else '0' for s in POLICY['targets']},
                history=history,broker_place_calls=0,order_preview_calls=0,formal_ledger_writes=0,
                note='Account validation only; no scheduled phase decision or live execution.'),indent=2))
        finally:journal.db.close()
    return 0

if __name__=='__main__':
    try:raise SystemExit(main())
    except Exception as exc:
        print(json.dumps(dict(status='BLOCKED',reason=type(exc).__name__+':'+str(exc),broker_place_calls=0)));raise SystemExit(2)

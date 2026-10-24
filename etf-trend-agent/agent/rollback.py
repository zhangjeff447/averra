"""Operator-run forward-ledger rollback preparation. Never starts a service or trades."""
from pathlib import Path
from datetime import datetime,timezone
import argparse,fcntl,hashlib,json,sqlite3,subprocess
from etf.journal import clone_legacy

def all_etf_services_unloaded():
 result=subprocess.run(['launchctl','list'],capture_output=True,text=True,check=True)
 labels=[line.split()[-1] for line in result.stdout.splitlines()[1:] if line.split()]
 if any(x.startswith('com.qqqsmh.agent') or x=='com.averra.etf.reports' for x in labels):raise ValueError('unload_all_etf_services_before_rollback')

def verify_forward_fills(old,new):
 with sqlite3.connect('file:'+str(old)+'?mode=ro',uri=True) as before,sqlite3.connect('file:'+str(new)+'?mode=ro',uri=True) as after:
  tables={r[0] for r in before.execute("SELECT name FROM sqlite_master WHERE type='table'")}
  for table,key in [('etf_fills','id'),('fills','fill_id')]:
   if table not in tables:continue
   columns=[r[1] for r in before.execute('PRAGMA table_info('+table+')')]
   for row in before.execute('SELECT * FROM '+table):
    value=row[columns.index(key)];seen=after.execute('SELECT '+','.join(columns)+' FROM '+table+' WHERE '+key+'=?',(value,)).fetchone()
    if seen!=row:raise ValueError('forward_fill_missing_or_changed')

def forward_to_old(current,previous):
 current=Path(current).resolve();previous=Path(previous).resolve()
 a=json.loads((current/'config/runtime.json').read_text());b=json.loads((previous/'config/runtime.json').read_text())
 if current==previous or a['account_hash']!=b['account_hash']:raise ValueError('rollback_account_mismatch')
 if hashlib.sha256((previous/'agent/etf/policy.json').read_bytes()).hexdigest()!=a['previous_policy_hash']:raise ValueError('rollback_code_version_changed')
 verify_forward_fills(previous/'state/v1_state.sqlite',current/'state/v1_state.sqlite')
 tag=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ');backup=previous/'state'/('before-forward-rollback-'+tag);backup.mkdir(mode=0o700)
 for name in ['v1_state.sqlite','operations.sqlite']:
  old=previous/'state'/name;new=current/'state'/name
  if not new.exists():continue
  if old.exists():clone_legacy(old,backup/name);(backup/name).chmod(0o600)
  # Both services are unloaded, caller holds their shared executor lock. Copy
  # through SQLite backup rather than replacing a database with a stale WAL.
  with sqlite3.connect('file:'+str(new)+'?mode=ro',uri=True) as source,sqlite3.connect(old) as target:
   if source.execute('PRAGMA quick_check').fetchall()!=[('ok',)]:raise ValueError('rollback_database_invalid')
   source.backup(target)
  old.chmod(0o600)
 halt=current/'state/live_execution_state.json'
 if halt.exists():
  payload=json.loads(halt.read_text())
  if type(payload.get('trading_halted')) is not bool:raise ValueError('rollback_halt_state_invalid')
  target=previous/'state/live_execution_state.json';target.write_text(json.dumps(payload,indent=2));target.chmod(0o600)
 return dict(status='FORWARD_LEDGER_PRESERVED',old_code_unchanged=True,services_started=False,orders_placed=0)

if __name__=='__main__':
 parser=argparse.ArgumentParser(description='Carry newest ledger to old runtime after all ETF services stop; does not activate it')
 parser.add_argument('--current',type=Path,required=True);parser.add_argument('--previous',type=Path,required=True);args=parser.parse_args()
 all_etf_services_unloaded();cfg=json.loads((args.current/'config/runtime.json').read_text())
 with Path(cfg['shared_executor_lock']).open('a') as lock:
  fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
  print(json.dumps(forward_to_old(args.current,args.previous),indent=2))

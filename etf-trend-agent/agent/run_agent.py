"""Return-first runtime. Check/config/download never place trades; live is explicit."""
import argparse,fcntl,hashlib,json,os,sqlite3,subprocess,tempfile
from pathlib import Path
from datetime import datetime,timezone
ROOT=Path(__file__).resolve().parent

def implementation_hash():
 h=hashlib.sha256()
 for p in sorted(ROOT.rglob('*.py')):
  if any(x in ('tests_return_first','__pycache__') for x in p.relative_to(ROOT).parts):continue
  h.update(str(p.relative_to(ROOT)).encode());h.update(p.read_bytes())
 return h.hexdigest()

def check_other_executors():
 result=subprocess.run(['launchctl','list'],capture_output=True,text=True,check=True)
 others=[line.split()[-1] for line in result.stdout.splitlines()[1:] if line.split() and (line.split()[-1].startswith('com.qqqsmh.agent') or line.split()[-1]=='com.averra.etf.reports') and line.split()[-1] not in ('com.qqqsmh.agent.return-first','com.qqqsmh.agent.return-first-reports')]
 if others:raise ValueError('legacy_executor_still_loaded')

def evidence_path():return ROOT.parent/'evidence/read-only-acceptance.json'
def main():
 parser=argparse.ArgumentParser(description='ETF Return First execution candidate')
 modes=parser.add_mutually_exclusive_group(required=True)
 for name in ['check-config','download-history','validate-read-only','accept-local','live']:modes.add_argument('--'+name,action='store_true')
 args=parser.parse_args()
 from etf.cycle import POLICY
 from etf.calendar import ET,scheduled,risk_session
 now=datetime.now(timezone.utc)
 # Gate before constructing an adapter or doing broker I/O.
 if args.live and not risk_session(now):return 0
 if args.live and not scheduled(now) and not POLICY.get('tqqq_stop_loss_enabled',True):return 0
 policy_hash=hashlib.sha256((ROOT/'etf/policy.json').read_bytes()).hexdigest()
 if args.check_config:
  print(json.dumps(dict(POLICY,policy_hash=policy_hash,permissions={'SMH':['BUY'],'QQQ':['BUY'],'TQQQ':['BUY','SELL']}),indent=2));return 0
 from etf.history import download,signal
 if args.download_history:
  print(json.dumps(download(ROOT.parent/'data',end=now.astimezone(ET).date().isoformat()),indent=2));return 0
 cfg_path=ROOT.parent/'config/runtime.json';cfg=json.loads(cfg_path.read_text())
 if cfg.get('implementation_hash')!=implementation_hash():raise ValueError('implementation_hash_changed')
 state=ROOT.parent/'state';state.mkdir(exist_ok=True)
 from etf.journal import Journal,clone_legacy
 if args.accept_local:
  check_other_executors()
  evidence=json.loads(evidence_path().read_text())
  age=(now-datetime.fromisoformat(evidence['at'])).total_seconds()
  if not 0<=age<=900 or evidence.get('policy_hash')!=policy_hash or evidence.get('account_hash')!=cfg['account_hash'] or evidence.get('status')!='PASS' or evidence.get('implementation_hash')!=implementation_hash():raise ValueError('fresh_read_only_acceptance_required')
  # This command only changes LOCAL acceptance state when run by the operator.
  j=Journal(state/'v1_state.sqlite')
  try:
   if j.active() or j.legacy_active() or j.halt_reason():raise ValueError('local_order_state_not_clear')
   if j.load('rf_baseline') and j.load('rf_baseline')!=evidence['baseline']:raise ValueError('activation_already_bound')
   with j.transaction():
    j.save('rf_baseline',evidence['baseline']);j.save('rf_accepted_at',now.isoformat())
   cfg.update(deployment_authorized=True,policy_hash=policy_hash,accepted_at=now.isoformat());cfg_path.write_text(json.dumps(cfg,indent=2));cfg_path.chmod(0o600)
  finally:j.db.close()
  print(json.dumps(dict(status='LOCAL_ACCEPTANCE_RECORDED',services_started=False,orders_placed=0)));return 0
 if args.live:
  if cfg.get('deployment_authorized') is not True or cfg.get('policy_hash')!=policy_hash:raise ValueError('local_acceptance_required')
  check_other_executors()
  halt=json.loads((state/'live_execution_state.json').read_text())
  if halt.get('trading_halted') is not False:raise ValueError('TRADING_HALTED')
 lock_path=Path(cfg['shared_executor_lock']);lock_path.parent.mkdir(parents=True,exist_ok=True)
 with lock_path.open('a') as lock:
  try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
  except BlockingIOError:return 0
  # Read-only validation uses a private cloned database, including ALL history.
  isolated=tempfile.TemporaryDirectory(prefix='rf-validate-') if args.validate_read_only else None
  database=state/'v1_state.sqlite'
  if isolated:
   target=Path(isolated.name)/'v1_state.sqlite';clone_legacy(database,target);database=target
  j=Journal(database)
  try:
   from etf.recovery import window_key
   window=window_key(now) if args.live else None
   risk_only=bool(args.live and not window)
   if risk_only:window='STOP:'+now.astimezone(ET).date().isoformat()
   done_key='rf_window_completed:'+str(window)
   completed=j.load(done_key) if args.live and not risk_only else None
   if completed is not None:
    from etf.notifications import enqueue
    enqueue(completed,j)
    if not POLICY.get('tqqq_stop_loss_enabled',True):return 0
    risk_only=True;window='STOP:'+now.astimezone(ET).date().isoformat()
   from etf.broker import NativeAdapter,Broker
   from etf.runtime import cycle,checked,report
   try:market=signal(ROOT.parent/'data',now=now)
   except (ValueError,FileNotFoundError):
    download(ROOT.parent/'data',end=now.astimezone(ET).date().isoformat());market=signal(ROOT.parent/'data',now=now)
   adapter=NativeAdapter(str(Path(isolated.name) if isolated else state),cfg['credential_config'],live=args.live)
   broker=Broker(adapter,expected_account_hash=cfg['account_hash'])
   broker.verify_order_schema()
   result=cycle(j,broker,market,live=args.live,now=now,window=window,risk_only=risk_only if args.live else False)
   if args.validate_read_only:
    raw=broker.read();snapshot=checked(j,raw,now=datetime.now(timezone.utc))
    if j.active() or j.legacy_active() or j.halt_reason() or raw.get('pending_approvals'):raise ValueError('read_only_account_not_clear')
    baseline=dict(quantities=snapshot['quantities'],orders=[o['id'] for o in raw['orders']],fills=[r[0] for r in j.db.execute('SELECT id FROM etf_fills')])
    proof=dict(status='PASS',at=datetime.now(timezone.utc).isoformat(),account_hash=cfg['account_hash'],policy_hash=policy_hash,baseline=baseline,order_schema_verified=True,broker_write_calls=0,implementation_hash=implementation_hash())
    evidence_path().parent.mkdir(exist_ok=True);evidence_path().write_text(json.dumps(proof,indent=2));evidence_path().chmod(0o600)
   result['run_at']=datetime.now(timezone.utc).isoformat()
   if args.live and risk_only and not result.get('executions') and not result.get('selected') and result.get('reason')=='WAIT_STOP_NOT_TRIGGERED':return 0
   if args.live:
    if not risk_only:j.save(done_key,result)
    from tools.ops_utils import append_jsonl
    append_jsonl(ROOT.parent/'logs/cycles.jsonl',result)
   print(report(result))
   if args.live:
    from etf.notifications import enqueue
    enqueue(result,j)
   return 0
  finally:
   j.db.close()
   if isolated:isolated.cleanup()
if __name__=='__main__':
 try:raise SystemExit(main())
 except Exception as exc:
  from tools.ops_utils import redact_payload
  error=redact_payload(type(exc).__name__+':'+str(exc))[:240]
  print(json.dumps(dict(status='BLOCKED',reason=error),ensure_ascii=False))
  if '--live' in __import__('sys').argv:
   try:
    from etf.notifications import enqueue
    enqueue(dict(selected=None,reason=error),None)
   except Exception:pass
  raise SystemExit(2)

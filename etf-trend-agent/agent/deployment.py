"""Operator-run staging utility. Does not start, stop or register any service."""
import argparse,hashlib,json,shutil,sqlite3,plistlib,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parent

def prepare(source,destination):
 source=Path(source).resolve();destination=Path(destination).resolve()
 if destination==source or source in destination.parents or destination.exists():raise ValueError('fresh_separate_destination_required')
 original=json.loads((source/'config/runtime.json').read_text())
 if original.get('deployment_authorized') is not True:raise ValueError('legacy_runtime_unverified')
 # Preparation is allowed while old services exist, but activation requires all
 # old ETF executors to be unloaded and an explicit fresh read-only acceptance.
 destination.mkdir(parents=True,mode=0o700)
 shutil.copytree(ROOT,destination/'agent',ignore=shutil.ignore_patterns('__pycache__','.pytest_cache'))
 if (ROOT.parent/'audit').exists():shutil.copytree(ROOT.parent/'audit',destination/'audit',ignore=shutil.ignore_patterns('__pycache__'))
 for name in ['config','state','evidence','data','logs']:(destination/name).mkdir(mode=0o700)
 from etf.journal import clone_legacy
 clone_legacy(source/'state/v1_state.sqlite',destination/'state/v1_state.sqlite');(destination/'state/v1_state.sqlite').chmod(0o600)
 if (source/'state/operations.sqlite').exists():
  clone_legacy(source/'state/operations.sqlite',destination/'state/operations.sqlite');(destination/'state/operations.sqlite').chmod(0o600)
 shutil.copy2(source/'state/live_execution_state.json',destination/'state/live_execution_state.json')
 policy_hash=hashlib.sha256((destination/'agent/etf/policy.json').read_bytes()).hexdigest()
 from run_agent import implementation_hash
 config=dict(previous_policy_hash=original['policy_hash'],implementation_hash=implementation_hash(),account_hash=original['account_hash'],credential_config=original['credential_config'],shared_executor_lock=str(source/'state/cycle.lock'),policy_hash=policy_hash,deployment_authorized=False,previous_runtime=str(source),executor_label='com.qqqsmh.agent.return-first')
 p=destination/'config/runtime.json';p.write_text(json.dumps(config,indent=2));p.chmod(0o600)
 return destination

def render_plist(destination,python):
 destination=Path(destination).resolve();python=str(Path(python).absolute())
 plist=dict(Label='com.qqqsmh.agent.return-first',ProgramArguments=[python,str(destination/'agent/run_agent.py'),'--live'],WorkingDirectory=str(destination),StartInterval=60,RunAtLoad=True,StartCalendarInterval=[dict(Hour=h,Minute=m) for h,m in [(10,5),(12,30),(15,30)]],StandardOutPath=str(destination/'logs/scheduler.stdout.log'),StandardErrorPath=str(destination/'logs/scheduler.stderr.log'))
 path=destination/'com.qqqsmh.agent.return-first.plist';path.write_bytes(plistlib.dumps(plist))
 report=dict(Label='com.qqqsmh.agent.return-first-reports',ProgramArguments=[python,str(destination/'agent/run_notifications.py')],WorkingDirectory=str(destination),StartInterval=60,StandardOutPath=str(destination/'logs/reports.stdout.log'),StandardErrorPath=str(destination/'logs/reports.stderr.log'))
 (destination/'com.qqqsmh.agent.return-first-reports.plist').write_bytes(plistlib.dumps(report))
 return path

if __name__=='__main__':
 parser=argparse.ArgumentParser(description='Prepare separate runtime; no service activation')
 parser.add_argument('--source',type=Path,required=True);parser.add_argument('--destination',type=Path,required=True);parser.add_argument('--python',type=Path,required=True)
 args=parser.parse_args();destination=prepare(args.source,args.destination);plist=render_plist(destination,args.python)
 print(json.dumps(dict(status='STAGED_NOT_ACTIVATED',destination=str(destination),plist=str(plist),services_started=False,orders_placed=0),indent=2))

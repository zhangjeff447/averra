"""Stock desktop adapter: selected data is read-only; no broker, AI, scheduler or Telegram calls."""
import json, shutil, sqlite3, subprocess, sys
from datetime import date
from contextlib import closing
from pathlib import Path

def initialize(resources, workspace):
    marker=workspace/'.stock-desktop-source-version'
    if marker.exists():return
    workspace.mkdir(parents=True,exist_ok=True,mode=0o700)
    for source in (resources/'payload').iterdir():
        target=workspace/source.name
        if target.exists():raise ValueError('Incomplete app workspace; preserve it and review before recovery.')
        if source.is_dir():shutil.copytree(source,target)
        else:shutil.copy2(source,target)
    marker.write_text('20261009\n');marker.chmod(0o600)

def rows(path, query):
    if not path.is_file():return None
    with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=2)) as db:
        db.execute('PRAGMA query_only=ON');db.row_factory=sqlite3.Row
        return [dict(row) for row in db.execute(query)]

def read_monitor(source):
    if not (source/'stock_agent.py').is_file():raise ValueError('Select a Stock Agent project folder containing stock_agent.py.')
    report={'read_only':True,'broker_connections':0,'network_requests':0,'ai_calls':0,'background_changes':0,'issues':[]}
    queries=[('recent_runs','shadow_daily_state.db','SELECT trading_date,state,completed_at,failed_phase,error_type,telegram_status,telegram_message_id FROM shadow_daily_runs ORDER BY trading_date DESC LIMIT 10'),
             ('held_tickers','stock_agent.db',"SELECT ticker,status FROM tracked_holdings WHERE status='HELD' ORDER BY ticker LIMIT 200"),
             ('market_date','market_data.db',"SELECT MAX(date) AS latest_spy_date FROM daily_bars WHERE ticker='SPY'")]
    for key,file,query in queries:
        try:
            value=rows(source/'data'/file,query)
            report[key]=value
            if value is None:report['issues'].append(key.upper()+'_UNAVAILABLE')
        except sqlite3.Error:report[key]=None;report['issues'].append(key.upper()+'_UNREADABLE')
    report['status']='LOCAL_RECORDS_AVAILABLE' if not report['issues'] else 'LOCAL_RECORDS_INCOMPLETE'
    report['note']='Saved records only; not a live market update, service-health guarantee, or trading recommendation.'
    return report

def verified(workspace):
    try:
        receipt=json.loads((workspace/'.desktop-verified.json').read_text())
        return receipt.get('mode')=='stock' and isinstance(receipt.get('verifiedAt'),str) and bool(receipt['verifiedAt'])
    except (OSError,ValueError):return False

def run(resources,workspace,mode,request):
    if mode!='stock':raise ValueError('Stock mode required')
    action=request.get('action')
    if action not in ('status','run','check','demo'):raise ValueError('Unsupported action')
    initialize(resources,workspace)
    chinese=request.get('language','zh')=='zh'
    if action=='status':return {'ready':verified(workspace),'mode':'stock','execution':'MONITOR_ONLY','workspace':str(workspace)}
    if action=='demo':
        # Only bundled code runs, in a temporary simulation DB; selected projects never execute.
        import tempfile
        with tempfile.TemporaryDirectory(prefix='averra-stock-desktop-example-') as temp:
            result=subprocess.run([sys.executable,str(resources/'payload/phase6_simulation.py')],cwd=temp,env={'PATH':'/usr/bin:/bin','PYTHONDONTWRITEBYTECODE':'1','PYTHONNOUSERSITE':'1'},capture_output=True,text=True,timeout=30)
        explanation=''
        if result.returncode==0:
            example=json.loads(result.stdout)
            held=len(example['tracked_after_buy']);remaining=example['tracked_after_sell']
            explanation=(f'示例流程：确认买入后跟踪 {held} 只股票；确认卖出后剩余 {remaining} 只持仓。模拟数据库已清理，没有真实订单。' if chinese else f'Example flow: {held} stock tracked after a buy confirmation; {remaining} holdings remain after a sell confirmation. The simulation database was cleaned up. No real orders.')
        return {'exitCode':result.returncode,'explanation':explanation,'output':('离线持仓确认示例 · 合成数据 · 不连接网络\n' if chinese else 'OFFLINE HOLDINGS EXAMPLE · SYNTHETIC DATA · NO NETWORK\n')+result.stdout+result.stderr}
    if not verified(workspace):raise ValueError('Verify your Stock key first.')
    source=request.get('source','')
    if not source:return {'exitCode':0,'summary_state':'empty','output':('请在连接与设置中选择已有 Stock Agent 工程目录。此新 App 未启动后台服务，也尚未载入行情或持仓记录。' if chinese else 'Choose an existing Stock Agent folder in Settings. This new app has not started background services or loaded market/holding records.')}
    report=read_monitor(Path(source).expanduser().resolve())
    runs=report.get('recent_runs') or []
    record_date=str(runs[0].get('trading_date') or '') if runs else ''
    summary='incomplete' if report['issues'] else 'empty' if not runs else 'saved'
    if summary=='saved':
        try:
            if (date.today()-date.fromisoformat(record_date)).days>7:summary='stale'
        except ValueError:summary='incomplete'
    return {'exitCode':0,'summary_state':summary,'record_date':record_date,'output':('本地保存状态 · 只读 · 不代表实时扫描已完成\n' if chinese else 'SAVED LOCAL STATE · READ ONLY · NOT A LIVE SCAN\n')+json.dumps(report,ensure_ascii=False,indent=2)}

if __name__=='__main__':
    try:print(json.dumps(run(Path(sys.argv[1]),Path(sys.argv[2]),sys.argv[3],json.load(sys.stdin)),ensure_ascii=False))
    except Exception:print(json.dumps({'error':'Stock local check did not complete. Check the selected folder and its data; no source files or services were changed.'}));sys.exit(1)

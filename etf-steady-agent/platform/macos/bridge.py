"""Desktop-only adapter. No order submission. Monitor cycles return market signals to the desktop."""
import json, os, shutil, signal, subprocess, sys
from decimal import Decimal
from pathlib import Path

def execute(cmd, data, timeout):
    child=subprocess.Popen(cmd,cwd=data,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,start_new_session=True)
    def stop(signum, frame):
        os.killpg(child.pid,signal.SIGTERM)
        raise SystemExit(128+signum)
    previous=signal.signal(signal.SIGTERM,stop)
    try:
        stdout,stderr=child.communicate(timeout=timeout)
        return subprocess.CompletedProcess(cmd,child.returncode,stdout,stderr)
    except subprocess.TimeoutExpired:
        os.killpg(child.pid,signal.SIGTERM)
        child.communicate()
        raise
    finally:signal.signal(signal.SIGTERM,previous)

def initialize(resources, data):
    source=resources/'payload'; data.mkdir(parents=True,exist_ok=True,mode=0o700)
    if not (data/'VERSION').exists():
        for item in source.iterdir():
            target=data/item.name
            if target.exists(): raise RuntimeError('Existing incomplete workspace: retain it and review before recovery.')
            if item.is_dir(): shutil.copytree(item,target)
            else: shutil.copy2(item,target)
    os.chmod(data,0o700)

def run(resources, data, mode, request):
    if mode not in ('agent','monitor'):raise ValueError('Unknown mode')
    initialize(resources,data)
    language=request.get('language','zh')
    def text(zh,en):return zh if language=='zh' else en
    action=request.get('action'); os.chdir(data);sys.path.insert(0,str(data))
    os.environ['PYTHONDONTWRITEBYTECODE']='1'
    for key in list(os.environ):
        if key in ('AGENT_MODE','TRADING_MODE','ALLOW_LIVE_ORDERS','V1_SUBMISSION_APPROVED','FINAL_DEPLOYMENT_AUTHORIZED','TELEGRAM_ENABLED','QQQ_TARGET_WEIGHT','SMH_TARGET_WEIGHT'):os.environ.pop(key)
    if action=='status':
        return {'ready':(data/'.desktop-verified.json').exists(),'workspace':str(data),'version':(data/'VERSION').read_text().strip(),'mode':mode,'execution':'READ_ONLY_VALIDATION' if mode=='agent' else 'MONITOR_ONLY'}
    if action=='demo':
        if mode=='monitor':cmd=[sys.executable,str(resources/'payload/monitor.py'),'--signal-file',str(resources/'payload/signal.example.json'),'--json']
        else:cmd=[sys.executable,'run_agent.py','--check-config']
        result=execute(cmd,data,30)
        return {'exitCode':result.returncode,'output':text('离线示例\n','OFFLINE EXAMPLE\n')+result.stdout+result.stderr}
    if not (data/'.desktop-verified.json').exists():raise RuntimeError('Verify your mode-specific Averra key first.')
    if action=='mcp_connect':
        if mode!='agent':raise ValueError('MCP is not available in Monitor mode')
        from qqq_mcp_adapter import MCPAdapter
        try:
            adapter=MCPAdapter(workspace=str(data),direct_transport=True)
            tools=adapter.transport.list_tools()
            names={item.get('name','').split('__')[-1] for item in tools if isinstance(item,dict)}
            if not {'get_accounts','get_equity_positions'}<=names:
                return {'exitCode':1,'output':text('MCP 已响应，但缺少必要的账户只读工具；不能用于 Agent 验证。','MCP responded, but required read-only account tools are missing. Agent validation is unavailable.')}
            return {'exitCode':0,'output':text('MCP 连接成功。握手与只读工具可用性检查通过；未读取账户、未下单。下一步可点击运行只读验证。','MCP connected. Handshake and read-only tool availability checks passed; no account was read and no order was submitted. You can now run read-only validation.')}
        except Exception:
            return {'exitCode':1,'output':text('MCP 未连接。请确认所选目录包含 config.toml、robinhood-trading 服务和有效登录授权。授权过期时，请先在 Codex 中重新登录 Robinhood MCP，再重试。未读取账户、未下单。','MCP not connected. Check that the selected folder contains config.toml, robinhood-trading, and a valid login. Renew your Robinhood MCP login in Codex if needed, then retry. No account was read and no order was submitted.')}
    if action=='weights' and mode=='agent':
        return {'exitCode':1,'output':text('V3 策略已冻结：SMH 70% / QQQ 20% / SGOV 10%。网站配比仅为草案，不覆盖本机策略。','V3 policy is frozen: SMH 70% / QQQ 20% / SGOV 10%. Website allocations are drafts and do not overwrite local policy.')}
    if action=='weights':
        value=Decimal(str(request.get('qqq')))
        if not value.is_finite() or not 0<=value<=1:raise ValueError('Weight must be between 0 and 1')
        path=data/('.env.production' if mode=='agent' else '.env')
        existing={}
        if path.exists():
            for line in path.read_text().splitlines():
                if '=' in line and not line.startswith('#'):
                    k,v=line.split('=',1);existing[k]=v
        existing.update(QQQ_TARGET_WEIGHT=str(value),SMH_TARGET_WEIGHT=str(1-value))
        if mode=='agent' and set(existing)-{'QQQ_TARGET_WEIGHT','SMH_TARGET_WEIGHT'}:raise RuntimeError('Review existing Agent preferences before changing them.')
        path.write_text(''.join(k+'='+v+'\n' for k,v in existing.items()));path.chmod(0o600)
        return {'output':text('分配比例已保存。QQQ ','Allocation saved. QQQ ')+str(value*100)+'% / SMH '+str((1-value)*100)+'%'}
    if action not in ('check','run','monitor_cycle'):raise ValueError('Unsupported action')
    if mode=='agent':
        if 'etf_cycle_v3' not in (data/'VERSION').read_text():raise RuntimeError('Keep your existing records. Use a new V3 workspace or complete the migration review before this upgrade.')
        if action=='monitor_cycle':raise ValueError('Monitor cycle is unavailable in Agent mode')
        policy=json.loads((data/'config/unified_policy.json').read_text())
        if policy.get('AGENT_MODE')!='READ_ONLY_VALIDATION' or any(policy.get(k)!='false' for k in ('ALLOW_LIVE_ORDERS','V1_SUBMISSION_APPROVED','FINAL_DEPLOYMENT_AUTHORIZED')):raise RuntimeError('Desktop v1 only supports read-only mode. Live policy detected; operation blocked.')
        cmd=[sys.executable,'run_agent.py','--check-config' if action=='check' else '--validate-read-only']
    else:
        symbol=request.get('symbol','QQQ')
        if symbol not in ('QQQ','SMH'):raise ValueError('Only QQQ / SMH supported')
        if action=='check':return {'output':text('纯监控模式：不连接券商、不下单，仅支持 QQQ / SMH。','Monitor mode: no brokerage connection, no order submission. QQQ / SMH only.')}
        if action=='monitor_cycle':
            result=execute([sys.executable,str(resources/'payload/monitor.py'),'--both','--machine'],data,180)
            if result.returncode != 0:return {'exitCode':1,'output':text('行情检查失败，没有新信号；下一轮将重试。','Market check failed; no new signal. The next cycle will retry.')}
            return dict(json.loads(result.stdout),exitCode=0)
        for line in (data/'.env').read_text().splitlines() if (data/'.env').exists() else []:
            if '=' in line and not line.startswith('#'):
                k,v=line.split('=',1)
                if k in ('QQQ_TARGET_WEIGHT','SMH_TARGET_WEIGHT'):os.environ[k]=v.strip()
        cmd=[sys.executable,str(resources/'payload/monitor.py'),'--symbol',symbol,'--json']
    result=execute(cmd,data,180)
    return {'exitCode':result.returncode,'output':result.stdout+result.stderr}

if __name__=='__main__':
    try:
        resources=Path(sys.argv[1]);data=Path(sys.argv[2]);mode=sys.argv[3];request=json.load(sys.stdin)
        result=run(resources,data,mode,request);print(json.dumps(result,ensure_ascii=False))
    except Exception as error:print(json.dumps({'error':str(error)},ensure_ascii=False));sys.exit(1)

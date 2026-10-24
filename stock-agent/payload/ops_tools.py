"""Read-only history/diagnostics. Never imported by scoring or Entry/Exit logic."""
import json
from pathlib import Path
from agent_ops import Operations,category
ROOT=Path(__file__).resolve().parent
CONFIGS=['config.json','shadow_v11_config.json','phase4_config.json','phase4_2_config.json','phase4_tiers_config.json','quant_config.json','phase5_config.json','phase6_config.json','intraday_config.json']


def observe(root=ROOT):
    store=Operations(root/'data/operations.sqlite')
    settings={n:json.loads((root/n).read_text()) for n in CONFIGS if (root/n).exists()}
    store.version('STOCK_MONITOR_ONLY',dict(settings=settings,broker_access=False,model_history_is_explanatory_only=True))
    return store


def index_history(root=ROOT):
    store=observe(root)
    try:
        for path in sorted((root/'data/shadow_daily').glob('*/research.json')):
            day=path.parent.name
            if store.source_changed(path):
                packet=json.loads(path.read_text())
                for case in packet.get('cases',[]):
                    ticker=case.get('ticker')
                    if ticker:store.index(ticker,day,str(path.relative_to(root)),json.dumps(case,ensure_ascii=False,sort_keys=True))
                store.source_done(path)
            final=path.with_name('final.json')
            if final.exists() and store.source_changed(final):
                result=json.loads(final.read_text());run='stock:'+day
                store.event(run,'PIPELINE_COMPLETE',dict(market_data_as_of=result.get('market_data_as_of'),generated_at=result.get('generated_at'),phase_statuses=result.get('phase_statuses'),luna_calls=result.get('luna_calls'),research=result.get('research')))
                store.source_done(final)
        research_db=root/'data/research_data.db'
        if research_db.exists():
            import sqlite3
            with sqlite3.connect('file:'+str(research_db)+'?mode=ro',uri=True) as db:
                db.row_factory=sqlite3.Row
                present={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if 'evidence_reasoner_results' in present:
                    for row in db.execute("SELECT ticker,evidence_asof,result_json,created_at FROM evidence_reasoner_results WHERE research_status='COMPLETE'"):
                        body=json.loads(row['result_json'])
                        if isinstance(body,dict):
                            packet=dict(body,record_type='VERIFIED_SAVED_RESEARCH',evidence_as_of=row['evidence_asof'],saved_at=row['created_at'])
                            store.index(row['ticker'],row['created_at'][:10],'research_data.db/evidence_reasoner_results',json.dumps(packet,ensure_ascii=False,sort_keys=True))
        state=root/'data/shadow_daily_state.db'
        if state.exists():
            import sqlite3
            with sqlite3.connect('file:'+str(state)+'?mode=ro',uri=True) as db:
                db.row_factory=sqlite3.Row
                for row in db.execute('SELECT * FROM shadow_daily_runs ORDER BY trading_date DESC LIMIT 15'):
                    row=dict(row);run='stock:'+row['trading_date']
                    store.event(run,'SCHEDULER_AND_DELIVERY',dict(state=row.get('state'),phase=row.get('failed_phase'),telegram_status=row.get('telegram_status'),message_id=row.get('telegram_message_id')))
                    error=row.get('error_type') or row.get('state','')
                    if row.get('state') in ('FAILED','MISSED_DATA_WINDOW'):store.fault(run,category(error))
    finally:store.close()


def completed(day,report):
    try:index_history()
    except Exception:pass  # Archiving cannot change a completed research result.



def research_summary(case):
    labels={'BULL_CASE':'原报告看好理由','BEAR_CASE':'原报告看空理由','KEY_CATALYST':'催化因素','KEY_RISK':'主要风险','THESIS_INVALIDATION':'观点失效条件','NEW_INFORMATION':'当时的新信息'}
    claims=case.get('claims') or {};lines=[]
    if isinstance(claims,list):claims={kind:[c for c in claims if c.get('kind')==kind] for kind in labels}
    for kind,label in labels.items():
        items=claims.get(kind,[]) if isinstance(claims,dict) else []
        if items:
            item=items[0];body=item.get('text','') if isinstance(item,dict) else str(item)
            lines.append(label+'：'+body[:180])
    if not lines:
        mode={'VALID_CACHE':'复用已有研究','FULL_RESEARCH_REQUIRED':'当时需要首次研究','INITIAL_REFRESH_REQUIRED':'当时需要更新研究','RESEARCH_COMPLETE':'研究已完成'}.get(case.get('cache_status'),'已保存的研究记录')
        lines.append(mode)
        if case.get('reason'):lines.append('记录说明：'+str(case['reason'])[:250])
    if case.get('evidence_as_of'):lines.append('证据日期：'+str(case['evidence_as_of']))
    for key,label in [('research_score','研究评分'),('conviction_score','确信度评分')]:
        if case.get(key) is not None:lines.append(label+'：'+str(case[key]))
    return '\n'.join(lines)[:850]


def query(command,parts,root=ROOT):
    index_history(root);store=observe(root)
    try:
        if command=='/research_history':
            if len(parts)<2:return '用法：/research_history TICKER [关键词]'
            ticker=parts[1].upper();words=' '.join(parts[2:])
            for chinese,english in {'财报':'earnings','风险':'risk','看好':'bull','负债':'debt','催化':'catalyst','监管':'regulatory'}.items():words=words.replace(chinese,english)
            rows=store.search(ticker,words,limit=3)
            lines=['历史研究 | '+ticker+' | 仅供回顾，不参与当前评分']
            for row in rows:
                case=json.loads(row['body'])
                lines+=['',row['day']+' | 已保存报告',research_summary(case)]
            return '\n'.join(lines+['','历史记录不代表当前仍然有效，请以今日报告的原有核验结果为准。']) if rows else '\n'.join(lines+['没有匹配的已保存研究。'])
        if command=='/settings_history':
            return 'Stock 设置历史（仅本 Agent）\n'+'\n'.join('版本 '+str(r['id'])+' | '+r['at']+' | '+','.join(r['changed_keys']) for r in store.versions(limit=5))
        status=store.status();lines=['Stock 运行记录 | 仅监测','历史研究条目：'+str(status['research_documents'])]
        state_names={'COMPLETE':'已完成','FAILED':'处理失败','WAITING_FOR_DATA':'等待数据','MISSED_DATA_WINDOW':'错过数据窗口','RUNNING':'正在处理'}
        sent_names={'SENT':'已送达','PENDING':'等待发送','UNKNOWN':'送达情况待核对','NOT_READY':'等待报告完成','IN_FLIGHT':'正在发送'}
        for e in status['latest_events'][:3]:
            payload=e['payload'];state=state_names.get(payload.get('state'),'报告已保存')
            lines.append(e['run_id'].replace('stock:','')+'：'+state)
            if payload.get('telegram_status'):lines.append('通知：'+sent_names.get(payload['telegram_status'],'待核实'))
        lines+=['诊断：'+('；'.join(f['meaning']+'：'+f['next'] for f in status['faults'][:2] if not f.get('resolved_at')) or '无待处理故障'),'无券商或下单连接。']
        return '\n'.join(lines)
    finally:store.close()

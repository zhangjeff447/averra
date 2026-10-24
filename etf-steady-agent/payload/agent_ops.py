"""Local operational records only. No broker, strategy, model, or network access."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
from datetime import datetime,timezone


def now():return datetime.now(timezone.utc).isoformat()
def canonical(value):return json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(',',':'))
def digest(value):return hashlib.sha256(canonical(value).encode()).hexdigest()


def clean(value):
    if isinstance(value,dict):
        return {str(k):clean(v) for k,v in value.items() if not re.search(r'(^|_)(token|secret|password|credential|account_number|api_key|access_code|cookie|authorization)(_|$)',str(k),re.I)}
    if isinstance(value,(tuple,list)):return [clean(v) for v in value]
    if value is None or isinstance(value,(str,int,float,bool)):return value
    return str(value)


RUNBOOKS={
 'AUTH_REQUIRED':{'meaning':'账户连接需要重新验证','next':'重新连接后进行只读验收；保持下单保护'},
 'VALUATION_UNRECONCILED':{'meaning':'账户总值与持仓估值未对齐','next':'重新读取同一账户和报价并核对；不放宽估值校验'},
 'DATA_NOT_READY':{'meaning':'行情或证据尚未满足原有时效要求','next':'等待原有允许窗口；保留原数据门槛'},
 'DELIVERY_UNKNOWN':{'meaning':'消息可能已送达，但没有获得可确认的回执','next':'核对原消息；不自动重发、不重跑订单'},
 'DELIVERY_REJECTED':{'meaning':'Telegram 明确拒绝了消息','next':'检查发送配置或限流；只处理通知'},
 'REPORT_DATA_UNAVAILABLE':{'meaning':'报告数据暂未核实','next':'仅重试只读查询；不产生订单'},
 'OPERATIONS_UNAVAILABLE':{'meaning':'运行记录暂时无法写入','next':'检查本机磁盘与独立运行记录库；不修改交易账本'},
}

def category(reason):
    text=str(reason).lower()
    if any(x in text for x in ('401','unauthorized','auth','credential')):return 'AUTH_REQUIRED'
    if 'portfolio_value_unreconciled' in text:return 'VALUATION_UNRECONCILED'
    if any(x in text for x in ('stale','waiting_for_data','missed_data_window','data_not_ready')):return 'DATA_NOT_READY'
    return 'REPORT_DATA_UNAVAILABLE'


class Operations:
    def __init__(self,path):
        path=Path(path);path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
        self.path=path;self.db=sqlite3.connect(path,timeout=5,isolation_level=None);self.db.row_factory=sqlite3.Row
        self.db.execute('PRAGMA busy_timeout=5000')
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS settings_versions(id INTEGER PRIMARY KEY,scope TEXT NOT NULL,at TEXT NOT NULL,origin TEXT NOT NULL,hash TEXT NOT NULL,payload TEXT NOT NULL,changed_keys TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS run_events(id INTEGER PRIMARY KEY,run_id TEXT NOT NULL,stage TEXT NOT NULL,at TEXT NOT NULL,payload TEXT NOT NULL,hash TEXT NOT NULL,UNIQUE(run_id,stage,hash));
          CREATE TABLE IF NOT EXISTS faults(id INTEGER PRIMARY KEY,run_id TEXT NOT NULL,category TEXT NOT NULL,at TEXT NOT NULL,UNIQUE(run_id,category));
          CREATE TABLE IF NOT EXISTS outbox(key TEXT PRIMARY KEY,kind TEXT NOT NULL,body TEXT NOT NULL,state TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,attempts INTEGER NOT NULL DEFAULT 0,message_id INTEGER,error TEXT);
          CREATE TABLE IF NOT EXISTS indexed_sources(path TEXT PRIMARY KEY,size INTEGER NOT NULL,mtime_ns INTEGER NOT NULL);
          CREATE TABLE IF NOT EXISTS documents(id INTEGER PRIMARY KEY,ticker TEXT NOT NULL,day TEXT NOT NULL,source TEXT NOT NULL,body TEXT NOT NULL,hash TEXT NOT NULL,at TEXT NOT NULL,UNIQUE(ticker,day,source,hash));
        ''')
        if 'resolved_at' not in {r[1] for r in self.db.execute('PRAGMA table_info(faults)')}:self.db.execute('ALTER TABLE faults ADD COLUMN resolved_at TEXT')
        os.chmod(path,0o600)
    def close(self):self.db.close()
    def version(self,scope,payload,origin='OBSERVED_EFFECTIVE_CONFIGURATION'):
        value=clean(payload);fingerprint=digest(value)
        row=self.db.execute('SELECT * FROM settings_versions WHERE scope=? ORDER BY id DESC LIMIT 1',(scope,)).fetchone()
        if row and row['hash']==fingerprint and (origin!='USER_CONFIRMED_UI' or row['origin']==origin):return row['id']
        old=json.loads(row['payload']) if row else {}
        keys=sorted(k for k in set(old)|set(value) if old.get(k)!=value.get(k))
        return self.db.execute('INSERT INTO settings_versions(scope,at,origin,hash,payload,changed_keys) VALUES(?,?,?,?,?,?)',(scope,now(),origin,fingerprint,canonical(value),canonical(keys))).lastrowid
    def versions(self,scope=None,limit=20):
        rows=self.db.execute('SELECT * FROM settings_versions '+('WHERE scope=? ' if scope else '')+'ORDER BY id DESC LIMIT ?',((scope,limit) if scope else (limit,))).fetchall()
        return [dict(r,payload=json.loads(r['payload']),changed_keys=json.loads(r['changed_keys'])) for r in rows]
    def event(self,run_id,stage,payload):
        value=clean(payload)
        self.db.execute('INSERT OR IGNORE INTO run_events(run_id,stage,at,payload,hash) VALUES(?,?,?,?,?)',(run_id,stage,now(),canonical(value),digest(value)))
    def events(self,run_id=None,limit=50):
        rows=self.db.execute('SELECT * FROM run_events '+('WHERE run_id IN (?,?,?) ' if run_id else '')+'ORDER BY id DESC LIMIT ?',((run_id,'decision:'+run_id,'decision:'+run_id+':blocked',limit) if run_id else (limit,))).fetchall()
        return [dict(r,payload=json.loads(r['payload'])) for r in rows]
    def fault(self,run_id,kind):
        if kind not in RUNBOOKS:kind=category(kind)
        self.db.execute('INSERT OR IGNORE INTO faults(run_id,category,at) VALUES(?,?,?)',(run_id,kind,now()))
        self.db.execute('UPDATE faults SET resolved_at=NULL WHERE run_id=? AND category=?',(run_id,kind))
    def resolve(self,run_id):
        self.db.execute('UPDATE faults SET resolved_at=? WHERE run_id=? AND resolved_at IS NULL',(now(),run_id))
    def diagnoses(self):
        return [dict(r,**RUNBOOKS[r['category']]) for r in self.db.execute('SELECT * FROM faults ORDER BY id DESC LIMIT 20')]
    def enqueue(self,key,kind,text):
        self.db.execute("INSERT OR IGNORE INTO outbox(key,kind,body,state,created_at,updated_at) VALUES(?,?,?,'PENDING',?,?)",(key,kind,text,now(),now()))
        return dict(self.db.execute('SELECT * FROM outbox WHERE key=?',(key,)).fetchone())
    def message(self,key):
        row=self.db.execute('SELECT * FROM outbox WHERE key=?',(key,)).fetchone();return dict(row) if row else None
    def claim(self,key):
        row=self.db.execute("UPDATE outbox SET state='SENDING',attempts=attempts+1,updated_at=? WHERE key=? AND state='PENDING'",(now(),key))
        return row.rowcount==1
    def finish(self,key,state,*,message_id=None,error=None):
        if state not in ('SENT','UNKNOWN','REJECTED','PENDING'):raise ValueError('invalid_delivery_state')
        if state=='SENT' and (type(message_id) is not int or message_id<=0):raise ValueError('confirmed_message_id_required')
        self.db.execute("UPDATE outbox SET state=?,message_id=?,error=?,updated_at=? WHERE key=? AND state='SENDING'",(state,message_id,error,now(),key))
        self.event(key,'NOTIFICATION_'+state,dict(message_id=message_id,error=error))
        if state=='SENT':self.resolve(key)
        if state in ('UNKNOWN','REJECTED'):self.fault(key,'DELIVERY_UNKNOWN' if state=='UNKNOWN' else 'DELIVERY_REJECTED')
    def retry_rejected(self,key):
        return self.db.execute("UPDATE outbox SET state='PENDING',error=NULL,updated_at=? WHERE key=? AND state='REJECTED' AND message_id IS NULL",(now(),key)).rowcount==1
    def interrupted_sends(self):
        # Caller holds the single delivery-process lock, so no sender is active.
        for r in self.db.execute("SELECT key FROM outbox WHERE state='SENDING'").fetchall():self.finish(r['key'],'UNKNOWN',error='INTERRUPTED_SEND')
    def source_changed(self,path):
        path=Path(path);stat=path.stat();row=self.db.execute('SELECT size,mtime_ns FROM indexed_sources WHERE path=?',(str(path),)).fetchone()
        return not row or tuple(row)!=(stat.st_size,stat.st_mtime_ns)
    def source_done(self,path):
        path=Path(path);stat=path.stat();self.db.execute('INSERT OR REPLACE INTO indexed_sources VALUES(?,?,?)',(str(path),stat.st_size,stat.st_mtime_ns))
    def index(self,ticker,day,source,body):
        self.db.execute('INSERT OR IGNORE INTO documents(ticker,day,source,body,hash,at) VALUES(?,?,?,?,?,?)',(ticker,day,source,body,digest(body),now()))
    def search(self,ticker,query='',limit=5):
        clauses=['ticker=?'];params=[ticker.upper()]
        for word in query[:120].split()[:5]:clauses.append('instr(lower(body),lower(?))>0');params.append(word)
        return [dict(r) for r in self.db.execute('SELECT ticker,day,source,body,hash FROM documents WHERE '+' AND '.join(clauses)+' ORDER BY day DESC,id DESC LIMIT ?',(*params,min(limit,10)))]
    def status(self):
        return dict(scope_versions=[dict(r) for r in self.db.execute('SELECT scope,MAX(id) AS latest_revision FROM settings_versions GROUP BY scope')],
                    delivery={r[0]:r[1] for r in self.db.execute('SELECT state,COUNT(*) FROM outbox GROUP BY state')},
                    latest_events=self.events(limit=8),faults=self.diagnoses(),research_documents=self.db.execute('SELECT COUNT(*) FROM documents').fetchone()[0])


def safe_event(path,run_id,stage,payload):
    try:
        store=Operations(path)
        try:store.event(run_id,stage,payload)
        finally:store.close()
    except (OSError,sqlite3.Error):pass


def safe_fault(path,run_id,reason):
    try:
        store=Operations(path)
        try:store.fault(run_id,reason)
        finally:store.close()
    except (OSError,sqlite3.Error):pass


def safe_resolve(path,run_id):
    try:
        store=Operations(path)
        try:store.resolve(run_id)
        finally:store.close()
    except (OSError,sqlite3.Error):pass


def main():
    p=argparse.ArgumentParser(description='Read local operations; never execute trades or model calls')
    p.add_argument('--db',required=True);p.add_argument('action',choices=['status','settings','runs','diagnose','research','retry-rejected']);p.add_argument('--key');p.add_argument('--run');p.add_argument('--ticker');p.add_argument('--query',default='');args=p.parse_args()
    store=Operations(args.db)
    try:
        result={'retry-rejected':lambda:{'requeued':store.retry_rejected(args.key or '')},'status':store.status,'settings':store.versions,'runs':lambda:store.events(args.run),'diagnose':store.diagnoses,'research':lambda:store.search(args.ticker or '',args.query)}[args.action]()
        print(json.dumps(result,ensure_ascii=False,indent=2))
    finally:store.close()

if __name__=='__main__':main()

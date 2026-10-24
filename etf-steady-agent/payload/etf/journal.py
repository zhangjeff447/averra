"""One SQLite account journal: BUY money, SELL shares, fills and phase memory."""
import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo
from .cycle import authorize_order, money, down, POLICY
from .funds import timestamp

ACTIVE=('RESERVED','SUBMITTING','SUBMITTED','AWAITING_USER_APPROVAL','EXECUTION_UNKNOWN',
        'PARTIALLY_FILLED','CANCEL_PENDING','RECONCILIATION_REQUIRED')
TERMINAL=('FILLED','CANCELED','REJECTED')
ET=ZoneInfo('America/New_York')


def now_iso():return datetime.now(timezone.utc).isoformat()


class Journal:
    def __init__(self,path):
        path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
        self.db=sqlite3.connect(path,isolation_level=None,timeout=15)
        self.db.row_factory=sqlite3.Row
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS etf_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS etf_intents(
            id TEXT PRIMARY KEY,decision_id TEXT UNIQUE NOT NULL,symbol TEXT NOT NULL,side TEXT NOT NULL,
            amount TEXT NOT NULL,quantity TEXT NOT NULL,status TEXT NOT NULL,broker_id TEXT UNIQUE,
            attempts INTEGER NOT NULL DEFAULT 0,created_at TEXT NOT NULL,plan TEXT NOT NULL);
          CREATE UNIQUE INDEX IF NOT EXISTS etf_one_unresolved ON etf_intents((1))
            WHERE status NOT IN ('FILLED','CANCELED','REJECTED');
          CREATE TABLE IF NOT EXISTS etf_fills(id TEXT PRIMARY KEY,intent_id TEXT NOT NULL,
            gross TEXT NOT NULL,fees TEXT NOT NULL,quantity TEXT NOT NULL,executed_at TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS etf_audit(seq INTEGER PRIMARY KEY AUTOINCREMENT,
            event TEXT NOT NULL,detail TEXT NOT NULL,at TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS etf_slots(slot TEXT PRIMARY KEY,status TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS etf_funding_events(id TEXT PRIMARY KEY,kind TEXT NOT NULL,
            net_amount TEXT NOT NULL,received_at TEXT NOT NULL);
        ''')

    @contextmanager
    def transaction(self):
        self.db.execute('BEGIN IMMEDIATE')
        try:yield
        except BaseException:self.db.execute('ROLLBACK');raise
        else:self.db.execute('COMMIT')

    def audit(self,event,detail):
        self.db.execute('INSERT INTO etf_audit(event,detail,at) VALUES(?,?,?)',(event,json.dumps(detail,sort_keys=True),now_iso()))

    def load(self,key,default=None):
        row=self.db.execute('SELECT value FROM etf_meta WHERE key=?',(key,)).fetchone()
        return json.loads(row[0]) if row else default

    def save(self,key,value):
        self.db.execute('INSERT INTO etf_meta VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(key,json.dumps(value,sort_keys=True)))

    def bind_account(self,account):
        with self.transaction():
            old=self.load('account_hash')
            if old and old!=account:raise ValueError('account_identity_changed')
            self.save('account_hash',account)

    def halt_reason(self):
        reason=self.load('halt_reason')
        if reason:return reason
        if 'meta' in {r[0] for r in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}:
            row=self.db.execute("SELECT value FROM meta WHERE key='halt_reason'").fetchone()
            if row:return row[0]
        return None

    def legacy_active(self):
        tables={r[0] for r in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'intents' not in tables:return []
        return [dict(r) for r in self.db.execute("SELECT * FROM intents WHERE status NOT IN ('FILLED','CANCELED','REJECTED')")]

    def active(self):
        return [dict(r) for r in self.db.execute("SELECT * FROM etf_intents WHERE status NOT IN ('FILLED','CANCELED','REJECTED')")]

    def get(self,iid):
        row=self.db.execute('SELECT * FROM etf_intents WHERE id=?',(iid,)).fetchone()
        return dict(row) if row else None

    def buy_events(self,now):
        day=now.astimezone(ET).date()
        rows=[dict(r) for r in self.db.execute("SELECT symbol,side,created_at,status,id,json_extract(plan,'$.score') AS score FROM etf_intents WHERE attempts>0")]
        tables={r[0] for r in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'intents' in tables:
            rows += [dict(r) for r in self.db.execute("SELECT ticker AS symbol,'BUY' AS side,created_at,status,intent_id AS id,score,cumulative_filled_cents FROM intents WHERE submit_attempts>0")]
        for row in rows:
            if row['side']!='BUY':continue
            if 'cumulative_filled_cents' in row:
                fills=self.db.execute('SELECT observed_at FROM fills WHERE intent_id=?',(row['id'],)).fetchall()
            else:
                fills=self.db.execute('SELECT executed_at FROM etf_fills WHERE intent_id=?',(row['id'],)).fetchall()
            if any(timestamp(f[0]).astimezone(ET).date()==day for f in fills):row['fill_today']=True
        return sorted([r for r in rows if r['side']=='BUY' and (timestamp(r['created_at']).astimezone(ET).date()==day or r.get('fill_today'))],key=lambda r:(r['created_at'],r['id']))

    def gates(self,scores,now):
        rows=self.buy_events(now);result={}
        smh=[r for r in rows if r['symbol']=='SMH'];qqq=[r for r in rows if r['symbol']=='QQQ']
        if smh:result['SMH']='SMH_DAILY_BUY_LIMIT'
        if len(qqq)>=2:result['QQQ']='QQQ_DAILY_BUY_LIMIT'
        elif qqq:
            if qqq[0]['status']!='FILLED' or (qqq[0].get('cumulative_filled_cents',0)<=0 and self.filled_gross(qqq[0]['id'])<=0):result['QQQ']='FIRST_QQQ_BUY_NOT_FILLED'
            elif qqq[0]['score'] is None:result['QQQ']='FIRST_QQQ_SCORE_UNVERIFIED'
            elif not (money(qqq[0]['score'])>=50 and money(scores.get('QQQ'))>=50 or money(scores.get('QQQ'))>=money(qqq[0]['score'])+10):
                result['QQQ']='QQQ_SECOND_BUY_SCORE_INCREASE_REQUIRED'
        return result

    def daily_spend(self,side,now,*,exclude_intent=None):
        day=now.astimezone(ET).date();total=Decimal(0)
        for r in self.db.execute('SELECT f.*,i.side FROM etf_fills f JOIN etf_intents i ON i.id=f.intent_id WHERE i.side=?',(side,)):
            if timestamp(r['executed_at']).astimezone(ET).date()==day:
                total+=money(r['gross'])+(money(r['fees']) if side=='BUY' else 0)
        tables={r[0] for r in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if side=='BUY' and 'fills' in tables:
            for r in self.db.execute('SELECT cents,observed_at FROM fills'):
                if timestamp(r['observed_at']).astimezone(ET).date()==day:total+=Decimal(r['cents'])/100
        for r in self.active():
            if r['id']!=exclude_intent and r['side']==side:
                filled=self.filled_gross(r['id'])
                if side=='BUY':filled+=sum((money(f[0]) for f in self.db.execute('SELECT fees FROM etf_fills WHERE intent_id=?',(r['id'],))),Decimal(0))
                total+=max(Decimal(0),money(r['amount'])-filled)
        return total

    def filled_gross(self,iid):
        return sum((money(r[0]) for r in self.db.execute('SELECT gross FROM etf_fills WHERE intent_id=?',(iid,))),Decimal(0))

    def total_flow(self,side):
        total=Decimal(0)
        for r in self.db.execute('SELECT gross,fees FROM etf_fills f JOIN etf_intents i ON i.id=f.intent_id WHERE i.side=?',(side,)):
            total+=money(r[0])+(money(r[1]) if side=='BUY' else -money(r[1]))
        if side=='BUY' and 'fills' in {r[0] for r in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}:
            total+=sum((Decimal(r[0])/100 for r in self.db.execute('SELECT cents FROM fills')),Decimal(0))
        return total

    def cooled(self,side,session_dates):
        other='SELL' if side=='BUY' else 'BUY'
        rows=self.db.execute("SELECT f.executed_at FROM etf_fills f JOIN etf_intents i ON i.id=f.intent_id WHERE i.symbol='SGOV' AND i.side=?",(other,)).fetchall()
        if not rows:return True
        last=max(timestamp(r[0]).astimezone(ET).date().isoformat() for r in rows)
        return sum(d>last for d in session_dates)>=POLICY['sgov_reverse_cooldown_sessions']

    def reserve(self,decision_id,order,snapshot,now=None):
        now=now or datetime.now(timezone.utc)
        authorize_order(order['symbol'],order['side'])
        amount=money(order['amount'])
        if amount!=down(amount) or amount<1 or amount>100:raise ValueError('order_amount_limit')
        with self.transaction():
            if self.halt_reason():raise ValueError('trading_halted')
            if self.active() or self.legacy_active():raise ValueError('one_unresolved_intent')
            if self.load('account_hash')!=snapshot['account_hash']:raise ValueError('account_not_bound')
            remaining=Decimal(100)-self.daily_spend(order['side'],now)
            if amount>remaining:raise ValueError('daily_limit')
            qty=Decimal(0)
            if order['side']=='BUY':
                if amount>money(snapshot['safe_cash']):raise ValueError('cash_limit')
                if self.gates(order.get('all_scores',{}),now).get(order['symbol']):raise ValueError('symbol_daily_gate')
            else:
                bid=money(snapshot['quotes']['SGOV']['bid'])
                # Conservative share quantity; actual net proceeds only exist
                # after dated fills. Never turn a sale estimate into cash.
                estimated=(amount/bid).quantize(Decimal('.000001'),rounding='ROUND_DOWN')
                available=money(snapshot['quantities']['SGOV'])
                if estimated>available:raise ValueError('sell_share_limit')
                # Dollar SELL shares vary with price. Fence all available shares
                # while the account has one unresolved intent; never allow short.
                qty=available
                if qty<=0 or qty>money(snapshot['quantities']['SGOV']):raise ValueError('sell_share_limit')
            iid=str(uuid.uuid4())
            self.db.execute('INSERT INTO etf_intents VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                            (iid,decision_id,order['symbol'],order['side'],str(amount),str(qty),'RESERVED',None,0,now.isoformat(),json.dumps(order),))
            self.audit('RESERVE',dict(id=iid,symbol=order['symbol'],side=order['side'],amount=str(amount),quantity=str(qty)))
        return self.get(iid)

    def begin_submit(self,iid):
        with self.transaction():
            row=self.get(iid)
            if row['status']!='RESERVED' or row['attempts']!=0:raise ValueError('submit_once')
            self.db.execute("UPDATE etf_intents SET attempts=1,status='SUBMITTING' WHERE id=?",(iid,));self.audit('SUBMIT_ATTEMPT',dict(id=iid))

    def status(self,iid,status,broker_id=None):
        if status not in ACTIVE+TERMINAL:raise ValueError('intent_status_invalid')
        self.db.execute('UPDATE etf_intents SET status=?,broker_id=COALESCE(?,broker_id) WHERE id=?',(status,broker_id,iid))
        self.audit('STATUS',dict(id=iid,status=status))

    def reconcile(self,orders,*,complete):
        if not complete or not isinstance(orders,list):raise ValueError('history_incomplete')
        by_id={};by_ref={}
        for order in orders:
            oid=str(order.get('id') or '')
            ref=str(order.get('ref_id') or '')
            if oid:
                if oid in by_id:raise ValueError('duplicate_broker_order')
                by_id[oid]=order
            if ref:
                if ref in by_ref:raise ValueError('duplicate_broker_ref')
                by_ref[ref]=order
        for row in self.active():
            order=by_id.get(row['broker_id']) if row['broker_id'] else by_ref.get(row['id'])
            if order is None:
                if row['attempts']:self.status(row['id'],'RECONCILIATION_REQUIRED')
                continue
            if not order.get('id') or order.get('symbol')!=row['symbol'] or str(order.get('side')).upper()!=row['side']:
                self.status(row['id'],'RECONCILIATION_REQUIRED');continue
            executions=order.get('executions')
            if not isinstance(executions,list):self.status(row['id'],'RECONCILIATION_REQUIRED',order['id']);continue
            try:
                quantity=Decimal(0);gross=Decimal(0);seen=set()
                with self.transaction():
                    for fill in executions:
                        fid=fill['id'];at=timestamp(fill['timestamp'])
                        if fid in seen or at>datetime.now(timezone.utc) or fill.get('currency')!='USD':raise ValueError('fill_evidence_invalid')
                        seen.add(fid);price=money(fill['price']);qty=money(fill['quantity']);fees=money(fill['fees'])
                        if price<=0 or qty<=0:raise ValueError('fill_nonpositive')
                        g=price*qty;quantity+=qty;gross+=g
                        if row['side']=='SELL' and fees>g:raise ValueError('sell_fee_exceeds_gross')
                        known=self.db.execute('SELECT * FROM etf_fills WHERE id=?',(fid,)).fetchone()
                        facts=(row['id'],str(g),str(fees),str(qty),at.isoformat())
                        if known and tuple(known)[1:]!=facts:raise ValueError('fill_conflict')
                        if not known:self.db.execute('INSERT INTO etf_fills VALUES(?,?,?,?,?,?)',(fid,*facts))
                    if money(order.get('cumulative_quantity'))!=quantity:raise ValueError('cumulative_fill_mismatch')
                    if row['side']=='SELL' and quantity>money(row['quantity']):raise ValueError('sell_fill_exceeds_reserved_shares')
                    if row['side']=='BUY' and gross+sum(money(f['fees']) for f in executions)>money(row['amount'])+Decimal('.01'):
                        raise ValueError('buy_fill_exceeds_reserved_cash')
                    state=order.get('state')
                    if state=='filled' and quantity>0:status='FILLED'
                    elif state in ('canceled','cancelled','expired','voided','partially_filled_rest_cancelled'):status='CANCELED'
                    elif state in ('rejected','failed'):status='REJECTED'
                    elif state in ('pending_approval','awaiting_user_approval','approval_required'):status='AWAITING_USER_APPROVAL'
                    elif state=='pending_cancelled':status='CANCEL_PENDING'
                    elif state in ('open','pending','queued','submitted','confirmed','unconfirmed','partially_filled'):
                        status='PARTIALLY_FILLED' if quantity else 'SUBMITTED'
                    else:status='RECONCILIATION_REQUIRED'
                    self.status(row['id'],status,order['id'])
            except (ValueError,KeyError,ArithmeticError,TypeError):
                self.status(row['id'],'RECONCILIATION_REQUIRED',order['id'])
                self.save('halt_reason','broker_fill_evidence_conflict')

    def observe_external_cash_events(self,events):
        """Audit actual net deposits/dividends once; never add them to broker cash."""
        if not isinstance(events,list):raise ValueError('cash_events_incomplete')
        with self.transaction():
            for event in events:
                if event.get('source')!='broker' or event.get('status')!='completed':
                    raise ValueError('cash_event_not_completed')
                if event.get('kind') not in ('DEPOSIT','DIVIDEND') or not event.get('id'):
                    raise ValueError('external_cash_event_kind_invalid')
                net=money(event.get('net_amount'));at=timestamp(event.get('received_at'))
                if at>datetime.now(timezone.utc):raise ValueError('cash_event_in_future')
                facts=(event['id'],event['kind'],str(net),at.isoformat())
                old=self.db.execute('SELECT * FROM etf_funding_events WHERE id=?',(event['id'],)).fetchone()
                if old and tuple(old)!=facts:raise ValueError('cash_event_conflict')
                if not old:self.db.execute('INSERT INTO etf_funding_events VALUES(?,?,?,?)',facts)

    def claim_slot(self,slot):
        with self.transaction():
            if self.db.execute('SELECT 1 FROM etf_slots WHERE slot=?',(slot,)).fetchone():return False
            self.db.execute("INSERT INTO etf_slots VALUES(?,'STARTED')",(slot,));return True


def clone_legacy(source,target):
    """SQLite backup snapshot keeps EVERY historical table and forward fact."""
    target=Path(target)
    if target.exists():raise ValueError('migration_target_exists')
    target.parent.mkdir(parents=True,exist_ok=True)
    with sqlite3.connect('file:'+str(Path(source).resolve())+'?mode=ro',uri=True) as old:
        if old.execute('PRAGMA quick_check').fetchall()!=[('ok',)]:raise ValueError('legacy_database_invalid')
        with sqlite3.connect(target) as new:old.backup(new)

"""Verify preserved dated fills against complete broker history before allocation."""
from decimal import Decimal,ROUND_HALF_UP
from .cycle import money
from .funds import timestamp


def validate_history(journal,raw):
    if raw.get('complete_orders') is not True:raise ValueError('history_incomplete')
    orders={}
    for o in raw['orders']:
        key=o.get('id')
        if not key or key in orders:raise ValueError('broker_order_identity_conflict')
        orders[key]=o
    tables={r[0] for r in journal.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    verified=0
    if 'meta' in tables:
        row=journal.db.execute("SELECT value FROM meta WHERE key='account_hash'").fetchone()
        if not row or row[0]!=raw['account_hash']:raise ValueError('legacy_account_mismatch')
    if 'fills' in tables:
        for row in journal.db.execute('SELECT f.*,i.broker_order_id,i.ticker FROM fills f JOIN intents i ON i.intent_id=f.intent_id'):
            order=orders.get(row['broker_order_id'])
            if not order or order.get('symbol')!=row['ticker'] or order.get('side')!='buy':raise ValueError('legacy_fill_order_missing')
            matches=[f for f in order.get('executions',[]) if f.get('id')==row['fill_id']]
            if len(matches)!=1:raise ValueError('legacy_fill_identity_mismatch')
            f=matches[0]
            cost=(money(f['price'])*money(f['quantity'])+money(f.get('fees') or 0)).quantize(Decimal('.01'),rounding=ROUND_HALF_UP)
            if int(cost*100)!=row['cents'] or timestamp(f['timestamp'])!=timestamp(row['observed_at']):
                raise ValueError('legacy_fill_facts_changed')
            verified+=1
    for row in journal.db.execute('SELECT f.*,i.broker_id,i.symbol,i.side,i.status FROM etf_fills f JOIN etf_intents i ON i.id=f.intent_id'):
        order=orders.get(row['broker_id'])
        if not order or order.get('symbol')!=row['symbol'] or str(order.get('side')).upper()!=row['side']:raise ValueError('v3_fill_order_missing')
        matches=[f for f in order.get('executions',[]) if f.get('id')==row['id']]
        if len(matches)!=1:raise ValueError('v3_fill_identity_mismatch')
        f=matches[0]
        if (money(f['price'])*money(f['quantity'])!=money(row['gross']) or money(f.get('fees'))!=money(row['fees'])
            or money(f['quantity'])!=money(row['quantity']) or timestamp(f['timestamp'])!=timestamp(row['executed_at'])):
            raise ValueError('v3_fill_facts_changed')
        verified+=1
    activation=journal.load('activation_at')
    if activation:
        known={r[0] for r in journal.db.execute('SELECT broker_id FROM etf_intents WHERE broker_id IS NOT NULL')}
        if 'intents' in tables:known.update(r[0] for r in journal.db.execute('SELECT broker_order_id FROM intents WHERE broker_order_id IS NOT NULL'))
        for o in orders.values():
            if o.get('agent_initiated') is True and timestamp(o['created_at'])>=timestamp(activation) and o['id'] not in known:
                raise ValueError('unattributed_new_agent_order')
    return dict(verified_dated_fills=verified,complete_orders=True)

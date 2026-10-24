"""Verify preserved dated fills against complete broker history before allocation."""
from decimal import Decimal,ROUND_HALF_UP
from datetime import datetime,timezone
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


def validate_activation_positions(journal,raw):
    baseline=journal.load('rf_baseline')
    if not baseline:return
    if raw.get('complete_orders') is not True:raise ValueError('history_incomplete')
    if journal.load('account_hash')!=raw.get('account_hash'):raise ValueError('external_order_account_mismatch')
    known={r[0] for r in journal.db.execute('SELECT broker_id FROM etf_intents WHERE broker_id IS NOT NULL')}
    local_refs={r[0] for r in journal.db.execute('SELECT id FROM etf_intents')}
    if 'intents' in {r[0] for r in journal.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}:
        known.update(r[0] for r in journal.db.execute('SELECT broker_order_id FROM intents WHERE broker_order_id IS NOT NULL'))
        local_refs.update(r[0] for r in journal.db.execute('SELECT intent_id FROM intents'))
    recorded={r[0].removeprefix('rf_external_order:'):journal.load(r[0]) for r in journal.db.execute("SELECT key FROM etf_meta WHERE key LIKE 'rf_external_order:%'")}
    if set(recorded)&known:raise ValueError('external_order_local_identity_conflict')
    if set(recorded)-{o['id'] for o in raw['orders']}:raise ValueError('external_order_history_missing')
    initial=set(baseline['orders']);drip=[];manual=[]
    for order in raw['orders']:
        if order['id'] in initial or order['id'] in known:continue
        if order.get('placed_agent')=='user' and order.get('agent_initiated') is False:
            if order.get('ref_id') in local_refs:raise ValueError('external_order_local_identity_conflict')
            if order.get('symbol') not in baseline['quantities'] or order.get('side') not in ('buy','sell'):
                raise ValueError('external_order_asset_or_side_invalid')
            if order.get('state') not in ('filled','canceled','cancelled','rejected','failed','expired','voided','partially_filled_rest_cancelled'):
                raise ValueError('external_order_not_terminal')
            executions=order.get('executions')
            if not isinstance(executions,list):raise ValueError('external_fill_evidence_missing')
            total=Decimal(0);seen=set();facts=[]
            created=timestamp(order['created_at']);now=datetime.now(timezone.utc)
            if created>now:raise ValueError('external_order_in_future')
            for f in executions:
                if not f.get('id') or f['id'] in seen or f.get('currency')!='USD':raise ValueError('external_fill_evidence_invalid')
                quantity=money(f['quantity']);price=money(f['price']);fees=money(f['fees']);at=timestamp(f['timestamp'])
                if quantity<=0 or price<=0 or not created<=at<=now:raise ValueError('external_fill_evidence_invalid')
                seen.add(f['id']);total+=quantity
                facts.append(dict(id=f['id'],quantity=str(quantity.normalize()),price=str(price.normalize()),fees=str(fees.normalize()),timestamp=at.astimezone(timezone.utc).isoformat(),currency='USD'))
            if total!=money(order['cumulative_quantity']) or (order['state']=='filled' and not executions):
                raise ValueError('external_cumulative_mismatch')
            evidence=dict(classification='EXTERNAL_OR_MANUAL_ACTIVITY',broker_order_id=order['id'],symbol=order['symbol'],side=order['side'],state=order['state'],ref_id=order.get('ref_id'),created_at=created.astimezone(timezone.utc).isoformat(),placed_agent='user',agent_initiated=False,executions=sorted(facts,key=lambda x:x['id']),agent_spend_attributed=False)
            if order['id'] in recorded and recorded[order['id']]!=evidence:raise ValueError('external_order_facts_changed')
            manual.append((order['symbol'],total*(1 if order['side']=='buy' else -1),evidence))
            continue
        if not (order.get('placed_agent')=='drip' and order.get('state')=='filled' and order.get('symbol') in ('QQQ','SMH') and str(order.get('side')).lower()=='buy'):
            raise ValueError('unattributed_post_activation_order')
        executions=order.get('executions')
        if not isinstance(executions,list) or not executions:raise ValueError('drip_evidence_missing')
        total=Decimal(0);seen=set()
        for f in executions:
            if not f.get('id') or f['id'] in seen or f.get('currency')!='USD' or money(f['quantity'])<=0 or money(f['price'])<=0 or money(f['fees'])<0:raise ValueError('drip_evidence_invalid')
            timestamp(f['timestamp']);seen.add(f['id']);total+=money(f['quantity'])
        if total!=money(order['cumulative_quantity']):raise ValueError('drip_cumulative_mismatch')
        drip.append((order['symbol'],total))
    quantities={s:money(q) for s,q in baseline['quantities'].items()}
    for row in journal.db.execute('SELECT f.id,f.quantity,i.symbol,i.side FROM etf_fills f JOIN etf_intents i ON i.id=f.intent_id'):
        if row['id'] in baseline['fills']:continue
        quantities[row['symbol']]+=money(row['quantity'])*(1 if row['side']=='BUY' else -1)
    for symbol,q in drip:quantities[symbol]+=q
    for symbol,q,_ in manual:quantities[symbol]+=q
    for symbol,q in quantities.items():
        if abs(q-money(raw['positions'][symbol]['quantity']))>Decimal('.000001'):raise ValueError('position_history_unreconciled:'+symbol)
    # Record only proven external facts, after the entire position bridge passes.
    # Never create Agent intents/fills, consume action limits or assign rotation.
    new=[evidence for _,_,evidence in manual if evidence['broker_order_id'] not in recorded]
    if new:
        with journal.transaction():
            for evidence in new:
                journal.save('rf_external_order:'+evidence['broker_order_id'],evidence)
                journal.audit('EXTERNAL_OR_MANUAL_ACTIVITY',evidence)

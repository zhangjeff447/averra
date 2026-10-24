"""Shared account BUY caps survive windows, partial fills and restarts."""
from decimal import Decimal
import json
from .cycle import POLICY,money,down
from .funds import timestamp
from .calendar import ET,scheduled


def slot_key(now):
    slot=scheduled(now)
    return now.astimezone(ET).date().isoformat()+':'+slot if slot else None


def usage(journal,day,*,slot=None,symbol=None,exclude_intent=None):
    total=Decimal(0)
    for row in journal.db.execute("SELECT * FROM etf_intents WHERE side='BUY'"):
        if symbol and row['symbol']!=symbol:continue
        plan=json.loads(row['plan']);key=plan.get('window_key') or slot_key(timestamp(row['created_at']))
        if slot and key!=slot:continue
        fills=journal.db.execute('SELECT gross,fees,executed_at FROM etf_fills WHERE intent_id=?',(row['id'],)).fetchall()
        all_filled=sum((money(f['gross'])+money(f['fees']) for f in fills),Decimal(0))
        total+=sum((money(f['gross'])+money(f['fees']) for f in fills if slot or timestamp(f['executed_at']).astimezone(ET).date().isoformat()==day),Decimal(0))
        if row['id']!=exclude_intent and row['status'] not in ('FILLED','CANCELED','REJECTED'):
            total+=max(Decimal(0),money(row['amount'])-all_filled)
    # Preserved legacy Agent fills remain part of today's shared spend.
    tables={r[0] for r in journal.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if 'fills' in tables:
        for row in journal.db.execute('SELECT f.*,i.ticker,i.created_at FROM fills f JOIN intents i ON i.intent_id=f.intent_id'):
            if symbol and row['ticker']!=symbol:continue
            if slot:
                if slot_key(timestamp(row['created_at']))==slot:total+=Decimal(row['cents'])/100
            elif timestamp(row['observed_at']).astimezone(ET).date().isoformat()==day:total+=Decimal(row['cents'])/100
        for row in journal.db.execute("SELECT * FROM intents WHERE status NOT IN ('FILLED','CANCELED','REJECTED')"):
            if symbol and row['ticker']!=symbol:continue
            if slot and slot_key(timestamp(row['created_at']))!=slot:continue
            total+=max(Decimal(0),Decimal(row['reserved_remaining_cents'])/100)
    return total


def allowance(journal,now,*,slot=None,exclude_intent=None):
    if POLICY.get('daily_buy_limit') is None:return Decimal('1000000000')
    day=now.astimezone(ET).date().isoformat();slot=slot or slot_key(now)
    if not slot:return Decimal(0)
    return down(max(Decimal(0),min(money(POLICY['daily_buy_limit'])-usage(journal,day,exclude_intent=exclude_intent),money(POLICY['per_window_buy_limit'])-usage(journal,day,slot=slot,exclude_intent=exclude_intent))))


def enforce(journal,order,now,*,exclude_intent=None):
    if order['side']!='BUY' or POLICY.get('daily_buy_limit') is None:return
    key=slot_key(now)
    if not key or order.get('window_key')!=key:raise ValueError('buy_window_unverified')
    if money(order['amount'])>money(POLICY['per_order_limit']) or money(order['amount'])>allowance(journal,now,slot=key,exclude_intent=exclude_intent):raise ValueError('shared_buy_limit')
    basket=journal.load('rf_window_allocation:'+key)
    if not basket or basket.get('policy_version')!=POLICY['version']:raise ValueError('window_allocation_unverified')
    remaining=money(basket['amounts'].get(order['symbol'],'0'))-usage(journal,now.astimezone(ET).date().isoformat(),slot=key,symbol=order['symbol'],exclude_intent=exclude_intent)
    if money(order['amount'])>remaining:raise ValueError('symbol_window_allocation_limit')

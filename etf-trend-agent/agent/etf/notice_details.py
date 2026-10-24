"""Read-only notification facts; reconciliation runs only in an in-memory copy."""
from datetime import datetime,timezone
from decimal import Decimal
from .cycle import POLICY,money
from .funds import normalize,timestamp
from .journal import Journal
from .reconciliation import validate_history


def collect(journal,raw,result,slot,*,before_order=False):
    if raw.get('account_hash')!=journal.load('account_hash'):
        raise ValueError('notice_account_mismatch')
    age=(datetime.now(timezone.utc)-timestamp(raw['as_of'])).total_seconds()
    if not -5<=age<=60:raise ValueError('notice_snapshot_stale')
    copy=Journal(':memory:')
    try:
        journal.db.backup(copy.db)
        # A failed post-submit refresh must not replace current intent evidence
        # with the earlier order list, even in this disposable report copy.
        if not before_order:
            copy.reconcile(raw['orders'],complete=raw['complete_orders'])
            validate_history(copy,raw)
            if copy.halt_reason():raise ValueError('notice_reconciliation_blocked')
        row=copy.db.execute('SELECT * FROM etf_intents WHERE decision_id=?',(slot,)).fetchone()
        order=None
        if row:
            row=dict(row)
            intent=(result.get('execution') or {}).get('intent_id')
            if intent and intent!=row['id']:raise ValueError('notice_intent_mismatch')
            fills=copy.db.execute('SELECT gross,fees,quantity FROM etf_fills WHERE intent_id=?',(row['id'],)).fetchall()
            gross=sum((money(f['gross']) for f in fills),Decimal(0))
            quantity=sum((money(f['quantity']) for f in fills),Decimal(0))
            # An uncertain submit is an attempt, not proof of acceptance.
            accepted=row['status'] in ('SUBMITTED','PARTIALLY_FILLED','FILLED','CANCEL_PENDING') or bool(row['broker_id'])
            order=dict(symbol=row['symbol'],status=row['status'],
                       submitted=row['amount'] if accepted else None if row['attempts'] else '0.00',
                       filled=str(gross) if fills or row['status'] in ('FILLED','CANCELED','REJECTED','RESERVED') or not row['attempts'] else None,
                       fill_price=str(gross/quantity) if quantity else None)
        data=dict(as_of=raw['as_of'],before_order=before_order,order=order,
                  cash=str(money(raw['funds']['reported_cash'])),quotes={},quote_as_of={},holdings={})
        for symbol in ('QQQ','SMH'):
            quote=raw['quotes'][symbol]
            age=(datetime.now(timezone.utc)-timestamp(quote['timestamp'])).total_seconds()
            if -5<=age<=60 and quote.get('is_stale') is not True:
                data['quotes'][symbol]=str(money(quote['last']))
                data['quote_as_of'][symbol]=quote['timestamp']
        try:
            snapshot=normalize(raw)
        except ValueError:
            snapshot=None
        if snapshot and not before_order:
            data.update(safe_cash=snapshot['safe_cash'],net_assets=snapshot['net_assets'])
            data['holdings']={s:dict(quantity=snapshot['quantities'][s],value=snapshot['values'][s]) for s in ('QQQ','SMH')}
        if not before_order:
            committed=copy.daily_spend('BUY',datetime.now(timezone.utc))
            limit=money(POLICY['daily_buy_limit'])
            data['daily_budget']=dict(limit=str(limit),committed=str(committed),remaining=str(max(Decimal(0),limit-committed)))
        return data
    finally:copy.db.close()

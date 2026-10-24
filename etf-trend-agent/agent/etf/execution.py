"""Reserve, fresh replan, preview and submit once; uncertain results stay held."""
from datetime import datetime,timezone
from .cycle import authorize_order,money
from .funds import normalize
from .calendar import ET
from .recovery import require_window,transient

def current_snapshot(journal,raw):
    now=datetime.now(timezone.utc)
    journal.sync_rotation()
    day=now.astimezone(ET).date().isoformat()
    return normalize(raw,now=now,held_cash=journal.unsettled_sale_cash(day))
from tools.approval_compatibility import approval_required,order_from_response,response_from_error


def execute(journal,broker,decision_id,initial,*,replan,live=False,window=None):
    if not live:raise PermissionError('read_only_execution_denied')
    selected=initial.get('selected')
    if not selected:return dict(status='WAIT',reason=initial['reason'])
    authorize_order(selected['symbol'],selected['side'])
    raw=broker.read()
    journal.reconcile(raw['orders'],complete=raw['complete_orders'])
    from .reconciliation import validate_history,validate_activation_positions
    validate_history(journal,raw);validate_activation_positions(journal,raw)
    if journal.active() or journal.legacy_active():return dict(status='WAIT',reason='unresolved_intent')
    if raw.get('pending_approvals'):return dict(status='WAIT',reason='pending_broker_approval')
    snapshot=current_snapshot(journal,raw)
    fresh=replan(snapshot)
    order=fresh.get('selected')
    stop=selected['side']=='SELL' and selected['reason']=='STOP_LOSS_6_6_ALL_TQQQ'
    if (not order or (order['symbol'],order['side'],order['reason'])!=(selected['symbol'],selected['side'],selected['reason'])
        or (not stop and abs(money(order['amount'])-money(selected['amount']))>5)
        or (stop and money(order['quantity'])!=money(selected['quantity']))):
        return dict(status='WAIT',reason='fresh_plan_changed')
    require_window(window)
    row=journal.reserve(decision_id,order,snapshot)
    try:
        preview=broker.review(row)
        if preview_reason(preview,row,snapshot):
            journal.status(row['id'],'REJECTED');journal.save('halt_reason','broker_pretrade_warning');return dict(status='WAIT',reason=preview_reason(preview,row,snapshot))
        raw=broker.read()
        other=journal.active()
        if len(other)!=1 or other[0]['id']!=row['id'] or journal.legacy_active():raise ValueError('unresolved_intent_changed')
        journal.reconcile(raw['orders'],complete=raw['complete_orders'])
        validate_history(journal,raw);validate_activation_positions(journal,raw)
        if journal.get(row['id'])['status']!='RESERVED':raise ValueError('reserved_intent_changed')
        if raw.get('pending_approvals'):raise ValueError('pending_broker_approval')
        latest=current_snapshot(journal,raw);revised=replan(latest,exclude_intent=row['id']);candidate=revised.get('selected')
        if (not candidate or (candidate['symbol'],candidate['side'],candidate['reason'])!=(row['symbol'],row['side'],order['reason'])
            or (not stop and (money(candidate['amount'])<money(row['amount']) or abs(money(candidate['amount'])-money(row['amount']))>5))
            or (stop and money(candidate['quantity'])!=money(row['quantity']))):
            raise ValueError('post_preview_plan_changed')
        if row['side']=='BUY' and money(row['amount'])>money(latest['safe_cash']):raise ValueError('fresh_cash_shortfall')
        if row['side']=='SELL':
            available=money(raw['positions']['TQQQ'].get('shares_available_for_sells'))
            if money(row['quantity'])>available or money(candidate['quantity'])<money(row['quantity']):raise ValueError('fresh_sell_shares_unavailable')
        age=(datetime.now(timezone.utc)-datetime.fromisoformat(latest['as_of'])).total_seconds()
        if not 0<=age<=60:raise ValueError('pre_submit_stale')
        require_window(window)
    except Exception as exc:
        if journal.get(row['id'])['attempts']==0:
            with journal.transaction():
                journal.status(row['id'],'REJECTED')
                if transient(exc) or str(exc)=='recovery_window_expired':
                    journal.db.execute('DELETE FROM rf_actions WHERE intent_id=?',(row['id'],))
                    journal.audit('PRE_SUBMIT_RECOVERY',dict(id=row['id'],reason=type(exc).__name__,submit_attempts=0))
        raise
    journal.begin_submit(row['id'])
    try:
        response=broker.submit(row)
    except Exception as exc:
        evidence=response_from_error(exc)
        if evidence and approval_required(evidence):
            journal.status(row['id'],'AWAITING_USER_APPROVAL')
            return dict(status='AWAITING_USER_APPROVAL',intent_id=row['id'],retry=False)
        journal.status(row['id'],'EXECUTION_UNKNOWN')
        return dict(status='EXECUTION_UNKNOWN',intent_id=row['id'],retry=False)
    broker_order=order_from_response(response)
    oid=str((broker_order or {}).get('id') or (broker_order or {}).get('order_id') or '')
    if approval_required(response):journal.status(row['id'],'AWAITING_USER_APPROVAL',oid or None)
    elif oid:journal.status(row['id'],'SUBMITTED',oid)
    else:journal.status(row['id'],'EXECUTION_UNKNOWN')
    return dict(status=journal.get(row['id'])['status'],intent_id=row['id'],retry=False)


def preview_reason(preview,row,snapshot):
    """Validate Robinhood's native pre-trade shape, preserving all warnings."""
    data=preview.get('data') if isinstance(preview,dict) else None
    if not isinstance(data,dict) or preview.get('isError') or preview.get('ok') is False:
        return 'preview_rejected'
    if snapshot.get('margin_evidence')=='verified_agentic_limited_margin_no_borrowing_capability':
        if any(str(data.get(k,'')).lower()!=str(row[k]).lower() for k in ('symbol','side')) or data.get('type')!='market':
            return 'preview_order_identity_mismatch'
        try:
            if row['side']=='SELL':
                if money(data.get('quantity'))!=money(row['quantity']):return 'preview_quantity_mismatch'
            elif money(data.get('dollar_amount'))!=money(row['amount']):return 'preview_amount_mismatch'
        except (ValueError,ArithmeticError):return 'preview_amount_unverified'
        if data.get('order_checks')!={}:return 'broker_pretrade_warning'
        quote=data.get('quote_data')
        if not isinstance(quote,dict) or quote.get('state')!='active' or quote.get('has_traded') is not True:
            return 'preview_quote_unavailable'
        price=quote.get('ask_price') if row['side']=='BUY' else quote.get('bid_price')
        try:
            if money(price)<=0:return 'preview_price_unavailable'
        except (ValueError,ArithmeticError):return 'preview_price_unavailable'
        return None
    # Explicitly evidenced ordinary accounts retain the stronger component gate.
    check=data.get('funding_check') or {}
    try:
        if check.get('verified') is not True or money(check.get('projected_margin_used'))!=0 or money(check.get('fees'))>money(snapshot['funds'].get('fee_buffer','0.25')):
            return 'preview_funds_unverified'
    except (ValueError,ArithmeticError):return 'preview_funds_unverified'
    return None

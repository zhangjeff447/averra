"""Explicit own-funds contract. Missing broker evidence always blocks trading."""
from datetime import datetime, timezone
from decimal import Decimal
from .cycle import SYMBOLS, money, down


def timestamp(raw):
    parsed=datetime.fromisoformat(str(raw).replace('Z','+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('timestamp_timezone_missing')
    return parsed


def normalize(raw, *, now=None, held_cash=0):
    now=now or datetime.now(timezone.utc)
    if raw.get('source')!='broker_mcp' or raw.get('scope')!='ETF_ONLY':
        raise ValueError('broker_account_scope_unverified')
    if not raw.get('account_hash') or len(raw['account_hash'])!=64:
        raise ValueError('broker_account_identity_missing')
    if raw.get('complete_orders') is not True or not isinstance(raw.get('orders'),list):
        raise ValueError('broker_order_history_incomplete')
    for order in raw['orders']:
        if order.get('state') not in ('filled','canceled','cancelled','rejected','failed','expired','voided','partially_filled_rest_cancelled'):
            raise ValueError('open_or_unknown_broker_order')
    age=(now-timestamp(raw.get('as_of'))).total_seconds()
    if not -5<=age<=60:
        raise ValueError('account_snapshot_stale')
    if set(raw.get('positions',{}))!=set(SYMBOLS):
        raise ValueError('return_first_positions_required')
    funds=raw.get('funds') or {}
    limited = raw.get('account_type') == 'limited_margin'
    if limited:
        if raw.get('agentic_confirmed') is not True or funds.get('account_type') != 'limited_margin':
            raise ValueError('agentic_limited_margin_unverified')
        # Robinhood's Agentic limited margin permits unsettled SALE proceeds,
        # but provides no margin borrowing or leverage. This is a verified
        # account capability, not a missing margin balance defaulted to zero.
        pending=money(funds.get('pending_external_credits'))
        if pending != 0:
            raise ValueError('pending_external_credit_wait')
        if funds.get('margin_used') is not None and money(funds['margin_used']) != 0:
            raise ValueError('limited_margin_evidence_conflict')
        unsettled=Decimal(str(funds.get('unsettled_funds')))
        if not unsettled.is_finite():raise ValueError('unsettled_funds_unverified')
        net_cash=money(funds.get('reported_cash'))
        reusable=net_cash
        reported_total=money(raw.get('reported_net_assets'))
        margin_basis='verified_agentic_limited_margin_no_borrowing_capability'
        # Cash is the CURRENT broker balance. Unsettled proceeds, historical
        # fills and deposits are not added to it again. All open orders have
        # already blocked this path, so no order holds are subtracted twice.
    else:
        if funds.get('components_verified') is not True:
            raise ValueError('own_cash_components_unverified')
        if money(funds.get('margin_used'))!=0:
            raise ValueError('margin_used_nonzero')
        # Own settled cash must EXCLUDE Instant Deposits/credit/margin. Broker cash
        # may already include proceeds; rebuild once from components, then compare.
        own=money(funds.get('settled_own_cash'))
        net_cash=own
        reusable=own
        timeline={}
        seen=set()
        for flow in funds.get('settlements',[]):
            fid=flow.get('id')
            if not fid or fid in seen:
                raise ValueError('settlement_duplicate_or_missing_id')
            seen.add(fid)
            if flow.get('source')!='broker' or flow.get('status')!='executed':
                raise ValueError('settlement_execution_unverified')
            amount=money(flow.get('net_amount'))
            kind=flow.get('kind')
            day=datetime.fromisoformat(flow['settlement_date']).date().isoformat()
            if kind=='SALE':
                net_cash+=amount
                if day<=now.astimezone(__import__('zoneinfo').ZoneInfo('America/New_York')).date().isoformat():
                    reusable+=amount
                timeline[day]=timeline.get(day,Decimal(0))+amount
            elif kind in ('BUY_PAYABLE','WITHDRAWAL'):
                net_cash-=amount
                reusable-=amount
                timeline[day]=timeline.get(day,Decimal(0))-amount
            else:
                raise ValueError('settlement_kind_unverified')
        balance=own
        for day in sorted(timeline):
            balance+=timeline[day]
            if balance<0:
                raise ValueError('projected_margin_borrowing')
        if net_cash<0 or reusable<0:
            raise ValueError('own_cash_negative')
        # Pending external deposits/dividends are excluded, never promised capital.
        pending=money(funds.get('pending_external_credits'))
        included=funds.get('pending_credits_in_reported_cash')
        if type(included) is not bool:
            raise ValueError('pending_credit_cash_basis_unverified')
        reported=money(funds.get('reported_cash'))-(pending if included else 0)
        if abs(reported-net_cash)>Decimal('.01'):
            raise ValueError('cash_components_unreconciled')
    if not limited:
        reported_total=money(raw.get('reported_net_assets'))-(pending if included else 0)
        margin_basis='explicit_current_and_projected_balance_reconciliation'
    values,quantities={},{}
    for symbol in SYMBOLS:
        position=raw['positions'][symbol]
        quantities[symbol]=money(position.get('quantity'))
        quote=(raw.get('quotes') or {}).get(symbol) or {}
        if quote.get('source') not in ('broker_mcp','robinhood_mcp') or quote.get('is_realtime') is not True or quote.get('is_stale') is not False:
            raise ValueError('quote_unverified:'+symbol)
        quote_age=(now-timestamp(quote.get('timestamp'))).total_seconds()
        if not -5<=quote_age<=60:
            raise ValueError('quote_stale:'+symbol)
        last=money(quote.get('last'));bid=money(quote.get('bid'));ask=money(quote.get('ask'))
        if min(last,bid,ask)<=0 or bid>ask or (ask-bid)/last>Decimal('.02'):
            raise ValueError('quote_spread_invalid:'+symbol)
        values[symbol]=quantities[symbol]*last
    rebuilt=sum(values.values())+net_cash
    if abs(rebuilt-reported_total)>Decimal('.25'):
        raise ValueError('portfolio_value_unreconciled')
    broker_pending=Decimal(str(funds.get('unsettled_funds') or 0))
    if not broker_pending.is_finite():raise ValueError('unsettled_funds_invalid')
    hold=max(money(held_cash),broker_pending,Decimal(0))
    safe=max(Decimal(0),min(reusable,max(Decimal(0),net_cash-hold),money(funds.get('unleveraged_buying_power')))-Decimal('.25'))
    sellable={s:str(money(raw['positions'][s].get('shares_available_for_sells'))) if raw['positions'][s].get('shares_available_for_sells') is not None else '0' for s in SYMBOLS}
    cost=money(raw['positions']['TQQQ'].get('average_buy_price')) if quantities['TQQQ'] else Decimal(0)
    if quantities['TQQQ'] and cost<=0:raise ValueError('tqqq_cost_basis_unverified')
    return dict(tqqq_average_cost=str(cost),sellable=sellable,source='broker_mcp',account_hash=raw['account_hash'],as_of=raw['as_of'],
                safe_cash=str(down(safe)),cash_for_weight=str(net_cash),net_assets=str(rebuilt),
                values={s:str(v) for s,v in values.items()},quantities={s:str(v) for s,v in quantities.items()},
                orders=raw['orders'],quotes=raw['quotes'],pending_external_credits=str(pending),
                funds=funds,projected_margin_used='0',margin_used='0',margin_evidence=margin_basis)

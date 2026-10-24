"""Account-wide TQQQ cost stop; no core sales or automatic stop reset."""
from decimal import Decimal
from .cycle import money,POLICY,slow_average


def protection(snapshot,signal,journal):
    qty=money(snapshot['quantities']['TQQQ'])
    if not POLICY.get('tqqq_stop_loss_enabled',True):
        cost=money(snapshot.get('tqqq_average_cost')) if qty else Decimal(0)
        if qty and cost<=0:raise ValueError('tqqq_cost_basis_unverified')
        return dict(enabled=False,triggered=False,blocked=False,average_cost=str(cost),stop_price=None,buy_allowed=True)
    latch=journal.load('tqqq_stop_latch')
    recovered=bool(latch and qty==0 and signal['date']>latch['signal_date']
                   and money(signal['close'])>=slow_average(signal))
    blocked=bool(latch and not recovered)
    cost=money(snapshot.get('tqqq_average_cost')) if qty else Decimal(0)
    if qty and cost<=0:raise ValueError('tqqq_cost_basis_unverified')
    threshold=cost*(1-money(POLICY['tqqq_stop_loss_pct'])/100)
    hit=bool(qty and (money(snapshot['quotes']['TQQQ']['bid'])<=threshold or blocked))
    return dict(triggered=hit,blocked=blocked,average_cost=str(cost),stop_price=str(threshold),
                buy_allowed=not hit and not blocked and money(signal['close'])>=slow_average(signal))

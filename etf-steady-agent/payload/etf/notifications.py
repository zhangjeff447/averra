"""Use the existing ETF sender only after a verified production activation."""
from pathlib import Path
from decimal import Decimal
from datetime import datetime
from zoneinfo import ZoneInfo
from .cycle import POLICY
from .runtime import report
from tools.ops_utils import load_env_file


def configure_sender(broker):
    secrets=load_env_file(Path(broker.adapter.credential_config).parent.parent/'.env.secrets')
    token=secrets.get('TELEGRAM_BOT_TOKEN');chat=secrets.get('TELEGRAM_CHAT_ID')
    if not token or not chat:raise ValueError('existing_etf_sender_missing')
    broker.adapter.telegram_bot_token=token;broker.adapter.telegram_chat_id=chat


def telegram_report(result):
    def dollars(value,places=2):
        return '$'+format(Decimal(str(value)),'.'+str(places)+'f') if value is not None else '待确认'
    selected=result.get('selected')
    notice=result.get('notice') or {};order=notice.get('order') or {}
    execution=result.get('execution')
    execution_status=execution.get('status') if isinstance(execution,dict) else None
    labels={'SUBMITTED':'已提交，待确认成交','FILLED':'已成交',
            'AWAITING_USER_APPROVAL':'待人工审批，不重提',
            'EXECUTION_UNKNOWN':'结果待对账，不重提',
            'RECONCILIATION_REQUIRED':'等待对账，不新增订单',
            'PARTIALLY_FILLED':'部分成交，余量待确认',
            'CANCELED':'已取消','REJECTED':'已拒绝','WAIT':'未下单'}
    status=order.get('status') or execution_status
    execution_text=labels.get(status,'状态待核实，不重提') if status else str(execution or ('未下单' if result.get('status')=='LIVE' else '只读预览'))
    reason=str(result.get('reason',''))
    reasons={'WAIT_ORIGINAL_8020':'暂不满足买入条件',
             'ValueError:portfolio_value_unreconciled':'账户估值待对账',
             'BLOCKED_BY_UNRESOLVED_INTENT':'已有订单待对账',
             'WAIT_PENDING_APPROVAL':'等待券商审批'}
    reason_text=reasons.get(reason,'检查未通过，暂不下单' if result.get('status')=='BLOCKED' or reason.startswith('TRADING_HALTED') else '等待买入机会')
    as_of=notice.get('as_of')
    stamp=datetime.fromisoformat(as_of).astimezone(ZoneInfo('America/New_York')).strftime('%Y-%m-%d %H:%M ET') if as_of else datetime.now(ZoneInfo('America/New_York')).strftime('%Y-%m-%d %H:%M ET')
    signal=(selected['side']+' '+selected['symbol']+' '+dollars(selected['amount'])+'（计划）') if selected else 'WAIT'
    title='决策通知' if result.get('status') in ('LIVE','BLOCKED') else '只读预览'
    lines=['📊 ETF Agent | '+title+' | '+stamp,'信号：'+signal,report(result).splitlines()[1]]
    if not selected:lines.append('原因：'+reason_text)
    for symbol in ('QQQ','SMH','SGOV'):
        chosen=bool(selected and selected['symbol']==symbol)
        item=(result.get('original_core_plan') or {}).get('etfs',{}).get(symbol,{})
        score=item.get('final_score',(result.get('scores') or {}).get(symbol))
        this_order=order if order.get('symbol')==symbol else {}
        uncertain=(chosen and execution_status not in (None,'WAIT')) or result.get('broker_place_calls',0)>0
        submitted=this_order.get('submitted') if this_order else None if uncertain else '0.00'
        filled=this_order.get('filled') if this_order else None if uncertain else '0.00'
        fill_price='—（未成交）' if filled is not None and Decimal(filled)==0 else dollars(this_order.get('fill_price'),4)
        state=execution_text if chosen or result.get('status')=='BLOCKED' else '未下单；本轮未选择' if selected else '未下单；'+reason_text
        lines+=['',symbol+' | 信号：'+(selected['side'] if chosen else 'WAIT')+' | 评分：'+(str(score) if score is not None else '待确认'),
                '计划金额：'+dollars(selected['amount'] if chosen else None if result.get('status')=='BLOCKED' else '0.00'),
                '提交金额：'+dollars(submitted)+' | 成交金额：'+dollars(filled),
                '参考价：'+dollars(notice.get('quotes',{}).get(symbol))+' | 成交均价：'+fill_price,
                '执行：'+state]
    cash_label='下单前现金' if notice.get('before_order') else 'Robinhood 现金'
    lines+=['',cash_label+'：'+dollars(notice.get('cash')),
            '剩余可用自有资金：'+dollars(notice.get('safe_cash'))]
    budget=notice.get('daily_budget') or {}
    lines+=['今日额度：'+dollars(budget.get('limit'))+' | 已用及预留：'+dollars(budget.get('committed')),
            '今日剩余额度：'+dollars(budget.get('remaining'))]
    if notice.get('holdings'):
        lines+=['总资产：'+dollars(notice.get('net_assets'))]
        for symbol,h in notice['holdings'].items():
            lines.append(symbol+' 持仓：'+format(Decimal(h['quantity']),'.6f')+' 股 | '+dollars(h['value']))
    if notice.get('quote_as_of'):
        lines.append('报价时间（券商）：'+' / '.join(s+' '+datetime.fromisoformat(t).astimezone(ZoneInfo('America/New_York')).strftime('%H:%M ET') for s,t in notice['quote_as_of'].items()))
    return '\n'.join(lines)


def notify(result,broker,*,live=False):
    if live and (result.get('suppress_notification') or result.get('reason')=='WAIT_SLOT_ALREADY_USED'):
        return dict(delivered=False,reason='duplicate_slot_suppressed')
    text=telegram_report(result)
    if not live:return dict(preview_only=True,text=text)
    if POLICY['activation']!='LIVE_VERIFIED':raise PermissionError('notification_activation_required')
    # Persist once. A separate read-only service handles Telegram delivery.
    from .operational import DB
    from agent_ops import Operations
    store=None
    try:
        store=Operations(DB)
        key='decision:'+str(result['notification_key'])
        item=store.enqueue(key,'DECISION',text)
        return dict(queued=True,state=item['state'],key=key)
    except Exception:
        return dict(queued=False,reason='OPERATIONS_UNAVAILABLE')
    finally:
        if store is not None:store.close()

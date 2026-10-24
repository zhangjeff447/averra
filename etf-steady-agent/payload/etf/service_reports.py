"""Read-only report/delivery worker. GuardedTransport permanently denies orders."""
try:
    import fcntl
except ImportError:
    from tools import windows_lock as fcntl
import json
from datetime import datetime,timezone,timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo
import sqlite3
from agent_ops import Operations,category
from .broker import NativeAdapter,Broker
from .cycle import POLICY,money
from .funds import normalize,timestamp
from .journal import Journal
from .reconciliation import validate_history
from .notifications import configure_sender
from tools.ops_utils import is_trading_day

ET=ZoneInfo('America/New_York')
ROOT=Path(__file__).resolve().parents[1]


def due(at):
    local=at.astimezone(ET)
    return is_trading_day(local) and (15,31)<=(local.hour,local.minute)<(20,0)


def latest_result(root,day):
    path=root/'logs/cycles.jsonl'
    if not path.exists():return {}
    for line in reversed(path.read_text().splitlines()):
        try:
            item=json.loads(line)
            if (item.get('score_at') or '').startswith(day):return item
        except (ValueError,TypeError):continue
    return {}


def holdings_report(journal,raw,result,at):
    """Reconcile only an in-memory copy. Closing/stale quotes are labelled."""
    if raw.get('account_hash')!=journal.load('account_hash'):raise ValueError('report_account_mismatch')
    copy=Journal(':memory:')
    try:
        journal.db.backup(copy.db);copy.reconcile(raw['orders'],complete=raw['complete_orders']);validate_history(copy,raw)
        if copy.halt_reason():raise ValueError('report_reconciliation_blocked')
        local=at.astimezone(ET);day=local.date().isoformat()
        def usd(v):return '$'+format(Decimal(str(v)),'.2f') if v is not None else '数据待确认'
        total=money(raw['reported_net_assets']);cash=money(raw['funds']['reported_cash'])
        def pct(v):return format(v/total*100,'.2f')+'%' if total else '待确认'
        lines=['📊 ETF Agent | 每日持仓 | '+day,'账户：已核实 | Robinhood 现金：'+usd(cash)]
        rebuilt=cash;quote_times=[]
        for symbol in ('QQQ','SMH','SGOV'):
            quote=raw['quotes'][symbol];time=timestamp(quote['timestamp']);qty=money(raw['positions'][symbol]['quantity']);price=money(quote['last']);value=qty*price;rebuilt+=value
            if time>datetime.now(timezone.utc)+timedelta(seconds=5) or price<=0:raise ValueError('report_quote_invalid')
            quote_times.append(symbol+' '+time.astimezone(ET).strftime('%m-%d %H:%M ET'))
            lines.append(symbol+'：'+format(qty,'.6f')+' 股 | '+usd(value)+' | '+pct(value))
        lines+=['现金：'+usd(cash)+' | '+pct(cash)+' | 总资产（券商）：'+usd(total),'报价时间：'+'；'.join(quote_times)]
        if abs(rebuilt-total)>Decimal('.25'):lines.append('持仓参考估值与券商总值待对齐；不影响原有下单保护。')
        try:safe=normalize(raw)['safe_cash']
        except ValueError:safe=None
        daily=copy.daily_spend('BUY',at);limit=money(POLICY['daily_buy_limit'])
        lines+=['可用自有资金：'+usd(safe),'今日额度：'+usd(limit)+' | 今日已用及预留：'+usd(daily),'今日剩余额度：'+usd(max(Decimal(0),limit-daily)),
                '今日剩余额度是每日上限余额，不代表账户现金或可买金额。']
        trend=result.get('market_trend')
        lines+=['','市场行情：'+(trend['name']+'（'+trend['date']+' 收盘）' if trend else '数据待确认')]
        for symbol in ('QQQ','SMH','SGOV'):
            score=((result.get('original_core_plan') or {}).get('etfs',{}).get(symbol) or {}).get('final_score',(result.get('scores') or {}).get(symbol))
            lines.append(symbol+' 评分：'+(str(score) if score is not None else '数据待确认'))
        lines+=['未完成订单：'+('是' if copy.active() or copy.legacy_active() else '否'),
                '交易暂停：'+('是' if copy.halt_reason() or result.get('report_trading_halted') is True else '否' if result.get('report_trading_halted') is False else '未核实')+' | 实盘许可：'+('是' if POLICY['activation']=='LIVE_VERIFIED' else '否'),
                '数据查询：'+timestamp(raw['as_of']).astimezone(ET).strftime('%Y-%m-%d %H:%M ET'),'只读持仓报告；未执行交易。']
        return '\n'.join(lines)
    finally:copy.db.close()


def send(text,adapter):
    # Return explicit evidence. Transport uncertainty never becomes a retry.
    import requests
    response=requests.post('https://api.telegram.org/bot'+adapter.telegram_bot_token+'/sendMessage',
                           json={'chat_id':adapter.telegram_chat_id,'text':text},timeout=15)
    data=response.json();mid=(data.get('result') or {}).get('message_id')
    if response.status_code==200 and data.get('ok') is True and type(mid) is int and mid>0:return dict(state='SENT',message_id=mid)
    if data.get('ok') is False:return dict(state='REJECTED',error='TELEGRAM_HTTP_'+str(response.status_code))
    return dict(state='UNKNOWN',error='MISSING_CONFIRMED_RECEIPT')


def drain(store,adapter,sender=send):
    # This worker's exclusive file lock proves no previous sender is running.
    store.interrupted_sends()
    pending=store.db.execute("SELECT key,body FROM outbox WHERE state='PENDING' ORDER BY created_at,key").fetchall()
    if not pending:return
    configure_sender(type('Sender',(),{'adapter':adapter})())  # Before claim: missing credentials cannot send.
    for row in pending:
        if not store.claim(row['key']):continue
        try:
            result=sender(row['body'],adapter)
            store.finish(row['key'],result['state'],message_id=result.get('message_id'),error=result.get('error'))
        except Exception:store.finish(row['key'],'UNKNOWN',error='TRANSPORT_UNCERTAIN')


def run(root=ROOT,at=None,*,adapter_factory=NativeAdapter,sender=send):
    at=at or datetime.now(timezone.utc);local=at.astimezone(ET)
    if POLICY['activation']!='LIVE_VERIFIED':return 0
    if not is_trading_day(local) or not (9,30)<=(local.hour,local.minute)<(20,0):return 0
    base=root.parent;state=base/'state';state.mkdir(exist_ok=True)
    with (state/'notifications.lock').open('a') as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:return 0
        store=Operations(state/'operations.sqlite')
        try:
            config=json.loads((base/'config/runtime.json').read_text())
            import hashlib
            if config.get('deployment_authorized') is not True or config.get('policy_hash')!=hashlib.sha256((root/'etf/policy.json').read_bytes()).hexdigest():raise ValueError('report_configuration_unverified')
            adapter=adapter_factory(str(state/'report-reader'),config['credential_config'],live=False)
            key='holdings:'+local.date().isoformat()
            if due(at) and not store.message(key):
                failures=[e for e in store.events(key,limit=1) if e['stage']=='READ_FAILED']
                retry=not failures or (at-timestamp(failures[0]['at'])).total_seconds()>=300
                if retry:
                    with (state/'cycle.lock').open('a') as cycle_lock:
                        try:fcntl.flock(cycle_lock,fcntl.LOCK_SH|fcntl.LOCK_NB)
                        except BlockingIOError:return 0
                        journal=Journal(':memory:')
                        try:
                            with sqlite3.connect('file:'+str(state/'v1_state.sqlite')+'?mode=ro',uri=True) as source:source.backup(journal.db)
                            raw=Broker(adapter,expected_account_hash=config['account_hash']).read()
                            result=latest_result(base,local.date().isoformat())
                            try:result['report_trading_halted']=json.loads((state/'live_execution_state.json').read_text()).get('trading_halted')
                            except (OSError,ValueError):result['report_trading_halted']=None
                            text=holdings_report(journal,raw,result,at)
                            from tools.report_only import append_to
                            text,monitor=append_to(text,{'quotes':raw['quotes'],'volatility_context':{}},at)
                            store.resolve(key);store.enqueue(key,'DAILY_HOLDINGS',text);store.event(key,'REPORT_READY',dict(account_as_of=raw['as_of'],market_as_of=(result.get('market_trend') or {}).get('date'),monitor_status=monitor.get('status')))
                        except Exception as exc:
                            store.fault(key,category(exc));store.event(key,'READ_FAILED',dict(category=category(exc)))
                        finally:journal.db.close()
            try:drain(store,adapter,sender)
            except Exception as exc:store.fault('notification-worker:'+local.date().isoformat(),category(exc))
            assert not any(c in ('review_equity_order','place_equity_order') for c in adapter.transport.calls)
            return 0
        finally:store.close()

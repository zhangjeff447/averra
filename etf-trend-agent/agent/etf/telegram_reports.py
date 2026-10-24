"""Presentation and durable read-only account observation; never executes orders."""
import hashlib,json,sqlite3
from datetime import datetime,timezone
from decimal import Decimal
from zoneinfo import ZoneInfo
ET=ZoneInfo('America/New_York')
SYMBOLS=('QQQ','SMH','TQQQ')
def usd(value):
    try:
        n=Decimal(str(value))
        return '$'+format(n,'.2f') if n.is_finite() and n>=0 else '待核实'
    except Exception:return '待核实'
def trend(result):
    phase=result.get('phase')
    return '上涨趋势' if phase=='ATTACK' else '防御等待' if phase=='DEFENSIVE' else '数据待确认'
def decision(result):
    selected=result.get('selected');executions=result.get('executions',[])
    lines=['📊 ETF Agent | 决策通知','市场行情：'+trend(result)]
    from .cycle import POLICY
    period=result.get('slow_ma_period',POLICY.get('slow_ma_period',100))
    if result.get('signal_date'):lines.append('行情依据：'+result['signal_date']+' 收盘 · QQQ SMA50 '+str(result.get('sma50'))+' / SMA'+str(period)+' '+str(result.get('sma'+str(period))))
    if POLICY.get('daily_buy_limit') is None:lines.append('资金规则：以已核实自有资金为限；无策略每日／窗口金额上限')
    for symbol,reading in result.get('intraday',{}).items():
        labels={'INTRADAY_STABILIZED':'短线止跌、量差合格','INTRADAY_ABOVE_VWAP_BAND':'高于估算VWAP允许范围','INTRADAY_QUOTE_MOVED':'报价已偏离已完成K线','INTRADAY_NOT_STABILIZED':'5分钟走势尚未止跌','INTRADAY_FALLING_15M':'15分钟跌幅过大','INTRADAY_VOLUME_THIN':'相对成交量不足','INTRADAY_SPREAD_WIDE':'买卖价差偏大'}
        text='；'.join(labels.get(x,'分钟数据未通过验证（'+x+'）') for x in reading.get('reasons',[]))
        lines.append(symbol+' 分钟判断：'+reading['decision']+'｜'+text)
    if result.get('window_allocation') is not None:lines.append('买入上限：每窗口合计$20／每日合计$100；按目标缺口分配')
    orders=[x['plan'] for x in executions] or ([selected] if selected else [])
    lines.append('信号：'+' / '.join(x['side']+' '+x['symbol'] for x in orders) if orders else '信号：WAIT')
    for index,order in enumerate(orders):
        state=executions[index]['execution']['status'] if executions else 'NOT_SUBMITTED'
        lines+= [order['symbol']+' '+order['side']+' | 计划 '+usd(order.get('amount'))+' | 参考价 '+usd(order.get('price')),
                  '执行：'+{'SUBMITTED':'已提交，等待成交确认','FILLED':'已成交（以成交记录为准）','AWAITING_USER_APPROVAL':'等待 Robinhood 审批','EXECUTION_UNKNOWN':'状态待核查，不重复下单','NOT_SUBMITTED':'只读计划，未提交'}.get(state,state)]
    safety=result.get('tqqq_protection')
    if safety and safety.get('enabled',True) and Decimal(safety['average_cost'])>0:lines.append('TQQQ 止损线：'+usd(safety['stop_price'])+'（账户均价下跌6.6%）')
    if not POLICY.get('tqqq_stop_loss_enabled',True):lines.append('TQQQ：无固定价格止损；保留50／200日均线退出及超额减仓')
    lines.append(('下单前可用自有资金：' if result.get('cash_basis')=='PRE_ORDER' else '可用自有资金：')+usd(result.get('safe_cash')))
    lines.append('原因：'+{'BLOCKED_BY_UNRESOLVED_INTENT':'已有订单待对账','WAIT_PENDING_APPROVAL':'等待券商审批','NO_ELIGIBLE_ORDER':'暂无符合条件的交易','WAIT_DAILY_BUY_LIMIT':'当日买入额度已用完（含预约）','WAIT_WINDOW_BUY_LIMIT':'本窗口剩余额度不足券商最低额','WAIT_INTRADAY_FILTER':'分钟择时条件尚未通过，等待下个窗口','STOP_LOSS_6_6_ALL_TQQQ':'TQQQ触及成本止损6.6%，卖出全部TQQQ（含手动仓位）','STOP_LOSS_FILLED_WAIT_SETTLEMENT':'TQQQ止损已成交，等待回款结算后转入SMH／QQQ'}.get(result.get('reason'),str(result.get('reason') or '按当前策略判断')))
    return '\n'.join(lines)
def fill_message(order,fill,raw,regime):
    price=Decimal(str(fill['price']));quantity=Decimal(str(fill['quantity']));fees=fill.get('fees')
    return '\n'.join(['📊 ETF Agent | 账户成交记录','市场行情：'+regime,'信号：'+str(order['side']).upper()+' '+order['symbol'],
        '成交金额：'+usd(price*quantity)+' | 成交价格：'+usd(price),'成交数量：'+str(quantity)+' 股 | 费用：'+usd(fees),
        '账户现金：'+usd(raw['funds'].get('reported_cash'))+'（查询时点，非保证可用）',
        '可用自有资金：'+safe_cash(raw), '成交时间：'+datetime.fromisoformat(fill['timestamp'].replace('Z','+00:00')).astimezone(ET).strftime('%m-%d %H:%M:%S ET'),
        '来自券商实际成交；不代表本 App 提交。'])
def safe_cash(raw):
    from .funds import normalize
    try:return usd(normalize(raw)['safe_cash'])
    except (ValueError,KeyError):return '待核实'
def daily(raw,result,at):
    total=Decimal(str(raw['reported_net_assets']));cash=Decimal(str(raw['funds']['reported_cash']))
    lines=['📊 ETF Agent | 每日持仓 | '+at.astimezone(ET).date().isoformat(),'市场行情：'+trend(result),'Robinhood 现金：'+usd(cash)]
    for symbol in SYMBOLS:
        q=Decimal(str(raw['positions'][symbol]['quantity']));quote=raw['quotes'][symbol];price=Decimal(str(quote['last']));value=q*price
        pct=format(value/total*100,'.2f')+'%' if total>0 else '待核实'
        lines.append(symbol+'：'+format(q,'.6f')+' 股 | '+usd(value)+' | '+pct+' | 现价 '+usd(price))
    lines+=['现金：'+usd(cash)+' | 总资产（券商）：'+usd(total),'可用自有资金：'+safe_cash(raw),
        '未完成订单：'+('是' if any(o.get('state') not in ('filled','canceled','cancelled','rejected','failed','expired','voided','partially_filled_rest_cancelled') for o in raw['orders']) else '否'),
        '查询：'+at.astimezone(ET).strftime('%Y-%m-%d %H:%M ET'),'报价：'+'；'.join(s+' '+datetime.fromisoformat(raw['quotes'][s]['timestamp'].replace('Z','+00:00')).astimezone(ET).strftime('%m-%d %H:%M') for s in SYMBOLS),
        '只读报告；报价按所列时间，现金不等于可用资金。']
    return '\n'.join(lines)
class Observer:
    def __init__(self,path):
        self.db=sqlite3.connect(path,timeout=1,isolation_level=None)
        self.db.executescript('CREATE TABLE IF NOT EXISTS tg_meta(key TEXT PRIMARY KEY,value TEXT); CREATE TABLE IF NOT EXISTS tg_seen(id TEXT PRIMARY KEY,facts TEXT); CREATE TABLE IF NOT EXISTS tg_outbox(key TEXT PRIMARY KEY,body TEXT,state TEXT,message_id INTEGER);')
    def observe(self,raw,result,at,*,daily_due=False):
        if raw.get('complete_orders') is not True:raise ValueError('notification_orders_incomplete')
        account=raw['account_hash'];bound=self.db.execute("SELECT value FROM tg_meta WHERE key='account'").fetchone()
        if bound and bound[0]!=account:raise ValueError('notification_account_changed')
        self.db.execute('BEGIN IMMEDIATE')
        try:
            first=bound is None
            if first:self.db.execute("INSERT INTO tg_meta VALUES('account',?)",(account,))
            for order in raw['orders']:
                if order.get('symbol') not in SYMBOLS:continue
                for fill in order.get('executions',[]):
                    key=str(order['id'])+':'+str(fill['id']);facts=json.dumps([order['symbol'],order['side'],fill],sort_keys=True)
                    known=self.db.execute('SELECT facts FROM tg_seen WHERE id=?',(key,)).fetchone()
                    if known and known[0]!=facts:raise ValueError('notification_fill_conflict')
                    if known:continue
                    price=Decimal(str(fill['price']));qty=Decimal(str(fill['quantity']));when=datetime.fromisoformat(fill['timestamp'].replace('Z','+00:00'))
                    if not price.is_finite() or not qty.is_finite() or price<=0 or qty<=0 or when.tzinfo is None or when>at or fill.get('currency')!='USD':raise ValueError('notification_fill_invalid')
                    self.db.execute('INSERT INTO tg_seen VALUES(?,?)',(key,facts))
                    if not first:self.enqueue('fill:'+key,fill_message(order,fill,raw,trend(result)))
            if daily_due:self.enqueue('holdings:'+at.astimezone(ET).date().isoformat(),daily(raw,result,at))
            self.db.execute('COMMIT')
        except BaseException:self.db.execute('ROLLBACK');raise
    def enqueue(self,key,text):self.db.execute("INSERT OR IGNORE INTO tg_outbox VALUES(?,?,'PENDING',NULL)",(key,text))
    def drain(self,sender):
        # Atomic claim prevents duplicate sends across concurrent processes.
        # A crashed/uncertain send stays SENDING/UNKNOWN and is never retried.
        for key,body in self.db.execute("SELECT key,body FROM tg_outbox WHERE state='PENDING' ORDER BY rowid").fetchall():
            if self.db.execute("UPDATE tg_outbox SET state='SENDING' WHERE key=? AND state='PENDING'",(key,)).rowcount!=1:continue
            try:
                receipt=sender(body);mid=receipt.get('message_id')
                if receipt.get('state')=='SENT' and type(mid) is int and mid>0:state='SENT'
                else:state='REJECTED' if receipt.get('state')=='REJECTED' else 'UNKNOWN';mid=None
            except Exception:state='UNKNOWN';mid=None
            self.db.execute('UPDATE tg_outbox SET state=?,message_id=? WHERE key=?',(state,mid,key))

"""Native MCP connector; fixtures cannot enter the live transport."""
import hashlib
from datetime import datetime,timezone
from pathlib import Path
from .cycle import SYMBOLS,authorize_order,money
from qqq_mcp_adapter import MCPAdapter
from .recovery import retry_read

READS=frozenset({'get_accounts','get_portfolio','get_equity_positions','get_equity_orders',
                'get_equity_quotes','get_equity_price_book','get_equity_historicals','get_trade_approval_setting','get_trade_approvals'})
WRITES=frozenset({'review_equity_order','place_equity_order'})


class GuardedTransport:
    def __init__(self,transport,*,live=False):
        self.transport=transport;self.live=live;self.calls=[]
    def call_tool(self,name,params):
        if name not in READS and not (self.live and name in WRITES):raise PermissionError('denied_before_transport:'+name)
        if name in WRITES:
            authorize_order(params.get('symbol'),str(params.get('side')).upper())
            if params.get('type')!='market' or params.get('market_hours')!='regular_hours':raise PermissionError('order_shape')
            if str(params.get('side')).upper()=='SELL':
                if 'dollar_amount' in params or money(params.get('quantity'))<=0:raise PermissionError('share_sell_required')
            elif 'quantity' in params or money(params.get('dollar_amount'))<1:raise PermissionError('notional_buy_required')
        if name in ('get_equity_quotes','get_equity_price_book') and set(params.get('symbols',[]))-set(SYMBOLS):
            raise PermissionError('quote_symbols')
        if name=='get_equity_historicals':
            if set(params.get('symbols',[]))!=set(SYMBOLS) or params.get('interval')!='minute' or params.get('bounds')!='regular' or params.get('adjustment_type')!='none':raise PermissionError('minute_request_shape')
        self.calls.append(name)
        return self.transport.call_tool(name,params)
    def list_tools(self):return self.transport.list_tools()


class NativeAdapter(MCPAdapter):
    ALLOWED_MCP_TOOLS=set(MCPAdapter.ALLOWED_MCP_TOOLS)|{'get_equity_historicals'}
    def __init__(self,workspace,credential_config,*,live=False):
        self.credential_config=Path(credential_config)
        super().__init__(workspace=workspace,direct_transport=False)
        self.transport=GuardedTransport(retry_read(self._build_direct_transport),live=live)
    def reconnect_read(self):
        previous=self.transport
        self.transport=GuardedTransport(self._build_direct_transport(),live=previous.live)
        self.transport.calls=previous.calls
        self.runtime_tools=None
        self._mcp_session_generation+=1
        self._log_snapshot_retry(event='read_only_reconnected',attempt=self._mcp_session_generation)
    def _codex_config_path(self):return self.credential_config
    def _run_codex_exec_raw(self,*args,**kwargs):raise PermissionError('model_fallback_disabled')
    def _call_tool(self,name,params,retries=0,timeout_seconds=55):
        if name not in READS|WRITES:raise PermissionError('tool_allowlist')
        return super()._call_tool(name,params,retries=0,timeout_seconds=timeout_seconds)
    def _invalidate_mcp_session(self,reason):raise RuntimeError('session_uncertain_no_automatic_retry')


class Broker:
    def __init__(self,adapter,*,expected_account_hash):
        self.adapter=adapter;self.expected_account_hash=expected_account_hash;self.account=None
    def call(self,name,params):return self.adapter._unwrap_mcp_result(self.adapter._call_tool(name,params,retries=0))
    def collection(self,name,field):
        rows=[];cursor=None;seen=set()
        for _ in range(100):
            page=self.call(name,dict(account_number=self.account,**({'cursor':cursor} if cursor else {})))['data']
            if not isinstance(page.get(field),list):raise ValueError('collection_incomplete:'+field)
            rows+=page[field];cursor=page.get('next')
            if not cursor:return rows
            if cursor in seen:raise ValueError('pagination_loop')
            seen.add(cursor)
        raise ValueError('pagination_incomplete')
    def read(self):
        # Discard the entire partial snapshot after a connection failure.
        return retry_read(self._read_once,reconnect=getattr(self.adapter,'reconnect_read',None))
    def _read_once(self):
        started=datetime.now(timezone.utc)
        accounts=self.call('get_accounts',{})['data']['accounts']
        eligible=[a for a in accounts if a.get('agentic_allowed') is True and a.get('state')=='active'
                  and hashlib.sha256(str(a.get('account_number')).encode()).hexdigest()==self.expected_account_hash]
        if len(eligible)!=1:raise ValueError('authorized_agentic_account_not_unique')
        account=eligible[0];self.account=account['account_number']
        positions=self.collection('get_equity_positions','positions');orders=self.collection('get_equity_orders','orders')
        approvals=self.collection('get_trade_approvals','approvals')
        setting=self.call('get_trade_approval_setting',dict(account_number=self.account))['data']['setting']
        books=self.call('get_equity_quotes',dict(symbols=list(SYMBOLS)))
        candidates=self.adapter._extract_quote_candidates(books)
        quotes={s:self.adapter._normalize_quote(s,candidates.get(s) or self.adapter._extract_symbol_payload(books,s)) for s in SYMBOLS}
        p=self.call('get_portfolio',dict(account_number=self.account))['data']
        if p.get('currency')!='USD':raise ValueError('account_currency')
        if any(money(p.get(k))!=0 for k in ('options_value','futures_value','event_contracts_value','crypto_value','mutual_funds_value','fixed_income_value')):
            raise ValueError('other_strategy_assets_present')
        if any(x.get('symbol') not in SYMBOLS and money(x.get('quantity'))!=0 for x in positions):raise ValueError('other_equities_present')
        by_symbol={s:dict(quantity='0',shares_available_for_sells='0') for s in SYMBOLS}
        observed=set()
        for pos in positions:
            if pos.get('symbol') in by_symbol:
                if pos['symbol'] in observed:raise ValueError('duplicate_position')
                observed.add(pos['symbol']);by_symbol[pos['symbol']]=pos
        if money(by_symbol['TQQQ']['quantity'])>0 and by_symbol['TQQQ'].get('shares_available_for_sells') is None:raise ValueError('native_sellable_quantity_missing')
        # Explicit components remain required for ordinary accounts. Verified
        # Agentic limited_margin accounts use the documented non-borrowing
        # account capability and current cash/UNLEVERAGED buying-power path.
        components=('margin_used','settled_own_cash','settlements','pending_credits_in_reported_cash')
        funds=dict(components_verified=all(k in p for k in components),
                   margin_used=p.get('margin_used'),settled_own_cash=p.get('settled_own_cash'),
                   settlements=p.get('settlements'),reported_cash=p.get('cash'),
                   pending_external_credits=p.get('pending_deposits'),
                   pending_credits_in_reported_cash=p.get('pending_credits_in_reported_cash'),
                   account_type=account.get('type'),unsettled_funds=account.get('unsettled_funds'),
                   unleveraged_buying_power=p.get('buying_power',{}).get('unleveraged_buying_power'))
        if 'external_cash_events' in p:funds['external_cash_events']=p['external_cash_events']
        for o in orders:
            currency=(o.get('dollar_based_amount') or {}).get('currency_code') or o.get('currency_code') or p['currency']
            if currency!='USD':raise ValueError('order_currency_conflict')
            for f in o.get('executions',[]):
                if currency:f.setdefault('currency',currency)
        active=[a for a in approvals if a.get('status',a.get('state')) not in ('approved','rejected','expired','cancelled','canceled')]
        return dict(source='broker_mcp',scope='ETF_ONLY',account_hash=self.expected_account_hash,
                    as_of=started.isoformat(),reported_net_assets=p['total_value'],
                    account_type=account.get('type'),agentic_confirmed=account.get('agentic_allowed') is True,
                    positions=by_symbol,quotes=quotes,orders=orders,complete_orders=True,funds=funds,
                    pending_approvals=active,approval_enabled=setting.get('human_must_approve_trades'),
                    missing_funding_fields=[k for k in components if k not in p])
    def minute_data(self,now):
        from .intraday import fetch
        package=retry_read(lambda:fetch(self.call,now),reconnect=getattr(self.adapter,'reconnect_read',None))
        import json,gzip
        payload=json.dumps(package,sort_keys=True).encode();digest=hashlib.sha256(payload).hexdigest()
        folder=Path(self.adapter.workspace)/'minute-evidence'/now.astimezone(__import__('zoneinfo').ZoneInfo('America/New_York')).date().isoformat()
        folder.mkdir(parents=True,exist_ok=True,mode=0o700);path=folder/(digest+'.json.gz')
        if not path.exists():path.write_bytes(gzip.compress(payload));path.chmod(0o600)
        package['evidence_sha256']=digest;package['evidence_path']=str(path)
        return package
    def verify_order_schema(self):
        tools=retry_read(lambda:self.adapter.transport.list_tools(),reconnect=getattr(self.adapter,'reconnect_read',None))
        by_name={t['name']:t for t in tools}
        for name in WRITES:
            schema=by_name.get(name,{}).get('inputSchema') or by_name.get(name,{}).get('input_schema') or {}
            fields=schema.get('properties',{})
            if not {'account_number','symbol','side','type','quantity','dollar_amount','market_hours','time_in_force'}<=set(fields):raise ValueError('native_order_schema_unverified:'+name)
            if name=='place_equity_order' and 'ref_id' not in fields:raise ValueError('native_idempotency_unverified')
        return True

    def order_params(self,row,*,preview=False):
        authorize_order(row['symbol'],row['side'])
        sizing={'quantity':row['quantity']} if row['side']=='SELL' else {'dollar_amount':row['amount']}
        params=dict(account_number=self.account,symbol=row['symbol'],side=row['side'].lower(),type='market',market_hours='regular_hours',time_in_force='gfd',**sizing)
        if not preview:params['ref_id']=row['id']
        return params
    def review(self,row):return self.call('review_equity_order',self.order_params(row,preview=True))
    def submit(self,row):return self.call('place_equity_order',self.order_params(row))

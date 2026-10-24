"""One broker adapter, with an explicit mode guard at the transport boundary."""
from pathlib import Path

from broker.live_safety import LiveTradingDisabled
from config.unified import READ_ONLY_VALIDATION, LIVE_TRADING
from qqq_mcp_adapter import MCPAdapter
from scripts.validate_robinhood_read_only import ReadOnlyMCPAdapter, READ_ONLY_MCP_TOOLS

ROOT = Path(__file__).resolve().parents[1]


class ModeTransport:
    def __init__(self, transport, mode, live_authorized):
        if mode not in {READ_ONLY_VALIDATION, LIVE_TRADING}:
            raise LiveTradingDisabled('explicit_adapter_mode_required')
        if mode == LIVE_TRADING and not live_authorized:
            raise LiveTradingDisabled('final_deployment_authorization_required')
        self._transport, self.mode = transport, mode
        self.calls, self.denied = [], []

    def call_tool(self, name, params):
        allowed = set(READ_ONLY_MCP_TOOLS)
        if self.mode == LIVE_TRADING:
            allowed.update({'review_equity_order', 'place_equity_order'})
        if name not in allowed:
            self.denied.append(name)
            raise LiveTradingDisabled('blocked_before_transport:' + name)
        if name in {'review_equity_order', 'place_equity_order'} and (
                str(params.get('side')).lower() != 'buy' or params.get('symbol') not in {'SMH', 'QQQ'}):
            self.denied.append(name)
            raise LiveTradingDisabled('BUY_QQQ_SMH_ONLY')
        if name in {'get_equity_quotes', 'get_equity_price_book'} and set(params.get('symbols', [])) - {'SMH', 'QQQ'}:
            raise LiveTradingDisabled('quote_symbols_not_allowed')
        self.calls.append(name)
        return self._transport.call_tool(name, params)

    def list_tools(self):
        raise LiveTradingDisabled('capability_discovery_not_part_of_validation')


class UnifiedBrokerAdapter(ReadOnlyMCPAdapter):
    def __init__(self, *, config, workspace=None, transport=None):
        self.operation_mode = config.operation_mode
        authorized = (config.final_deployment_authorized and config.allow_live_orders
                      and config.v1_submission_approved and config.trading_mode == 'live')
        if self.operation_mode == READ_ONLY_VALIDATION and (config.trading_mode != 'read_only'
                or config.allow_live_orders or config.v1_submission_approved or config.final_deployment_authorized):
            raise LiveTradingDisabled('ambiguous_read_only_mode')
        if self.operation_mode == LIVE_TRADING and not authorized:
            raise LiveTradingDisabled('final_deployment_authorization_required')
        if self.operation_mode not in {READ_ONLY_VALIDATION, LIVE_TRADING}:
            raise LiveTradingDisabled('explicit_adapter_mode_required')
        super().__init__(workspace=str(workspace or ROOT), server_name='robinhood-trading',
                         transport=transport, direct_transport=True,
                         require_native_read_proof=True, regular_trade_quotes=True)
        self.transport = ModeTransport(self.transport, self.operation_mode, authorized)

    @staticmethod
    def _codex_config_path():
        # The candidate owns its scoped OAuth file. No writes to legacy/Codex credentials.
        return ROOT / 'config/mcp.toml'

    def _call_tool(self, tool_name, params, retries=2, timeout_seconds=60):
        canonical = self._canonical_tool_name(str(tool_name))
        allowed = set(READ_ONLY_MCP_TOOLS)
        if self.operation_mode == LIVE_TRADING:
            allowed.update({'review_equity_order', 'place_equity_order'})
        if canonical not in allowed:
            self.transport.denied.append(canonical)
            raise LiveTradingDisabled('blocked_before_transport:' + canonical)
        if canonical in {'review_equity_order', 'place_equity_order'} and (
                str(params.get('side')).lower() != 'buy' or params.get('symbol') not in {'SMH', 'QQQ'}):
            raise LiveTradingDisabled('BUY_QQQ_SMH_ONLY')
        return MCPAdapter._call_tool(self, tool_name, params, retries=0, timeout_seconds=timeout_seconds)

    def _run_codex_exec_raw(self, *args, **kwargs):
        raise LiveTradingDisabled('AI_FALLBACK_DISABLED')

    def _invalidate_mcp_session(self, reason):
        self.transport = ModeTransport(self._build_direct_transport(), self.operation_mode,
                                       self.operation_mode == LIVE_TRADING)

    def discover_runtime_tools(self):
        raise LiveTradingDisabled('capability_discovery_not_part_of_validation')

    def _notify_telegram(self, text, priority='normal'):
        from tools.telegram_delivery import deliver
        self.last_telegram_result = deliver(text, priority=priority)
        return self.last_telegram_result

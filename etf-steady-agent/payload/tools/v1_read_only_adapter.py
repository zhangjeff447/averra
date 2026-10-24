"""Narrow adapter for V1 acceptance; order writes are never forwarded."""


READ_TOOLS = frozenset({
    "get_accounts", "get_portfolio", "get_equity_positions",
    "get_equity_orders", "get_equity_price_book",
})


class ReadOnlyBrokerAdapter:
    def __init__(self, transport, *, allow_verified_preview=False):
        self.transport = transport
        self.allow_verified_preview = allow_verified_preview

    def call_tool_exact(self, name, arguments):
        if name not in READ_TOOLS and not (
            name == "review_equity_order" and self.allow_verified_preview
        ):
            raise PermissionError("DRY_RUN_ONLY:broker_write_denied")
        return self.transport.call_tool_exact(name, arguments)

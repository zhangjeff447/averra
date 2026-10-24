from dataclasses import dataclass


class LiveTradingDisabled(RuntimeError):
    """Raised whenever a live execution operation is attempted while disabled."""


@dataclass
class LiveSafety:
    broker_mode: str='paper'; enabled: bool=False; allow_order_submission: bool=False; shadow_mode: bool=True; read_only: bool=True; buy_only: bool=True; runtime_confirmation: bool=False
    def submission_allowed(self): return self.broker_mode=='robinhood' and self.enabled and self.allow_order_submission and not self.read_only and not self.shadow_mode and self.buy_only and self.runtime_confirmation

from datetime import datetime
from .decision import DailyDecisionEngine
from .indicators import build_indicators

class StrategyAgent:
    """Existing score only. V1 is the sole allocation engine and SQLite the sole budget."""
    def __init__(self, decision_engine=None):
        self.decision_engine = decision_engine or DailyDecisionEngine()
        self.decision_engine.thresholds['min_buy_amount'] = 0

    def evaluate_signals(self, holdings, cash, closes_by_symbol, volatility_context=None):
        decisions = {}
        for symbol in ('SMH', 'QQQ'):
            indicators = build_indicators(closes_by_symbol[symbol])
            decisions[symbol] = self.decision_engine.build_decision(symbol, indicators, 0, 0,
                volatility_context=volatility_context).to_dict()
        return {'datetime': datetime.now().isoformat(), 'decisions': decisions, 'overall': 'WAIT',
                'portfolio': {'cash': cash, 'holdings': holdings},
                'volatility_context': volatility_context or {}}

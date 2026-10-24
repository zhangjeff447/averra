from dataclasses import dataclass, asdict
from typing import Dict, Any, List, Optional

from tools.volatility_context import volatility_adjustment


@dataclass
class Decision:
    action: str
    symbol: str
    amount: float
    buy_score: float
    confidence: float
    decision_type: str
    reason: List[str]
    metrics: Dict[str, float]
    budget: Dict[str, float]
    risk_score: float = 0.0
    budget_used: float = 0.0
    budget_remaining: float = 0.0
    metadata: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "action": self.action,
            "symbol": self.symbol,
            "amount": round(self.amount, 2),
            "buy_score": round(self.buy_score, 2),
            "confidence": round(self.confidence, 2),
            "decision_type": self.decision_type,
            "reason": self.reason,
            "metrics": self.metrics,
            "budget": self.budget,
            "risk_score": round(self.risk_score, 2),
            "budget_used": round(self.budget_used, 2),
            "budget_remaining": round(self.budget_remaining, 2),
            "metadata": self.metadata or {},
        }


class DailyDecisionEngine:
    def __init__(self, thresholds: Dict[str, Any] = None):
        self.thresholds = thresholds or {
            "strong_buy_rsi": 35.0,
            "buy_rsi": 45.0,
            "ema_delta": 0.0,
            "drawdown_strong": 12.0,
            "drawdown_buy": 6.0,
            "macd_histogram": 0.0,
            "min_buy_amount": 0.0,
            "confidence_scale": 100.0,
        }

    def score_symbol(self, indicators: Dict[str, float]) -> Dict[str, Any]:
        score = 0.0
        reasons = []
        rsi = indicators.get("rsi14", 0.0)
        dd = indicators.get("drawdown60_pct", 0.0)
        ema20 = indicators.get("ema20", 0.0)
        ema50 = indicators.get("ema50", 0.0)
        macd_hist = indicators.get("histogram", 0.0)

        if rsi <= self.thresholds["strong_buy_rsi"]:
            score += 40
            reasons.append("RSI deep oversold")
        elif rsi <= self.thresholds["buy_rsi"]:
            score += 25
            reasons.append("RSI undervalued")
        else:
            score += 5
            reasons.append("RSI neutral or overbought")

        if dd >= self.thresholds["drawdown_strong"]:
            score += 30
            reasons.append("Strong pullback")
        elif dd >= self.thresholds["drawdown_buy"]:
            score += 15
            reasons.append("Moderate retracement")
        else:
            reasons.append("No significant dip")

        ema_diff = ema20 - ema50
        if ema_diff > self.thresholds["ema_delta"]:
            score += 15
            reasons.append("Positive short-term momentum")
        else:
            score += 2
            reasons.append("Weak or negative momentum")

        if macd_hist > self.thresholds["macd_histogram"]:
            score += 10
            reasons.append("MACD histogram improving")
        else:
            reasons.append("MACD not confirming")

        return {
            "score": min(max(score, 0.0), 100.0),
            "reasons": reasons,
            "rsi": rsi,
            "drawdown60_pct": dd,
            "ema20": ema20,
            "ema50": ema50,
            "macd_histogram": macd_hist,
        }

    def build_decision(
        self,
        symbol: str,
        indicators: Dict[str, float],
        recommended_amount: float,
        budget_remaining: float,
        volatility_context: Optional[Dict[str, Any]] = None,
    ) -> Decision:
        scored = self.score_symbol(indicators)
        base_score = scored["score"]
        volatility = volatility_adjustment(symbol, indicators, volatility_context)
        score = min(max(base_score + volatility["adjustment"], 0.0), 100.0)
        confidence = min(max(score / self.thresholds["confidence_scale"], 0.0), 1.0)
        if score >= 70 and round(recommended_amount, 2) > 0 and recommended_amount >= self.thresholds["min_buy_amount"] and budget_remaining >= recommended_amount:
            action = "BUY"
            reason = ["Strong buy signal with budget available"]
        elif score >= 40 and round(recommended_amount, 2) > 0 and recommended_amount >= self.thresholds["min_buy_amount"] and budget_remaining >= recommended_amount:
            action = "BUY"
            reason = ["Buy signal with reasonable momentum"]
        else:
            action = "WAIT"
            if budget_remaining <= 0.0:
                reason = ["Budget exhausted"]
            elif budget_remaining < recommended_amount:
                reason = [f"Insufficient budget: available ${budget_remaining:.2f}, required ${recommended_amount:.2f}"]
            elif round(recommended_amount, 2) <= 0.0:
                reason = ["No buy allocation available after cash, allocation and spending limits"]
            elif recommended_amount < self.thresholds["min_buy_amount"]:
                reason = [f"Recommended allocation ${recommended_amount:.2f} below minimum order ${self.thresholds['min_buy_amount']:.2f}"]
            else:
                reason = ["Signal not strong enough"]

        return Decision(
            action=action,
            symbol=symbol,
            amount=recommended_amount if action == "BUY" else 0.0,
            buy_score=score,
            confidence=confidence,
            decision_type=action,
            reason=reason,
            metrics={
                "rsi14": scored["rsi"],
                "drawdown60_pct": scored["drawdown60_pct"],
                "ema20": scored["ema20"],
                "ema50": scored["ema50"],
                "macd_histogram": scored["macd_histogram"],
            },
            budget={
                "monthly_budget": self.thresholds.get("monthly_budget", 300.0),
                "base_budget_remaining": budget_remaining,
                "opportunity_cash_available": 0.0,
                "monthly_spent": 0.0,
                "monthly_remaining": budget_remaining,
            },
            risk_score=100.0 - score,
            budget_used=recommended_amount if action == "BUY" else 0.0,
            budget_remaining=budget_remaining,
            metadata={
                "base_existing_score": base_score,
                "signal_score": score,
                "reasons": scored["reasons"],
                "volatility_contribution": volatility,
            },
        )



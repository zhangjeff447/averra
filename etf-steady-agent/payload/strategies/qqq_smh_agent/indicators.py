from typing import Dict, List


def ema(closes: List[float], period: int) -> float:
    if len(closes) < period or period <= 0:
        return float('nan')
    alpha = 2.0 / (period + 1)
    ema_value = sum(closes[:period]) / period
    for price in closes[period:]:
        ema_value = alpha * price + (1 - alpha) * ema_value
    return ema_value


def sma(closes: List[float], period: int) -> float:
    if len(closes) < period or period <= 0:
        return float('nan')
    return sum(closes[-period:]) / period


def rsi_from_closes(closes: List[float], period: int = 14) -> float:
    if len(closes) < period + 1:
        return float('nan')
    deltas = [closes[i + 1] - closes[i] for i in range(len(closes) - 1)]
    gains = [d if d > 0 else 0.0 for d in deltas]
    losses = [-d if d < 0 else 0.0 for d in deltas]
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0.0:
        return 50.0 if avg_gain == 0.0 else 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def macd(closes: List[float]) -> Dict[str, float]:
    if len(closes) < 34:
        return {"macd": float('nan'), "signal": float('nan'), "histogram": float('nan')}
    series = [ema(closes[:end], 12) - ema(closes[:end], 26)
              for end in range(26, len(closes) + 1)]
    signal = ema(series, 9)
    macd_value = series[-1]
    histogram = macd_value - signal
    return {"macd": macd_value, "signal": signal, "histogram": histogram}


def drawdown(closes: List[float], window: int = 60) -> float:
    if len(closes) < 2:
        return 0.0
    window_closes = closes[-window:] if len(closes) >= window else closes
    high = max(window_closes)
    current = window_closes[-1]
    if high <= 0.0:
        return 0.0
    return (high - current) / high * 100.0


def build_indicators(closes: List[float]) -> Dict[str, float]:
    return {
        "rsi14": rsi_from_closes(closes, period=14),
        "drawdown60_pct": drawdown(closes, window=60),
        "ema20": ema(closes, period=20),
        "ema50": ema(closes, period=50),
        "ema200": ema(closes, period=200),
        "sma5": sma(closes, period=5),
        **macd(closes),
    }

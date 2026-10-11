"""Explainable, rule-based decision layer for Meridian's paper-trading experiment."""
from dataclasses import asdict, dataclass
import math


@dataclass(frozen=True)
class BrainDecision:
    action: str
    score: int
    trend: str
    momentum: str
    rsi_state: str
    volatility_pct: float
    reason: str

    def to_dict(self):
        return asdict(self)


class MeridianBrain:
    """Small deterministic strategy; risk checks remain in RealisticPortfolio."""

    def __init__(self, min_buy_score=2, sell_score=-1, overbought_rsi=75.0):
        self.min_buy_score = int(min_buy_score)
        self.sell_score = int(sell_score)
        self.overbought_rsi = float(overbought_rsi)

    def decide(self, close, sma, rsi, atr, previous_close, is_owned=False):
        values = (close, sma, rsi, atr, previous_close)
        if not all(isinstance(v, (int, float)) and math.isfinite(v) for v in values):
            return BrainDecision("HOLD", 0, "unknown", "unknown", "unknown", 0.0,
                                 "Waiting for valid indicator data").to_dict()
        if close <= 0 or sma <= 0 or atr <= 0 or previous_close <= 0:
            return BrainDecision("HOLD", 0, "unknown", "unknown", "unknown", 0.0,
                                 "Waiting for positive price and volatility data").to_dict()
        if not 0.0 <= rsi <= 100.0:
            return BrainDecision("HOLD", 0, "unknown", "unknown", "unknown", 0.0,
                                 "Waiting for RSI within its valid 0–100 range").to_dict()

        trend = "up" if close > sma else "down" if close < sma else "flat"
        momentum = "rising" if close > previous_close else "falling" if close < previous_close else "flat"
        rsi_state = "strong" if 50.0 <= rsi < self.overbought_rsi else "weak" if rsi < 50.0 else "overbought"
        volatility_pct = atr / close * 100.0

        score = 0
        score += 1 if trend == "up" else -1 if trend == "down" else 0
        score += 1 if momentum == "rising" else -1 if momentum == "falling" else 0
        score += 1 if rsi_state == "strong" else -1 if rsi_state == "weak" else 0

        if is_owned and (score <= self.sell_score or rsi_state == "overbought"):
            action = "SELL"
            reason = "Exit condition: bearish score or RSI overbought"
        elif not is_owned and score >= self.min_buy_score and rsi_state == "strong":
            action = "BUY"
            reason = "Entry condition: trend, momentum, and RSI score agree"
        else:
            action = "HOLD"
            reason = "No sufficiently aligned entry/exit conditions"

        return BrainDecision(action, score, trend, momentum, rsi_state, volatility_pct, reason).to_dict()

"""
Ultra-Lightweight ML Trading Agent for Lambda (Pure Python - No Dependencies)
Implements machine learning trading strategies without external libraries
"""
from typing import Dict, Optional, List
import math


class MLTradingAgent:
    """
    Pure Python ML trading agent - no numpy/sklearn required
    Uses ensemble of technical indicators and rule-based learning
    """

    def __init__(self, symbol: str):
        self.symbol = symbol.upper()

    @staticmethod
    def mean(values: List[float]) -> float:
        """Calculate mean of list"""
        return sum(values) / len(values) if values else 0

    @staticmethod
    def std_dev(values: List[float]) -> float:
        """Calculate standard deviation"""
        if len(values) < 2:
            return 0
        avg = sum(values) / len(values)
        variance = sum((x - avg) ** 2 for x in values) / len(values)
        return math.sqrt(variance)

    def calculate_sma(self, prices: List[float], period: int) -> Optional[float]:
        """Simple Moving Average"""
        if len(prices) < period:
            return None
        return self.mean(prices[-period:])

    def calculate_ema(self, prices: List[float], period: int) -> Optional[float]:
        """Exponential Moving Average"""
        if len(prices) < period:
            return None

        multiplier = 2 / (period + 1)
        ema = self.mean(prices[:period])

        for price in prices[period:]:
            ema = (price - ema) * multiplier + ema

        return ema

    def calculate_rsi(self, prices: List[float], period: int = 14) -> Optional[float]:
        """Relative Strength Index"""
        if len(prices) < period + 1:
            return None

        gains = []
        losses = []

        for i in range(1, len(prices)):
            change = prices[i] - prices[i-1]
            if change > 0:
                gains.append(change)
                losses.append(0)
            else:
                gains.append(0)
                losses.append(abs(change))

        if len(gains) < period:
            return None

        avg_gain = self.mean(gains[-period:])
        avg_loss = self.mean(losses[-period:])

        if avg_loss == 0:
            return 100

        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))

        return rsi

    def calculate_macd(self, prices: List[float]) -> Optional[Dict]:
        """MACD Indicator"""
        if len(prices) < 26:
            return None

        ema12 = self.calculate_ema(prices, 12)
        ema26 = self.calculate_ema(prices, 26)

        if ema12 is None or ema26 is None:
            return None

        macd = ema12 - ema26

        # Signal line (9-period EMA of MACD)
        # Simplified: use last value as approximation
        signal = macd * 0.9  # Approximation

        return {
            'macd': macd,
            'signal': signal,
            'histogram': macd - signal
        }

    def calculate_bollinger_bands(self, prices: List[float], period: int = 20) -> Optional[Dict]:
        """Bollinger Bands"""
        if len(prices) < period:
            return None

        sma = self.calculate_sma(prices, period)
        std = self.std_dev(prices[-period:])

        return {
            'upper': sma + (2 * std),
            'middle': sma,
            'lower': sma - (2 * std)
        }

    def analyze_stock(self, prices: List[float], volume: List[int] = None) -> Dict:
        """
        Comprehensive ML-based stock analysis

        Args:
            prices: List of closing prices (oldest to newest)
            volume: Optional list of volume data

        Returns:
            Complete analysis with recommendation
        """
        if len(prices) < 50:
            return {
                'error': 'Need at least 50 days of price data',
                'recommendation': 'hold',
                'confidence': 0.0
            }

        current_price = prices[-1]

        # Calculate all indicators
        sma20 = self.calculate_sma(prices, 20)
        sma50 = self.calculate_sma(prices, 50)
        sma200 = self.calculate_sma(prices, 200) if len(prices) >= 200 else None
        rsi = self.calculate_rsi(prices, 14)
        macd_data = self.calculate_macd(prices)
        bb = self.calculate_bollinger_bands(prices, 20)

        # Calculate momentum
        momentum_10 = ((prices[-1] - prices[-10]) / prices[-10] * 100) if len(prices) >= 10 else 0
        momentum_20 = ((prices[-1] - prices[-20]) / prices[-20] * 100) if len(prices) >= 20 else 0

        # Volatility
        volatility = self.std_dev(prices[-20:])
        volatility_pct = (volatility / current_price) * 100

        # Generate signals with confidence scores
        signals = []
        confidences = []

        # Signal 1: RSI Strategy
        if rsi is not None:
            if rsi < 30:
                signals.append('buy')
                confidences.append(0.85)  # High confidence - oversold
            elif rsi > 70:
                signals.append('sell')
                confidences.append(0.85)  # High confidence - overbought
            elif 40 <= rsi <= 60:
                signals.append('hold')
                confidences.append(0.6)

        # Signal 2: Moving Average Crossover
        if sma20 and sma50:
            if sma20 > sma50 * 1.02:  # 2% above
                signals.append('buy')
                confidences.append(0.75)
            elif sma20 < sma50 * 0.98:  # 2% below
                signals.append('sell')
                confidences.append(0.75)

        # Signal 3: Golden/Death Cross (SMA200)
        if sma50 and sma200:
            if sma50 > sma200:
                signals.append('buy')
                confidences.append(0.70)
            else:
                signals.append('sell')
                confidences.append(0.70)

        # Signal 4: MACD
        if macd_data:
            if macd_data['macd'] > macd_data['signal']:
                signals.append('buy')
                confidences.append(0.70)
            else:
                signals.append('sell')
                confidences.append(0.70)

        # Signal 5: Bollinger Bands
        if bb:
            if current_price < bb['lower']:
                signals.append('buy')
                confidences.append(0.80)  # Price at lower band - oversold
            elif current_price > bb['upper']:
                signals.append('sell')
                confidences.append(0.80)  # Price at upper band - overbought

        # Signal 6: Momentum
        if momentum_10 > 5:  # Strong positive momentum
            signals.append('buy')
            confidences.append(0.65)
        elif momentum_10 < -5:  # Strong negative momentum
            signals.append('sell')
            confidences.append(0.65)

        # Aggregate signals using weighted voting
        buy_count = signals.count('buy')
        sell_count = signals.count('sell')
        hold_count = signals.count('hold')

        buy_confidence = self.mean([conf for sig, conf in zip(signals, confidences) if sig == 'buy']) if buy_count > 0 else 0
        sell_confidence = self.mean([conf for sig, conf in zip(signals, confidences) if sig == 'sell']) if sell_count > 0 else 0

        # Determine final recommendation
        if buy_count > sell_count and buy_count > hold_count:
            recommendation = 'buy'
            confidence = buy_confidence
        elif sell_count > buy_count and sell_count > hold_count:
            recommendation = 'sell'
            confidence = sell_confidence
        else:
            recommendation = 'hold'
            confidence = 0.5

        # Generate detailed reasoning
        reasoning = self._generate_reasoning(
            rsi, sma20, sma50, macd_data, bb, current_price,
            momentum_10, volatility_pct
        )

        return {
            'symbol': self.symbol,
            'recommendation': recommendation.upper(),
            'confidence': round(confidence, 2),
            'ml_score': round(confidence * 100, 1),
            'signals': {
                'buy': buy_count,
                'sell': sell_count,
                'hold': hold_count,
                'total': len(signals)
            },
            'indicators': {
                'rsi': round(rsi, 2) if rsi else None,
                'sma_20': round(sma20, 2) if sma20 else None,
                'sma_50': round(sma50, 2) if sma50 else None,
                'macd': round(macd_data['macd'], 2) if macd_data else None,
                'momentum_10d': round(momentum_10, 2),
                'volatility_pct': round(volatility_pct, 2)
            },
            'reasoning': reasoning,
            'risk_level': self._assess_risk(volatility_pct, rsi)
        }

    def _generate_reasoning(self, rsi, sma20, sma50, macd_data, bb, price, momentum, volatility):
        """Generate human-readable reasoning"""
        reasons = []

        if rsi:
            if rsi < 30:
                reasons.append(f"🔴 RSI oversold ({rsi:.1f})")
            elif rsi > 70:
                reasons.append(f"🔴 RSI overbought ({rsi:.1f})")
            else:
                reasons.append(f"✓ RSI neutral ({rsi:.1f})")

        if sma20 and sma50:
            if sma20 > sma50:
                reasons.append("✓ Bullish MA trend")
            else:
                reasons.append("✗ Bearish MA trend")

        if macd_data:
            if macd_data['histogram'] > 0:
                reasons.append("✓ MACD bullish")
            else:
                reasons.append("✗ MACD bearish")

        if bb and price:
            if price < bb['lower']:
                reasons.append("💡 Price below lower BB")
            elif price > bb['upper']:
                reasons.append("⚠️ Price above upper BB")

        if abs(momentum) > 5:
            direction = "up" if momentum > 0 else "down"
            reasons.append(f"📈 Strong momentum {direction}")

        return " | ".join(reasons)

    def _assess_risk(self, volatility_pct, rsi):
        """Assess risk level"""
        if volatility_pct > 5 or (rsi and (rsi < 25 or rsi > 75)):
            return "HIGH"
        elif volatility_pct > 3 or (rsi and (rsi < 35 or rsi > 65)):
            return "MEDIUM"
        else:
            return "LOW"


def get_ml_recommendation(symbol: str, prices: List[float]) -> Dict:
    """Quick ML recommendation"""
    agent = MLTradingAgent(symbol)
    return agent.analyze_stock(prices)

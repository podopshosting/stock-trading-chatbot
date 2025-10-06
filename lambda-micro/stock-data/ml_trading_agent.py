"""
Lightweight ML Trading Agent for Lambda
Adapted for serverless environment with Alpha Vantage integration
"""
import json
import numpy as np
from typing import Dict, Optional, List
from datetime import datetime, timedelta


class LightweightTradingAgent:
    """
    Simplified trading agent for Lambda - uses rule-based ML without sklearn
    to avoid heavy dependencies
    """

    def __init__(self, symbol: str):
        self.symbol = symbol.upper()
        self.historical_data = []

    def calculate_technical_indicators(self, prices: List[float]) -> Dict:
        """
        Calculate technical indicators from price data
        Returns dict with MA, RSI, MACD, Volatility
        """
        if len(prices) < 50:
            return None

        prices_array = np.array(prices)

        # Moving Averages
        ma20 = np.mean(prices_array[-20:]) if len(prices) >= 20 else None
        ma50 = np.mean(prices_array[-50:]) if len(prices) >= 50 else None

        # RSI
        rsi = self._calculate_rsi(prices_array)

        # MACD
        macd, signal = self._calculate_macd(prices_array)

        # Volatility (Standard Deviation)
        volatility = np.std(prices_array[-20:]) if len(prices) >= 20 else None

        # Momentum
        momentum = (prices_array[-1] - prices_array[-10]) / prices_array[-10] if len(prices) >= 10 else 0

        return {
            'ma20': ma20,
            'ma50': ma50,
            'rsi': rsi,
            'macd': macd,
            'macd_signal': signal,
            'volatility': volatility,
            'momentum': momentum,
            'current_price': prices_array[-1]
        }

    def _calculate_rsi(self, prices: np.ndarray, period: int = 14) -> Optional[float]:
        """Calculate RSI indicator"""
        if len(prices) < period + 1:
            return None

        deltas = np.diff(prices)
        gains = np.where(deltas > 0, deltas, 0)
        losses = np.where(deltas < 0, -deltas, 0)

        avg_gain = np.mean(gains[-period:])
        avg_loss = np.mean(losses[-period:])

        if avg_loss == 0:
            return 100

        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))

        return float(rsi)

    def _calculate_macd(self, prices: np.ndarray, fast: int = 12, slow: int = 26, signal_period: int = 9):
        """Calculate MACD indicator"""
        if len(prices) < slow:
            return None, None

        # EMA calculation
        ema_fast = self._ema(prices, fast)
        ema_slow = self._ema(prices, slow)

        macd = ema_fast - ema_slow
        signal = self._ema(np.array([macd] * signal_period), signal_period)

        return float(macd), float(signal)

    def _ema(self, prices: np.ndarray, period: int) -> float:
        """Calculate Exponential Moving Average"""
        if len(prices) < period:
            return np.mean(prices)

        multiplier = 2 / (period + 1)
        ema = np.mean(prices[:period])

        for price in prices[period:]:
            ema = (price - ema) * multiplier + ema

        return float(ema)

    def generate_ml_signals(self, indicators: Dict) -> Dict:
        """
        Generate trading signals based on technical indicators
        Uses rule-based machine learning approach
        """
        signals = []
        confidence_scores = []

        # Signal 1: RSI Oversold/Overbought
        if indicators['rsi'] is not None:
            if indicators['rsi'] < 30:
                signals.append('buy')
                confidence_scores.append(0.8)  # High confidence on oversold
            elif indicators['rsi'] > 70:
                signals.append('sell')
                confidence_scores.append(0.8)  # High confidence on overbought

        # Signal 2: Moving Average Crossover
        if indicators['ma20'] and indicators['ma50']:
            if indicators['ma20'] > indicators['ma50']:
                signals.append('buy')
                confidence_scores.append(0.6)
            elif indicators['ma20'] < indicators['ma50']:
                signals.append('sell')
                confidence_scores.append(0.6)

        # Signal 3: MACD Crossover
        if indicators['macd'] is not None and indicators['macd_signal'] is not None:
            if indicators['macd'] > indicators['macd_signal']:
                signals.append('buy')
                confidence_scores.append(0.7)
            else:
                signals.append('sell')
                confidence_scores.append(0.7)

        # Signal 4: Momentum
        if indicators['momentum'] > 0.02:  # 2% positive momentum
            signals.append('buy')
            confidence_scores.append(0.5)
        elif indicators['momentum'] < -0.02:  # 2% negative momentum
            signals.append('sell')
            confidence_scores.append(0.5)

        # Aggregate signals
        buy_count = signals.count('buy')
        sell_count = signals.count('sell')

        if buy_count > sell_count:
            recommendation = 'buy'
            confidence = np.mean([conf for sig, conf in zip(signals, confidence_scores) if sig == 'buy'])
        elif sell_count > buy_count:
            recommendation = 'sell'
            confidence = np.mean([conf for sig, conf in zip(signals, confidence_scores) if sig == 'sell'])
        else:
            recommendation = 'hold'
            confidence = 0.5

        return {
            'recommendation': recommendation,
            'confidence': float(confidence),
            'signals_breakdown': {
                'buy_signals': buy_count,
                'sell_signals': sell_count,
                'total_signals': len(signals)
            },
            'reasoning': self._generate_reasoning(indicators, recommendation)
        }

    def _generate_reasoning(self, indicators: Dict, recommendation: str) -> str:
        """Generate human-readable reasoning for the recommendation"""
        reasons = []

        # RSI reasoning
        if indicators['rsi'] is not None:
            if indicators['rsi'] < 30:
                reasons.append(f"RSI is oversold at {indicators['rsi']:.1f}")
            elif indicators['rsi'] > 70:
                reasons.append(f"RSI is overbought at {indicators['rsi']:.1f}")
            else:
                reasons.append(f"RSI is neutral at {indicators['rsi']:.1f}")

        # MA reasoning
        if indicators['ma20'] and indicators['ma50']:
            if indicators['ma20'] > indicators['ma50']:
                reasons.append("20-day MA is above 50-day MA (bullish)")
            else:
                reasons.append("20-day MA is below 50-day MA (bearish)")

        # MACD reasoning
        if indicators['macd'] is not None and indicators['macd_signal'] is not None:
            if indicators['macd'] > indicators['macd_signal']:
                reasons.append("MACD is above signal line (bullish)")
            else:
                reasons.append("MACD is below signal line (bearish)")

        # Volatility reasoning
        if indicators['volatility']:
            vol_pct = (indicators['volatility'] / indicators['current_price']) * 100
            if vol_pct > 5:
                reasons.append(f"High volatility ({vol_pct:.1f}%) - higher risk")
            else:
                reasons.append(f"Low volatility ({vol_pct:.1f}%) - stable")

        return " | ".join(reasons)

    def calculate_position_size(self, portfolio_value: float, current_price: float,
                               risk_percent: float = 0.02) -> Dict:
        """
        Calculate optimal position size based on risk management
        Risk 2% of portfolio per trade (conservative)
        """
        risk_amount = portfolio_value * risk_percent

        # Use volatility for stop loss calculation
        # Simplified: assume 5% stop loss
        stop_loss_percent = 0.05
        stop_loss_price = current_price * (1 - stop_loss_percent)

        # Calculate shares based on risk
        price_risk = current_price - stop_loss_price
        shares = int(risk_amount / price_risk) if price_risk > 0 else 0

        # Don't exceed 20% of portfolio in single position
        max_position_value = portfolio_value * 0.20
        max_shares = int(max_position_value / current_price)

        shares = min(shares, max_shares)

        return {
            'shares': shares,
            'position_value': shares * current_price,
            'stop_loss': stop_loss_price,
            'take_profit': current_price * 1.10,  # 10% profit target
            'risk_reward_ratio': 2.0  # 10% profit / 5% risk = 2:1
        }

    def analyze(self, price_history: List[float], portfolio_value: float = 100000) -> Dict:
        """
        Main analysis function - combines all ML signals

        Args:
            price_history: List of historical closing prices
            portfolio_value: Current portfolio value for position sizing

        Returns:
            Complete analysis with recommendation, confidence, and reasoning
        """
        if len(price_history) < 50:
            return {
                'error': 'Insufficient historical data (need at least 50 days)',
                'recommendation': 'hold',
                'confidence': 0.0
            }

        # Calculate technical indicators
        indicators = self.calculate_technical_indicators(price_history)

        if not indicators:
            return {
                'error': 'Unable to calculate technical indicators',
                'recommendation': 'hold',
                'confidence': 0.0
            }

        # Generate ML-based signals
        ml_signals = self.generate_ml_signals(indicators)

        # Calculate position sizing
        position_info = self.calculate_position_size(
            portfolio_value,
            indicators['current_price']
        )

        return {
            'symbol': self.symbol,
            'recommendation': ml_signals['recommendation'],
            'confidence': ml_signals['confidence'],
            'reasoning': ml_signals['reasoning'],
            'indicators': {
                'rsi': indicators['rsi'],
                'ma20': indicators['ma20'],
                'ma50': indicators['ma50'],
                'macd': indicators['macd'],
                'volatility': indicators['volatility'],
                'momentum': indicators['momentum']
            },
            'signals_breakdown': ml_signals['signals_breakdown'],
            'position_sizing': position_info,
            'current_price': indicators['current_price']
        }


def get_ml_recommendation(symbol: str, price_history: List[float]) -> Dict:
    """
    Convenience function for Lambda integration

    Args:
        symbol: Stock ticker
        price_history: List of closing prices (oldest to newest)

    Returns:
        ML-based trading recommendation
    """
    agent = LightweightTradingAgent(symbol)
    return agent.analyze(price_history)


# Example usage
if __name__ == "__main__":
    # Test with sample data
    sample_prices = [150, 152, 151, 153, 155, 154, 156, 158, 157, 159,
                    160, 162, 161, 163, 165, 164, 166, 168, 167, 169,
                    170, 172, 171, 173, 175, 174, 176, 178, 177, 179,
                    180, 182, 181, 183, 185, 184, 186, 188, 187, 189,
                    190, 192, 191, 193, 195, 194, 196, 198, 197, 199, 200]

    agent = LightweightTradingAgent("AAPL")
    result = agent.analyze(sample_prices)

    print(json.dumps(result, indent=2))

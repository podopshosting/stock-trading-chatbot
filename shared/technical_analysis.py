"""
Technical analysis engine for stock data
Implements moving averages, RSI, MACD, and other indicators
"""
import pandas as pd
import numpy as np
from typing import Dict, Tuple, List
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class TechnicalAnalyzer:
    """Performs technical analysis on stock data"""

    def __init__(self, data: pd.DataFrame):
        """
        Initialize with historical stock data

        Args:
            data: DataFrame with OHLCV data (Open, High, Low, Close, Volume)
        """
        self.data = data.copy()
        if self.data.empty:
            logger.warning("Empty data provided to TechnicalAnalyzer")

    def calculate_sma(self, period: int = 20, column: str = 'Close') -> pd.Series:
        """Calculate Simple Moving Average"""
        return self.data[column].rolling(window=period).mean()

    def calculate_ema(self, period: int = 20, column: str = 'Close') -> pd.Series:
        """Calculate Exponential Moving Average"""
        return self.data[column].ewm(span=period, adjust=False).mean()

    def calculate_rsi(self, period: int = 14, column: str = 'Close') -> pd.Series:
        """
        Calculate Relative Strength Index

        RSI ranges from 0-100:
        - Above 70: Overbought (potential sell signal)
        - Below 30: Oversold (potential buy signal)
        """
        delta = self.data[column].diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()

        rs = gain / loss
        rsi = 100 - (100 / (1 + rs))
        return rsi

    def calculate_macd(self, column: str = 'Close') -> Tuple[pd.Series, pd.Series, pd.Series]:
        """
        Calculate MACD (Moving Average Convergence Divergence)

        Returns:
            (macd_line, signal_line, histogram)
        """
        exp1 = self.data[column].ewm(span=12, adjust=False).mean()
        exp2 = self.data[column].ewm(span=26, adjust=False).mean()

        macd_line = exp1 - exp2
        signal_line = macd_line.ewm(span=9, adjust=False).mean()
        histogram = macd_line - signal_line

        return macd_line, signal_line, histogram

    def calculate_bollinger_bands(self, period: int = 20, std_dev: float = 2,
                                   column: str = 'Close') -> Tuple[pd.Series, pd.Series, pd.Series]:
        """
        Calculate Bollinger Bands

        Returns:
            (upper_band, middle_band, lower_band)
        """
        middle_band = self.data[column].rolling(window=period).mean()
        std = self.data[column].rolling(window=period).std()

        upper_band = middle_band + (std * std_dev)
        lower_band = middle_band - (std * std_dev)

        return upper_band, middle_band, lower_band

    def calculate_volume_analysis(self) -> Dict:
        """Analyze volume patterns"""
        if 'Volume' not in self.data.columns:
            return {'error': 'Volume data not available'}

        avg_volume = self.data['Volume'].mean()
        recent_volume = self.data['Volume'].iloc[-1]
        volume_ratio = recent_volume / avg_volume if avg_volume > 0 else 0

        return {
            'average_volume': avg_volume,
            'recent_volume': recent_volume,
            'volume_ratio': volume_ratio,
            'high_volume': volume_ratio > 1.5  # Volume spike
        }

    def get_trend_direction(self, short_period: int = 50, long_period: int = 200) -> str:
        """
        Determine trend direction using moving averages

        Returns:
            'bullish', 'bearish', or 'neutral'
        """
        if len(self.data) < long_period:
            return 'insufficient_data'

        short_ma = self.calculate_sma(short_period)
        long_ma = self.calculate_sma(long_period)

        if pd.isna(short_ma.iloc[-1]) or pd.isna(long_ma.iloc[-1]):
            return 'neutral'

        if short_ma.iloc[-1] > long_ma.iloc[-1]:
            return 'bullish'
        elif short_ma.iloc[-1] < long_ma.iloc[-1]:
            return 'bearish'
        else:
            return 'neutral'

    def check_golden_cross(self) -> bool:
        """
        Check for Golden Cross (50-day MA crosses above 200-day MA)
        Strong bullish signal
        """
        if len(self.data) < 200:
            return False

        sma_50 = self.calculate_sma(50)
        sma_200 = self.calculate_sma(200)

        # Check if cross happened in last 5 days
        for i in range(1, min(6, len(self.data))):
            if (sma_50.iloc[-i] > sma_200.iloc[-i] and
                sma_50.iloc[-i-1] <= sma_200.iloc[-i-1]):
                return True
        return False

    def check_death_cross(self) -> bool:
        """
        Check for Death Cross (50-day MA crosses below 200-day MA)
        Strong bearish signal
        """
        if len(self.data) < 200:
            return False

        sma_50 = self.calculate_sma(50)
        sma_200 = self.calculate_sma(200)

        # Check if cross happened in last 5 days
        for i in range(1, min(6, len(self.data))):
            if (sma_50.iloc[-i] < sma_200.iloc[-i] and
                sma_50.iloc[-i-1] >= sma_200.iloc[-i-1]):
                return True
        return False

    def get_support_resistance(self, window: int = 20) -> Dict:
        """Calculate support and resistance levels"""
        recent_data = self.data.tail(window)

        support = recent_data['Low'].min()
        resistance = recent_data['High'].max()
        current_price = self.data['Close'].iloc[-1]

        return {
            'support': float(support),
            'resistance': float(resistance),
            'current_price': float(current_price),
            'distance_to_support': float((current_price - support) / current_price * 100),
            'distance_to_resistance': float((resistance - current_price) / current_price * 100)
        }

    def generate_signals(self) -> Dict:
        """
        Generate buy/sell/hold signals based on multiple indicators

        Returns:
            Dictionary with signals and confidence scores
        """
        if len(self.data) < 200:
            return {'error': 'Insufficient data for analysis (need at least 200 days)'}

        signals = []
        confidence_scores = []

        # RSI Analysis
        rsi = self.calculate_rsi()
        current_rsi = rsi.iloc[-1]
        if current_rsi < 30:
            signals.append('buy')
            confidence_scores.append(0.7)
        elif current_rsi > 70:
            signals.append('sell')
            confidence_scores.append(0.7)
        else:
            signals.append('hold')
            confidence_scores.append(0.3)

        # MACD Analysis
        macd_line, signal_line, histogram = self.calculate_macd()
        if macd_line.iloc[-1] > signal_line.iloc[-1] and macd_line.iloc[-2] <= signal_line.iloc[-2]:
            signals.append('buy')
            confidence_scores.append(0.6)
        elif macd_line.iloc[-1] < signal_line.iloc[-1] and macd_line.iloc[-2] >= signal_line.iloc[-2]:
            signals.append('sell')
            confidence_scores.append(0.6)

        # Moving Average Trend
        trend = self.get_trend_direction()
        if trend == 'bullish':
            signals.append('buy')
            confidence_scores.append(0.5)
        elif trend == 'bearish':
            signals.append('sell')
            confidence_scores.append(0.5)

        # Golden/Death Cross
        if self.check_golden_cross():
            signals.append('buy')
            confidence_scores.append(0.9)
        elif self.check_death_cross():
            signals.append('sell')
            confidence_scores.append(0.9)

        # Aggregate signals
        buy_count = signals.count('buy')
        sell_count = signals.count('sell')
        hold_count = signals.count('hold')

        if buy_count > sell_count:
            final_signal = 'buy'
        elif sell_count > buy_count:
            final_signal = 'sell'
        else:
            final_signal = 'hold'

        avg_confidence = np.mean(confidence_scores) if confidence_scores else 0.5

        return {
            'signal': final_signal,
            'confidence': float(avg_confidence),
            'rsi': float(current_rsi),
            'trend': trend,
            'golden_cross': self.check_golden_cross(),
            'death_cross': self.check_death_cross(),
            'indicators': {
                'buy_signals': buy_count,
                'sell_signals': sell_count,
                'hold_signals': hold_count
            }
        }

    def get_full_analysis(self) -> Dict:
        """Get comprehensive technical analysis"""
        if self.data.empty:
            return {'error': 'No data available'}

        try:
            rsi = self.calculate_rsi()
            macd_line, signal_line, histogram = self.calculate_macd()
            upper_bb, middle_bb, lower_bb = self.calculate_bollinger_bands()
            sma_20 = self.calculate_sma(20)
            sma_50 = self.calculate_sma(50)
            sma_200 = self.calculate_sma(200)

            return {
                'current_price': float(self.data['Close'].iloc[-1]),
                'rsi': float(rsi.iloc[-1]) if not pd.isna(rsi.iloc[-1]) else None,
                'macd': {
                    'macd_line': float(macd_line.iloc[-1]),
                    'signal_line': float(signal_line.iloc[-1]),
                    'histogram': float(histogram.iloc[-1])
                },
                'moving_averages': {
                    'sma_20': float(sma_20.iloc[-1]) if not pd.isna(sma_20.iloc[-1]) else None,
                    'sma_50': float(sma_50.iloc[-1]) if not pd.isna(sma_50.iloc[-1]) else None,
                    'sma_200': float(sma_200.iloc[-1]) if not pd.isna(sma_200.iloc[-1]) else None
                },
                'bollinger_bands': {
                    'upper': float(upper_bb.iloc[-1]),
                    'middle': float(middle_bb.iloc[-1]),
                    'lower': float(lower_bb.iloc[-1])
                },
                'volume': self.calculate_volume_analysis(),
                'support_resistance': self.get_support_resistance(),
                'signals': self.generate_signals(),
                'trend': self.get_trend_direction()
            }
        except Exception as e:
            logger.error(f"Error in full analysis: {e}")
            return {'error': str(e)}


if __name__ == "__main__":
    # Test the module
    from stock_data import get_stock_data

    print("Testing technical analysis...")
    data = get_stock_data('AAPL', period='1y')

    if not data.empty:
        analyzer = TechnicalAnalyzer(data)
        analysis = analyzer.get_full_analysis()

        print("\nTechnical Analysis for AAPL:")
        print(f"Current Price: ${analysis['current_price']:.2f}")
        print(f"RSI: {analysis['rsi']:.2f}")
        print(f"Trend: {analysis['trend']}")
        print(f"Signal: {analysis['signals']['signal']} (confidence: {analysis['signals']['confidence']:.2f})")

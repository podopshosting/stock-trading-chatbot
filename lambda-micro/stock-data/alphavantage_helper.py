"""
Alpha Vantage API Integration for Real-time Stock Data
Provides current prices, historical data, and technical indicators
"""
import requests
import json
from typing import Dict, Optional, List
from datetime import datetime, timedelta


class AlphaVantageClient:
    """Client for Alpha Vantage API"""

    BASE_URL = "https://www.alphavantage.co/query"

    def __init__(self, api_key: str):
        self.api_key = api_key

    def get_quote(self, symbol: str) -> Optional[Dict]:
        """
        Get real-time quote for a stock
        Returns: {
            'symbol': 'AAPL',
            'price': 150.25,
            'change': 2.50,
            'change_percent': '1.69%',
            'volume': 50000000,
            'latest_trading_day': '2025-10-06',
            'previous_close': 147.75,
            'open': 148.00,
            'high': 151.00,
            'low': 147.50
        }
        """
        params = {
            'function': 'GLOBAL_QUOTE',
            'symbol': symbol,
            'apikey': self.api_key
        }

        try:
            response = requests.get(self.BASE_URL, params=params, timeout=10)
            data = response.json()

            if 'Global Quote' not in data:
                print(f"Error: No quote data for {symbol}")
                return None

            quote = data['Global Quote']

            if not quote:
                return None

            return {
                'symbol': quote.get('01. symbol', symbol),
                'price': float(quote.get('05. price', 0)),
                'change': float(quote.get('09. change', 0)),
                'change_percent': quote.get('10. change percent', '0%'),
                'volume': int(quote.get('06. volume', 0)),
                'latest_trading_day': quote.get('07. latest trading day', ''),
                'previous_close': float(quote.get('08. previous close', 0)),
                'open': float(quote.get('02. open', 0)),
                'high': float(quote.get('03. high', 0)),
                'low': float(quote.get('04. low', 0))
            }

        except Exception as e:
            print(f"Error fetching quote for {symbol}: {e}")
            return None

    def get_intraday_data(self, symbol: str, interval: str = '5min', outputsize: str = 'compact') -> Optional[Dict]:
        """
        Get intraday time series data
        interval: 1min, 5min, 15min, 30min, 60min
        outputsize: compact (latest 100 data points) or full (full history)
        """
        params = {
            'function': 'TIME_SERIES_INTRADAY',
            'symbol': symbol,
            'interval': interval,
            'outputsize': outputsize,
            'apikey': self.api_key
        }

        try:
            response = requests.get(self.BASE_URL, params=params, timeout=10)
            data = response.json()

            time_series_key = f'Time Series ({interval})'
            if time_series_key not in data:
                return None

            time_series = data[time_series_key]

            # Convert to list format
            prices = []
            for timestamp, values in list(time_series.items())[:100]:  # Limit to 100
                prices.append({
                    'timestamp': timestamp,
                    'open': float(values['1. open']),
                    'high': float(values['2. high']),
                    'low': float(values['3. low']),
                    'close': float(values['4. close']),
                    'volume': int(values['5. volume'])
                })

            return {
                'symbol': symbol,
                'interval': interval,
                'prices': prices
            }

        except Exception as e:
            print(f"Error fetching intraday data for {symbol}: {e}")
            return None

    def get_daily_data(self, symbol: str, outputsize: str = 'compact') -> Optional[Dict]:
        """
        Get daily time series data
        outputsize: compact (100 days) or full (20+ years)
        """
        params = {
            'function': 'TIME_SERIES_DAILY',
            'symbol': symbol,
            'outputsize': outputsize,
            'apikey': self.api_key
        }

        try:
            response = requests.get(self.BASE_URL, params=params, timeout=10)
            data = response.json()

            if 'Time Series (Daily)' not in data:
                return None

            time_series = data['Time Series (Daily)']

            # Convert to list format
            prices = []
            for date, values in list(time_series.items())[:100]:
                prices.append({
                    'date': date,
                    'open': float(values['1. open']),
                    'high': float(values['2. high']),
                    'low': float(values['3. low']),
                    'close': float(values['4. close']),
                    'volume': int(values['5. volume'])
                })

            return {
                'symbol': symbol,
                'prices': prices
            }

        except Exception as e:
            print(f"Error fetching daily data for {symbol}: {e}")
            return None

    def get_sma(self, symbol: str, interval: str = 'daily', time_period: int = 20, series_type: str = 'close') -> Optional[Dict]:
        """
        Get Simple Moving Average (SMA)
        interval: 1min, 5min, 15min, 30min, 60min, daily, weekly, monthly
        time_period: Number of data points (e.g., 20, 50, 200)
        series_type: close, open, high, low
        """
        params = {
            'function': 'SMA',
            'symbol': symbol,
            'interval': interval,
            'time_period': time_period,
            'series_type': series_type,
            'apikey': self.api_key
        }

        try:
            response = requests.get(self.BASE_URL, params=params, timeout=10)
            data = response.json()

            if 'Technical Analysis: SMA' not in data:
                return None

            sma_data = data['Technical Analysis: SMA']

            # Get latest SMA value
            latest_date = list(sma_data.keys())[0]
            latest_sma = float(sma_data[latest_date]['SMA'])

            return {
                'symbol': symbol,
                'indicator': 'SMA',
                'time_period': time_period,
                'interval': interval,
                'value': latest_sma,
                'date': latest_date
            }

        except Exception as e:
            print(f"Error fetching SMA for {symbol}: {e}")
            return None

    def get_rsi(self, symbol: str, interval: str = 'daily', time_period: int = 14, series_type: str = 'close') -> Optional[Dict]:
        """
        Get Relative Strength Index (RSI)
        time_period: Number of data points (typically 14)
        """
        params = {
            'function': 'RSI',
            'symbol': symbol,
            'interval': interval,
            'time_period': time_period,
            'series_type': series_type,
            'apikey': self.api_key
        }

        try:
            response = requests.get(self.BASE_URL, params=params, timeout=10)
            data = response.json()

            if 'Technical Analysis: RSI' not in data:
                return None

            rsi_data = data['Technical Analysis: RSI']

            # Get latest RSI value
            latest_date = list(rsi_data.keys())[0]
            latest_rsi = float(rsi_data[latest_date]['RSI'])

            return {
                'symbol': symbol,
                'indicator': 'RSI',
                'time_period': time_period,
                'interval': interval,
                'value': latest_rsi,
                'date': latest_date,
                'signal': 'oversold' if latest_rsi < 30 else 'overbought' if latest_rsi > 70 else 'neutral'
            }

        except Exception as e:
            print(f"Error fetching RSI for {symbol}: {e}")
            return None

    def get_macd(self, symbol: str, interval: str = 'daily', series_type: str = 'close') -> Optional[Dict]:
        """
        Get Moving Average Convergence Divergence (MACD)
        """
        params = {
            'function': 'MACD',
            'symbol': symbol,
            'interval': interval,
            'series_type': series_type,
            'apikey': self.api_key
        }

        try:
            response = requests.get(self.BASE_URL, params=params, timeout=10)
            data = response.json()

            if 'Technical Analysis: MACD' not in data:
                return None

            macd_data = data['Technical Analysis: MACD']

            # Get latest MACD values
            latest_date = list(macd_data.keys())[0]
            latest = macd_data[latest_date]

            macd = float(latest['MACD'])
            signal = float(latest['MACD_Signal'])
            histogram = float(latest['MACD_Hist'])

            return {
                'symbol': symbol,
                'indicator': 'MACD',
                'interval': interval,
                'macd': macd,
                'signal': signal,
                'histogram': histogram,
                'date': latest_date,
                'trend': 'bullish' if macd > signal else 'bearish'
            }

        except Exception as e:
            print(f"Error fetching MACD for {symbol}: {e}")
            return None

    def get_comprehensive_analysis(self, symbol: str) -> Dict:
        """
        Get comprehensive analysis combining quote, daily data, and technical indicators
        """
        analysis = {
            'symbol': symbol,
            'timestamp': datetime.now().isoformat(),
            'quote': None,
            'technical_indicators': {},
            'trend_analysis': None
        }

        # Get current quote
        quote = self.get_quote(symbol)
        if quote:
            analysis['quote'] = quote

        # Get technical indicators
        sma_20 = self.get_sma(symbol, interval='daily', time_period=20)
        sma_50 = self.get_sma(symbol, interval='daily', time_period=50)
        rsi = self.get_rsi(symbol, interval='daily', time_period=14)
        macd = self.get_macd(symbol, interval='daily')

        analysis['technical_indicators'] = {
            'sma_20': sma_20,
            'sma_50': sma_50,
            'rsi': rsi,
            'macd': macd
        }

        # Trend analysis
        if quote and sma_20 and sma_50:
            current_price = quote['price']
            sma20_value = sma_20['value']
            sma50_value = sma_50['value']

            trend = "neutral"
            if current_price > sma20_value > sma50_value:
                trend = "strong_bullish"
            elif current_price > sma20_value:
                trend = "bullish"
            elif current_price < sma20_value < sma50_value:
                trend = "strong_bearish"
            elif current_price < sma20_value:
                trend = "bearish"

            analysis['trend_analysis'] = {
                'trend': trend,
                'current_price': current_price,
                'sma_20': sma20_value,
                'sma_50': sma50_value,
                'price_vs_sma20': ((current_price - sma20_value) / sma20_value * 100),
                'price_vs_sma50': ((current_price - sma50_value) / sma50_value * 100)
            }

        return analysis


def get_recommendation(analysis: Dict) -> Dict:
    """
    Generate buy/sell/hold recommendation based on comprehensive analysis
    """
    if not analysis or 'quote' not in analysis or not analysis['quote']:
        return {
            'action': 'hold',
            'confidence': 0.0,
            'reason': 'Insufficient data'
        }

    signals = []

    # Trend signal
    if analysis.get('trend_analysis'):
        trend = analysis['trend_analysis']['trend']
        if trend == 'strong_bullish':
            signals.append(('buy', 0.8))
        elif trend == 'bullish':
            signals.append(('buy', 0.6))
        elif trend == 'strong_bearish':
            signals.append(('sell', 0.8))
        elif trend == 'bearish':
            signals.append(('sell', 0.6))

    # RSI signal
    rsi_data = analysis['technical_indicators'].get('rsi')
    if rsi_data:
        rsi_value = rsi_data['value']
        if rsi_value < 30:
            signals.append(('buy', 0.7))  # Oversold
        elif rsi_value > 70:
            signals.append(('sell', 0.7))  # Overbought

    # MACD signal
    macd_data = analysis['technical_indicators'].get('macd')
    if macd_data:
        if macd_data['trend'] == 'bullish':
            signals.append(('buy', 0.6))
        else:
            signals.append(('sell', 0.6))

    # Aggregate signals
    if not signals:
        return {
            'action': 'hold',
            'confidence': 0.5,
            'reason': 'No clear signals'
        }

    buy_signals = [s[1] for s in signals if s[0] == 'buy']
    sell_signals = [s[1] for s in signals if s[0] == 'sell']

    buy_score = sum(buy_signals) if buy_signals else 0
    sell_score = sum(sell_signals) if sell_signals else 0

    if buy_score > sell_score and buy_score > 1.0:
        action = 'buy'
        confidence = min(buy_score / len(signals), 1.0)
        reason = f"Bullish signals detected (RSI: {rsi_data['value'] if rsi_data else 'N/A'}, Trend: {analysis['trend_analysis']['trend'] if analysis.get('trend_analysis') else 'N/A'})"
    elif sell_score > buy_score and sell_score > 1.0:
        action = 'sell'
        confidence = min(sell_score / len(signals), 1.0)
        reason = f"Bearish signals detected (RSI: {rsi_data['value'] if rsi_data else 'N/A'}, Trend: {analysis['trend_analysis']['trend'] if analysis.get('trend_analysis') else 'N/A'})"
    else:
        action = 'hold'
        confidence = 0.5
        reason = "Mixed signals, wait for clearer trend"

    return {
        'action': action,
        'confidence': confidence,
        'reason': reason,
        'signals_count': len(signals),
        'buy_score': buy_score,
        'sell_score': sell_score
    }

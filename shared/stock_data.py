"""
Stock data fetching module using yfinance
"""
import yfinance as yf
import pandas as pd
from datetime import datetime, timedelta
from typing import Dict, List, Optional
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class StockDataFetcher:
    """Fetches real-time and historical stock data"""

    SUPPORTED_STOCKS = ['AAPL', 'TSLA', 'MSFT', 'GOOGL', 'AMZN', 'NVDA', 'META', 'NFLX', 'AMD', 'INTC']

    def __init__(self):
        self.cache = {}
        self.cache_timeout = 300  # 5 minutes

    def get_current_price(self, symbol: str) -> Optional[float]:
        """Get current stock price"""
        try:
            ticker = yf.Ticker(symbol)
            data = ticker.history(period='1d', interval='1m')
            if not data.empty:
                return float(data['Close'].iloc[-1])
            return None
        except Exception as e:
            logger.error(f"Error fetching current price for {symbol}: {e}")
            return None

    def get_historical_data(self, symbol: str, period: str = '1y', interval: str = '1d') -> pd.DataFrame:
        """
        Get historical stock data

        Args:
            symbol: Stock ticker symbol
            period: Data period (1d, 5d, 1mo, 3mo, 6mo, 1y, 2y, 5y, 10y, ytd, max)
            interval: Data interval (1m, 2m, 5m, 15m, 30m, 60m, 90m, 1h, 1d, 5d, 1wk, 1mo, 3mo)

        Returns:
            DataFrame with OHLCV data
        """
        try:
            ticker = yf.Ticker(symbol)
            data = ticker.history(period=period, interval=interval)

            if data.empty:
                logger.warning(f"No historical data available for {symbol}")
                return pd.DataFrame()

            return data
        except Exception as e:
            logger.error(f"Error fetching historical data for {symbol}: {e}")
            return pd.DataFrame()

    def get_stock_info(self, symbol: str) -> Dict:
        """Get detailed stock information"""
        try:
            ticker = yf.Ticker(symbol)
            info = ticker.info

            return {
                'symbol': symbol,
                'name': info.get('longName', symbol),
                'sector': info.get('sector', 'Unknown'),
                'industry': info.get('industry', 'Unknown'),
                'market_cap': info.get('marketCap', 0),
                'pe_ratio': info.get('trailingPE', None),
                'dividend_yield': info.get('dividendYield', None),
                '52_week_high': info.get('fiftyTwoWeekHigh', None),
                '52_week_low': info.get('fiftyTwoWeekLow', None),
                'avg_volume': info.get('averageVolume', 0),
                'description': info.get('longBusinessSummary', '')
            }
        except Exception as e:
            logger.error(f"Error fetching stock info for {symbol}: {e}")
            return {'symbol': symbol, 'error': str(e)}

    def get_multiple_stocks(self, symbols: List[str], period: str = '1mo') -> Dict[str, pd.DataFrame]:
        """Get historical data for multiple stocks"""
        results = {}
        for symbol in symbols:
            results[symbol] = self.get_historical_data(symbol, period)
        return results

    def get_intraday_data(self, symbol: str, interval: str = '5m') -> pd.DataFrame:
        """Get intraday data for real-time analysis"""
        return self.get_historical_data(symbol, period='1d', interval=interval)

    def validate_symbol(self, symbol: str) -> bool:
        """Check if a symbol is valid and tradeable"""
        try:
            ticker = yf.Ticker(symbol)
            info = ticker.info
            return 'regularMarketPrice' in info or 'currentPrice' in info
        except:
            return False

    def get_price_change(self, symbol: str, period: str = '1d') -> Dict:
        """Get price change over a period"""
        try:
            data = self.get_historical_data(symbol, period=period)
            if data.empty or len(data) < 2:
                return {'error': 'Insufficient data'}

            start_price = float(data['Close'].iloc[0])
            end_price = float(data['Close'].iloc[-1])
            change = end_price - start_price
            change_percent = (change / start_price) * 100

            return {
                'symbol': symbol,
                'start_price': start_price,
                'end_price': end_price,
                'change': change,
                'change_percent': change_percent,
                'period': period
            }
        except Exception as e:
            logger.error(f"Error calculating price change for {symbol}: {e}")
            return {'error': str(e)}


# Convenience functions
def get_stock_price(symbol: str) -> Optional[float]:
    """Quick function to get current stock price"""
    fetcher = StockDataFetcher()
    return fetcher.get_current_price(symbol)


def get_stock_data(symbol: str, period: str = '1y') -> pd.DataFrame:
    """Quick function to get historical data"""
    fetcher = StockDataFetcher()
    return fetcher.get_historical_data(symbol, period)


if __name__ == "__main__":
    # Test the module
    fetcher = StockDataFetcher()

    print("Testing stock data fetcher...")
    print(f"\nCurrent AAPL price: ${fetcher.get_current_price('AAPL')}")

    print("\nAAPL Info:")
    info = fetcher.get_stock_info('AAPL')
    for key, value in info.items():
        if key != 'description':
            print(f"  {key}: {value}")

    print("\nAAPL 1-day change:")
    change = fetcher.get_price_change('AAPL', '1d')
    print(f"  Change: ${change.get('change', 0):.2f} ({change.get('change_percent', 0):.2f}%)")

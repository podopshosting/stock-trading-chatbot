"""
Unit tests for stock_data module
"""
import pytest
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../shared'))

from stock_data import StockDataFetcher


def test_stock_fetcher_initialization():
    """Test StockDataFetcher initialization"""
    fetcher = StockDataFetcher()
    assert fetcher is not None
    assert hasattr(fetcher, 'SUPPORTED_STOCKS')


def test_validate_symbol():
    """Test symbol validation"""
    fetcher = StockDataFetcher()
    assert fetcher.validate_symbol('AAPL') == True
    assert fetcher.validate_symbol('INVALID123') == False


def test_get_current_price():
    """Test getting current stock price"""
    fetcher = StockDataFetcher()
    price = fetcher.get_current_price('AAPL')
    assert price is not None
    assert isinstance(price, float)
    assert price > 0


def test_get_historical_data():
    """Test getting historical data"""
    fetcher = StockDataFetcher()
    data = fetcher.get_historical_data('AAPL', period='1mo')
    assert not data.empty
    assert 'Close' in data.columns
    assert 'Volume' in data.columns


def test_get_stock_info():
    """Test getting stock information"""
    fetcher = StockDataFetcher()
    info = fetcher.get_stock_info('AAPL')
    assert 'symbol' in info
    assert info['symbol'] == 'AAPL'
    assert 'name' in info


if __name__ == '__main__':
    pytest.main([__file__, '-v'])

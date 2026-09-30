"""
Unit tests for the legacy shared/stock_data module (yfinance architecture).

LEGACY - NOT THE PRODUCTION PATH.
Production runs lambda-micro/chatbot-router on Alpha Vantage; see
tests/test_chatbot_router.py. This module is kept for the historical
yfinance implementation in shared/, which no deployed Lambda imports.

These tests need pytest + yfinance and hit the live network, so they are
skipped automatically when those are unavailable (e.g. in CI) rather than
failing the build for code that is not deployed.
"""
import sys
import os
import unittest

pytest = None
StockDataFetcher = None
try:
    import pytest  # noqa: F401
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../shared'))
    from stock_data import StockDataFetcher
except Exception as _import_error:  # pragma: no cover - environment dependent
    raise unittest.SkipTest(
        f"legacy yfinance tests skipped: {_import_error}"
    )


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

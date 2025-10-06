"""
AWS Lambda handler for stock analysis
"""
import json
import os
import sys

# Add shared modules to path
sys.path.insert(0, '/opt/python')  # Lambda layer path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../../shared'))

from stock_data import StockDataFetcher
from technical_analysis import TechnicalAnalyzer
import logging

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def lambda_handler(event, context):
    """
    Perform technical analysis on a stock

    Event format:
    {
        "symbol": "AAPL",
        "period": "1y"  # optional
    }
    """
    try:
        # Parse input
        if isinstance(event.get('body'), str):
            body = json.loads(event['body'])
        else:
            body = event

        symbol = body.get('symbol', '').upper()
        period = body.get('period', '1y')

        if not symbol:
            return {
                'statusCode': 400,
                'headers': {
                    'Content-Type': 'application/json',
                    'Access-Control-Allow-Origin': '*'
                },
                'body': json.dumps({
                    'error': 'Symbol parameter is required'
                })
            }

        logger.info(f"Analyzing stock: {symbol}")

        # Fetch stock data
        fetcher = StockDataFetcher()

        if not fetcher.validate_symbol(symbol):
            return {
                'statusCode': 404,
                'headers': {
                    'Content-Type': 'application/json',
                    'Access-Control-Allow-Origin': '*'
                },
                'body': json.dumps({
                    'error': f'Invalid stock symbol: {symbol}'
                })
            }

        # Get data
        current_price = fetcher.get_current_price(symbol)
        historical_data = fetcher.get_historical_data(symbol, period=period)
        stock_info = fetcher.get_stock_info(symbol)
        price_change = fetcher.get_price_change(symbol, period='1d')

        if historical_data.empty:
            return {
                'statusCode': 500,
                'headers': {
                    'Content-Type': 'application/json',
                    'Access-Control-Allow-Origin': '*'
                },
                'body': json.dumps({
                    'error': 'Unable to fetch historical data'
                })
            }

        # Perform technical analysis
        analyzer = TechnicalAnalyzer(historical_data)
        analysis = analyzer.get_full_analysis()

        # Prepare historical data for response (last 90 days)
        recent_data = historical_data.tail(90)
        historical_json = {
            'dates': recent_data.index.strftime('%Y-%m-%d').tolist(),
            'open': recent_data['Open'].tolist(),
            'high': recent_data['High'].tolist(),
            'low': recent_data['Low'].tolist(),
            'close': recent_data['Close'].tolist(),
            'volume': recent_data['Volume'].tolist()
        }

        return {
            'statusCode': 200,
            'headers': {
                'Content-Type': 'application/json',
                'Access-Control-Allow-Origin': '*'
            },
            'body': json.dumps({
                'symbol': symbol,
                'current_price': current_price,
                'price_change': price_change,
                'stock_info': stock_info,
                'technical_analysis': analysis,
                'historical_data': historical_json
            }, default=str)
        }

    except Exception as e:
        logger.error(f"Error analyzing stock: {str(e)}", exc_info=True)
        return {
            'statusCode': 500,
            'headers': {
                'Content-Type': 'application/json',
                'Access-Control-Allow-Origin': '*'
            },
            'body': json.dumps({
                'error': 'Internal server error',
                'message': str(e)
            })
        }


# For local testing
if __name__ == "__main__":
    test_event = {
        "symbol": "AAPL",
        "period": "1y"
    }

    result = lambda_handler(test_event, None)
    print(json.dumps(json.loads(result['body']), indent=2))

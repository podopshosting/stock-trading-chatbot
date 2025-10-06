"""
Stock data microservice - uses Yahoo Finance API via HTTP
No yfinance library needed - just HTTP requests
"""
import json
import requests
from datetime import datetime, timedelta


def lambda_handler(event, context):
    """Fetch stock data via Yahoo Finance API"""
    try:
        if isinstance(event.get('body'), str):
            body = json.loads(event['body'])
        else:
            body = event

        symbol = body.get('symbol', '').upper()

        if not symbol:
            return {
                'statusCode': 400,
                'headers': {'Content-Type': 'application/json', 'Access-Control-Allow-Origin': '*'},
                'body': json.dumps({'error': 'Symbol required'})
            }

        # Use Yahoo Finance API directly (no library needed)
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
        params = {
            'interval': '1d',
            'range': '1y'
        }

        response = requests.get(url, params=params, timeout=10)
        data = response.json()

        if 'chart' not in data or 'result' not in data['chart']:
            return {
                'statusCode': 404,
                'headers': {'Content-Type': 'application/json', 'Access-Control-Allow-Origin': '*'},
                'body': json.dumps({'error': 'Stock not found'})
            }

        result = data['chart']['result'][0]
        meta = result.get('meta', {})
        quotes = result.get('indicators', {}).get('quote', [{}])[0]

        current_price = meta.get('regularMarketPrice', 0)
        previous_close = meta.get('previousClose', 0)
        change = current_price - previous_close
        change_percent = (change / previous_close * 100) if previous_close > 0 else 0

        # Get last 90 days of data for charts
        timestamps = result.get('timestamp', [])[-90:]
        closes = quotes.get('close', [])[-90:]

        # Calculate simple moving averages
        sma_20 = sum(closes[-20:]) / 20 if len(closes) >= 20 else current_price
        sma_50 = sum(closes[-50:]) / 50 if len(closes) >= 50 else current_price

        return {
            'statusCode': 200,
            'headers': {'Content-Type': 'application/json', 'Access-Control-Allow-Origin': '*'},
            'body': json.dumps({
                'symbol': symbol,
                'price': current_price,
                'change': change,
                'change_percent': change_percent,
                'previous_close': previous_close,
                'volume': meta.get('regularMarketVolume', 0),
                'sma_20': sma_20,
                'sma_50': sma_50,
                'historical': {
                    'timestamps': timestamps,
                    'closes': [c for c in closes if c is not None]
                }
            })
        }

    except Exception as e:
        print(f"Error: {e}")
        return {
            'statusCode': 500,
            'headers': {'Content-Type': 'application/json', 'Access-Control-Allow-Origin': '*'},
            'body': json.dumps({'error': str(e)})
        }

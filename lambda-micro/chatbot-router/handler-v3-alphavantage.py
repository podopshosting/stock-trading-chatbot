"""
Enhanced chatbot router with Alpha Vantage integration
Provides real-time stock data and comprehensive technical analysis
"""
import json
import os
import boto3
import requests
from typing import Optional, Dict


def get_secret(secret_name: str) -> Optional[str]:
    """Get secret from AWS Secrets Manager"""
    try:
        client = boto3.client('secretsmanager', region_name='us-east-2')
        response = client.get_secret_value(SecretId=secret_name)
        return response['SecretString']
    except Exception as e:
        print(f"Error getting secret {secret_name}: {e}")
        return None


def get_alpha_vantage_quote(symbol: str, api_key: str) -> Optional[Dict]:
    """Get real-time stock quote from Alpha Vantage"""
    try:
        url = "https://www.alphavantage.co/query"
        params = {
            'function': 'GLOBAL_QUOTE',
            'symbol': symbol,
            'apikey': api_key
        }

        response = requests.get(url, params=params, timeout=10)
        data = response.json()

        if 'Global Quote' not in data or not data['Global Quote']:
            print(f"No quote data for {symbol}")
            return None

        quote = data['Global Quote']

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
        print(f"Error fetching quote: {e}")
        return None


def get_alpha_vantage_rsi(symbol: str, api_key: str) -> Optional[Dict]:
    """Get RSI indicator from Alpha Vantage"""
    try:
        url = "https://www.alphavantage.co/query"
        params = {
            'function': 'RSI',
            'symbol': symbol,
            'interval': 'daily',
            'time_period': '14',
            'series_type': 'close',
            'apikey': api_key
        }

        response = requests.get(url, params=params, timeout=10)
        data = response.json()

        if 'Technical Analysis: RSI' not in data:
            return None

        rsi_data = data['Technical Analysis: RSI']
        latest_date = list(rsi_data.keys())[0]
        latest_rsi = float(rsi_data[latest_date]['RSI'])

        return {
            'value': latest_rsi,
            'date': latest_date,
            'signal': 'oversold' if latest_rsi < 30 else 'overbought' if latest_rsi > 70 else 'neutral'
        }
    except Exception as e:
        print(f"Error fetching RSI: {e}")
        return None


def get_alpha_vantage_sma(symbol: str, api_key: str, time_period: int = 20) -> Optional[Dict]:
    """Get SMA indicator from Alpha Vantage"""
    try:
        url = "https://www.alphavantage.co/query"
        params = {
            'function': 'SMA',
            'symbol': symbol,
            'interval': 'daily',
            'time_period': str(time_period),
            'series_type': 'close',
            'apikey': api_key
        }

        response = requests.get(url, params=params, timeout=10)
        data = response.json()

        if 'Technical Analysis: SMA' not in data:
            return None

        sma_data = data['Technical Analysis: SMA']
        latest_date = list(sma_data.keys())[0]
        latest_sma = float(sma_data[latest_date]['SMA'])

        return {
            'value': latest_sma,
            'period': time_period,
            'date': latest_date
        }
    except Exception as e:
        print(f"Error fetching SMA: {e}")
        return None


def analyze_stock(symbol: str, av_api_key: str) -> Dict:
    """Perform comprehensive stock analysis using Alpha Vantage"""
    analysis = {
        'symbol': symbol,
        'quote': None,
        'rsi': None,
        'sma_20': None,
        'sma_50': None,
        'trend': 'neutral',
        'recommendation': 'hold',
        'confidence': 0.5
    }

    # Get quote
    quote = get_alpha_vantage_quote(symbol, av_api_key)
    if quote:
        analysis['quote'] = quote

    # Get technical indicators (with rate limiting consideration)
    rsi = get_alpha_vantage_rsi(symbol, av_api_key)
    if rsi:
        analysis['rsi'] = rsi

    # For demo, we'll limit API calls to avoid rate limits
    # In production, implement caching or use premium API

    # Determine recommendation based on available data
    signals = []

    if quote:
        # Price momentum signal
        change_percent = float(quote['change_percent'].rstrip('%'))
        if change_percent > 2:
            signals.append(('buy', 0.6))
        elif change_percent < -2:
            signals.append(('sell', 0.6))

    if rsi:
        # RSI signal
        if rsi['signal'] == 'oversold':
            signals.append(('buy', 0.7))
        elif rsi['signal'] == 'overbought':
            signals.append(('sell', 0.7))

    # Aggregate signals
    if signals:
        buy_signals = [s[1] for s in signals if s[0] == 'buy']
        sell_signals = [s[1] for s in signals if s[0] == 'sell']

        buy_score = sum(buy_signals) if buy_signals else 0
        sell_score = sum(sell_signals) if sell_signals else 0

        if buy_score > sell_score and buy_score > 0.5:
            analysis['recommendation'] = 'buy'
            analysis['confidence'] = min(buy_score, 1.0)
            analysis['trend'] = 'bullish'
        elif sell_score > buy_score and sell_score > 0.5:
            analysis['recommendation'] = 'sell'
            analysis['confidence'] = min(sell_score, 1.0)
            analysis['trend'] = 'bearish'

    return analysis


def call_openai(prompt: str, api_key: str) -> Optional[str]:
    """Call OpenAI API directly via HTTP"""
    try:
        url = "https://api.openai.com/v1/chat/completions"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}"
        }
        data = {
            "model": "gpt-4o-mini",
            "messages": [
                {"role": "system", "content": "You are a knowledgeable stock trading advisor who provides concise, data-driven analysis. Keep responses under 150 words."},
                {"role": "user", "content": prompt}
            ],
            "max_tokens": 250,
            "temperature": 0.7
        }

        response = requests.post(url, headers=headers, json=data, timeout=30)
        response.raise_for_status()

        result = response.json()
        return result['choices'][0]['message']['content']
    except Exception as e:
        print(f"OpenAI API error: {e}")
        return None


def lambda_handler(event, context):
    """Enhanced chatbot handler with Alpha Vantage integration"""
    try:
        # Parse input
        if isinstance(event.get('body'), str):
            body = json.loads(event['body'])
        else:
            body = event

        query = body.get('query', '')

        if not query:
            return {
                'statusCode': 400,
                'headers': {'Content-Type': 'application/json', 'Access-Control-Allow-Origin': '*'},
                'body': json.dumps({'error': 'Query required'})
            }

        # Extract stock symbol
        symbols = ['AAPL', 'TSLA', 'MSFT', 'GOOGL', 'AMZN', 'NVDA', 'META', 'NFLX', 'AMD', 'INTC']
        query_upper = query.upper()
        stock_symbol = None

        for symbol in symbols:
            if symbol in query_upper:
                stock_symbol = symbol
                break

        if not stock_symbol:
            return {
                'statusCode': 200,
                'headers': {'Content-Type': 'application/json', 'Access-Control-Allow-Origin': '*'},
                'body': json.dumps({
                    'response': f"Please mention a stock symbol like {', '.join(symbols[:5])}...\n\nSupported stocks: {', '.join(symbols)}"
                })
            }

        # Get API keys
        openai_key = get_secret('stock-chatbot/openai-api-key')
        av_key = get_secret('stock-chatbot/alphavantage-api-key')

        # Perform stock analysis with Alpha Vantage
        analysis = {'quote': None}
        if av_key:
            analysis = analyze_stock(stock_symbol, av_key)

        # Build response with real data
        quote = analysis.get('quote')
        rsi = analysis.get('rsi')
        recommendation = analysis.get('recommendation', 'hold')
        trend = analysis.get('trend', 'neutral')

        if quote:
            price = quote['price']
            change = quote['change']
            change_pct = quote['change_percent']
            volume = quote['volume']
        else:
            price = "N/A"
            change = 0
            change_pct = "0%"
            volume = 0

        # Generate AI response with real-time data
        if openai_key and quote:
            # Enhanced prompt with real data
            prompt = f"""Analyze this stock based on real-time data:

Stock: {stock_symbol}
Current Price: ${price}
Change: ${change} ({change_pct})
Volume: {volume:,}
Trend: {trend}
RSI: {rsi['value'] if rsi else 'N/A'} ({rsi['signal'] if rsi else 'N/A'})
Technical Recommendation: {recommendation.upper()}

User Question: {query}

Provide a concise analysis (2-3 sentences) addressing their question with this data. Include the recommendation and end with a disclaimer."""

            ai_response = call_openai(prompt, openai_key)

            if ai_response:
                response_text = f"""📊 **{stock_symbol}** - ${price}

📈 **Today's Performance**
Change: ${change} ({change_pct})
Volume: {volume:,}

🔍 **Technical Analysis**
Trend: {trend.upper()}
RSI: {rsi['value']:.1f} ({rsi['signal']}) if rsi else 'N/A'
Recommendation: {recommendation.upper()}

{ai_response}"""
            else:
                response_text = f"""📊 **{stock_symbol}** - ${price}

Change: ${change} ({change_pct})
Volume: {volume:,}

Based on current technical indicators (Trend: {trend}, RSI: {rsi['value']:.1f if rsi else 'N/A'}), the recommendation is to **{recommendation.upper()}**.

⚠️ This is not financial advice. Please consult a licensed financial advisor before making investment decisions."""
        else:
            # Fallback without AI
            response_text = f"""📊 **{stock_symbol}** - ${price}

Change: ${change} ({change_pct})
Volume: {volume:,}

Current market data shows {trend} trend. For detailed analysis, please check financial news sources.

⚠️ This is not financial advice."""

        return {
            'statusCode': 200,
            'headers': {
                'Content-Type': 'application/json',
                'Access-Control-Allow-Origin': '*'
            },
            'body': json.dumps({
                'response': response_text,
                'symbol': stock_symbol,
                'query': query,
                'data': {
                    'price': price,
                    'change': change,
                    'change_percent': change_pct,
                    'volume': volume,
                    'trend': trend,
                    'rsi': rsi['value'] if rsi else None,
                    'recommendation': recommendation
                }
            })
        }

    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()
        return {
            'statusCode': 500,
            'headers': {'Content-Type': 'application/json', 'Access-Control-Allow-Origin': '*'},
            'body': json.dumps({'error': str(e)})
        }

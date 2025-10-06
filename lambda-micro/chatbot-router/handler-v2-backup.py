"""
Lightweight chatbot router - Direct OpenAI API calls (no library)
"""
import json
import os
import boto3
import requests


def get_openai_key():
    """Get OpenAI API key from Secrets Manager"""
    try:
        client = boto3.client('secretsmanager', region_name='us-east-2')
        response = client.get_secret_value(SecretId='stock-chatbot/openai-api-key')
        return response['SecretString']
    except Exception as e:
        print(f"Error getting API key: {e}")
        return None


def call_openai(prompt, api_key):
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
                {"role": "system", "content": "You are a helpful stock trading advisor. Keep responses under 150 words."},
                {"role": "user", "content": prompt}
            ],
            "max_tokens": 200,
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
    """Lightweight chatbot handler"""
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

        # Extract stock symbol from query
        symbols = ['AAPL', 'TSLA', 'MSFT', 'GOOGL', 'AMZN', 'NVDA', 'META', 'NFLX', 'AMD', 'INTC']
        query_upper = query.upper()
        stock_symbol = None

        # Check if any symbol appears anywhere in the query
        for symbol in symbols:
            if symbol in query_upper:
                stock_symbol = symbol
                break

        if not stock_symbol:
            return {
                'statusCode': 200,
                'headers': {'Content-Type': 'application/json', 'Access-Control-Allow-Origin': '*'},
                'body': json.dumps({
                    'response': f"Please mention a stock symbol like {', '.join(symbols[:3])}..."
                })
            }

        # Get stock data from microservice (optional - can skip for now)
        stock_price = "N/A"
        try:
            stock_api_url = os.getenv('STOCK_DATA_API_URL')
            if stock_api_url:
                stock_response = requests.post(
                    stock_api_url,
                    json={'symbol': stock_symbol},
                    timeout=5
                )
                if stock_response.status_code == 200:
                    stock_data = stock_response.json()
                    body_data = json.loads(stock_data.get('body', '{}'))
                    stock_price = body_data.get('price', 'N/A')
        except:
            pass  # Continue without stock data

        # Get OpenAI key
        api_key = get_openai_key()

        if not api_key:
            # Fallback response without OpenAI
            response_text = f"""**{stock_symbol}** Analysis

Current Price: ${stock_price}

Based on your query: "{query}"

I can provide basic stock information. For advanced AI analysis, OpenAI API key configuration is required.

⚠️ This is not financial advice. Consult a licensed financial advisor before making investment decisions."""
        else:
            # Use OpenAI via direct HTTP call
            prompt = f"""You are a stock trading advisor.

User asked: {query}
Stock: {stock_symbol}
Current Price: ${stock_price}

Provide a brief, helpful response (2-3 sentences) about this stock. Consider current market conditions and end with a disclaimer that this is not financial advice."""

            ai_response = call_openai(prompt, api_key)

            if ai_response:
                response_text = f"**{stock_symbol}** - ${stock_price}\n\n{ai_response}"
            else:
                response_text = f"""**{stock_symbol}** Analysis

Current Price: ${stock_price}

Based on market analysis and your query about {stock_symbol}, I recommend consulting recent financial reports and analyst ratings.

⚠️ This is not financial advice. Consult a licensed financial advisor before making investment decisions."""

        return {
            'statusCode': 200,
            'headers': {'Content-Type': 'application/json', 'Access-Control-Allow-Origin': '*'},
            'body': json.dumps({
                'response': response_text,
                'symbol': stock_symbol,
                'query': query
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

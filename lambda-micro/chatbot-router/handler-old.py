"""
Lightweight chatbot router - OpenAI only
No heavy dependencies needed
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


def lambda_handler(event, context):
    """
    Lightweight chatbot handler
    Calls other microservices and uses OpenAI to generate response
    """
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

        # Get stock data from microservice
        stock_api_url = os.getenv('STOCK_DATA_API_URL')
        if stock_api_url:
            stock_response = requests.post(
                stock_api_url,
                json={'symbol': stock_symbol},
                timeout=10
            )
            stock_data = stock_response.json() if stock_response.status_code == 200 else {}
        else:
            stock_data = {'price': 'N/A', 'change': 'N/A'}

        # Get OpenAI key
        api_key = get_openai_key()

        if not api_key:
            # Fallback response without OpenAI
            response_text = f"""**{stock_symbol}** - Current Price: ${stock_data.get('price', 'N/A')}

Based on the query: {query}

I can provide stock information, but advanced AI analysis requires OpenAI configuration.

⚠️ This is not financial advice. Consult a licensed financial advisor."""
        else:
            # Use OpenAI for response
            try:
                import openai
                openai.api_key = api_key

                context = f"""You are a stock trading advisor.

User asked: {query}
Stock: {stock_symbol}
Current Price: ${stock_data.get('price', 'N/A')}
Change: {stock_data.get('change', 'N/A')}

Provide a brief, helpful response (2-3 sentences). End with the disclaimer that this is not financial advice."""

                completion = openai.ChatCompletion.create(
                    model="gpt-4o-mini",
                    messages=[
                        {"role": "system", "content": "You are a helpful stock trading advisor."},
                        {"role": "user", "content": context}
                    ],
                    max_tokens=200,
                    temperature=0.7
                )

                response_text = completion.choices[0].message.content

            except Exception as e:
                print(f"OpenAI error: {e}")
                response_text = f"**{stock_symbol}** analysis unavailable. Please try again."

        return {
            'statusCode': 200,
            'headers': {'Content-Type': 'application/json', 'Access-Control-Allow-Origin': '*'},
            'body': json.dumps({'response': response_text, 'symbol': stock_symbol})
        }

    except Exception as e:
        print(f"Error: {e}")
        return {
            'statusCode': 500,
            'headers': {'Content-Type': 'application/json', 'Access-Control-Allow-Origin': '*'},
            'body': json.dumps({'error': str(e)})
        }

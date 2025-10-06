"""
News fetcher microservice - lightweight HTTP requests only
"""
import json
import os
import requests
import boto3


def get_marketaux_key():
    """Get Marketaux API key from env or Secrets Manager"""
    key = os.getenv('MARKETAUX_API_KEY')
    if key:
        return key

    try:
        client = boto3.client('secretsmanager', region_name='us-east-2')
        response = client.get_secret_value(SecretId='stock-chatbot/api-keys')
        secrets = json.loads(response['SecretString'])
        return secrets.get('MARKETAUX_API_KEY')
    except:
        return None


def lambda_handler(event, context):
    """Fetch news for a stock"""
    try:
        if isinstance(event.get('body'), str):
            body = json.loads(event['body'])
        else:
            body = event

        symbol = body.get('symbol', '').upper()
        limit = body.get('limit', 5)

        if not symbol:
            return {
                'statusCode': 400,
                'headers': {'Content-Type': 'application/json', 'Access-Control-Allow-Origin': '*'},
                'body': json.dumps({'error': 'Symbol required'})
            }

        api_key = get_marketaux_key()

        if not api_key:
            return {
                'statusCode': 200,
                'headers': {'Content-Type': 'application/json', 'Access-Control-Allow-Origin': '*'},
                'body': json.dumps({
                    'symbol': symbol,
                    'articles': [],
                    'message': 'News API not configured'
                })
            }

        # Fetch news from Marketaux
        url = 'https://api.marketaux.com/v1/news/all'
        params = {
            'api_token': api_key,
            'symbols': symbol,
            'limit': limit,
            'language': 'en'
        }

        response = requests.get(url, params=params, timeout=10)
        data = response.json()

        articles = []
        if 'data' in data:
            for article in data['data']:
                articles.append({
                    'title': article.get('title', ''),
                    'description': article.get('description', ''),
                    'url': article.get('url', ''),
                    'source': article.get('source', ''),
                    'published_at': article.get('published_at', '')
                })

        return {
            'statusCode': 200,
            'headers': {'Content-Type': 'application/json', 'Access-Control-Allow-Origin': '*'},
            'body': json.dumps({
                'symbol': symbol,
                'articles': articles,
                'count': len(articles)
            })
        }

    except Exception as e:
        print(f"Error: {e}")
        return {
            'statusCode': 500,
            'headers': {'Content-Type': 'application/json', 'Access-Control-Allow-Origin': '*'},
            'body': json.dumps({'error': str(e)})
        }

"""
AWS Lambda handler for news and sentiment analysis
"""
import json
import os
import sys

# Add shared modules to path
sys.path.insert(0, '/opt/python')  # Lambda layer path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../../shared'))

from news_fetcher import get_news_fetcher
from sentiment_analyzer import SentimentAnalyzer
import logging

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def lambda_handler(event, context):
    """
    Fetch news and perform sentiment analysis

    Event format:
    {
        "symbol": "AAPL",  # optional
        "limit": 10,       # optional
        "analyze_sentiment": true  # optional
    }
    """
    try:
        # Parse input
        if isinstance(event.get('body'), str):
            body = json.loads(event['body'])
        else:
            body = event

        symbol = body.get('symbol', '').upper() if body.get('symbol') else None
        limit = body.get('limit', 10)
        analyze_sentiment = body.get('analyze_sentiment', True)

        logger.info(f"Fetching news for: {symbol if symbol else 'market'}")

        # Initialize fetcher
        api_key = os.getenv('MARKETAUX_API_KEY')
        news_fetcher = get_news_fetcher(api_key)

        # Fetch news
        if symbol:
            articles = news_fetcher.get_stock_news(symbol, limit=limit)
        else:
            articles = news_fetcher.get_market_news(limit=limit)

        # Perform sentiment analysis if requested
        sentiment_results = None
        if analyze_sentiment and articles:
            sentiment_analyzer = SentimentAnalyzer()
            if symbol:
                sentiment_results = sentiment_analyzer.analyze_stock_sentiment(symbol, articles)
            else:
                sentiment_results = sentiment_analyzer.analyze_multiple_articles(articles)

        return {
            'statusCode': 200,
            'headers': {
                'Content-Type': 'application/json',
                'Access-Control-Allow-Origin': '*'
            },
            'body': json.dumps({
                'symbol': symbol,
                'article_count': len(articles),
                'articles': articles[:limit],  # Limit response size
                'sentiment_analysis': sentiment_results
            })
        }

    except Exception as e:
        logger.error(f"Error fetching news: {str(e)}", exc_info=True)
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
        "limit": 5,
        "analyze_sentiment": True
    }

    result = lambda_handler(test_event, None)
    print(json.dumps(json.loads(result['body']), indent=2))

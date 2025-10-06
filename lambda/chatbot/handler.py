"""
AWS Lambda handler for chatbot queries
"""
import json
import os
import sys

# Add shared modules to path
sys.path.insert(0, '/opt/python')  # Lambda layer path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../../shared'))

from recommendation_engine import RecommendationEngine
from openai_helper import enhance_response_with_openai
import logging

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def lambda_handler(event, context):
    """
    Handle chatbot queries

    Event format:
    {
        "query": "Should I invest in AAPL?",
        "user_id": "optional_user_id"
    }
    """
    try:
        # Parse input
        if isinstance(event.get('body'), str):
            body = json.loads(event['body'])
        else:
            body = event

        query = body.get('query', '')
        user_id = body.get('user_id', 'anonymous')
        use_openai = body.get('use_openai', True)  # Enable OpenAI by default

        if not query:
            return {
                'statusCode': 400,
                'headers': {
                    'Content-Type': 'application/json',
                    'Access-Control-Allow-Origin': '*'
                },
                'body': json.dumps({
                    'error': 'Query parameter is required'
                })
            }

        logger.info(f"Processing query from {user_id}: {query}")

        # Initialize recommendation engine
        api_key = os.getenv('MARKETAUX_API_KEY')
        engine = RecommendationEngine(api_key=api_key)

        # Check if this is a specific stock query
        query_lower = query.lower()
        symbols = [w.upper() for w in query_lower.split()
                  if w.upper() in ['AAPL', 'TSLA', 'MSFT', 'GOOGL', 'AMZN', 'NVDA', 'META', 'NFLX', 'AMD', 'INTC']]

        # If asking about a specific stock and OpenAI is enabled, use enhanced response
        if symbols and use_openai:
            symbol = symbols[0]
            analysis = engine.analyze_stock(symbol)

            if 'error' not in analysis:
                response = enhance_response_with_openai(analysis, query)
            else:
                response = f"Sorry, I couldn't analyze {symbol}: {analysis.get('error', 'Unknown error')}"
        else:
            # Use standard recommendation engine
            response = engine.process_query(query)

        return {
            'statusCode': 200,
            'headers': {
                'Content-Type': 'application/json',
                'Access-Control-Allow-Origin': '*'
            },
            'body': json.dumps({
                'response': response,
                'query': query,
                'user_id': user_id
            })
        }

    except Exception as e:
        logger.error(f"Error processing query: {str(e)}", exc_info=True)
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
        "query": "Should I invest in AAPL?",
        "user_id": "test_user"
    }

    result = lambda_handler(test_event, None)
    print(json.dumps(json.loads(result['body']), indent=2))

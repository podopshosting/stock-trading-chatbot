"""
OpenAI integration for enhanced chatbot responses
"""
import os
import json
import boto3
from typing import Optional, Dict
import logging

logger = logging.getLogger(__name__)


def get_openai_api_key() -> Optional[str]:
    """Retrieve OpenAI API key from AWS Secrets Manager"""
    try:
        # Try environment variable first
        api_key = os.getenv('OPENAI_API_KEY')
        if api_key:
            return api_key

        # Fallback to Secrets Manager
        region = os.getenv('AWS_REGION', 'us-east-2')
        secret_name = 'stock-chatbot/openai-api-key'

        client = boto3.client('secretsmanager', region_name=region)
        response = client.get_secret_value(SecretId=secret_name)

        if 'SecretString' in response:
            return response['SecretString']
        else:
            logger.error("Secret not found in expected format")
            return None

    except Exception as e:
        logger.error(f"Error retrieving OpenAI API key: {e}")
        return None


def enhance_response_with_openai(
    stock_analysis: Dict,
    user_query: str,
    api_key: Optional[str] = None
) -> str:
    """
    Use OpenAI to generate a more natural, conversational response

    Args:
        stock_analysis: Technical and sentiment analysis results
        user_query: Original user query
        api_key: OpenAI API key (optional, will retrieve if not provided)

    Returns:
        Enhanced natural language response
    """
    try:
        # Get API key if not provided
        if not api_key:
            api_key = get_openai_api_key()

        if not api_key:
            # Fallback to basic response if no API key
            return generate_basic_response(stock_analysis)

        # Try importing openai (won't be available in all environments)
        try:
            import openai
            openai.api_key = api_key
        except ImportError:
            logger.warning("OpenAI library not available, using basic response")
            return generate_basic_response(stock_analysis)

        # Prepare context for OpenAI
        symbol = stock_analysis.get('symbol', 'UNKNOWN')
        recommendation = stock_analysis.get('recommendation', {})
        technical = stock_analysis.get('technical_analysis', {})
        sentiment = stock_analysis.get('sentiment_analysis', {})

        context = f"""
You are a professional stock trading advisor. Analyze this stock data and provide a clear, actionable recommendation.

Stock: {symbol}
Current Price: ${stock_analysis.get('current_price', 'N/A')}

Technical Analysis:
- Recommendation: {recommendation.get('action', 'N/A')}
- Confidence: {recommendation.get('confidence', 0) * 100:.0f}%
- RSI: {technical.get('rsi', 'N/A')}
- Trend: {technical.get('trend', 'N/A')}
- Rationale: {recommendation.get('rationale', 'N/A')}

Sentiment Analysis:
- News Sentiment: {sentiment.get('sentiment', 'N/A')}
- Article Count: {sentiment.get('article_count', 0)}
- Recommendation: {sentiment.get('recommendation', 'N/A')}

User Question: {user_query}

Provide a conversational, professional response that:
1. Directly answers the user's question
2. Explains the key factors influencing your recommendation
3. Mentions important technical indicators
4. Notes any significant news sentiment
5. Ends with the disclaimer that this is not financial advice

Keep the response under 150 words and make it easy to understand for non-experts.
"""

        # Call OpenAI API - Using GPT-4o-mini for cost efficiency
        response = openai.ChatCompletion.create(
            model="gpt-4o-mini",  # Much cheaper: $0.15/$0.60 per 1M tokens vs GPT-4's $30/$60
            messages=[
                {
                    "role": "system",
                    "content": "You are a helpful stock trading advisor who provides clear, actionable insights based on technical and sentiment analysis. Always remind users this is not financial advice."
                },
                {
                    "role": "user",
                    "content": context
                }
            ],
            max_tokens=300,
            temperature=0.7
        )

        return response.choices[0].message.content.strip()

    except Exception as e:
        logger.error(f"Error using OpenAI: {e}")
        return generate_basic_response(stock_analysis)


def generate_basic_response(stock_analysis: Dict) -> str:
    """
    Generate a basic response without OpenAI (fallback)

    Args:
        stock_analysis: Analysis results

    Returns:
        Basic formatted response
    """
    symbol = stock_analysis.get('symbol', 'UNKNOWN')
    recommendation = stock_analysis.get('recommendation', {})
    price = stock_analysis.get('current_price', 0)

    action = recommendation.get('action', 'HOLD')
    confidence = recommendation.get('confidence', 0) * 100
    rationale = recommendation.get('rationale', 'No analysis available')

    response = f"""**{symbol} Analysis** (${price:.2f})

**Recommendation:** {action} (Confidence: {confidence:.0f}%)

{rationale}

⚠️ This is not financial advice. Consult a licensed financial advisor before making investment decisions."""

    return response


def get_openai_market_summary(stocks_data: list, api_key: Optional[str] = None) -> str:
    """
    Generate a market summary using OpenAI

    Args:
        stocks_data: List of stock analysis data
        api_key: OpenAI API key

    Returns:
        Market summary text
    """
    try:
        if not api_key:
            api_key = get_openai_api_key()

        if not api_key:
            return "Market summary not available."

        try:
            import openai
            openai.api_key = api_key
        except ImportError:
            return "Market summary not available."

        # Prepare summary data
        summary_points = []
        for stock in stocks_data[:5]:  # Top 5 stocks
            symbol = stock.get('symbol', 'N/A')
            rec = stock.get('recommendation', {})
            action = rec.get('action', 'N/A')
            summary_points.append(f"{symbol}: {action}")

        context = f"""
Provide a brief 2-3 sentence market summary based on these stock recommendations:
{', '.join(summary_points)}

Make it conversational and highlight any interesting trends.
"""

        response = openai.ChatCompletion.create(
            model="gpt-4o-mini",  # Cost-effective choice
            messages=[
                {"role": "system", "content": "You are a stock market analyst."},
                {"role": "user", "content": context}
            ],
            max_tokens=150,
            temperature=0.7
        )

        return response.choices[0].message.content.strip()

    except Exception as e:
        logger.error(f"Error generating market summary: {e}")
        return "Market summary not available."


if __name__ == "__main__":
    # Test the module
    print("Testing OpenAI helper...")

    api_key = get_openai_api_key()
    if api_key:
        print("✅ API key retrieved successfully")
        print(f"Key starts with: {api_key[:10]}...")
    else:
        print("❌ Could not retrieve API key")

    # Test basic response generation
    test_analysis = {
        'symbol': 'AAPL',
        'current_price': 175.50,
        'recommendation': {
            'action': 'BUY',
            'confidence': 0.85,
            'rationale': 'Strong technical indicators and positive news sentiment'
        },
        'technical_analysis': {
            'rsi': 45.2,
            'trend': 'bullish'
        },
        'sentiment_analysis': {
            'sentiment': 'positive',
            'article_count': 10,
            'recommendation': 'Positive news coverage'
        }
    }

    response = generate_basic_response(test_analysis)
    print("\nBasic response:")
    print(response)

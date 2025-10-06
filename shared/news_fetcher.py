"""
News fetching module using marketaux API
"""
import requests
import os
from typing import List, Dict, Optional
from datetime import datetime, timedelta
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class NewsFetcher:
    """Fetches financial news related to stocks"""

    def __init__(self, api_key: Optional[str] = None):
        """
        Initialize news fetcher

        Args:
            api_key: Marketaux API key (or set MARKETAUX_API_KEY env variable)
        """
        self.api_key = api_key or os.getenv('MARKETAUX_API_KEY')
        self.base_url = 'https://api.marketaux.com/v1'

        if not self.api_key:
            logger.warning("No Marketaux API key provided. News fetching will be limited.")

    def get_stock_news(self, symbol: str, limit: int = 10, days: int = 7) -> List[Dict]:
        """
        Get news articles related to a specific stock

        Args:
            symbol: Stock ticker symbol
            limit: Number of articles to retrieve
            days: Number of days to look back

        Returns:
            List of news articles with metadata
        """
        if not self.api_key:
            return []

        try:
            params = {
                'api_token': self.api_key,
                'symbols': symbol,
                'limit': limit,
                'language': 'en'
            }

            response = requests.get(f'{self.base_url}/news/all', params=params, timeout=10)
            response.raise_for_status()

            data = response.json()

            if 'data' not in data:
                logger.warning(f"No news data returned for {symbol}")
                return []

            articles = []
            for article in data['data']:
                articles.append({
                    'title': article.get('title', ''),
                    'description': article.get('description', ''),
                    'url': article.get('url', ''),
                    'source': article.get('source', ''),
                    'published_at': article.get('published_at', ''),
                    'entities': article.get('entities', []),
                    'snippet': article.get('snippet', '')
                })

            return articles

        except requests.exceptions.RequestException as e:
            logger.error(f"Error fetching news for {symbol}: {e}")
            return []
        except Exception as e:
            logger.error(f"Unexpected error fetching news for {symbol}: {e}")
            return []

    def get_market_news(self, limit: int = 20) -> List[Dict]:
        """
        Get general market news

        Args:
            limit: Number of articles to retrieve

        Returns:
            List of news articles
        """
        if not self.api_key:
            return []

        try:
            params = {
                'api_token': self.api_key,
                'limit': limit,
                'language': 'en',
                'filter_entities': 'true'
            }

            response = requests.get(f'{self.base_url}/news/all', params=params, timeout=10)
            response.raise_for_status()

            data = response.json()

            if 'data' not in data:
                logger.warning("No market news data returned")
                return []

            articles = []
            for article in data['data']:
                articles.append({
                    'title': article.get('title', ''),
                    'description': article.get('description', ''),
                    'url': article.get('url', ''),
                    'source': article.get('source', ''),
                    'published_at': article.get('published_at', ''),
                    'entities': article.get('entities', []),
                    'snippet': article.get('snippet', '')
                })

            return articles

        except requests.exceptions.RequestException as e:
            logger.error(f"Error fetching market news: {e}")
            return []
        except Exception as e:
            logger.error(f"Unexpected error fetching market news: {e}")
            return []

    def get_multiple_stocks_news(self, symbols: List[str], limit: int = 5) -> Dict[str, List[Dict]]:
        """
        Get news for multiple stocks

        Args:
            symbols: List of stock ticker symbols
            limit: Number of articles per stock

        Returns:
            Dictionary mapping symbols to news articles
        """
        results = {}
        for symbol in symbols:
            results[symbol] = self.get_stock_news(symbol, limit=limit)
        return results

    def search_news(self, query: str, limit: int = 10) -> List[Dict]:
        """
        Search for news articles by keyword

        Args:
            query: Search query
            limit: Number of articles to retrieve

        Returns:
            List of matching news articles
        """
        if not self.api_key:
            return []

        try:
            params = {
                'api_token': self.api_key,
                'search': query,
                'limit': limit,
                'language': 'en'
            }

            response = requests.get(f'{self.base_url}/news/all', params=params, timeout=10)
            response.raise_for_status()

            data = response.json()

            if 'data' not in data:
                logger.warning(f"No news data returned for query: {query}")
                return []

            articles = []
            for article in data['data']:
                articles.append({
                    'title': article.get('title', ''),
                    'description': article.get('description', ''),
                    'url': article.get('url', ''),
                    'source': article.get('source', ''),
                    'published_at': article.get('published_at', ''),
                    'entities': article.get('entities', []),
                    'snippet': article.get('snippet', '')
                })

            return articles

        except requests.exceptions.RequestException as e:
            logger.error(f"Error searching news for '{query}': {e}")
            return []
        except Exception as e:
            logger.error(f"Unexpected error searching news for '{query}': {e}")
            return []

    def get_trending_news(self, limit: int = 10) -> List[Dict]:
        """Get trending financial news"""
        return self.get_market_news(limit=limit)

    def format_article_summary(self, article: Dict) -> str:
        """Format a news article into a readable summary"""
        title = article.get('title', 'No title')
        source = article.get('source', 'Unknown source')
        published = article.get('published_at', '')
        description = article.get('description', article.get('snippet', ''))

        summary = f"**{title}**\n"
        summary += f"Source: {source}\n"
        if published:
            summary += f"Published: {published}\n"
        if description:
            summary += f"\n{description}\n"

        return summary


# Fallback news sources (when API is not available)
class FallbackNewsFetcher:
    """Fallback news fetcher that returns empty results"""

    def get_stock_news(self, symbol: str, limit: int = 10, days: int = 7) -> List[Dict]:
        logger.warning("Using fallback news fetcher - no API key configured")
        return []

    def get_market_news(self, limit: int = 20) -> List[Dict]:
        logger.warning("Using fallback news fetcher - no API key configured")
        return []

    def get_multiple_stocks_news(self, symbols: List[str], limit: int = 5) -> Dict[str, List[Dict]]:
        return {symbol: [] for symbol in symbols}

    def search_news(self, query: str, limit: int = 10) -> List[Dict]:
        return []


def get_news_fetcher(api_key: Optional[str] = None):
    """Factory function to get appropriate news fetcher"""
    api_key = api_key or os.getenv('MARKETAUX_API_KEY')
    if api_key:
        return NewsFetcher(api_key)
    else:
        return FallbackNewsFetcher()


if __name__ == "__main__":
    # Test the module
    fetcher = get_news_fetcher()

    print("Testing news fetcher...")
    print("\nFetching AAPL news:")
    news = fetcher.get_stock_news('AAPL', limit=3)

    if news:
        for article in news:
            print(f"\n{article['title']}")
            print(f"Source: {article['source']}")
            print(f"URL: {article['url']}")
    else:
        print("No news available (API key may not be configured)")

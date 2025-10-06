"""
Sentiment analysis module for news articles and text
Uses VADER sentiment analysis for financial text
"""
from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
from typing import Dict, List
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class SentimentAnalyzer:
    """Analyzes sentiment of news articles and text"""

    def __init__(self):
        """Initialize VADER sentiment analyzer"""
        try:
            self.analyzer = SentimentIntensityAnalyzer()
        except Exception as e:
            logger.error(f"Error initializing sentiment analyzer: {e}")
            self.analyzer = None

    def analyze_text(self, text: str) -> Dict:
        """
        Analyze sentiment of a text string

        Args:
            text: Text to analyze

        Returns:
            Dictionary with sentiment scores and classification
        """
        if not self.analyzer or not text:
            return {
                'compound': 0.0,
                'positive': 0.0,
                'negative': 0.0,
                'neutral': 1.0,
                'sentiment': 'neutral'
            }

        try:
            scores = self.analyzer.polarity_scores(text)

            # Classify sentiment based on compound score
            compound = scores['compound']
            if compound >= 0.05:
                sentiment = 'positive'
            elif compound <= -0.05:
                sentiment = 'negative'
            else:
                sentiment = 'neutral'

            return {
                'compound': scores['compound'],
                'positive': scores['pos'],
                'negative': scores['neg'],
                'neutral': scores['neu'],
                'sentiment': sentiment
            }
        except Exception as e:
            logger.error(f"Error analyzing sentiment: {e}")
            return {
                'compound': 0.0,
                'positive': 0.0,
                'negative': 0.0,
                'neutral': 1.0,
                'sentiment': 'neutral',
                'error': str(e)
            }

    def analyze_article(self, article: Dict) -> Dict:
        """
        Analyze sentiment of a news article

        Args:
            article: Dictionary with article data (title, description, etc.)

        Returns:
            Sentiment analysis results
        """
        # Combine title and description for analysis
        text_parts = []

        title = article.get('title', '')
        description = article.get('description', article.get('snippet', ''))

        if title:
            text_parts.append(title)
        if description:
            text_parts.append(description)

        combined_text = ' '.join(text_parts)

        sentiment = self.analyze_text(combined_text)
        sentiment['article_title'] = title

        return sentiment

    def analyze_multiple_articles(self, articles: List[Dict]) -> Dict:
        """
        Analyze sentiment across multiple articles

        Args:
            articles: List of article dictionaries

        Returns:
            Aggregated sentiment analysis
        """
        if not articles:
            return {
                'overall_sentiment': 'neutral',
                'average_compound': 0.0,
                'positive_count': 0,
                'negative_count': 0,
                'neutral_count': 0,
                'total_articles': 0
            }

        sentiments = []
        positive_count = 0
        negative_count = 0
        neutral_count = 0

        for article in articles:
            sentiment = self.analyze_article(article)
            sentiments.append(sentiment)

            if sentiment['sentiment'] == 'positive':
                positive_count += 1
            elif sentiment['sentiment'] == 'negative':
                negative_count += 1
            else:
                neutral_count += 1

        # Calculate average compound score
        avg_compound = sum(s['compound'] for s in sentiments) / len(sentiments) if sentiments else 0

        # Determine overall sentiment
        if avg_compound >= 0.05:
            overall_sentiment = 'positive'
        elif avg_compound <= -0.05:
            overall_sentiment = 'negative'
        else:
            overall_sentiment = 'neutral'

        return {
            'overall_sentiment': overall_sentiment,
            'average_compound': avg_compound,
            'positive_count': positive_count,
            'negative_count': negative_count,
            'neutral_count': neutral_count,
            'total_articles': len(articles),
            'sentiment_score': self._calculate_sentiment_score(positive_count, negative_count, neutral_count),
            'individual_sentiments': sentiments
        }

    def _calculate_sentiment_score(self, positive: int, negative: int, neutral: int) -> float:
        """
        Calculate a normalized sentiment score from -1 to 1

        Args:
            positive: Number of positive articles
            negative: Number of negative articles
            neutral: Number of neutral articles

        Returns:
            Score from -1 (very negative) to 1 (very positive)
        """
        total = positive + negative + neutral
        if total == 0:
            return 0.0

        # Weight positive and negative, neutral doesn't affect score
        score = (positive - negative) / total
        return score

    def get_sentiment_label(self, compound_score: float) -> str:
        """
        Get descriptive label for sentiment score

        Args:
            compound_score: Compound sentiment score

        Returns:
            Descriptive label
        """
        if compound_score >= 0.5:
            return 'very_positive'
        elif compound_score >= 0.05:
            return 'positive'
        elif compound_score <= -0.5:
            return 'very_negative'
        elif compound_score <= -0.05:
            return 'negative'
        else:
            return 'neutral'

    def analyze_stock_sentiment(self, symbol: str, articles: List[Dict]) -> Dict:
        """
        Analyze sentiment for a specific stock based on news articles

        Args:
            symbol: Stock ticker symbol
            articles: List of news articles about the stock

        Returns:
            Comprehensive sentiment analysis
        """
        if not articles:
            return {
                'symbol': symbol,
                'sentiment': 'neutral',
                'confidence': 0.0,
                'article_count': 0,
                'recommendation': 'Insufficient news data for sentiment analysis'
            }

        analysis = self.analyze_multiple_articles(articles)

        # Calculate confidence based on number of articles and sentiment strength
        article_count = len(articles)
        confidence = min(1.0, (article_count / 10) * abs(analysis['average_compound']))

        # Generate recommendation
        sentiment = analysis['overall_sentiment']
        if sentiment == 'positive' and confidence > 0.5:
            recommendation = f"Positive news sentiment detected. {analysis['positive_count']} positive articles."
        elif sentiment == 'negative' and confidence > 0.5:
            recommendation = f"Negative news sentiment detected. {analysis['negative_count']} negative articles."
        else:
            recommendation = "Mixed or neutral news sentiment."

        return {
            'symbol': symbol,
            'sentiment': sentiment,
            'sentiment_score': analysis['sentiment_score'],
            'average_compound': analysis['average_compound'],
            'confidence': confidence,
            'article_count': article_count,
            'positive_articles': analysis['positive_count'],
            'negative_articles': analysis['negative_count'],
            'neutral_articles': analysis['neutral_count'],
            'recommendation': recommendation,
            'detailed_analysis': analysis
        }


# Convenience functions
def analyze_sentiment(text: str) -> Dict:
    """Quick function to analyze sentiment of text"""
    analyzer = SentimentAnalyzer()
    return analyzer.analyze_text(text)


def analyze_news_sentiment(articles: List[Dict]) -> Dict:
    """Quick function to analyze sentiment of news articles"""
    analyzer = SentimentAnalyzer()
    return analyzer.analyze_multiple_articles(articles)


if __name__ == "__main__":
    # Test the module
    analyzer = SentimentAnalyzer()

    print("Testing sentiment analyzer...")

    # Test with sample texts
    test_texts = [
        "Apple stock surges to all-time high on strong earnings report!",
        "Tesla faces challenges amid increased competition and regulatory scrutiny.",
        "Microsoft announces new product lineup.",
    ]

    for text in test_texts:
        sentiment = analyzer.analyze_text(text)
        print(f"\nText: {text}")
        print(f"Sentiment: {sentiment['sentiment']} (compound: {sentiment['compound']:.3f})")

    # Test with sample articles
    sample_articles = [
        {
            'title': 'Apple stock soars on record iPhone sales',
            'description': 'Apple reports better than expected quarterly earnings with strong iPhone demand'
        },
        {
            'title': 'Concerns grow over Apple supply chain issues',
            'description': 'Analysts worry about potential production delays'
        }
    ]

    print("\n\nAnalyzing multiple articles:")
    multi_sentiment = analyzer.analyze_multiple_articles(sample_articles)
    print(f"Overall sentiment: {multi_sentiment['overall_sentiment']}")
    print(f"Average compound: {multi_sentiment['average_compound']:.3f}")
    print(f"Positive: {multi_sentiment['positive_count']}, Negative: {multi_sentiment['negative_count']}")

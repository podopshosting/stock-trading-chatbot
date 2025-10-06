"""
Stock recommendation engine
Combines technical analysis, news sentiment, and market data to generate recommendations
"""
from typing import Dict, List, Optional
import logging
from datetime import datetime

from stock_data import StockDataFetcher
from technical_analysis import TechnicalAnalyzer
from news_fetcher import get_news_fetcher
from sentiment_analyzer import SentimentAnalyzer

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class RecommendationEngine:
    """Generates stock buy/sell/hold recommendations"""

    def __init__(self, api_key: Optional[str] = None):
        """
        Initialize recommendation engine

        Args:
            api_key: Marketaux API key for news
        """
        self.stock_fetcher = StockDataFetcher()
        self.news_fetcher = get_news_fetcher(api_key)
        self.sentiment_analyzer = SentimentAnalyzer()

    def analyze_stock(self, symbol: str) -> Dict:
        """
        Perform comprehensive analysis of a stock

        Args:
            symbol: Stock ticker symbol

        Returns:
            Complete analysis with recommendation
        """
        logger.info(f"Analyzing {symbol}...")

        # Validate symbol
        if not self.stock_fetcher.validate_symbol(symbol):
            return {
                'symbol': symbol,
                'error': 'Invalid stock symbol',
                'recommendation': 'N/A'
            }

        try:
            # Get stock data
            current_price = self.stock_fetcher.get_current_price(symbol)
            stock_info = self.stock_fetcher.get_stock_info(symbol)
            historical_data = self.stock_fetcher.get_historical_data(symbol, period='1y')

            if historical_data.empty:
                return {
                    'symbol': symbol,
                    'error': 'Unable to fetch historical data',
                    'recommendation': 'N/A'
                }

            # Technical analysis
            tech_analyzer = TechnicalAnalyzer(historical_data)
            technical_analysis = tech_analyzer.get_full_analysis()

            # News and sentiment analysis
            news_articles = self.news_fetcher.get_stock_news(symbol, limit=10)
            sentiment_analysis = self.sentiment_analyzer.analyze_stock_sentiment(symbol, news_articles)

            # Generate recommendation
            recommendation = self._generate_recommendation(
                symbol,
                technical_analysis,
                sentiment_analysis,
                stock_info
            )

            return {
                'symbol': symbol,
                'timestamp': datetime.now().isoformat(),
                'current_price': current_price,
                'stock_info': stock_info,
                'technical_analysis': technical_analysis,
                'sentiment_analysis': sentiment_analysis,
                'recommendation': recommendation,
                'news_count': len(news_articles)
            }

        except Exception as e:
            logger.error(f"Error analyzing {symbol}: {e}")
            return {
                'symbol': symbol,
                'error': str(e),
                'recommendation': 'N/A'
            }

    def _generate_recommendation(
        self,
        symbol: str,
        technical: Dict,
        sentiment: Dict,
        info: Dict
    ) -> Dict:
        """
        Generate final recommendation based on all analyses

        Args:
            symbol: Stock ticker
            technical: Technical analysis results
            sentiment: Sentiment analysis results
            info: Stock information

        Returns:
            Recommendation with action, confidence, and rationale
        """
        signals = technical.get('signals', {})
        tech_signal = signals.get('signal', 'hold')
        tech_confidence = signals.get('confidence', 0.5)

        sentiment_signal = sentiment.get('sentiment', 'neutral')
        sentiment_score = sentiment.get('sentiment_score', 0.0)

        # Weight factors
        TECHNICAL_WEIGHT = 0.6
        SENTIMENT_WEIGHT = 0.4

        # Convert to numeric scores
        signal_scores = {
            'buy': 1.0,
            'hold': 0.0,
            'sell': -1.0,
            'positive': 0.7,
            'negative': -0.7,
            'neutral': 0.0
        }

        tech_score = signal_scores.get(tech_signal, 0.0) * tech_confidence
        sent_score = signal_scores.get(sentiment_signal, 0.0)

        # Combined score
        combined_score = (tech_score * TECHNICAL_WEIGHT) + (sent_score * SENTIMENT_WEIGHT)

        # Determine action
        if combined_score > 0.3:
            action = 'BUY'
        elif combined_score < -0.3:
            action = 'SELL'
        else:
            action = 'HOLD'

        # Calculate confidence (0-1)
        confidence = min(1.0, abs(combined_score))

        # Build rationale
        rationale = self._build_rationale(
            symbol,
            action,
            technical,
            sentiment,
            info
        )

        return {
            'action': action,
            'confidence': float(confidence),
            'combined_score': float(combined_score),
            'rationale': rationale,
            'factors': {
                'technical_signal': tech_signal,
                'technical_confidence': tech_confidence,
                'sentiment': sentiment_signal,
                'sentiment_score': sentiment_score
            },
            'disclaimer': 'This is not financial advice. Consult a licensed financial advisor before making investment decisions.'
        }

    def _build_rationale(
        self,
        symbol: str,
        action: str,
        technical: Dict,
        sentiment: Dict,
        info: Dict
    ) -> str:
        """Build human-readable rationale for recommendation"""
        reasons = []

        # Technical reasons
        signals = technical.get('signals', {})
        rsi = technical.get('rsi')
        trend = technical.get('trend')

        if signals.get('golden_cross'):
            reasons.append("Golden Cross detected (50-day MA crossed above 200-day MA)")
        elif signals.get('death_cross'):
            reasons.append("Death Cross detected (50-day MA crossed below 200-day MA)")

        if rsi:
            if rsi < 30:
                reasons.append(f"RSI indicates oversold conditions ({rsi:.1f})")
            elif rsi > 70:
                reasons.append(f"RSI indicates overbought conditions ({rsi:.1f})")

        if trend:
            reasons.append(f"Overall trend is {trend}")

        # Sentiment reasons
        sent = sentiment.get('sentiment')
        article_count = sentiment.get('article_count', 0)

        if sent == 'positive' and article_count > 0:
            reasons.append(f"Positive news sentiment ({sentiment.get('positive_articles', 0)} positive articles)")
        elif sent == 'negative' and article_count > 0:
            reasons.append(f"Negative news sentiment ({sentiment.get('negative_articles', 0)} negative articles)")

        # Build final rationale
        if not reasons:
            rationale = f"{action} {symbol}: Mixed signals with moderate confidence."
        else:
            rationale = f"{action} {symbol}: " + "; ".join(reasons) + "."

        return rationale

    def compare_stocks(self, symbols: List[str]) -> List[Dict]:
        """
        Compare multiple stocks and rank by recommendation

        Args:
            symbols: List of stock ticker symbols

        Returns:
            List of analyses sorted by recommendation strength
        """
        analyses = []

        for symbol in symbols:
            analysis = self.analyze_stock(symbol)
            if 'error' not in analysis:
                analyses.append(analysis)

        # Sort by recommendation confidence (buy > hold > sell)
        def sort_key(analysis):
            rec = analysis.get('recommendation', {})
            action = rec.get('action', 'HOLD')
            confidence = rec.get('confidence', 0)

            if action == 'BUY':
                return (3, confidence)
            elif action == 'HOLD':
                return (2, confidence)
            else:  # SELL
                return (1, confidence)

        analyses.sort(key=sort_key, reverse=True)
        return analyses

    def get_top_picks(self, symbols: List[str], count: int = 5) -> List[Dict]:
        """
        Get top stock picks from a list

        Args:
            symbols: List of stock ticker symbols
            count: Number of top picks to return

        Returns:
            Top recommended stocks
        """
        analyses = self.compare_stocks(symbols)
        top_picks = [a for a in analyses if a['recommendation']['action'] == 'BUY']
        return top_picks[:count]

    def process_query(self, query: str) -> str:
        """
        Process natural language query about stocks

        Args:
            query: User query (e.g., "Should I buy AAPL?")

        Returns:
            Natural language response
        """
        query_lower = query.lower()

        # Extract stock symbols from query
        words = query_lower.split()
        symbols = [w.upper() for w in words if w.upper() in self.stock_fetcher.SUPPORTED_STOCKS]

        if not symbols:
            # Check if query is asking for recommendations
            if any(word in query_lower for word in ['recommend', 'suggest', 'best', 'top', 'good']):
                return self._generate_general_recommendations()
            else:
                return "I couldn't identify a stock symbol in your query. Please mention a stock ticker like AAPL, TSLA, MSFT, etc."

        # Analyze mentioned stocks
        if len(symbols) == 1:
            symbol = symbols[0]
            analysis = self.analyze_stock(symbol)

            if 'error' in analysis:
                return f"Sorry, I couldn't analyze {symbol}: {analysis['error']}"

            rec = analysis['recommendation']
            return self._format_recommendation_response(analysis)
        else:
            # Multiple stocks
            analyses = self.compare_stocks(symbols)
            return self._format_comparison_response(analyses)

    def _generate_general_recommendations(self) -> str:
        """Generate general stock recommendations"""
        top_picks = self.get_top_picks(self.stock_fetcher.SUPPORTED_STOCKS, count=3)

        if not top_picks:
            return "Unable to generate recommendations at this time. Please try again later."

        response = "Here are my top stock recommendations:\n\n"
        for i, analysis in enumerate(top_picks, 1):
            symbol = analysis['symbol']
            rec = analysis['recommendation']
            price = analysis.get('current_price', 'N/A')

            response += f"{i}. **{symbol}** (${price:.2f})\n"
            response += f"   {rec['rationale']}\n"
            response += f"   Confidence: {rec['confidence']*100:.0f}%\n\n"

        response += "\n⚠️ " + top_picks[0]['recommendation']['disclaimer']
        return response

    def _format_recommendation_response(self, analysis: Dict) -> str:
        """Format single stock analysis as response"""
        symbol = analysis['symbol']
        price = analysis.get('current_price', 'N/A')
        rec = analysis['recommendation']

        response = f"**{symbol} Analysis** (Current Price: ${price:.2f})\n\n"
        response += f"**Recommendation:** {rec['action']}\n"
        response += f"**Confidence:** {rec['confidence']*100:.0f}%\n\n"
        response += f"**Analysis:** {rec['rationale']}\n\n"

        # Add key metrics
        tech = analysis.get('technical_analysis', {})
        if tech.get('rsi'):
            response += f"RSI: {tech['rsi']:.1f} | "
        if tech.get('trend'):
            response += f"Trend: {tech['trend'].upper()}"

        response += f"\n\n⚠️ {rec['disclaimer']}"
        return response

    def _format_comparison_response(self, analyses: List[Dict]) -> str:
        """Format multiple stock comparison as response"""
        response = "**Stock Comparison:**\n\n"

        for analysis in analyses:
            symbol = analysis['symbol']
            rec = analysis['recommendation']
            price = analysis.get('current_price', 'N/A')

            response += f"• **{symbol}** (${price:.2f}): {rec['action']} "
            response += f"(Confidence: {rec['confidence']*100:.0f}%)\n"

        response += f"\n⚠️ {analyses[0]['recommendation']['disclaimer']}"
        return response


if __name__ == "__main__":
    # Test the module
    engine = RecommendationEngine()

    print("Testing recommendation engine...")

    # Test single stock analysis
    print("\nAnalyzing AAPL:")
    analysis = engine.analyze_stock('AAPL')
    if 'error' not in analysis:
        rec = analysis['recommendation']
        print(f"Action: {rec['action']}")
        print(f"Confidence: {rec['confidence']:.2f}")
        print(f"Rationale: {rec['rationale']}")

    # Test query processing
    print("\n\nProcessing query: 'Should I invest in TSLA?'")
    response = engine.process_query("Should I invest in TSLA?")
    print(response)

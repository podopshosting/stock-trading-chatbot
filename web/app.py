"""
Flask web application for Stock Trading Chatbot
"""
from flask import Flask, render_template, request, jsonify, session
from flask_cors import CORS
import os
import sys
from datetime import datetime

# Add shared modules to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../shared'))

from stock_data import StockDataFetcher
from recommendation_engine import RecommendationEngine
from news_fetcher import get_news_fetcher
from sentiment_analyzer import SentimentAnalyzer
import boto3
from botocore.exceptions import ClientError
import logging

# Initialize Flask app
app = Flask(__name__)
app.secret_key = os.getenv('SECRET_KEY', 'dev-secret-key-change-in-production')
CORS(app)

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Initialize components
stock_fetcher = StockDataFetcher()
recommendation_engine = RecommendationEngine(api_key=os.getenv('MARKETAUX_API_KEY'))
news_fetcher = get_news_fetcher()
sentiment_analyzer = SentimentAnalyzer()

# DynamoDB setup
dynamodb = boto3.resource('dynamodb', region_name=os.getenv('AWS_REGION', 'us-east-2'))
predictions_table = dynamodb.Table(os.getenv('DYNAMODB_TABLE_NAME', 'stock-chatbot-predictions'))

# Supported stocks
SUPPORTED_STOCKS = ['AAPL', 'TSLA', 'MSFT', 'GOOGL', 'AMZN', 'NVDA', 'META', 'NFLX', 'AMD', 'INTC']


@app.route('/')
def index():
    """Main dashboard page"""
    return render_template('index.html', supported_stocks=SUPPORTED_STOCKS)


@app.route('/api/stocks')
def get_stocks():
    """Get list of supported stocks with current prices"""
    try:
        stocks_data = []
        for symbol in SUPPORTED_STOCKS:
            price = stock_fetcher.get_current_price(symbol)
            change = stock_fetcher.get_price_change(symbol, period='1d')

            stocks_data.append({
                'symbol': symbol,
                'price': price,
                'change': change.get('change', 0),
                'change_percent': change.get('change_percent', 0)
            })

        return jsonify({
            'stocks': stocks_data
        })
    except Exception as e:
        logger.error(f"Error fetching stocks: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/stock/<symbol>')
def get_stock_detail(symbol):
    """Get detailed stock information"""
    try:
        symbol = symbol.upper()

        if symbol not in SUPPORTED_STOCKS:
            return jsonify({'error': 'Stock not supported'}), 404

        # Get comprehensive analysis
        analysis = recommendation_engine.analyze_stock(symbol)

        return jsonify(analysis)
    except Exception as e:
        logger.error(f"Error fetching stock detail for {symbol}: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/chatbot', methods=['POST'])
def chatbot():
    """Handle chatbot queries"""
    try:
        data = request.get_json()
        query = data.get('query', '')

        if not query:
            return jsonify({'error': 'Query is required'}), 400

        # Process query
        response = recommendation_engine.process_query(query)

        # Save to session history
        if 'chat_history' not in session:
            session['chat_history'] = []

        session['chat_history'].append({
            'query': query,
            'response': response,
            'timestamp': datetime.now().isoformat()
        })

        return jsonify({
            'response': response,
            'query': query
        })
    except Exception as e:
        logger.error(f"Error processing chatbot query: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/news/<symbol>')
def get_news(symbol):
    """Get news for a specific stock"""
    try:
        symbol = symbol.upper()
        limit = request.args.get('limit', 10, type=int)

        articles = news_fetcher.get_stock_news(symbol, limit=limit)
        sentiment = sentiment_analyzer.analyze_stock_sentiment(symbol, articles)

        return jsonify({
            'symbol': symbol,
            'articles': articles,
            'sentiment': sentiment
        })
    except Exception as e:
        logger.error(f"Error fetching news for {symbol}: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/predictions', methods=['GET', 'POST'])
def predictions():
    """Handle predictions - save and retrieve"""
    try:
        if request.method == 'POST':
            # Save new prediction
            data = request.get_json()

            prediction_id = f"{data['symbol']}_{datetime.now().strftime('%Y%m%d%H%M%S')}"

            item = {
                'prediction_id': prediction_id,
                'symbol': data['symbol'],
                'prediction': data['prediction'],
                'confidence': str(data['confidence']),
                'price_at_prediction': str(data.get('price_at_prediction', 0)),
                'rationale': data.get('rationale', ''),
                'timestamp': datetime.now().isoformat(),
                'outcome': 'pending',
                'created_at': int(datetime.now().timestamp())
            }

            predictions_table.put_item(Item=item)

            return jsonify({
                'message': 'Prediction saved',
                'prediction_id': prediction_id
            })

        else:
            # Get predictions
            symbol = request.args.get('symbol')
            limit = request.args.get('limit', 20, type=int)

            if symbol:
                response = predictions_table.query(
                    IndexName='symbol-index',
                    KeyConditionExpression='symbol = :symbol',
                    ExpressionAttributeValues={':symbol': symbol.upper()},
                    Limit=limit,
                    ScanIndexForward=False
                )
            else:
                response = predictions_table.scan(Limit=limit)

            items = response.get('Items', [])

            return jsonify({
                'predictions': items,
                'count': len(items)
            })

    except Exception as e:
        logger.error(f"Error handling predictions: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/accuracy')
def get_accuracy():
    """Get prediction accuracy statistics"""
    try:
        symbol = request.args.get('symbol')

        if symbol:
            response = predictions_table.query(
                IndexName='symbol-index',
                KeyConditionExpression='symbol = :symbol',
                ExpressionAttributeValues={':symbol': symbol.upper()}
            )
        else:
            response = predictions_table.scan()

        items = response.get('Items', [])

        # Calculate stats
        total = len(items)
        correct = sum(1 for item in items if item.get('outcome') == 'correct')
        incorrect = sum(1 for item in items if item.get('outcome') == 'incorrect')
        pending = sum(1 for item in items if item.get('outcome') == 'pending')

        accuracy = (correct / (correct + incorrect) * 100) if (correct + incorrect) > 0 else 0

        return jsonify({
            'total_predictions': total,
            'correct': correct,
            'incorrect': incorrect,
            'pending': pending,
            'accuracy_percentage': round(accuracy, 2),
            'symbol': symbol
        })

    except Exception as e:
        logger.error(f"Error calculating accuracy: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/compare', methods=['POST'])
def compare_stocks():
    """Compare multiple stocks"""
    try:
        data = request.get_json()
        symbols = data.get('symbols', [])

        if not symbols or len(symbols) > 5:
            return jsonify({'error': 'Provide 1-5 stock symbols'}), 400

        analyses = recommendation_engine.compare_stocks(symbols)

        return jsonify({
            'comparisons': analyses
        })

    except Exception as e:
        logger.error(f"Error comparing stocks: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/health')
def health_check():
    """Health check endpoint"""
    return jsonify({
        'status': 'healthy',
        'timestamp': datetime.now().isoformat()
    })


@app.errorhandler(404)
def not_found(e):
    """Handle 404 errors"""
    return jsonify({'error': 'Not found'}), 404


@app.errorhandler(500)
def server_error(e):
    """Handle 500 errors"""
    return jsonify({'error': 'Internal server error'}), 500


if __name__ == '__main__':
    port = int(os.getenv('PORT', 5000))
    debug = os.getenv('FLASK_DEBUG', 'False').lower() == 'true'

    print(f"Starting Stock Trading Chatbot web server...")
    print(f"Port: {port}")
    print(f"Debug: {debug}")
    print(f"Supported stocks: {', '.join(SUPPORTED_STOCKS)}")

    app.run(host='0.0.0.0', port=port, debug=debug)

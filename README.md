# 📈 Stock Trading Chatbot MVP

An AI-powered virtual day trader that analyzes stock trends, historical data, and news to provide investment recommendations with a beautiful web interface.

![Version](https://img.shields.io/badge/version-1.0.0-blue)
![Python](https://img.shields.io/badge/python-3.9+-green)
![AWS](https://img.shields.io/badge/AWS-Lambda%20%7C%20DynamoDB%20%7C%20API%20Gateway-orange)

## ✨ Features

- 🤖 **AI Chatbot**: Natural language interface for stock queries
- 📊 **Technical Analysis**: Moving averages, RSI, MACD, Bollinger Bands
- 📰 **News Sentiment**: Real-time news analysis with sentiment scoring
- 💡 **Smart Recommendations**: ML-powered buy/sell/hold suggestions
- 📈 **Interactive Charts**: Real-time stock price visualizations
- 🎯 **Prediction Tracking**: Monitor accuracy of past recommendations
- 🌐 **Web Dashboard**: Beautiful, responsive interface

## 🏗️ Architecture

```
┌─────────────────┐
│   Web Frontend  │ (Flask + Chart.js)
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│  API Gateway    │ (AWS)
└────────┬────────┘
         │
         ▼
┌─────────────────────────────────────┐
│        Lambda Functions             │
│  ┌─────────┬──────────┬──────────┐ │
│  │Chatbot  │Analysis  │ Tracking │ │
│  └─────────┴──────────┴──────────┘ │
└─────────────────────────────────────┘
         │
         ▼
┌─────────────────┐       ┌──────────────┐
│    DynamoDB     │       │  External    │
│  (Predictions)  │       │  APIs        │
└─────────────────┘       │ • yfinance   │
                          │ • marketaux  │
                          └──────────────┘
```

## 🚀 Quick Start

### Prerequisites

- Python 3.9+
- AWS CLI configured
- AWS Account with permissions for Lambda, DynamoDB, API Gateway
- API Keys (free):
  - [Marketaux](https://www.marketaux.com/) for financial news

### Installation

1. **Clone the repository**
```bash
git clone https://github.com/yourusername/stock-trading-chatbot.git
cd stock-trading-chatbot
```

2. **Install dependencies**
```bash
pip install -r requirements.txt
```

3. **Configure environment**
```bash
cp .env.example .env
# Edit .env with your API keys
nano .env
```

Required environment variables:
```bash
MARKETAUX_API_KEY=your_marketaux_api_key
AWS_REGION=us-east-2
DYNAMODB_TABLE_NAME=stock-chatbot-predictions
SECRET_KEY=your_secret_key
```

4. **Set up AWS infrastructure**

```bash
# Create DynamoDB table
python scripts/setup_dynamodb.py

# Deploy Lambda functions
./scripts/deploy_lambda.sh

# Set up API Gateway
./scripts/deploy_api_gateway.sh
```

5. **Run the web application locally**
```bash
cd web
python app.py
```

Visit [http://localhost:5000](http://localhost:5000)

## 📦 Project Structure

```
stock-trading-chatbot/
├── shared/                          # Shared modules
│   ├── stock_data.py               # Stock data fetching (yfinance)
│   ├── technical_analysis.py      # Technical indicators
│   ├── news_fetcher.py             # News API integration
│   ├── sentiment_analyzer.py      # Sentiment analysis (VADER)
│   └── recommendation_engine.py   # AI recommendation system
│
├── lambda/                         # AWS Lambda functions
│   ├── chatbot/handler.py         # Chatbot query handler
│   ├── stock_analysis/handler.py  # Stock analysis handler
│   ├── news_analysis/handler.py   # News sentiment handler
│   └── tracking/handler.py        # Prediction tracking handler
│
├── web/                           # Flask web application
│   ├── app.py                     # Flask server
│   ├── templates/                 # HTML templates
│   │   └── index.html
│   └── static/                    # CSS, JS, assets
│       ├── css/style.css
│       └── js/app.js
│
├── scripts/                       # Deployment scripts
│   ├── setup_dynamodb.py         # DynamoDB setup
│   ├── deploy_lambda.sh          # Lambda deployment
│   └── deploy_api_gateway.sh     # API Gateway setup
│
├── tests/                        # Unit tests
├── requirements.txt              # Python dependencies
├── .env.example                 # Example environment config
└── README.md                    # This file
```

## 🎯 Supported Stocks

Currently supports 10 major stocks:
- **AAPL** - Apple
- **TSLA** - Tesla
- **MSFT** - Microsoft
- **GOOGL** - Google
- **AMZN** - Amazon
- **NVDA** - NVIDIA
- **META** - Meta
- **NFLX** - Netflix
- **AMD** - AMD
- **INTC** - Intel

## 💬 Example Queries

Try asking the chatbot:
- "Should I invest in AAPL?"
- "What's the trend for TSLA?"
- "Give me your top 3 stock recommendations"
- "Analyze MSFT for me"
- "Is NVDA a good buy right now?"

## 🔧 API Endpoints

### Chatbot Query
```bash
POST /api/chatbot
{
  "query": "Should I invest in AAPL?"
}
```

### Stock Analysis
```bash
GET /api/stock/{symbol}
```

### News & Sentiment
```bash
GET /api/news/{symbol}?limit=10
```

### Predictions
```bash
GET /api/predictions?symbol=AAPL&limit=20
POST /api/predictions
{
  "symbol": "AAPL",
  "prediction": "BUY",
  "confidence": 0.85,
  "price_at_prediction": 175.50,
  "rationale": "Strong technical indicators..."
}
```

### Accuracy Stats
```bash
GET /api/accuracy?symbol=AAPL
```

## 📊 Technical Indicators

The system analyzes multiple technical indicators:

- **Moving Averages**: SMA (20, 50, 200 day), EMA
- **RSI** (Relative Strength Index): Overbought/oversold detection
- **MACD** (Moving Average Convergence Divergence): Trend momentum
- **Bollinger Bands**: Volatility and price levels
- **Volume Analysis**: Trading volume patterns
- **Support/Resistance**: Key price levels
- **Golden/Death Cross**: Major trend signals

## 🧪 Testing

```bash
# Run unit tests
pytest tests/

# Test individual modules
python shared/stock_data.py
python shared/technical_analysis.py
python shared/recommendation_engine.py

# Test Lambda functions locally
python lambda/chatbot/handler.py
```

## 🚢 Deployment

### Deploy to AWS Lambda

```bash
# Set environment variables
export MARKETAUX_API_KEY="your_api_key"
export AWS_REGION="us-east-2"

# Deploy everything
./scripts/deploy_lambda.sh
./scripts/deploy_api_gateway.sh
```

### Deploy Web App

**Option 1: AWS Elastic Beanstalk**
```bash
eb init -p python-3.9 stock-chatbot-web
eb create stock-chatbot-prod
eb deploy
```

**Option 2: AWS EC2**
```bash
# On EC2 instance
git clone https://github.com/yourusername/stock-trading-chatbot.git
cd stock-trading-chatbot
pip install -r requirements.txt
cd web
gunicorn -w 4 -b 0.0.0.0:8000 app:app
```

**Option 3: Docker**
```bash
docker build -t stock-chatbot .
docker run -p 5000:5000 --env-file .env stock-chatbot
```

## 📈 Performance

- **API Response Time**: < 2 seconds average
- **Stock Data**: Real-time via yfinance (free, unlimited)
- **News Updates**: 100 API calls/day (marketaux free tier)
- **Lambda Cold Start**: ~3-5 seconds
- **Lambda Warm**: ~500ms

## 🔒 Security

- API keys stored in environment variables
- AWS IAM roles with least privilege
- CORS enabled for web security
- No sensitive data in logs
- DynamoDB encryption at rest

## 🐛 Troubleshooting

**Issue: "No module named 'yfinance'"**
```bash
pip install -r requirements.txt
```

**Issue: "Unable to locate credentials"**
```bash
aws configure
# Enter your AWS Access Key ID and Secret Access Key
```

**Issue: "API key not configured"**
```bash
# Check .env file
cat .env
# Ensure MARKETAUX_API_KEY is set
```

**Issue: Lambda timeout**
```bash
# Increase timeout in Lambda console or deployment script
# Default is 30 seconds, increase to 60 if needed
```

## 🗺️ Roadmap

- [ ] Add more stocks (expand to S&P 500)
- [ ] Real-time WebSocket updates
- [ ] Portfolio tracking
- [ ] Backtesting engine
- [ ] Email/SMS alerts
- [ ] Mobile app
- [ ] Advanced ML models (LSTM, Transformer)
- [ ] Social media sentiment (Twitter, Reddit)
- [ ] Multi-timeframe analysis
- [ ] Options trading recommendations

## ⚠️ Disclaimer

**IMPORTANT**: This application is for educational and informational purposes only. It does NOT provide financial, investment, or trading advice.

- This is NOT professional financial advice
- Always consult a licensed financial advisor before making investment decisions
- Past performance does not guarantee future results
- Stock trading involves substantial risk of loss
- The creators are not responsible for any financial losses

## 📝 License

MIT License - see [LICENSE](LICENSE) file for details

## 🤝 Contributing

Contributions welcome! Please:

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/amazing-feature`)
3. Commit changes (`git commit -m 'Add amazing feature'`)
4. Push to branch (`git push origin feature/amazing-feature`)
5. Open a Pull Request

## 📧 Contact

Questions? Issues? Feedback?

- GitHub Issues: [Create an issue](https://github.com/yourusername/stock-trading-chatbot/issues)
- Email: your.email@example.com

## 🙏 Acknowledgments

- [yfinance](https://github.com/ranaroussi/yfinance) - Stock data
- [marketaux](https://www.marketaux.com/) - Financial news API
- [VADER Sentiment](https://github.com/cjhutto/vaderSentiment) - Sentiment analysis
- [Chart.js](https://www.chartjs.org/) - Data visualization
- [Flask](https://flask.palletsprojects.com/) - Web framework
- [AWS](https://aws.amazon.com/) - Cloud infrastructure

## ⭐ Star History

If you find this project useful, please consider giving it a star!

---

**Built with ❤️ for the developer and trading community**

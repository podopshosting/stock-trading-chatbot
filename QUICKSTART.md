# 🚀 Quick Start Guide - Stock Trading Chatbot

Get your AI stock trading assistant up and running in minutes!

## 📋 Prerequisites Checklist

- [ ] Python 3.9+ installed
- [ ] AWS CLI installed and configured
- [ ] Git installed
- [ ] AWS Account with admin access
- [ ] Marketaux API key (free from [marketaux.com](https://www.marketaux.com/))

## ⚡ 5-Minute Setup

### 1. Clone and Install (2 minutes)

```bash
# Clone the repository
git clone https://github.com/podopshosting/stock-trading-chatbot.git
cd stock-trading-chatbot

# Install dependencies
pip install -r requirements.txt
```

### 2. Get API Keys (1 minute)

**Marketaux (Free)**
1. Visit [https://www.marketaux.com/](https://www.marketaux.com/)
2. Sign up for free account
3. Copy your API key

### 3. Configure Environment (1 minute)

```bash
# Copy example environment file
cp .env.example .env

# Edit with your favorite editor
nano .env
```

Add your API key:
```bash
MARKETAUX_API_KEY=your_api_key_here
AWS_REGION=us-east-2
DYNAMODB_TABLE_NAME=stock-chatbot-predictions
SECRET_KEY=change_this_to_random_string
```

### 4. Test Locally (1 minute)

```bash
# Run the web app
cd web
python app.py
```

Open [http://localhost:5000](http://localhost:5000) in your browser!

## 🎯 Try These First

Once the app is running, try these queries in the chatbot:

1. **"Should I invest in AAPL?"** - Get a recommendation for Apple stock
2. **"What's the trend for TSLA?"** - See Tesla's market trend
3. **"Give me your top 3 recommendations"** - Get top stock picks
4. **"Analyze MSFT"** - Deep dive into Microsoft

## 🌩️ Deploy to AWS (Optional)

### Prerequisites for AWS Deployment

```bash
# Configure AWS CLI
aws configure
# Enter your AWS Access Key ID and Secret Access Key
```

### Deploy Step-by-Step

```bash
# 1. Create DynamoDB table
python scripts/setup_dynamodb.py

# 2. Deploy Lambda functions (takes 5-10 minutes)
./scripts/deploy_lambda.sh

# 3. Set up API Gateway (takes 2-3 minutes)
./scripts/deploy_api_gateway.sh
```

After deployment, you'll get an API URL like:
```
https://xyz123.execute-api.us-east-2.amazonaws.com/prod
```

### Update Web App to Use AWS Backend

Edit `web/static/js/app.js`:
```javascript
// Change this line:
const API_BASE = '';

// To this:
const API_BASE = 'https://your-api-id.execute-api.us-east-2.amazonaws.com/prod';
```

## 🔧 Troubleshooting

### Python Errors

**"No module named 'yfinance'"**
```bash
pip install -r requirements.txt --upgrade
```

### AWS Errors

**"Unable to locate credentials"**
```bash
aws configure
# Enter your credentials
```

**"AccessDenied" errors**
- Ensure your AWS user has Lambda, DynamoDB, and API Gateway permissions
- Or use an admin user for initial setup

### API Errors

**"News not loading"**
- Check your MARKETAUX_API_KEY in .env
- Verify the key is active at marketaux.com
- Free tier has 100 calls/day limit

**"Stock data not loading"**
- yfinance is free and unlimited
- Check your internet connection
- Try a different stock symbol

## 📱 Using the Web Interface

### Dashboard Sections

1. **Chatbot** - Ask natural language questions about stocks
2. **Watchlist** - Monitor 10 major tech stocks in real-time
3. **Charts** - Interactive stock price charts
4. **Predictions** - Track AI recommendations over time
5. **News** - Latest financial news with sentiment analysis
6. **Accuracy** - See how well the AI performs

### Navigation Tips

- Click any stock card to see detailed analysis
- Use the tabs to switch between different views
- Charts update automatically every minute
- Press Enter in chat to send messages

## 🎓 Understanding the Results

### Recommendation Actions

- **BUY** - Strong positive signals, good time to consider buying
- **HOLD** - Mixed signals, best to wait and monitor
- **SELL** - Negative signals, consider selling if you own it

### Confidence Scores

- **80-100%** - Very confident prediction
- **60-80%** - Moderately confident
- **40-60%** - Low confidence, proceed with caution
- **Below 40%** - Not enough data or conflicting signals

### Technical Indicators

- **RSI < 30** - Oversold (potential buy)
- **RSI > 70** - Overbought (potential sell)
- **Golden Cross** - Strong bullish signal (50-day MA crosses above 200-day MA)
- **Death Cross** - Strong bearish signal (50-day MA crosses below 200-day MA)

### Sentiment Analysis

- **Positive** - Good news coverage
- **Neutral** - Mixed or unclear news
- **Negative** - Bad news coverage

## 🧪 Testing the System

### Test Individual Components

```bash
# Test stock data fetching
python shared/stock_data.py

# Test technical analysis
python shared/technical_analysis.py

# Test recommendation engine
python shared/recommendation_engine.py
```

### Run Unit Tests

```bash
pytest tests/ -v
```

## 📊 Monitoring and Maintenance

### Check AWS Costs

```bash
# View Lambda invocations
aws cloudwatch get-metric-statistics \
    --namespace AWS/Lambda \
    --metric-name Invocations \
    --dimensions Name=FunctionName,Value=stock-chatbot-query \
    --start-time 2025-01-01T00:00:00Z \
    --end-time 2025-01-02T00:00:00Z \
    --period 3600 \
    --statistics Sum

# View DynamoDB usage
aws dynamodb describe-table --table-name stock-chatbot-predictions
```

### Expected AWS Costs (Free Tier)

- **Lambda**: First 1M requests/month free
- **DynamoDB**: 25GB storage + 200M requests/month free
- **API Gateway**: First 1M calls/month free

For MVP usage: **$0-5/month**

## 🔐 Security Best Practices

1. **Never commit .env file** - It's in .gitignore by default
2. **Rotate API keys regularly** - Update in .env and redeploy
3. **Use IAM roles** - Don't hardcode AWS credentials
4. **Enable CloudWatch logs** - Monitor for suspicious activity
5. **Set up billing alerts** - Get notified if costs spike

## 🆘 Getting Help

### Common Issues

**Q: Why are predictions not saving?**
A: Ensure DynamoDB table is created: `python scripts/setup_dynamodb.py`

**Q: Chatbot responses are slow**
A: First Lambda cold start takes 3-5 seconds, then speeds up

**Q: Can I add more stocks?**
A: Yes! Edit `SUPPORTED_STOCKS` in `shared/stock_data.py` and `web/app.py`

**Q: How accurate are the predictions?**
A: Check the Accuracy tab. Typical range: 55-70% for short-term predictions

### Need More Help?

- 📖 Read the full [README.md](README.md)
- 🐛 Report issues on [GitHub Issues](https://github.com/podopshosting/stock-trading-chatbot/issues)
- 💬 Check existing issues for solutions

## 🎉 Next Steps

Once you have the basic system running:

1. **Explore the code** - Understand how each component works
2. **Customize** - Add your favorite stocks
3. **Experiment** - Try different technical indicators
4. **Extend** - Add new features like email alerts
5. **Share** - Show your friends and get feedback!

## ⚠️ Important Reminders

- This is **NOT** financial advice
- Always do your own research
- Never invest more than you can afford to lose
- Past performance ≠ future results
- Use this tool for **learning and research** only

---

**Happy Trading! 📈**

Questions? Open an issue on GitHub!

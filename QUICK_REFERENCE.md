> **⚠️ SUPERSEDED — historical record.**
> This document does not describe what is deployed. See
> [PRODUCTION-STATE.md](PRODUCTION-STATE.md) for the current architecture,
> deployment procedure and known constraints.

# 📋 Stock Trading Chatbot - Quick Reference

## 🌐 Live URLs

**Web App:** http://stock-chatbot-web.s3-website.us-east-2.amazonaws.com

**API Endpoint:** https://lmi4hshs7h.execute-api.us-east-2.amazonaws.com/prod/chatbot

---

## 🔑 API Keys (AWS Secrets Manager)

| Secret Name | Value | Purpose |
|------------|-------|---------|
| `stock-chatbot/openai-api-key` | `sk-proj-8qLm...` | OpenAI GPT-4o-mini |
| `stock-chatbot/alphavantage-api-key` | `<REDACTED - retrieve from AWS Secrets Manager>` | Alpha Vantage real-time data |

---

## 🧪 Quick Test Commands

### Test via API
```bash
# AAPL
curl -X POST 'https://lmi4hshs7h.execute-api.us-east-2.amazonaws.com/prod/chatbot' \
  -H 'Content-Type: application/json' \
  -d '{"query":"Should I buy AAPL?"}' | jq -r '.response'

# TSLA with full data
curl -X POST 'https://lmi4hshs7h.execute-api.us-east-2.amazonaws.com/prod/chatbot' \
  -H 'Content-Type: application/json' \
  -d '{"query":"What about TSLA?"}' | jq '.'
```

### Test Lambda Directly
```bash
aws lambda invoke \
  --function-name stock-chatbot-router \
  --payload '{"body":"{\"query\":\"Is NVDA a good buy?\"}"}' \
  /tmp/response.json \
  --region us-east-2 && cat /tmp/response.json | jq -r '.body' | jq -r '.response'
```

---

## 📊 Supported Stocks

**10 Major Tech Stocks:**
- AAPL (Apple)
- TSLA (Tesla)
- MSFT (Microsoft)
- GOOGL (Google)
- AMZN (Amazon)
- NVDA (NVIDIA)
- META (Meta/Facebook)
- NFLX (Netflix)
- AMD (AMD)
- INTC (Intel)

---

## 🚀 Deployment Commands

### Deploy Lambda Function
```bash
cd lambda-micro/chatbot-router
pip3 install requests -t package/
cd package && zip -r ../deployments/chatbot.zip .
cd .. && zip -g deployments/chatbot.zip handler.py
aws lambda update-function-code \
  --function-name stock-chatbot-router \
  --zip-file fileb://deployments/chatbot.zip \
  --region us-east-2
```

### Deploy Web App
```bash
cd web/static
aws s3 cp index.html s3://stock-chatbot-web/index.html \
  --content-type "text/html" \
  --region us-east-2
```

---

## 📈 Response Format

### JSON Structure
```json
{
  "response": "📊 AAPL - $258.02\n\n📈 Today's Performance...",
  "symbol": "AAPL",
  "query": "Should I buy AAPL?",
  "data": {
    "price": 258.02,
    "change": 0.89,
    "change_percent": "0.3461%",
    "volume": 49155614,
    "trend": "bearish",
    "rsi": 71.05,
    "recommendation": "sell"
  }
}
```

---

## 💰 Cost Per Query

| Component | Cost |
|-----------|------|
| OpenAI GPT-4o-mini | $0.00015 |
| Lambda | ~$0.0000002 |
| API Gateway | $0.0000035 |
| Alpha Vantage | $0 (free tier) |
| **Total** | **~$0.00015** |

---

## 🔧 Monitoring

### CloudWatch Logs
```bash
aws logs tail /aws/lambda/stock-chatbot-router --follow --region us-east-2
```

### Lambda Configuration
```bash
aws lambda get-function-configuration \
  --function-name stock-chatbot-router \
  --region us-east-2
```

### API Gateway Status
```bash
aws apigateway get-rest-api \
  --rest-api-id lmi4hshs7h \
  --region us-east-2
```

---

## 📚 Documentation Files

| File | Description |
|------|-------------|
| `README.md` | Project overview |
| `DEPLOYMENT.md` | Complete deployment guide |
| `WEB_DEPLOYMENT.md` | Web frontend deployment |
| `ALPHAVANTAGE_INTEGRATION.md` | Alpha Vantage API guide |
| `ALPHA_VANTAGE_ENHANCEMENT_SUMMARY.md` | Enhancement summary |
| `DEPLOYMENT_COMPLETE.md` | Full deployment status |
| `QUICK_REFERENCE.md` | This file |

---

## 🎯 Key Features

✅ Real-time stock prices (Alpha Vantage)
✅ RSI technical indicator
✅ Buy/sell/hold recommendations
✅ AI-powered analysis (GPT-4o-mini)
✅ Trend detection (bullish/bearish/neutral)
✅ Volume tracking
✅ Web interface
✅ REST API

---

## 🔐 AWS Resources

| Type | Name/ID | Region |
|------|---------|--------|
| Lambda | stock-chatbot-router | us-east-2 |
| Lambda | stock-data-service | us-east-2 |
| Lambda | stock-news-service | us-east-2 |
| Lambda | stock-prediction-service | us-east-2 |
| API Gateway | lmi4hshs7h | us-east-2 |
| S3 Bucket | stock-chatbot-web | us-east-2 |
| DynamoDB | stock-chatbot-predictions | us-east-2 |
| IAM Role | stock-chatbot-lambda-role | us-east-2 |

---

## 🧪 Example Queries

- "Should I buy AAPL?"
- "What's the trend for TSLA?"
- "Is NVDA overbought?"
- "Should I invest in MSFT today?"
- "What about AMZN?"
- "Is GOOGL a good investment?"
- "Tell me about META"

---

## 📞 Quick Links

- **GitHub:** https://github.com/podopshosting/stock-trading-chatbot
- **Web App:** http://stock-chatbot-web.s3-website.us-east-2.amazonaws.com
- **Alpha Vantage:** https://www.alphavantage.co/documentation/
- **OpenAI:** https://platform.openai.com/

---

## ⚡ Troubleshooting

### Issue: API returns error
```bash
# Check Lambda logs
aws logs tail /aws/lambda/stock-chatbot-router --follow
```

### Issue: Web app not loading
```bash
# Verify S3 bucket
aws s3 ls s3://stock-chatbot-web/
```

### Issue: CORS error
```bash
# Test OPTIONS request
curl -X OPTIONS https://lmi4hshs7h.execute-api.us-east-2.amazonaws.com/prod/chatbot \
  -H "Origin: http://localhost" \
  -H "Access-Control-Request-Method: POST" -i
```

---

**Last Updated:** October 6, 2025
**Version:** 2.0 (Alpha Vantage Enhanced)

# OpenAI Integration Guide

The Stock Trading Chatbot now supports OpenAI's GPT-4 for enhanced, conversational responses.

## 🎯 Benefits

- **Natural Language Responses**: GPT-4 generates human-like, conversational answers
- **Context-Aware**: Understands nuances in user queries
- **Better Explanations**: Clearer explanations of technical concepts
- **Consistent Tone**: Professional yet accessible language
- **Fallback Support**: Automatically falls back to basic responses if OpenAI unavailable

## 🔐 Security Setup

### Step 1: Store API Key in AWS Secrets Manager

The OpenAI API key is already stored securely:

```bash
Secret Name: stock-chatbot/openai-api-key
Secret ARN: arn:aws:secretsmanager:us-east-2:899383035514:secret:stock-chatbot/openai-api-key-MaHqDO
Region: us-east-2
```

### Step 2: Grant Lambda Access

Lambda functions automatically retrieve the key from Secrets Manager. The deployment script adds the necessary IAM permissions:

```bash
./scripts/deploy_lambda.sh
```

This grants the Lambda execution role access to:
- ✅ AWS Secrets Manager (read access)
- ✅ DynamoDB
- ✅ CloudWatch Logs

## 📊 How It Works

### 1. User Query Processing

When a user asks a question:

```
User: "Should I invest in AAPL?"
```

### 2. Technical Analysis

The system performs:
- Stock data fetching (yfinance)
- Technical analysis (RSI, MACD, etc.)
- News sentiment analysis

### 3. OpenAI Enhancement

The analysis is sent to GPT-4 with this prompt structure:

```
You are a professional stock trading advisor.

Stock: AAPL
Current Price: $175.50

Technical Analysis:
- Recommendation: BUY
- Confidence: 85%
- RSI: 45.2
- Trend: bullish

Sentiment Analysis:
- News Sentiment: positive
- Article Count: 10

User Question: Should I invest in AAPL?

Provide a conversational, professional response...
```

### 4. Natural Response

GPT-4 generates:

```
Based on the current analysis, AAPL looks promising for investment.
The stock is showing bullish momentum with a healthy RSI of 45.2,
indicating it's neither overbought nor oversold. Technical indicators
suggest a BUY signal with 85% confidence.

Additionally, news sentiment is positive with recent coverage
highlighting strong fundamentals. The current price of $175.50
appears to be a good entry point.

However, remember this is not financial advice - always consult
with a licensed financial advisor before making investment decisions.
```

## 🔧 Configuration

### Environment Variables

```bash
# .env file
OPENAI_API_KEY=sk-proj-... # Optional if using Secrets Manager
AWS_REGION=us-east-2
```

### Lambda Environment

The Lambda function automatically:
1. Checks for `OPENAI_API_KEY` environment variable
2. Falls back to Secrets Manager if not found
3. Uses basic responses if neither available

### Enabling/Disabling OpenAI

**Via API Request:**
```json
{
  "query": "Should I invest in AAPL?",
  "use_openai": true  // Set to false to disable
}
```

**Default Behavior:**
- OpenAI is enabled by default for all stock-specific queries
- General queries use the standard recommendation engine

## 💰 Costs

### OpenAI Pricing (GPT-4)

- **Input**: $0.03 per 1K tokens
- **Output**: $0.06 per 1K tokens

### Typical Usage

Average response:
- Input: ~400 tokens ($0.012)
- Output: ~150 tokens ($0.009)
- **Cost per query: ~$0.021**

### Monthly Estimates

| Users/Day | Queries/Day | Monthly Cost |
|-----------|-------------|--------------|
| 10        | 50          | $31.50       |
| 50        | 250         | $157.50      |
| 100       | 500         | $315.00      |

### AWS Secrets Manager

- $0.40 per secret per month
- $0.05 per 10,000 API calls
- **Typical: ~$0.50/month**

## 🧪 Testing

### Test Locally

```bash
cd shared
python openai_helper.py
```

### Test Lambda Function

```bash
cd lambda/chatbot
python handler.py
```

### Test via API

```bash
curl -X POST https://your-api-id.execute-api.us-east-2.amazonaws.com/prod/chatbot \
  -H "Content-Type: application/json" \
  -d '{
    "query": "Should I invest in AAPL?",
    "use_openai": true
  }'
```

## 📈 Performance

### Response Times

- **Without OpenAI**: 1-2 seconds
- **With OpenAI**: 3-5 seconds (includes GPT-4 API call)
- **With OpenAI (cached)**: 2-3 seconds

### Optimization Tips

1. **Use GPT-3.5-turbo** for faster/cheaper responses:
   ```python
   model="gpt-3.5-turbo"  # Instead of gpt-4
   ```

2. **Reduce max_tokens** to limit response length:
   ```python
   max_tokens=150  # Instead of 300
   ```

3. **Enable caching** for common queries (future enhancement)

## 🔍 Monitoring

### CloudWatch Logs

View OpenAI usage in Lambda logs:

```bash
aws logs tail /aws/lambda/stock-chatbot-query --follow
```

Look for:
- ✅ "API key retrieved successfully"
- ⚠️ "Using fallback response"
- ❌ "Error using OpenAI"

### Cost Monitoring

```bash
# View OpenAI API usage at platform.openai.com/usage
```

## 🚨 Error Handling

The system gracefully handles:

1. **No API Key**: Falls back to basic responses
2. **API Errors**: Retries once, then uses fallback
3. **Rate Limits**: Returns cached response or basic format
4. **Timeout**: Uses pre-computed analysis

## 🔒 Security Best Practices

### ✅ DO

- Store API key in AWS Secrets Manager
- Use IAM roles for Lambda access
- Enable CloudWatch logging for auditing
- Rotate API keys regularly
- Set spending limits in OpenAI dashboard

### ❌ DON'T

- Hard-code API keys in code
- Commit API keys to git
- Share API keys in plain text
- Use root AWS credentials
- Skip encryption at rest

## 📚 Advanced Configuration

### Custom GPT Prompts

Edit `shared/openai_helper.py` to customize the system prompt:

```python
"role": "system",
"content": "You are a [customize this]..."
```

### Model Selection

Switch between models:

```python
# Fast & cheap
model="gpt-3.5-turbo"

# Balanced
model="gpt-4"

# Most capable
model="gpt-4-turbo"
```

### Temperature Tuning

Adjust creativity vs consistency:

```python
temperature=0.7  # Default (balanced)
temperature=0.3  # More consistent
temperature=1.0  # More creative
```

## 🛠️ Troubleshooting

### "OpenAI library not available"

```bash
pip install openai==1.12.0
```

### "Could not retrieve API key"

Check:
1. Secret exists: `aws secretsmanager get-secret-value --secret-id stock-chatbot/openai-api-key`
2. Lambda has permissions: Check IAM role
3. Region matches: `us-east-2`

### "Rate limit exceeded"

Solutions:
1. Upgrade OpenAI plan
2. Implement request throttling
3. Enable caching
4. Use GPT-3.5-turbo

### High costs

Monitor:
1. Check OpenAI usage dashboard
2. Review CloudWatch metrics
3. Set up billing alerts
4. Consider caching frequently asked questions

## 📊 Comparison

### With OpenAI
**Pros:**
- Natural, conversational responses
- Better context understanding
- Professional tone
- Flexible explanations

**Cons:**
- Additional cost (~$0.02/query)
- Slower response time (+2-3s)
- External dependency
- Requires API key management

### Without OpenAI
**Pros:**
- Free
- Fast (1-2s)
- No external dependencies
- Predictable

**Cons:**
- Template-based responses
- Less natural language
- Limited flexibility

## 🎓 Best Practices

1. **Use for specific queries**: Enable OpenAI for stock analysis, disable for simple lookups
2. **Monitor costs**: Set up billing alerts
3. **Cache common responses**: Reduce API calls
4. **Implement fallbacks**: Always have a backup response
5. **Log everything**: Track usage and errors
6. **Test thoroughly**: Validate responses before production
7. **Rotate keys**: Update API keys every 90 days

## 📞 Support

- OpenAI Documentation: https://platform.openai.com/docs
- OpenAI Support: https://help.openai.com
- Project Issues: https://github.com/podopshosting/stock-trading-chatbot/issues

---

**Last Updated**: October 6, 2025
**Version**: 1.1.0

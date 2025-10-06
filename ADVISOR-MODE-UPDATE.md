# Stock Trading Chatbot - Investment Advisor Mode

## Overview
Transformed the chatbot from a technical analyst into a professional investment advisor that provides personalized, actionable guidance.

## Key Changes

### 1. Enhanced System Prompt - Advisory Focus
**Location**: `lambda-micro/chatbot-router/handler.py:174-196`

**Before**: "You are a knowledgeable stock trading advisor..."
**After**: "You are a professional investment advisor providing personalized, data-driven guidance..."

#### New Advisory Approach
- Acts as a trusted financial advisor, not just an analyst
- Provides actionable recommendations tailored to different investor profiles
- Considers risk tolerance, investment horizon, and portfolio diversification
- Explains the "why" behind recommendations, not just the "what"
- Offers strategic guidance on entry/exit points, position sizing, and portfolio allocation

#### Added Penny Stock Expertise
```
PENNY STOCK EXPERTISE:
- Penny stocks trade under $5, often on OTC markets with high volatility and risk
- Look for: Real business model, revenue growth, low debt, insider ownership, catalysts
- Red flags: Dilution, pump-and-dump schemes, no revenue, frequent reverse splits
- Risk management: Never invest more than 5-10% of portfolio, use stop-losses, expect 50%+ volatility
```

### 2. Expanded Investment Strategies
Added to existing knowledge base:
- **Momentum trading**: Ride trends for short-term gains
- **Penny stocks**: High-risk, high-reward plays under $5
- **Position sizing**: Portfolio allocation recommendations
- **Risk management**: Stop-loss strategies and diversification

### 3. Increased Response Length
- **max_tokens**: 300 → 400
- **Word limit**: 150 → 200 words
- Allows for more comprehensive advisory responses

### 4. Enhanced Symbol Blacklist
Added 14 more common words to prevent false extractions:
- PENNY, STOCKS, WATCH, YEAR, YEARS, UNDER, OVER
- SAID, SAYS, PRICE, SHARE, TRADING, BOUGHT, SOLD
- HAVE, DOING, DONE, BEEN, BEING, COME, CAME, GOING, GONE

**Total blacklist**: 140+ words

## Testing Results

### ✅ Penny Stock Advisory
```
Query: "what are some penny stocks to watch?"

Response (Summary):
✓ Provided 3 category recommendations (Healthcare, Tech, Renewable Energy)
✓ Named specific stocks: AGEN (biotech), SIRI (tech), SUNW (renewable)
✓ Actionable tips: 5-10% portfolio allocation, stop-loss orders, monitor catalysts
✓ Risk warnings about volatility and potential losses
✓ Proper disclaimer
```

### ✅ Stock-Specific Advisory (NVDA)
```
Query: "Should I buy NVDA right now?"

Response:
✓ Real-time price: $187.62
✓ ML recommendation: BUY (70% confidence, LOW risk)
✓ Technical analysis: RSI 62.2 (nearing overbought), bullish MA trend
✓ Advisory guidance: "Consider entering a position if comfortable with short-term fluctuations"
✓ Portfolio risk assessment reminder
```

### ✅ Portfolio Allocation Advisory
```
Query: "I have $10,000 to invest, what should I do?"

Response (Summary):
✓ Diversified 4-part strategy:
  - 60% ($6,000): S&P 500 ETF (SPY) - core holdings
  - 20% ($2,000): Growth tech stocks (AAPL, MSFT)
  - 10% ($1,000): Dividend stocks (JNJ, KO)
  - 10% ($1,000): Penny stocks with strong fundamentals
✓ Entry strategy: Gradual 1-2 week deployment
✓ Risk-appropriate allocation
✓ Personalized disclaimer
```

## Comparison: Analyst vs. Advisor

### Before (Analyst Mode)
```
Query: "what are some penny stocks to watch?"
Response: "Penny stocks are high-risk investments trading under $5..."
- Focused on definitions and concepts
- Limited specific recommendations
- General risk warnings
- No actionable portfolio guidance
```

### After (Advisor Mode)
```
Query: "what are some penny stocks to watch?"
Response:
- Specific stock recommendations (AGEN, SIRI, SUNW)
- Category-based approach (Healthcare, Tech, Renewable)
- Portfolio allocation guidance (5-10% max)
- Risk management strategies (stop-losses, monitoring)
- Catalyst identification (FDA approvals, partnerships)
- Actionable next steps
```

## Advisory Features

### 🎯 Personalized Recommendations
- Considers investor risk profile
- Tailored to investment amount
- Portfolio-level guidance
- Entry/exit timing strategies

### 📊 Strategic Guidance
- Position sizing recommendations
- Diversification strategies
- Risk management tactics
- Catalyst monitoring

### 💡 Actionable Insights
- Specific stock suggestions with tickers
- Percentage allocations ($6,000 to S&P 500, etc.)
- Time-based deployment strategies
- Stop-loss and exit point guidance

### 🛡️ Risk Management
- Portfolio allocation limits (e.g., max 10% penny stocks)
- Volatility expectations
- Red flag identification
- Risk-appropriate disclaimers

## Deployment

**Function**: `stock-chatbot-router`
**Last Updated**: 2025-10-06T18:13:22.000+0000
**Status**: ✅ Active

## Example Queries

### Portfolio Allocation
- "I have $10,000 to invest, what should I do?"
- "How should I diversify my portfolio?"
- "What percentage should I allocate to growth stocks?"

### Penny Stocks
- "what are some penny stocks to watch?"
- "Should I invest in penny stocks?"
- "What are the risks of penny stock trading?"

### Stock-Specific Advice
- "Should I buy NVDA right now?"
- "Is TSLA a good long-term investment?"
- "What do you think about AAPL at current prices?"

### Investment Strategies
- "Explain Warren Buffett's strategy"
- "What is value investing?"
- "How do I identify growth stocks?"

## Benefits

1. **More Actionable**: Specific recommendations instead of general analysis
2. **Personalized**: Considers user's capital, risk tolerance, and goals
3. **Educational**: Explains the "why" behind recommendations
4. **Comprehensive**: Covers stocks, ETFs, portfolio allocation, risk management
5. **Professional**: Advisory tone builds trust and credibility

## Technical Details

**AI Model**: GPT-4o-mini
**Max Tokens**: 400 (increased from 300)
**Temperature**: 0.7
**Response Format**: Structured with actionable tips, portfolio breakdowns, and disclaimers

---

**Status**: ✅ Deployed and Tested
**Mode**: Investment Advisor
**Version**: v7-advisor-mode
**Date**: October 6, 2025

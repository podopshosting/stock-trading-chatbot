#!/bin/bash

# Test Enhanced Stock Trading Chatbot with Investment Knowledge

API_URL="https://lmi4hshs7h.execute-api.us-east-2.amazonaws.com/prod/chatbot"

echo "=========================================="
echo "Testing Enhanced Stock Trading Chatbot"
echo "=========================================="
echo ""

# Test 1: Stock query with ML analysis
echo "Test 1: Stock Query - AAPL"
echo "------------------------------------------"
curl -X POST "$API_URL" \
  -H 'Content-Type: application/json' \
  -d '{"query":"What do you think about AAPL?"}' \
  -s | python3 -c "import sys, json; data=json.load(sys.stdin); print(f\"Symbol: {data.get('symbol', 'N/A')}\"); print(f\"Response: {data.get('response', 'N/A')[:200]}...\")"
echo ""
echo ""

# Test 2: General investment question (no stock symbol)
echo "Test 2: Investment Knowledge - Warren Buffett"
echo "------------------------------------------"
curl -X POST "$API_URL" \
  -H 'Content-Type: application/json' \
  -d '{"query":"explain Warren Buffett investment strategy"}' \
  -s | python3 -c "import sys, json; data=json.load(sys.stdin); print(f\"Response: {data.get('response', 'N/A')[:300]}...\")"
echo ""
echo ""

# Test 3: Diversification knowledge
echo "Test 3: Investment Knowledge - Diversification"
echo "------------------------------------------"
curl -X POST "$API_URL" \
  -H 'Content-Type: application/json' \
  -d '{"query":"tell me about diversification"}' \
  -s | python3 -c "import sys, json; data=json.load(sys.stdin); print(f\"Response: {data.get('response', 'N/A')[:300]}...\")"
echo ""
echo ""

# Test 4: Multiple stock query
echo "Test 4: Stock Query - MSFT"
echo "------------------------------------------"
curl -X POST "$API_URL" \
  -H 'Content-Type: application/json' \
  -d '{"query":"Should I buy MSFT?"}' \
  -s | python3 -c "import sys, json; data=json.load(sys.stdin); print(f\"Symbol: {data.get('symbol', 'N/A')}\"); print(f\"ML Recommendation: {data.get('data', {}).get('recommendation', 'N/A').upper()}\"); print(f\"Confidence: {data.get('data', {}).get('ml_confidence', 'N/A')}%\"); print(f\"Risk Level: {data.get('data', {}).get('risk_level', 'N/A')}\")"
echo ""
echo ""

# Test 5: Market structure knowledge
echo "Test 5: Investment Knowledge - Market Structure"
echo "------------------------------------------"
curl -X POST "$API_URL" \
  -H 'Content-Type: application/json' \
  -d '{"query":"what are the major US stock exchanges?"}' \
  -s | python3 -c "import sys, json; data=json.load(sys.stdin); print(f\"Response: {data.get('response', 'N/A')[:300]}...\")"
echo ""
echo ""

echo "=========================================="
echo "All Tests Completed!"
echo "=========================================="

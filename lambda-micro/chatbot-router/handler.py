"""
Enhanced chatbot with ML Trading Agent integration
Combines Alpha Vantage data + ML analysis for superior recommendations
"""
import json
import time
import boto3
import requests
import re
from typing import Optional, Dict, List
from ml_agent_lite import get_ml_recommendation

ALPHA_VANTAGE_URL = "https://www.alphavantage.co/query"

# Alpha Vantage free tier allows roughly 1 request per second (and 25/day).
# A single stock query needs several calls, so they must be spaced out or the
# later ones come back throttled instead of carrying data.
_AV_MIN_INTERVAL = 1.2
_AV_MAX_RETRIES = 2
_AV_RETRY_DELAY = 1.5
_av_last_call = 0.0


class AlphaVantageRateLimited(Exception):
    """Raised when Alpha Vantage keeps throttling us.

    Distinct from "no data": a throttle says nothing about whether the
    ticker is valid, so callers must not report it as a bad symbol.
    """


def _is_throttle_response(data: Dict) -> bool:
    """Detect Alpha Vantage's rate-limit replies.

    Throttling arrives as HTTP 200 with an 'Information' or 'Note' key
    instead of the requested payload.
    """
    if not isinstance(data, dict):
        return False
    for key in ('Note', 'Information'):
        message = data.get(key)
        if not message:
            continue
        text = str(message).lower()
        if any(marker in text for marker in (
            'per second', 'sparingly', 'rate limit',
            'requests per day', 'call frequency', 'higher api call',
        )):
            return True
    return False


def _av_request(params: Dict, api_key: str, timeout: int = 15) -> Optional[Dict]:
    """Single entry point for Alpha Vantage calls.

    Spaces requests to respect the free-tier limit and retries throttled
    replies. Returns the parsed payload, None on a transport/parse failure,
    and raises AlphaVantageRateLimited if throttling persists.
    """
    global _av_last_call
    attempt = 0

    while True:
        wait = _AV_MIN_INTERVAL - (time.monotonic() - _av_last_call)
        if wait > 0:
            time.sleep(wait)

        try:
            response = requests.get(
                ALPHA_VANTAGE_URL,
                params={**params, 'apikey': api_key},
                timeout=timeout,
            )
            data = response.json()
        except Exception as e:
            _av_last_call = time.monotonic()
            print(f"Alpha Vantage request failed ({params.get('function')}): {e}")
            return None
        finally:
            _av_last_call = time.monotonic()

        if not _is_throttle_response(data):
            return data

        attempt += 1
        if attempt > _AV_MAX_RETRIES:
            print(
                f"Alpha Vantage rate limit persisted for "
                f"{params.get('function')} after {attempt} attempts"
            )
            raise AlphaVantageRateLimited(params.get('function', 'unknown'))

        print(
            f"Alpha Vantage throttled {params.get('function')}; "
            f"retry {attempt}/{_AV_MAX_RETRIES}"
        )
        time.sleep(_AV_RETRY_DELAY)


DATA_UNAVAILABLE_NOTE = (
    "Market data is temporarily unavailable (data provider rate limit). "
    "This is not a problem with the ticker - please try again in a minute."
)


def get_secret(secret_name: str) -> Optional[str]:
    """Get secret from AWS Secrets Manager"""
    try:
        client = boto3.client('secretsmanager', region_name='us-east-2')
        response = client.get_secret_value(SecretId=secret_name)
        return response['SecretString']
    except Exception as e:
        print(f"Error getting secret {secret_name}: {e}")
        return None


def extract_stock_symbols(query: str) -> List[str]:
    """Extract stock symbols from query"""
    symbols = []
    query_upper = query.upper()

    ticker_pattern = r'\b([A-Z]{2,5})\b'
    matches = re.findall(ticker_pattern, query_upper)

    common_words = {'THE', 'AND', 'FOR', 'ARE', 'BUT', 'NOT', 'YOU', 'ALL',
                   'CAN', 'HER', 'WAS', 'ONE', 'OUR', 'OUT', 'DAY', 'GET',
                   'HAS', 'HIM', 'HIS', 'HOW', 'MAN', 'NEW', 'NOW', 'OLD',
                   'SEE', 'TWO', 'WAY', 'WHO', 'BOY', 'DID', 'ITS', 'LET',
                   'PUT', 'SAY', 'SHE', 'TOO', 'USE', 'WHAT', 'WHEN', 'WHERE',
                   'WHICH', 'WHY', 'WILL', 'WITH', 'GOOD', 'BEST', 'STOCK',
                   'BUY', 'SELL', 'HOLD', 'SHOULD', 'COULD', 'WOULD', 'QUICK',
                   'WATCH', 'TRADE', 'INVEST', 'ABOUT', 'TELL', 'THINK', 'DOING', 'DO',
                   'IT', 'IS', 'AS', 'AT', 'BE', 'BY', 'IF', 'IN', 'OF', 'ON', 'OR', 'TO',
                   'UP', 'SO', 'NO', 'AN', 'MY', 'WE', 'US', 'GO', 'ME', 'AM', 'FROM',
                   'DOES', 'DIFFER', 'DIFFERS', 'DIFFERENCE', 'GROWTH', 'VALUE', 'INVESTING',
                   'KEY', 'KNOW', 'ALSO', 'MADE', 'MAKE', 'LIKE', 'JUST', 'TIME', 'VERY',
                   'THAN', 'FIND', 'GIVE', 'MANY', 'WELL', 'ONLY', 'THOSE', 'THEN', 'THEM',
                   'INTO', 'SOME', 'MAY', 'TAKE', 'THESE', 'WANT', 'LOOK', 'FIRST',
                   'SEC', 'ROLE', 'RULE', 'ACT', 'LAW', 'LAWS', 'NYSE', 'WORKS', 'WORK',
                   'HELP', 'HELPS', 'RISK', 'RISKS', 'MEAN', 'MEANS', 'TIPS', 'IDEAS',
                   'MAJOR', 'MINOR', 'BASIC', 'BOTH', 'MUCH', 'MORE', 'MOST', 'LESS',
                   'EACH', 'OTHER', 'SUCH', 'EVEN', 'SAME', 'LEARN', 'START', 'LONG',
                   'HIGH', 'LOW', 'OPEN', 'CLOSE', 'LAST', 'NEXT', 'BACK', 'DOWN',
                   'PENNY', 'STOCKS', 'SOME', 'WATCH', 'YEAR', 'YEARS', 'UNDER', 'OVER',
                   'SAID', 'SAYS', 'PRICE', 'SHARE', 'TRADING', 'BOUGHT', 'SOLD', 'HAVE',
                   'DOING', 'DONE', 'BEEN', 'BEING', 'COME', 'CAME', 'GOING', 'GONE'}

    for match in matches:
        if match not in common_words and len(match) <= 5:
            symbols.append(match)

    return list(set(symbols))


def get_alpha_vantage_quote(symbol: str, api_key: str) -> Optional[Dict]:
    """Get real-time stock quote.

    Raises AlphaVantageRateLimited if the provider keeps throttling, so the
    caller can tell that apart from an unknown ticker.
    """
    try:
        data = _av_request(
            {'function': 'GLOBAL_QUOTE', 'symbol': symbol}, api_key, timeout=10
        )

        if not data or 'Global Quote' not in data or not data['Global Quote']:
            return None

        quote = data['Global Quote']

        if not quote.get('05. price'):
            return None

        return {
            'symbol': quote.get('01. symbol', symbol),
            'price': float(quote.get('05. price', 0)),
            'change': float(quote.get('09. change', 0)),
            'change_percent': quote.get('10. change percent', '0%'),
            'volume': int(quote.get('06. volume', 0)),
            'latest_trading_day': quote.get('07. latest trading day', ''),
            'previous_close': float(quote.get('08. previous close', 0)),
            'open': float(quote.get('02. open', 0)),
            'high': float(quote.get('03. high', 0)),
            'low': float(quote.get('04. low', 0))
        }
    except AlphaVantageRateLimited:
        raise
    except Exception as e:
        print(f"Error fetching quote: {e}")
        return None


def get_historical_prices(symbol: str, api_key: str) -> Optional[List[float]]:
    """
    Fetch historical daily prices for ML analysis
    Returns list of closing prices (oldest to newest)
    """
    try:
        data = _av_request({
            'function': 'TIME_SERIES_DAILY',
            'symbol': symbol,
            'outputsize': 'compact',  # Last 100 days
        }, api_key)

        if not data or 'Time Series (Daily)' not in data:
            print(f"No historical data for {symbol}")
            return None

        time_series = data['Time Series (Daily)']

        # Extract closing prices in chronological order (oldest to newest)
        prices = []
        for date in sorted(time_series.keys()):
            close_price = float(time_series[date]['4. close'])
            prices.append(close_price)

        return prices

    except AlphaVantageRateLimited:
        raise
    except Exception as e:
        print(f"Error fetching historical prices: {e}")
        return None


def get_alpha_vantage_rsi(symbol: str, api_key: str) -> Optional[Dict]:
    """Get RSI indicator.

    Not used on the main query path: the ML agent already computes RSI from
    the daily series, so calling this too would spend a second request from
    the 25/day free-tier budget for a value we already have.
    """
    try:
        data = _av_request({
            'function': 'RSI',
            'symbol': symbol,
            'interval': 'daily',
            'time_period': '14',
            'series_type': 'close',
        }, api_key, timeout=10)

        if not data or 'Technical Analysis: RSI' not in data:
            return None

        rsi_data = data['Technical Analysis: RSI']
        latest_date = list(rsi_data.keys())[0]
        latest_rsi = float(rsi_data[latest_date]['RSI'])

        return {
            'value': latest_rsi,
            'date': latest_date,
            'signal': 'oversold' if latest_rsi < 30 else 'overbought' if latest_rsi > 70 else 'neutral'
        }
    except AlphaVantageRateLimited:
        raise
    except Exception as e:
        print(f"Error fetching RSI: {e}")
        return None


def call_openai(prompt: str, api_key: str) -> Optional[str]:
    """Call OpenAI API"""
    try:
        url = "https://api.openai.com/v1/chat/completions"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}"
        }
        system_prompt = """You are a professional investment advisor providing personalized, data-driven guidance with ML-powered insights.

ADVISORY APPROACH:
- Act as a trusted financial advisor, not just an analyst
- Provide actionable recommendations tailored to different investor profiles
- Consider risk tolerance, investment horizon, and portfolio diversification
- Explain the "why" behind recommendations, not just the "what"
- Offer strategic guidance on entry/exit points, position sizing, and portfolio allocation

US MARKET EXPERTISE:
- Markets: NYSE (largest), Nasdaq (tech-focused). Indices: S&P 500 (500 large-cap, 80% market), Dow Jones (30 blue-chips), Nasdaq Composite (3000+ stocks)
- Instruments: Stocks (common/preferred, growth/value), bonds, mutual funds/ETFs, penny stocks (<$5, high volatility)
- Investment Strategies: Value investing (low P/E, long-term), Growth investing (high-earnings tech), Diversification (spread risk via ETFs), Momentum trading (ride trends)
- Key Regulations: Securities Act 1933 (disclosure), Exchange Act 1934 (SEC oversight, anti-fraud Rule 10b-5), Sarbanes-Oxley (transparency), Dodd-Frank (consumer protection)
- Power Players: Warren Buffett (value, long-term hold, "own for 10 years"), Peter Lynch (invest in what you know, reasonable P/E), Ray Dalio (risk-balanced, "All Weather" portfolio)

PENNY STOCK EXPERTISE:
- Penny stocks trade under $5, often on OTC markets with high volatility and risk
- Look for: Real business model, revenue growth, low debt, insider ownership, catalysts (FDA approvals, partnerships, earnings)
- Red flags: Dilution, pump-and-dump schemes, no revenue, frequent reverse splits
- Risk management: Never invest more than 5-10% of portfolio, use stop-losses, expect 50%+ volatility

Keep responses under 200 words, focusing on actionable advice."""

        data = {
            "model": "gpt-4o-mini",
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt}
            ],
            "max_tokens": 400,
            "temperature": 0.7
        }

        response = requests.post(url, headers=headers, json=data, timeout=30)
        response.raise_for_status()

        result = response.json()
        return result['choices'][0]['message']['content']
    except Exception as e:
        print(f"OpenAI API error: {e}")
        return None


def lambda_handler(event, context):
    """Enhanced handler with ML trading agent"""
    try:
        if isinstance(event.get('body'), str):
            body = json.loads(event['body'])
        else:
            body = event

        query = body.get('query', '')

        if not query:
            return {
                'statusCode': 400,
                'headers': {'Content-Type': 'application/json', 'Access-Control-Allow-Origin': '*'},
                'body': json.dumps({'error': 'Query required'})
            }

        # Extract stock symbols
        potential_symbols = extract_stock_symbols(query)

        # Get API keys
        openai_key = get_secret('stock-chatbot/openai-api-key')
        av_key = get_secret('stock-chatbot/alphavantage-api-key')

        # Handle general queries
        if not potential_symbols:
            if openai_key:
                # No market data was retrieved on this path, so the model must
                # not invent any. Without this constraint an unrecognised
                # token (e.g. a 6-letter ticker, which the extractor skips)
                # yields confident, fabricated "analysis" of a security we
                # never looked up.
                general_prompt = (
                    f"User query: {query}\n\n"
                    "This is a general investing question. Provide concise, "
                    "actionable guidance.\n\n"
                    "IMPORTANT CONSTRAINTS:\n"
                    "- You have NO market data available in this context.\n"
                    "- Do not state, estimate or guess any specific security's "
                    "price, valuation, market cap or recent performance.\n"
                    "- Do not describe or characterise a specific ticker you "
                    "cannot verify. If the user named a ticker, say you could "
                    "not retrieve market data for it and ask them to check the "
                    "symbol, then answer only the general part of their question.\n"
                    "End with a disclaimer."
                )
                ai_response = call_openai(general_prompt, openai_key)
                return {
                    'statusCode': 200,
                    'headers': {'Content-Type': 'application/json', 'Access-Control-Allow-Origin': '*'},
                    'body': json.dumps({
                        'response': ai_response or "Please mention a stock ticker symbol for detailed ML analysis.",
                        'query': query
                    })
                }
            else:
                return {
                    'statusCode': 200,
                    'headers': {'Content-Type': 'application/json', 'Access-Control-Allow-Origin': '*'},
                    'body': json.dumps({
                        'response': "Please mention a specific stock ticker symbol (e.g., AAPL, TSLA, MSFT) for ML-powered analysis.",
                        'query': query
                    })
                }

        # Find valid stock
        stock_symbol = None
        quote = None

        quote_rate_limited = False
        for symbol in potential_symbols:
            if av_key:
                try:
                    temp_quote = get_alpha_vantage_quote(symbol, av_key)
                except AlphaVantageRateLimited:
                    quote_rate_limited = True
                    break
                if temp_quote:
                    stock_symbol = symbol
                    quote = temp_quote
                    break

        if not stock_symbol or not quote:
            # A rate limit tells us nothing about the ticker, so never call it
            # invalid on that basis.
            if quote_rate_limited:
                message = f"⏳ {DATA_UNAVAILABLE_NOTE}"
            else:
                message = (
                    f"❌ Invalid stock symbol: {', '.join(potential_symbols)}\n\n"
                    "Please use a valid ticker (e.g., AAPL, TSLA, MSFT)."
                )
            return {
                'statusCode': 200,
                'headers': {'Content-Type': 'application/json', 'Access-Control-Allow-Origin': '*'},
                'body': json.dumps({
                    'response': message,
                    'query': query,
                    'data_unavailable': quote_rate_limited
                })
            }

        # Get historical prices for ML analysis
        history_rate_limited = False
        historical_prices = None
        if av_key:
            try:
                historical_prices = get_historical_prices(stock_symbol, av_key)
            except AlphaVantageRateLimited:
                history_rate_limited = True

        # Run ML analysis
        ml_analysis = None
        if historical_prices and len(historical_prices) >= 50:
            ml_analysis = get_ml_recommendation(stock_symbol, historical_prices)

        # Build response
        price = quote['price']
        change = quote['change']
        change_pct = quote['change_percent']
        volume = quote['volume']

        # Use ML recommendation if available
        if ml_analysis:
            recommendation = ml_analysis['recommendation']
            confidence = ml_analysis['confidence']
            ml_score = ml_analysis['ml_score']
            reasoning = ml_analysis['reasoning']
            risk_level = ml_analysis['risk_level']

            # Generate AI response with ML data
            if openai_key:
                prompt = f"""Analyze this stock with ML insights:

Stock: {stock_symbol}
Current Price: ${price}
Change: ${change} ({change_pct})
Volume: {volume:,}

ML Analysis:
- Recommendation: {recommendation}
- ML Confidence: {ml_score}%
- Risk Level: {risk_level}
- Signals: {ml_analysis['signals']}
- RSI: {ml_analysis['indicators']['rsi']}
- Momentum: {ml_analysis['indicators']['momentum_10d']}%

User Question: {query}

Provide a concise ML-enhanced analysis (2-3 sentences) with this data. Mention the ML confidence score and risk level. End with disclaimer."""

                ai_response = call_openai(prompt, openai_key)

                if ai_response:
                    response_text = f"""📊 **{stock_symbol}** - ${price}

📈 **Today's Performance**
Change: ${change} ({change_pct})
Volume: {volume:,}

🤖 **ML Analysis** (Confidence: {ml_score}%)
Recommendation: {recommendation}
Risk Level: {risk_level}
Signals: {ml_analysis['signals']['buy']} Buy | {ml_analysis['signals']['sell']} Sell | {ml_analysis['signals']['hold']} Hold
{reasoning}

{ai_response}"""
                else:
                    response_text = f"""📊 **{stock_symbol}** - ${price}

📈 Change: ${change} ({change_pct})
📊 Volume: {volume:,}

🤖 **ML Recommendation: {recommendation}**
ML Confidence: {ml_score}%
Risk Level: {risk_level}

{reasoning}

⚠️ This is not financial advice. Consult a licensed financial advisor."""
            else:
                response_text = f"""📊 **{stock_symbol}** - ${price}

Change: ${change} ({change_pct})
Volume: {volume:,}

🤖 ML Recommendation: {recommendation}
Confidence: {ml_score}%
Risk: {risk_level}

{reasoning}"""

            return {
                'statusCode': 200,
                'headers': {'Content-Type': 'application/json', 'Access-Control-Allow-Origin': '*'},
                'body': json.dumps({
                    'response': response_text,
                    'symbol': stock_symbol,
                    'query': query,
                    'data': {
                        'price': price,
                        'change': change,
                        'change_percent': change_pct,
                        'volume': volume,
                        'recommendation': recommendation.lower(),
                        'ml_confidence': ml_score,
                        'risk_level': risk_level,
                        'rsi': ml_analysis['indicators']['rsi'],
                        'trend': 'bullish' if recommendation == 'BUY' else 'bearish' if recommendation == 'SELL' else 'neutral'
                    }
                })
            }
        else:
            # Fallback without ML. Say *why* the analysis is missing rather
            # than implying the stock is at fault.
            if history_rate_limited:
                ml_note = f"⏳ {DATA_UNAVAILABLE_NOTE}"
            else:
                ml_note = (
                    "ML analysis needs at least 50 days of price history, "
                    f"which isn't available for {stock_symbol} right now."
                )

            return {
                'statusCode': 200,
                'headers': {'Content-Type': 'application/json', 'Access-Control-Allow-Origin': '*'},
                'body': json.dumps({
                    'response': f"""📊 **{stock_symbol}** - ${price}

Change: ${change} ({change_pct})
Volume: {volume:,}

{ml_note}

⚠️ This is not financial advice.""",
                    'symbol': stock_symbol,
                    'query': query,
                    'data_unavailable': history_rate_limited,
                    'data': {
                        'price': price,
                        'change': change,
                        'change_percent': change_pct,
                        'volume': volume
                    }
                })
            }

    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()
        return {
            'statusCode': 500,
            'headers': {'Content-Type': 'application/json', 'Access-Control-Allow-Origin': '*'},
            'body': json.dumps({'error': str(e)})
        }

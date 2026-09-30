# Trading Data Sources

**Researched 2026-09-30.** Every figure below was taken from a primary source — the
provider's own docs, pricing payload, or legal pages — or from a live probe run on that
date. Third-party blog figures were used only to cross-check, never as the basis for a
claim. Anything that could not be confirmed from a current primary source is listed in
[Unverified claims](#unverified-claims) and is flagged inline with ⚠️.

Provider terms change without notice. Re-verify before depending on an exact number.

---

## TL;DR

The binding constraint today is Alpha Vantage's free tier: **25 requests/day** at
**1 request/second**. At 2 requests per analysis that caps the app at ~12 analyses/day.

**Alpha Vantage's free tier cannot be fixed by caching or batching — it has moved
intraday data behind the paywall entirely.** Verified live on 2026-09-30:

```
GET /query?function=TIME_SERIES_INTRADAY&symbol=AAPL&interval=5min&apikey=<free key>
→ {"Information": "Thank you for using Alpha Vantage! This is a premium endpoint.
   You may subscribe to any of the premium plans ... to instantly unlock all premium
   endpoints"}
```

So `agent/providers/alpha_vantage.py`, which maps `1min`/`5min` to
`TIME_SERIES_INTRADAY`, **cannot return intraday bars on a free key at any request
budget.** This is not a rate-limit problem. It is an entitlement problem.

**The fix is Alpaca.** A free, unfunded **paper-trading** account gets the Basic market
data plan by default: **200 requests/minute, no documented daily cap**, full-SIP
historical bars (1-min, 5-min, daily) back to 2016, delayed 15 minutes. That is roughly
**78,000 requests per 6.5-hour session** against today's 25/day — and it restores
intraday bars, which the current stack has silently lost.

Pair it with **Finnhub** (free, 60 req/min) for a genuinely real-time last price, which
is the one thing Alpaca's free tier will not give you.

---

## Comparison table

Free tiers only. ✅ = available free · ❌ = paid only · ⚠️ = caveat, see detail section.

| | **Alpaca** | **Alpha Vantage** | **Massive** (ex-Polygon.io) | **Finnhub** |
|---|---|---|---|---|
| **Requests/min** | **200** | ~5 (1 req/sec) | **5** | **60** |
| **Requests/day** | none documented | **25** ← binding | none documented | none documented |
| **Burst cap** | — | 1 req/sec | — | 30 req/sec |
| **Equity quote recency** | ⚠️ real-time **IEX only**, or 15-min delayed SIP | ⚠️ EOD only | ⚠️ **End-of-day** | ✅ **real-time** |
| **Feed** | IEX (~2.5% vol) realtime; full SIP if >15 min old | undisclosed | full SIP (CTA+UTP) | ⚠️ undisclosed |
| **1-min / 5-min bars** | ✅ full SIP, >15 min old | ❌ **premium** | ✅ 2 yrs, EOD-stale | ❌ **premium** |
| **Daily bars** | ✅ since 2016 | ✅ `TIME_SERIES_DAILY` | ✅ 2 yrs | ❌ premium |
| **Bid/ask (NBBO)** | ✅ >15 min old (SIP) | ❌ premium | ❌ ($199/mo) | ❌ premium |
| **Trades (tick)** | ✅ >15 min old (SIP) | ❌ | ❌ ($79/mo) | ❌ premium |
| **WebSocket** | ✅ IEX, 30 symbols | ❌ | ❌ ($29/mo) | ✅ **50 symbols** |
| **News** | ✅ Benzinga, since 2015 | ✅ `NEWS_SENTIMENT` | ✅ 2 yrs + sentiment | ✅ 1 yr |
| **Earnings calendar** | ❌ | ✅ `EARNINGS_CALENDAR` | ⚠️ unverified | ✅ 1 mo history |
| **Fundamentals** | ❌ | ✅ `OVERVIEW` | ❌ ($199/mo) | ✅ basic financials |
| **Splits / dividends** | ⚠️ endpoint exists, tier unverified | ❌ (adjusted daily is premium) | ✅ | ❌ **both premium** |
| **Cheapest paid** | $99/mo | $49.99/mo | **$29/mo** | $49.99/mo |
| **Personal use only** | ✅ yes | — | ✅ yes + non-pro warranty | ✅ yes + non-pro warranty |
| **Non-display / algo use** | not addressed | not addressed | ❌ **explicitly barred** | not addressed |

### Official / government sources

| | **SEC EDGAR** | **FRED** | **BLS** | **BEA** |
|---|---|---|---|---|
| **API key** | none | required (free) | v2 required (free) | required (free) |
| **Rate limit** | **10 req/sec** | **120 req/min** | 50 req / 10 sec | **100 req/min** |
| **Daily cap** | none | none | **25** (v1) / **500** (v2) | none |
| **Other throttles** | — | — | 50 series & 20 yrs per query (v2) | 100 MB/min, **30 errors/min** |
| **Intraday-relevant?** | ✅ 8-K lands within minutes | ❌ macro context only | ❌ scheduled releases | ❌ scheduled releases |

---

## Alpaca Market Data

**The recommendation.** Docs: <https://docs.alpaca.markets/us/docs/about-market-data-api>

### Getting a key costs nothing and requires no funding

> "The Basic plan serves as the default option for both **Paper and Live** trading
> accounts, ensuring all users can access essential data with zero cost."
> — <https://docs.alpaca.markets/us/docs/about-market-data-api> (2026-09-30)

A paper account issues its own API key pair. No deposit, no funded balance.

### Free (Basic) tier limits

From the plan table at <https://docs.alpaca.markets/us/docs/about-market-data-api>
(2026-09-30), verbatim:

| Field | Value |
|---|---|
| Real-time market coverage | `IEX` |
| Websocket subscriptions | `30 symbols` |
| Historical data limitation | `latest 15 minutes` |
| Historical API calls | `200 / min` |
| Historical data timeframe | `Since 2016` |

`429 Too Many Requests` on exceed
(<https://alpaca.markets/support/usage-limit-api-calls>). **No daily cap is documented
anywhere** — see [Unverified claims](#unverified-claims).

### The key finding: full SIP history is free, delayed 15 minutes

"Historical data limitation: latest 15 minutes" is easy to misread as "you only get IEX."
It means the opposite — you get the **full consolidated tape**, just not the most recent
15 minutes of it:

> "**Free-plan users accessing SIP or OPRA data for recent timestamps receive an error.
> Data older than 15 minutes is accessible on all feeds.**"
> — <https://alpaca.markets/learn/fetch-historical-data> (2026-09-30)

Corroborated by the Market Data FAQ: *"to query any SIP trades or quotes in the last 15
minutes, you need a special subscription"*
(<https://docs.alpaca.markets/us/docs/market-data-faq>).

So on a **free** key, `feed=sip` works for bars, quotes and trades as long as the `end`
parameter is ≥15 minutes in the past. For a swing or intraday-bar strategy that reasons
over completed bars, a 15-minute lag on the *consolidated* tape is far better than
real-time on IEX alone (~2.5% of US volume).

Valid `feed` values, from Alpaca's own SDK enum
(<https://github.com/alpacahq/alpaca-py/blob/master/alpaca/data/enums.py>):
`iex`, `sip`, `delayed_sip` ("SIP data with a 15 minute delay"), `otc`, `boats`,
`overnight`.

### History depth

Back to **2016**, and no further — Alpaca says so directly:
<https://alpaca.markets/support/alpaca-data-timeline>. The pricing page's "7+ years"
(<https://alpaca.markets/data>) is the same fact stated from 2026.

### Batch endpoints — this is what actually kills the quota problem

Every historical method takes a **list** of symbols, with `page_size` up to 10,000
(<https://github.com/alpacahq/alpaca-py/blob/master/alpaca/data/historical/stock.py>):

`get_stock_bars` · `get_stock_quotes` · `get_stock_trades` · `get_stock_snapshot` ·
`get_stock_latest_bar` · `get_stock_latest_quote` · `get_stock_latest_trade`

A 20-symbol watchlist scan is **one request**, not 20. Combined with 200 req/min, request
budget stops being a design constraint. `MarketDataProvider.get_snapshot()` in
`agent/providers/base.py` already anticipates this — its docstring notes that providers
with a native batch endpoint should override the sequential default.

### WebSocket

Free: IEX feed, **30 symbols**, one connection. Channels: `trades`, `quotes`, `bars`,
`dailyBars`, `updatedBars`, `statuses`, `lulds`, `imbalances`
(<https://docs.alpaca.markets/us/docs/real-time-stock-pricing-data>). `v2/sip` and
`v2/delayed_sip` stream endpoints exist but authentication fails if your plan lacks them.

### News

Benzinga, back to **2015**, ~130 articles/day, REST + WebSocket
(<https://docs.alpaca.markets/us/docs/historical-news-data>). ⚠️ Alpaca does not state
which plan tier the News API requires.

### Corporate actions

`GET /v1/corporate-actions` on `data.alpaca.markets` — 14 types including forward/reverse
splits, cash/stock dividends, mergers, spin-offs, name changes. Max 1,000 records/page
(<https://docs.alpaca.markets/us/reference/corporateactions-1>). ⚠️ Tier requirement not
documented.

### Cheapest paid tier

**Algo Trader Plus, $99/month** — full real-time SIP, unlimited WebSocket symbols, no
15-minute restriction, 10,000 req/min (<https://alpaca.markets/data>). Note the Terms
call this plan **"Pro"** while the pricing page calls it "Algo Trader Plus."

### Terms of service

From <https://files.alpaca.markets/disclosures/library/TermsAndConditions.pdf>
(text extracted 2026-09-30), verbatim:

> **Personal and Non-Commercial Usage** — "Other than as set forth herein, you agree to
> use the Services and Content solely for your own personal and non-commercial purposes.
> Should you wish to use the Services and Content for any other purposes, including
> without limitation commercial usage, or making the Services and Content available to
> others through your own application (a 'User Application'), you shall provide Alpaca
> with **30 days advance written notice** prior to making such User Application available
> to others."

Also: *"Alpaca offers a Basic market data plan, which is made available at no cost, as
well as a Pro market data plan, that is made available on a paid subscription basis."*

Worth noting: the NASDAQ OMX Global Subscriber Agreement is attached to selecting the
**Pro** plan — *"By selecting the Pro market data plan ... you agree to: (i) the NASDAQ
OMX Global Subscriber Agreement..."* The Basic plan is not described as triggering it.

**Nothing in Alpaca's Terms restricts automated or algorithmic access.** Alpaca is an
algo-trading broker; programmatic use is the product. The operative limits are the rate
cap and the personal/non-commercial scope.

---

## Alpha Vantage (current provider)

Docs: <https://www.alphavantage.co/documentation/>

### Free tier limits — verbatim from the live API, 2026-09-30

A probe of `TIME_SERIES_DAILY` returned:

> "Thank you for using Alpha Vantage! Please consider spreading out your free API
> requests more sparingly **(1 request per second)**. You may subscribe to any of the
> premium plans ... to lift the **free key rate limit (25 requests per day)**, raise the
> per-second burst limit, and instantly unlock all premium endpoints"

This matches the project's measured **~1 req/sec burst, 25 req/day** exactly, and matches
the support page: *"25 API requests per day and unlimited API requests for verified
open-source or educational projects"* (<https://www.alphavantage.co/support/>).

### ⚠️ Intraday is now a premium endpoint

The documentation states, for `TIME_SERIES_INTRADAY`:

> "This is a premium endpoint. If you would like to access realtime, 15-minute delayed,
> and/or historical intraday data, please subscribe to a premium membership plan."

Confirmed live on 2026-09-30 — the same key that got a rate-limit message from
`TIME_SERIES_DAILY` got `"This is a premium endpoint"` from `TIME_SERIES_INTRADAY`.
The support page is blunt: *"Realtime and 15-minute delayed US market data is
premium-only."*

**Premium-gated:** `TIME_SERIES_INTRADAY`, `TIME_SERIES_DAILY_ADJUSTED`,
`REALTIME_BULK_QUOTES`, `REALTIME_BULK_BID_ASK_PRICES`, index data APIs.

**Still free** (verified live 2026-09-30, subject to 25/day): `TIME_SERIES_DAILY`,
`GLOBAL_QUOTE` (end-of-day close), `OVERVIEW` (fundamentals), `NEWS_SENTIMENT`,
`EARNINGS_CALENDAR`, `EARNINGS`.

### What this means for the project

`agent/providers/alpha_vantage.py` maps `1min`/`5min`/`15min`/`30min`/`60min` to
`TIME_SERIES_INTRADAY`. On a free key those paths now return an `Information` string
rather than data. That string is not caught by the existing throttle detector:
`_THROTTLE_MARKERS` covers `per second`, `sparingly`, `rate limit`,
`requests per day`, `call frequency`, `higher api call`. The premium message contains
**none of those**, so it will not be classified as `RateLimited`; it will fall through to
whatever the parser does with a missing time-series key. Given this repo's stated
design rule — *"Unavailable is not the same as zero, and throttled is not the same as
unavailable"* — that path deserves an explicit `DataUnavailable` (or a new
`NotEntitled`) rather than a generic failure.

### Pricing

75 / 150 / 300 / 600 / 1200 req/min at **$49.99 / $99.99 / $149.99 / $199.99 /
$249.99** per month (<https://www.alphavantage.co/premium/>). Paid plans have no daily
cap.

### Terms

⚠️ I did not locate a terms-of-service page stating restrictions on automated use or
redistribution. Alpha Vantage's public pages describe rate limits and entitlements but I
could not verify a redistribution clause from a primary source.

---

## Massive (formerly Polygon.io)

**The rebrand is real.** Polygon.io became **Massive** effective **October 30, 2025,
4 PM ET** (<https://massive.com/blog/polygon-is-now-massive>). `polygon.io` now returns
`301 → massive.com`. `api.polygon.io` still works and still accepts existing keys;
`api.massive.com` is the new default and the old endpoints are slated to be phased out
"in the new year" with advance notice. Docs: <https://massive.com/docs>.

### Free tier ("Stocks Basic", $0)

Pulled directly from the pricing payload embedded in <https://massive.com/pricing>
(2026-09-30) — these are Massive's own field values, not a reading of the rendered table:

```
name: "Stocks Basic"   slug: stocks_basic   license_type: "personal"   price: $0.00
api_calls            5 API Calls / Minute
timeframe            End of Day Data
historical_data      2 Years Historical Data
market_coverage      100% Market Coverage
tickers              All US Stocks Tickers
minute_aggregates    available
second_aggregates    unavailable
quotes               unavailable
trades               unavailable
snapshot             unavailable
websocket            unavailable
corporate_actions    available
reference_data       available
technical_indicators available
flat_files           unavailable
```

**5 requests/minute** — one every 12 seconds. The knowledge base explains the limit is
*"Five requests a minute per asset class you have not subscribed to"* and is *"counted
per asset class rather than per account"*
(<https://massive.com/knowledge-base/article/what-is-the-request-limit-for-massives-restful-apis>).
`429` on exceed.

### The disqualifier: end-of-day, not delayed

Free is **`End of Day Data`** — not 15-minute delayed. You get 1-minute bars for *past*
sessions, never today's. For an intraday agent that is fatal as a primary source, though
perfectly good for backfill.

The upside: coverage is **full SIP (CTA + UTP), "100% Market Coverage"**, even on free.
Only recency is degraded, not breadth.

### News

Free and unusually good: *"Included in all Stocks plans"*, hourly refresh, with
**sentiment scoring and reasoning** plus keywords; 2 years of history on Basic
(<https://massive.com/docs/rest/stocks/news>).

### Cheapest paid tier

**Stocks Starter, $29/month** ($288/yr) — unlimited calls, 15-minute delayed, 5 years
history, plus WebSockets, snapshots, second aggregates, flat files. Still no trades
(Developer, $79/mo) and no NBBO quotes (Advanced, $199/mo, which is also where real-time
begins).

### ⚠️ Terms of service — the strictest of the four

<https://massive.com/legal/market-data-terms-of-service> §5, verbatim:

> "Absent prior express written consent from Massive ... you may not: ... (c)
> **Redistribute, display, disseminate, duplicate, license, sublicense, publish,
> broadcast, transmit, distribute, redistribute, perform, display, sell, resell, rebrand,
> or otherwise transfer the Market Data—or any data, charts, analytics, research, or
> other works based on, referring to, or derived from the Market Data ("Derived
> Works")—to any third party or use the Market Data for business or commercial
> purposes**; (d) **Use Market Data for non-display use or to create derivative works ...
> unless you are licensed to do so**"

And §2: *"any and all Market Data is strictly for display use only."*

**"Non-display use" is a market-data term of art that covers algorithmic consumption —
data ingested by a machine to compute signals without a human viewing it.** An automated
trading agent is a textbook non-display use. This clause is a genuine constraint on the
use case, not boilerplate. It is a licensing conversation, not something to engineer
around. *(This is a reading of standard industry language, not legal advice.)*

Massive also requires a **Non-Professional** warranty and incorporates the OPRA, Nasdaq/
UTP, NYSE and CME subscriber agreements.

---

## Finnhub

Docs: <https://finnhub.io/docs/api> · <https://finnhub.io/terms-of-service>

Finnhub's pricing and docs pages are client-rendered React; a plain fetch returns a ~10 KB
shell. The figures below come from the **OpenAPI spec embedded in the docs page payload**,
parsed per-endpoint, and from the docs' own rate-limit section — Finnhub's own first-party
data.

### Free tier limits

From the docs, section "Limits" (`urlId: rate-limit`), verbatim:

> "If your limit is exceeded, you will receive a response with status code `429`."
> "**On top of all plan's limit, there is a 30 API calls/ second limit.**"

Free plan: **60 API calls/minute**, license "Personal Use", coverage US. No daily cap is
documented — see [Unverified claims](#unverified-claims). The Terms add:
*"You will have 1 API limit for Fundamental data and 1 API limit for Market data API.
Subscribing to multiple data plans will not increase your API limit."*

### ⚠️ The headline: real-time quotes, but NO bars

`/quote` is free and, per its own description, **real-time**:

> "Get real-time quote data for US stocks. Constant polling is not recommended. Use
> websocket if you need real-time updates."

But `/stock/candle` — every OHLCV bar, intraday *and* daily — is flagged
`premium: "Premium Access Required"` in the spec. This contradicts a large number of
blog posts and AI-generated summaries still claiming free Finnhub candles. Two
independent parses of Finnhub's own payload agree on it.

⚠️ **Finnhub does not disclose which venue or feed backs the free real-time `/quote`.**
There is no "IEX only" or "full SIP" statement anywhere public. The only feed-provenance
disclosure is on the *paid* tick endpoint (US CTA/UTP full SIP, end-of-day). Do not
assume the free quote is consolidated.

### Free vs premium, from the spec's per-endpoint `premium` flag

**Free:** `/quote` · `/stock/symbol` · `/stock/profile2` · `/stock/metric` (basic
financials) · `/stock/financials-reported` · `/stock/recommendation` ·
`/stock/insider-transactions` · `/calendar/earnings` (1 mo history) · `/calendar/ipo` ·
`/company-news` (1 yr) · `/news` (market news) · `/stock/market-status` ·
`/stock/pattern` · economic calendar & data

**Premium:** `/stock/candle` · `/stock/split` · `/stock/dividend` · `/stock/bidask` ·
`/stock/tick` · `/stock/nbbo` · `/news-sentiment` · `/stock/price-target` ·
`/stock/upgrade-downgrade` · technical indicators · SEC filings

⚠️ `/stock/earnings` (historical EPS surprises) shows `premium: null` in the spec, but a
second reading of Finnhub's pricing table suggested it is gated. Treat as uncertain and
test with a real key.

**Corporate-actions gotcha:** splits *and* dividends are both paywalled — the opposite of
Massive, where corporate actions ship free.

### WebSocket — the best thing on Finnhub's free tier

> "Stream real-time trades for US stocks, forex and crypto. ... **1 API key can only open
> 1 connection at a time.**" — `wss://ws.finnhub.io?token=<token>`

Free cap: **50 symbols**. Payload per trade: symbol, last price, UNIX ms timestamp,
volume, conditions. This is the practical workaround for the missing candles — you can
build your own intraday bars from the live tape. News and press-release streams are
premium.

### Cheapest paid tier

**Market Data Basic, $49.99/month** — 150 req/min, daily OHLC (25 yrs), 1-minute OHLC,
tick data, unlimited WebSocket symbols. Ladder: $49.99 → Standard $129.99 (300/min) →
Professional $199.99. All-In-One is $3,500/month.

### Terms of service

<https://finnhub.io/terms-of-service>, verbatim:

> "You hereby agree to **not redistribute or share access to data or derived results from
> the data** obtained from Finnhub with anyone or any 3rd party without written approval
> from Finnhub. **All plan listed on Finnhub website is strictly for personal use unless
> explicitly stated otherwise. Personal plan can't be used by any business even
> internally without a written approval.**"

You are disqualified from personal-use plans if you are a registered securities
professional, are **"using this data for your business or registering under your business
name regardless of the industry,"** or are **"going to deduct this expense as a business
expense."**

Also: *"All data must be deleted should your subscription to that data ends."* And
inactive accounts are deleted periodically — relevant if a free key sits idle.

**Finnhub's Terms contain no clause restricting automated, programmatic or non-display
use.** There is no "display use only" language. The constraints are rate limits,
personal-use scope, and redistribution of raw *or derived* results. Silence is not
permission in a dispute, but the contractual posture is materially more permissive than
Massive's on its face.

---

## SEC EDGAR

Free, no API key, no cost. Docs:
<https://www.sec.gov/search-filings/edgar-application-programming-interfaces>

> "These APIs do not require any authentication or API keys to access."

### Endpoints

| Purpose | URL |
|---|---|
| Submissions (filing history) | `https://data.sec.gov/submissions/CIK##########.json` |
| Company concept (one XBRL tag) | `https://data.sec.gov/api/xbrl/companyconcept/CIK##########/us-gaap/{tag}.json` |
| Company facts (all XBRL) | `https://data.sec.gov/api/xbrl/companyfacts/CIK##########.json` |
| Frames (one tag, all filers) | `https://data.sec.gov/api/xbrl/frames/us-gaap/{tag}/USD/CY2019Q1I.json` |
| **Ticker → CIK map** | `https://www.sec.gov/files/company_tickers.json` |

CIK must be **zero-padded to 10 digits** (`CIK0000320193` for Apple). `company_tickers.json`
is an object keyed by stringified index, and `cik_str` is an **integer** despite the name —
you must pad it yourself:

```json
{"0":{"cik_str":1045810,"ticker":"NVDA","title":"NVIDIA CORP"},
 "1":{"cik_str":320193,"ticker":"AAPL","title":"Apple Inc."}}
```

Note: *"data.sec.gov does not support Cross Origin Resource Scripting (CORS)"* — server-side
only.

### ⚠️ The User-Agent requirement

The SEC requires a declared User-Agent carrying contact information. Its sample format,
verbatim from <https://www.sec.gov/os/webmaster-faq#developers>:

```
User-Agent: Sample Company Name AdminContact@<sample company domain>.com
```

i.e. **a declared name plus a contact email address.**

Rate limit, verbatim from the same page: *"Note that our current maximum access rate is 10
requests per second. This is carefully monitored to preserve equitable access for all
users."*

And from the SEC Internet Security Policy
(<https://www.sec.gov/about/privacy-information#security>):

> "Current guidelines limit users to a total of no more than 10 requests per second,
> **regardless of the number of machines used to submit requests.**"
> "If a user or application submits more than 10 requests per second, further requests from
> the IP address(es) may be limited for a brief period."
> "The SEC does not allow 'unclassified' bots or automated tools to crawl the site."

"Regardless of the number of machines" means the budget is per-requester — sharding across
IPs to go faster is explicitly out of bounds.

**Enforcement detail worth knowing:** live probing on 2026-09-30 found the technical block
fires on a **missing** User-Agent (403), not on a non-compliant one — a generic browser UA
currently returns 200. Do not build on that. Supplying a real contact string is costless,
is the stated policy, and keeps you off a manual block list. The failure mode that will
actually bite is an HTTP client that omits the header by default.

### Full-text search

`https://efts.sec.gov/LATEST/search-index?q=...&forms=8-K&dateRange=custom&startdt=...&enddt=...`

Returns a raw Elasticsearch-shaped response. Coverage, verbatim from
<https://www.sec.gov/edgar/search/efts-faq.html>: *"Full-Text Search will allow you to
search the full text of all EDGAR filings submitted electronically **since 2001**."*
There is a 10,000-result ceiling; `"total": {"relation": "gte"}` is the signal you hit it —
narrow the date range rather than deep-paging.

⚠️ **The SEC publishes no documentation for the EFTS endpoint itself** — no parameter list,
no response schema, no versioning or deprecation policy. The `LATEST` in the path is not a
version guarantee. Treat it as undocumented-but-public: build defensively, expect schema
drift, keep a fallback.

**Why this matters for intraday:** EDGAR is the only source in this document that updates
on an intraday-relevant timescale. An 8-K appears within minutes of filing.

---

## FRED (Federal Reserve Bank of St. Louis)

Docs: <https://fred.stlouisfed.org/docs/api/fred/overview.html>

**API key required, free.** *"All web service requests require an API key to identify
requests"* (<https://fred.stlouisfed.org/docs/api/api_key.html>). You must create a
`fredaccount.stlouisfed.org` account first. Keyless requests return `400`.

### Rate limit: 120 requests/minute

⚠️ This number appears **only** in the error-code table
(<https://fred.stlouisfed.org/docs/api/fred/errors.html>), not on any page titled as rate
limit policy. The `429` row reads, verbatim:

> "Too Many Requests (**Up to 120 requests per minute are allowed** before being served a
> 429 error code. Not complying with the throttling can result in a **temporary block**.)"

### Useful series

`DGS10` (10-yr Treasury) · `DGS2` · `T10Y2Y` (10y–2y spread) · `DFF` (daily effective fed
funds; `FEDFUNDS` is monthly) · `SOFR` · `DTWEXBGS` (broad dollar) · `T10YIE` (10-yr
breakeven inflation) · `BAMLH0A0HYM2` (high-yield OAS — a real risk-appetite gauge) ·
`VIXCLS` · `DCOILWTICO` · `SP500`

**Latency caveat:** FRED is a warehouse of *published* series, not a tick feed. `DGS10`
and `VIXCLS` post after the close, typically next business day. Right tool for regime
context and "what changed overnight"; wrong tool for anything intrabar.

### Terms

<https://fred.stlouisfed.org/docs/api/terms_of_use.html>:

- **Required attribution:** *"This product uses the FRED® API but is not endorsed or
  certified by the Federal Reserve Bank of St. Louis."* (Moot for a private agent with no
  UI; binding the moment you share it.)
- Limits may be adjusted unilaterally: *"The Federal Reserve Bank of St. Louis may impose
  or adjust the limit on the amount of bandwidth you may use..."*
- ⚠️ **Redistribution caveat:** FRED aggregates third-party copyrighted series. The terms
  require contacting the **data owner** before using copyrighted series *"for anything
  other than your own personal use."* Personal agent: fine. Republishing series data: not
  automatically permitted, and the terms do not enumerate which series are encumbered.

---

## BLS (Bureau of Labor Statistics)

Docs: <https://www.bls.gov/developers/api_faqs.htm>

| Service | v2.0 (registered) | v1.0 (unregistered) |
|---|---|---|
| Daily query limit | **500** | **25** |
| Series per query | 50 | 25 |
| Years per query | 20 | 10 |
| Request rate | 50 requests per 10 seconds | 50 requests per 10 seconds |
| Net/percent changes | yes | no |
| Series catalog info | yes | no |

The rate limit is identical across tiers — registration buys the **daily cap**. 25/day is
as tight as Alpha Vantage; register. Registered keys **must be renewed annually**.

- v1 GET: `https://api.bls.gov/publicAPI/v1/timeseries/data/{seriesId}`
- v2 POST: `https://api.bls.gov/publicAPI/v2/timeseries/data/` with
  `{"seriesid":[...],"startyear":"...","endyear":"...","registrationkey":"..."}`

### Useful series

`CUUR0000SA0` (CPI-U all items NSA) · `CUSR0000SA0` (SA) · `CUUR0000SA0L1E` (core) ·
`CES0000000001` (total nonfarm payrolls, SA) · `CES0500000003` (avg hourly earnings) ·
`LNS14000000` (U-3 unemployment) · `WPUFD4` (PPI final demand)

**Caveat:** BLS releases are scheduled, market-moving events (CPI ~08:30 ET; jobs report
first Friday ~08:30 ET). The API is not a low-latency release wire — polling it to
front-run a print is unreliable and, at 500 queries/day, budget-infeasible. Use the BLS
release calendar for *when*, the API for the *revised series*.

### Terms

<https://www.bls.gov/developers/termsOfService.htm>:

> "If BLS reasonably believes that a user has attempted to exceed or **circumvent** these
> limits, that user's access to the API may be permanently or temporarily blocked."

Note "circumvent" — rotating keys or IPs to beat the 25/day cap is explicitly contemplated
and sanctioned. Attribution required; the BLS logo may not be used.

---

## BEA (Bureau of Economic Analysis)

**API key required, free** (<https://apps.bea.gov/API/signup/>) — a 36-character UserID
passed as `?UserID=`.

Base: `https://apps.bea.gov/api/data/?&UserID={key}&method=GETDATA&DataSetName={ds}&ResultFormat=JSON`

### Rate limits

From the **BEA API User Guide, dated April 20, 2026**
(<https://apps.bea.gov/api/_pdf/bea_web_service_api_user_guide.pdf>, p.5), verbatim —
*"three standard limits applied for all API user accounts"*:

- **Number of requests per minute (100)**
- **Data volume retrieved per minute (100 MB)**
- **Errors per minute (30)**

Two details most summaries miss:

1. **Error-based throttling.** 30 errors/min locks you out as surely as 100 requests/min.
   An agent retrying a malformed `TableName` in a tight loop self-bans in seconds. **Bound
   your retries.**
2. **Proper `429` handling is available.** *"each throttling error response will return an
   HTTP 429 ... and include a **RETRY-AFTER** response header indicating how long to wait
   (in seconds)."* Honor it rather than guessing a backoff.

The guide notes limits *"have periodically been adjusted ... and will continue to be
evaluated and adjusted as needed."*

### Useful data

`NIPA` dataset: `T10101` (real GDP % change) · `T20600` (personal income & outlays,
monthly) · `T20804` (**PCE price index — the Fed's preferred inflation gauge**).
`RegionalData` is deprecated; use `Regional`.

Same latency caveat as BLS: quarterly/monthly scheduled releases, useless intrabar.

### Terms

<https://apps.bea.gov/api/_pdf/bea_api_tos.pdf>: attribution required (*"This product uses
the Bureau of Economic Analysis (BEA) Data API but is not endorsed or certified by BEA"*);
no misrepresentation; BEA may block for attempts to *"exceed or circumvent"* limits. BEA
data is US Government work, so redistribution is materially less constrained than FRED's
third-party series.

---

## Recommendation

### REQUIRED NOW

**1. Add an Alpaca provider and make it the primary source for bars and quotes.**

This is the single change that removes the constraint. Sign up for a **paper** account —
no funding, no deposit — and take the two API keys. You get:

- **200 req/min, no documented daily cap** (vs 25/day)
- **1-min, 5-min and daily bars on the full SIP tape**, delayed 15 minutes, back to 2016
- **Bid/ask and trades** on the same 15-minute-delayed basis — the `Quote.bid`/`Quote.ask`
  and `spread_pct` fields in `agent/providers/base.py` become real instead of `None`
- **Native batch endpoints** — a whole watchlist in one request

The existing abstraction makes this a drop-in. `MarketDataProvider` already defines
`get_quote`, `get_bars`, `get_intraday_bars`, `get_snapshot` and `capabilities()`; an
`AlpacaProvider` implements them and reports `daily_request_budget: None`,
`batch_quotes: True`, `bid_ask: True`, `intraday: True`. Override `get_snapshot()` to use
the native batch call — the base docstring already flags that as the thing that matters.

**Be honest in `Provenance` about what you are getting.** Free Alpaca is *either*
real-time IEX (~2.5% of volume) *or* 15-minute-delayed consolidated SIP. Set
`is_delayed=True` and record the feed in `note` when using `feed=sip`. The provenance
contract in this repo exists precisely so a caller can answer "how old is this?" — a
15-minute lag must not be presentable as real-time.

**2. Fix the Alpha Vantage premium-endpoint path.**

`_THROTTLE_MARKERS` does not match `"This is a premium endpoint"`, so an intraday request
on a free key is currently neither `RateLimited` nor cleanly `DataUnavailable`. Given this
repo's rule that *"throttled is not the same as unavailable,"* add explicit detection and
raise `DataUnavailable` (or a new `NotEntitled` error). A silent miss here is the same
class of bug as the ML outage this project already recovered from.

### USEFUL NEXT

**3. Finnhub, for the one thing Alpaca free will not give you: a real-time last price.**
60 req/min, free, `/quote` is real-time for US equities. It fills Alpaca's 15-minute gap
for "what is this trading at right now" without paying $99/mo. Also free and useful:
`/calendar/earnings`, `/company-news`, `/stock/metric`, `/stock/recommendation`.
Do **not** plan on Finnhub for bars — `/stock/candle` is premium.
⚠️ Its free-quote feed provenance is undisclosed; label it accordingly in `Provenance`.

**4. SEC EDGAR, for event-driven signal.** Free, no key, 10 req/s — the only source here
that moves on an intraday timescale. An 8-K appears within minutes of filing. Set a
compliant `User-Agent` with real contact info, cache `company_tickers.json` daily, and run
at 5–8 req/s with jitter.

**5. FRED, for macro regime context.** Free key, 120 req/min. `T10Y2Y`, `BAMLH0A0HYM2`,
`VIXCLS`, `DGS10` are cheap features that condition a strategy. A few dozen calls a day.

### FUTURE-OPTIONAL

**6. Massive, for historical backfill only.** Free tier is end-of-day and 5 req/min — no
use live, but fine for batch-loading 2 years of minute bars and for splits/dividends,
which Alpaca's tier requirement is unclear on and Finnhub paywalls.
⚠️ **Check the non-display clause first.** Massive explicitly bars non-display use and
derived works, which on its face covers an automated signal generator. This is the one
provider here whose terms are in real tension with the use case.

**7. Retain Alpha Vantage narrowly.** Its 25/day is fatal for price data but adequate for
things you fetch once a day: `OVERVIEW` (fundamentals), `EARNINGS_CALENDAR`,
`NEWS_SENTIMENT`. Keep the provider; demote it from the price path.

**8. BLS and BEA.** Only if you want CPI/payrolls/PCE as scheduled-event features.
Register for BLS v2 (25/day unregistered is unusable). Bound BEA retries — 30 errors/min
is a ban.

**9. Only then consider paying.** If real-time consolidated data ever becomes necessary,
**Alpaca Algo Trader Plus at $99/mo** is the coherent upgrade, because it is the same API
you will already be running — no migration. Massive Starter at $29/mo is cheaper but only
reaches 15-minute delayed, which free Alpaca already gives you.

### One legal note that applies across all of this

Every commercial provider here — Alpaca, Massive, Finnhub — licenses its free tier for
**personal, non-commercial, non-professional** use only, and all three bar redistributing
the data **or results derived from it** to third parties. That is fine for a personal
trading agent with one user. It is not fine the moment the app has users other than you,
is registered under a business name, or the expense is deducted as a business expense
(Finnhub names that last one explicitly). Settle that before building on free tiers, not
after. *None of this was reviewed by counsel.*

---

## Unverified claims

Stated honestly, because acting on an unverified number is worse than knowing you lack it.

1. **Alpaca daily cap.** No daily limit is documented, and the support page discusses only
   the per-minute figure. Absence of documentation is not proof of absence. I could not
   test without a key.
2. **Alpaca News API tier.** The news docs do not state which plan is required. Assumed
   free because it appears in the Market Data API alongside Basic-tier features; not
   confirmed.
3. **Alpaca Corporate Actions tier.** Same — the endpoint reference documents no
   subscription requirement either way.
4. **Alpaca market-data rate limit of 200/min.** Taken from the plan table on the docs
   page. The dedicated support article on usage limits quotes 200/min for the **Trading**
   API and does not separately state the Market Data figure.
5. **Finnhub free-quote feed provenance.** Finnhub does not disclose anywhere public which
   venue or feed backs the free real-time `/quote`. "Real-time" is claimed; the coverage
   behind it is unknown. Material unknown.
6. **Finnhub daily/monthly quota.** None documented in the pricing payload, docs or terms;
   could not prove none exists.
7. **Finnhub `/stock/earnings`.** The spec's `premium` flag reads `null` (free), but a
   second reading of the pricing table suggested it is gated. Unresolved — test with a
   real key.
8. **Massive earnings calendar.** No such row in the Stocks pricing comparison; could not
   confirm whether the endpoint exists or which tier carries it. Do not assume it is free.
9. **Massive daily/burst limits.** Only the per-minute limit is documented.
10. **`api.polygon.io` sunset date.** "Phase out in the new year" with advance notice; no
    specific date published.
11. **Alpha Vantage terms of service.** I could not locate a primary page stating
    restrictions on automated use or redistribution. Not verified either way.
12. **SEC EFTS full-text search semantics.** Endpoint verified working and the
    10,000-result ceiling observed, but the SEC publishes **no** documentation for it —
    parameters, response schema and limits are inferred from live probes.
13. **FRED's 120 req/min.** Verified, but only from the errors page, not from any stated
    rate-limit policy. The terms reserve the right to change it at any time.
14. **BLS v2 without a key.** A live POST to v2 succeeded with no `registrationkey`. No
    documentation explains this; which daily cap applies is unknown. Do not design around
    it.
15. **Whether any figure changed after 2026-09-30.** BEA's guide is dated April 20, 2026
    and says limits are periodically adjusted; FRED reserves unilateral adjustment;
    Finnhub's terms say *"We may change the Terms from time to time without notice."*
16. **Non-display-use analysis (Massive).** That clause is read as a market-data term of
    art covering algorithmic consumption. That is an informed reading, **not legal
    advice**, and no terms here were reviewed by counsel.

---

*Compiled 2026-09-30 from provider primary sources and live API probes. Re-verify rate
limits and entitlements before relying on them — all four commercial providers reserve the
right to change them without notice.*

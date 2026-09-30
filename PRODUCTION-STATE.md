# Production State — Stock Trading Chatbot

**Authoritative description of what is deployed.** Where any other document in
this repository disagrees with this one, this one is correct. The older
documents are kept as a historical record of how the system was built; several
describe an architecture that is no longer deployed (see
[Superseded documentation](#superseded-documentation)).

Last verified: **2026-09-30**

---

## 1. Live architecture

```
Browser (S3 static site)
      │  POST {"query": "..."}
      ▼
API Gateway  lmi4hshs7h  /prod/chatbot   (REST, POST + OPTIONS)
      │
      ▼
Lambda  stock-chatbot-router  (python3.12, x86_64, 256 MB, 30 s)
      ├── Secrets Manager ── stock-chatbot/openai-api-key
      ├── Secrets Manager ── stock-chatbot/alphavantage-api-key
      ├── Alpha Vantage ──── GLOBAL_QUOTE      (live quote)
      ├── Alpha Vantage ──── TIME_SERIES_DAILY (100 days, for ML)
      └── OpenAI ─────────── gpt-4o-mini       (narrative explanation)
```

| Resource | Identifier | Notes |
|---|---|---|
| AWS account | `899383035514` | **not** the default CLI profile — use `AWS_PROFILE=mypodops` |
| Region | `us-east-2` | |
| API endpoint | `https://lmi4hshs7h.execute-api.us-east-2.amazonaws.com/prod/chatbot` | |
| Web frontend | `http://stock-chatbot-web.s3-website.us-east-2.amazonaws.com` | S3 website hosting, HTTP only |
| Lambda | `stock-chatbot-router` | the only function on the live request path |
| IAM role | `stock-chatbot-lambda-role` | |

### Dead infrastructure (present but unreachable)

`stock-data-service`, `stock-news-service` and `stock-prediction-service` exist
and are `Active`, but they have **no function URL, no API Gateway route, and
are not invoked by the router**. The frontend calls only `/chatbot`. They cost
nothing at rest and have been left in place; nothing depends on them.

`stock-chatbot-predictions` (DynamoDB) is `ACTIVE` with **0 items**. Only the
unreachable prediction service would write to it.

---

## 2. Market-data constraints (important)

The Alpha Vantage key is on the **free tier**, whose real limits are:

| Limit | Value | Consequence |
|---|---|---|
| Burst | **~1 request per second** | back-to-back calls are throttled |
| Daily | **25 requests per day** | ~12 stock queries per day, total |

Throttling arrives as **HTTP 200** with an `"Information"` key instead of the
requested data — so naive code reads it as "no data available".

**A stock query costs 2 requests** (quote + daily series). A general investing
question costs 0.

> Older documents state "5 requests/minute". That figure is wrong and was the
> direct cause of an outage in the ML layer: three calls were issued
> back-to-back, so calls 2 and 3 were always throttled.

### How the router handles this

`_av_request()` in `lambda-micro/chatbot-router/handler.py` is the single entry
point for Alpha Vantage. It:

1. spaces requests at least `_AV_MIN_INTERVAL` (1.2 s) apart;
2. detects throttle replies and retries up to `_AV_MAX_RETRIES` (2) times;
3. raises `AlphaVantageRateLimited` if throttling persists.

The handler distinguishes a rate limit from missing data, so:

* a throttled quote is **never** reported as an invalid ticker;
* a throttled history call reports *"market data temporarily unavailable"*
  rather than blaming the stock;
* responses carry `data_unavailable: true` when data could not be fetched.

### Data freshness

Quotes come from Alpha Vantage `GLOBAL_QUOTE` and carry a
`latest trading day`. Describe the output as **the latest available quote**,
not as tick-level real-time; free-tier freshness during market hours has not
been independently measured (see follow-ups).

---

## 3. Deployment

All artifacts are built by **one** script, used by both the GitHub Actions
workflow and local deploys, so the two cannot drift apart:

```bash
./scripts/build_lambda_package.sh lambda-micro/chatbot-router dist/chatbot.zip
```

It builds in a fresh temp directory (no stale files), ships every required
module, excludes superseded `handler-*.py` backups, and **refuses to emit an
artifact** that is missing a required module or that does not import.

### Deploy the router

```bash
AWS_PROFILE=mypodops aws lambda update-function-code \
  --function-name stock-chatbot-router \
  --zip-file fileb://dist/chatbot.zip --region us-east-2
AWS_PROFILE=mypodops aws lambda wait function-updated \
  --function-name stock-chatbot-router --region us-east-2
```

CI: **Actions → Deploy to AWS → Run workflow** (manual dispatch only), choosing
a service. The workflow runs the unit tests, builds via the shared script, waits
for the update, and smoke-tests the function.

### Why packaging is guarded

`handler.py` does `from ml_agent_lite import get_ml_recommendation`. A
deployment that zips only `handler.py` cold-starts with
`Runtime.ImportModuleError: No module named 'ml_agent_lite'` and every request
fails. Both former deployment paths had exactly that defect. It is now blocked
by the build script and by `tests/test_packaging.py`.

---

## 4. Tests

```bash
python3 -m unittest discover -s tests    # offline, no AWS, no API quota
./test-enhanced-bot.sh --no-stocks       # live API, general queries only (0 quota)
./test-enhanced-bot.sh                   # live API, full (~5 Alpha Vantage requests)
```

| Suite | Covers |
|---|---|
| `tests/test_chatbot_router.py` | throttle recovery, rate-limit vs invalid ticker, quota efficiency, CORS, body parsing, no-fabrication prompt guard |
| `tests/test_packaging.py` | every imported module is packaged; both deploy paths use the shared builder |
| `tests/test_stock_data.py` | **legacy** yfinance code in `shared/`; auto-skips (not deployed) |

Tests assert on response *structure and behaviour*, never on frozen prices or
dates.

---

## 5. Security

* **No credentials belong in this repository.** Both keys live in AWS Secrets
  Manager and are read at runtime.
* The OpenAI key is not present in the repo or its history (verified).
* **The Alpha Vantage key was committed in plaintext** in three Markdown files
  and is present in pushed git history (commits `9a075ed`, `96ade16`,
  `4586d28`). The working tree has been redacted, **but history rewriting does
  not un-publish a pushed secret.**
  **→ That key must be treated as compromised and rotated.** See follow-ups.

---

## 6. Superseded documentation

These predate the current architecture. Kept for history; do not follow them
for operations:

| Document | Why it is stale |
|---|---|
| `README.md`, `README_FINAL.md`, `QUICKSTART.md` | describe the yfinance data layer and a fixed ~10-stock list; production uses Alpha Vantage and supports any ticker |
| `DEPLOYMENT.md` | superseded deployment steps; use §3 above |
| `ALPHA_VANTAGE_ENHANCEMENT_SUMMARY.md`, `ALPHAVANTAGE_INTEGRATION.md`, `QUICK_REFERENCE.md` | contained the live API key (now redacted); state "5 requests/minute", which is wrong |
| `docs/OPENAI_INTEGRATION.md` | references the yfinance architecture |

`shared/` and `lambda-layer/` hold the legacy yfinance implementation. No
deployed Lambda imports them.

---

## 7. Known issues / follow-ups

Not blocking; none of these prevent the application from working.

1. **Rotate the Alpha Vantage key** (security — requires access to the Alpha
   Vantage account) and update the `stock-chatbot/alphavantage-api-key` secret.
   Decide separately whether to rewrite git history.
2. **Cache market data.** At 25 requests/day the app supports ~12 stock queries
   daily. A short-lived cache (per-symbol, a few minutes) would cut Alpha
   Vantage usage roughly in half and remove most throttling. The clearest
   single improvement available.
3. **Measure free-tier quote freshness** during market hours, then make the UI's
   "Real-Time"/"Live" wording match what the data actually is.
4. **Stock-details modal drops the sign on the dollar change** — shows `$9.00`
   where the change is −$9.00 (the percentage is correctly negative).
   Cosmetic, but it is a sign error in a financial figure.
5. **Ticker extraction is capped at 5 characters**, so a 6+ character token is
   never looked up and falls to the general path. The general prompt now
   forbids inventing security specifics, so it no longer fabricates analysis,
   but genuine 6-letter tickers still are not resolved.
6. **HTTPS / custom domain.** The frontend is served over plain HTTP from S3;
   CloudFront + ACM would add TLS.
7. **No authentication or rate limiting on the API**, so the OpenAI and Alpha
   Vantage quotas are publicly consumable. API keys or WAF would bound this.
8. **Remove or wire up the three unreachable Lambdas** and the empty DynamoDB
   table, once a decision is made about prediction tracking.
9. **Delete superseded `handler-*.py` backups** from `lambda-micro/chatbot-router`
   once git history is trusted as the record. The build script already excludes
   them.

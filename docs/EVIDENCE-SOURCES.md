# Evidence Sources

What each source actually provides, verified against the live APIs on
**2026-09-30** with this project's own credentials. Nothing here is
quoted from marketing pages; every capability claim below was observed.

Nothing was purchased. No publisher was scraped.

---

## Summary

| Source | Tier | Key? | Verified | Role |
|---|---|---|---|---|
| SEC EDGAR | A — PRIMARY | no | ✅ working | filings, catalyst classification |
| Alpaca News | B — STRUCTURED_NEWS | yes (have) | ✅ working | timely ticker-tagged news |
| Alpha Vantage | C — AGGREGATED | yes (have) | ⚠️ not adopted | see below |
| Marketaux | C | no key held | ⚠️ not adopted | existing dead Lambda |
| Federal Reserve / BLS / BEA | A | no | ⛔ not implemented | macro calendar |
| FDA / DOJ / FTC | A | no | ⛔ not implemented | regulatory events |

---

## SEC EDGAR — Tier A, implemented

`agent/evidence/providers/sec.py`

**The best source available to this project, and free.** No key, no
account, no rate-limit negotiation. What a company files with the SEC is
what it is legally accountable for having said.

### Verified endpoints

```
GET https://www.sec.gov/files/company_tickers.json
    HTTP 200 · 780 KB · ~1.0 s · 10,431 ticker→CIK entries

GET https://data.sec.gov/submissions/CIK##########.json
    HTTP 200 · 156 KB · ~0.5 s · 1,000 most recent filings, no auth
```

### Access terms

| | |
|---|---|
| Authentication | none |
| Required header | descriptive `User-Agent` **with a contact email**; requests without one are refused |
| Rate limit | 10 requests/second (SEC policy). This client paces at **8/s** |
| Cost | free |
| Historical coverage | 1,000 recent filings inline; older in paged files |
| Licensing | **US government works — public domain.** Filings may be stored, displayed and quoted |
| Full text | yes, available |

### Why it matters most

The submissions payload carries an `items` field for 8-K filings: the
filer's own item numbers. That makes catalyst classification
**deterministic** for the most important filing type — no model needed.

Observed live:

```
NVDA 2026-08-26  items='2.02,9.01'   -> EARNINGS      (2.02 = Results of Operations)
NVDA 2026-08-17  items='1.01,2.03,7.01' -> MATERIAL_AGREEMENT
AAPL 2026-04-20  items='5.02'        -> MANAGEMENT_CHANGE
TSLA 2026-09-29  items='1.01,1.02,2.03,9.01'
```

Form mix observed per issuer (1,000 filings): Form 4 dominates
(452–595), then 8-K (61–127), 10-Q, 144, SC 13G/A, and the financing
forms 424B5 / 424B2 / S-3ASR.

### Caching

Filings are **immutable once published**, so they are cached hard:

| Object | TTL | Why |
|---|---|---|
| ticker→CIK map | 24 h | changes rarely; also indexed in-process |
| submissions list | 1 h | changes when the issuer files |
| a filing | effectively forever | immutable |

---

## Alpaca News — Tier B, implemented

`agent/evidence/providers/news.py`

### Verified live

```
GET https://data.alpaca.markets/v1beta1/news?symbols=NVDA&limit=3
    HTTP 200, items timestamped to the current minute

fields: author, content, created_at, headline, id, images, source,
        summary, symbols, updated_at, url
```

### The licensing fact that shaped the design

**`content` came back EMPTY — 0 characters.**

Our entitlement covers the **headline**, a short **summary** (~119 chars
observed) and the **URL**. Full article text is not returned. That is not
a bug to work around; it is the licence, and it means:

* there is no article body to store even if we wanted one
* `EvidenceItem.summary` holds only the provider's own excerpt
* the URL carries a reader to the publisher, where the article is read
  under the publisher's own terms

`agent/evidence/store.py::_strip_unlicensed` enforces this on write, so
a future provider that *did* start returning bodies could not quietly
fill the table with them.

### Multi-symbol tagging

Articles are tagged with **every** ticker they mention. One observed item
carried nine:

```
"Top 5 Semiconductor Stocks to Own Into Year End: Bank of America"
symbols: ADI AMAT AMD INTC LRCX MRVL MU NVDA ON
```

A sector round-up is not nine company-specific catalysts. Two controls
handle this — a breadth discount above five symbols, and the subject
relevance check in `agent/evidence/relevance.py`. See *Issues found*.

| | |
|---|---|
| Authentication | Alpaca key/secret, in headers (never in a URL) |
| Publisher observed | Benzinga |
| Real-time | yes |
| Rate limit | not documented on this tier; 429 is handled as `EvidenceRateLimited` |
| Historical | paged via `next_page_token` |
| Cost | included in the existing account |

---

## Alpha Vantage — Tier C, deliberately NOT adopted for evidence

The account holds a key, and `NEWS_SENTIMENT`, `EARNINGS_CALENDAR`,
`OVERVIEW` and `EARNINGS` exist on the free tier. They are **not** wired
into this milestone, for measured reasons:

1. **The free quota is 25 requests/day.** That is the whole budget for
   the whole system, and the price path already competes for it.
   Verified previously: `TIME_SERIES_INTRADAY` answers *"This is a
   premium endpoint"* on this key.
2. Alpaca News already supplies timelier, ticker-tagged news at no extra
   quota cost.
3. Its news sentiment is a Tier C aggregation — a score derived from
   other people's articles — which is the weakest provenance in the
   hierarchy.

**Appropriate future role**, on a daily or event-driven schedule with
aggressive caching: `EARNINGS_CALENDAR` (upcoming earnings dates) and
`OVERVIEW` (fundamentals). Both are low-frequency by nature.

**It must not return to the high-frequency path.** That was the original
defect this project was recovered from.

---

## Marketaux — existing dead code, not adopted

`lambda-micro/news-fetcher/` calls Marketaux and is deployed as
`stock-news-service`, but has **no function URL, no API Gateway route,
and is invoked by nothing**. It returns bare headlines and discards the
per-entity sentiment Marketaux does supply.

Free tier is ~100 calls/day. Alpaca News is better on every axis we need
and is already authenticated, so Marketaux was **not** revived. Reviving
a Lambda because it exists is not a reason.

---

## Not implemented

Named so the gaps are explicit rather than silently absent.

| Source | Would provide | Why not yet |
|---|---|---|
| Federal Reserve (FRED) | FOMC dates, rates | macro calendar is a separate build |
| BLS | CPI, PPI, payrolls, unemployment | same |
| BEA | GDP, retail sales | same |
| FDA | approvals, rejections, trial holds | no structured feed integrated |
| DOJ / FTC | enforcement, merger review | press-release scraping would be needed |
| Company IR feeds | issuer press releases, first-hand | no reliable *structured* feed across issuers; licensed news carrying the release is the safer source, and an 8-K usually carries it too |

The economic-event abstraction is modelled (`EvidenceType.FOMC`, `CPI`,
`PPI`, `PAYROLLS`, `UNEMPLOYMENT`, `GDP`, `RETAIL_SALES`) but has **no
provider behind it**. Consensus estimates are deliberately absent:
fabricating a consensus from unavailable data would be worse than having
none.

---

## Source hierarchy

```
Tier A  PRIMARY           reliability 1.00
        SEC filings, issuer press releases, Fed/BLS/BEA/FDA/DOJ/FTC

Tier B  STRUCTURED_NEWS   reliability 0.75
        licensed news feeds (Alpaca News today)

Tier C  AGGREGATED        reliability 0.50
        sentiment aggregators, provider summaries

        UNKNOWN           reliability 0.25
```

### Reliability means provenance, not interpretation

> Source reliability describes how confidently we can say the source
> **actually stated this**, not whether the interpretation is correct.

A company press release is a completely reliable record of what the
company announced, and is also written to present it favourably. Both
are true at once, and the model keeps them apart:
`SOURCE_RELIABILITY` scores the first; nothing in this system scores the
second. The caveat ships on every API response as
`source_reliability_caveat`.

---

## Content and copyright policy

**Never stored:** full article text, article bodies, or any field named
`content` / `body` / `article_text` / `full_text`.

**Stored:** headline (capped 400 chars), publisher, canonical URL, the
provider's own short excerpt (capped 1,200 chars), structured extraction
with source quotes, generated summaries, and classification metadata.

Enforcement is in `store.py::_strip_unlicensed`, applied on every write —
the policy is not a request made of providers, it is a filter on the way
into storage. `tests/test_evidence_service.py` proves an article body
supplied by a provider does not reach the table.

SEC and other US government documents are public domain and are handled
under their own terms.

---

## Issues found during integration

**A sector round-up was being reported as a company catalyst.** Asking
for AAPL returned *"Micron's AI Boom Isn't Done yet, Analysts Say"* —
tagged with AAPL, about Micron — and it became Apple's primary catalyst.
Fixed by `relevance.py`: a headline that names a different tagged company
while never naming ours is discounted to 0.30. NVDA then correctly
reported **no active catalyst** rather than a Micron story.

**A falling stock was reported as a positive catalyst.** *"Why Is Intel
Stock Falling on Monday?"* classified POSITIVE, because a keyword in the
summary matched while the headline said the opposite. Fixed with a
price-action sanity check: when the headline's plain sense contradicts
the keyword reading, direction is reported UNCERTAIN.

**Nothing was cached.** The cache backend's method is `put`; the first
implementation called `set`, and a `except Exception: pass` guard
swallowed the `AttributeError`. The 780 KB ticker map was re-downloaded
on every lookup — a 4-second run took 33 seconds with no error anywhere.
Fixed, and the guard now logs instead of hiding.

---

## Security

No credential appears in source. Alpaca keys are read from Secrets
Manager and passed in **headers**, never in a URL, because a query
string reaches access logs and error messages. The OpenAI key is read
from Secrets Manager at use time and is never logged. SEC requires no
credential at all.

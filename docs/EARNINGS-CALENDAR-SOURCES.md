# A forward earnings calendar: sources

Researched 2026-10-01. **Nothing purchased, nothing wired, no account
created.** This replaces `docs/progress/EARNINGS-CALENDAR-OPTIONS.md` as
the canonical comparison; that file remains as the record of why the
question came up.

## What is missing

`GET /agent/company/{symbol}/earnings` returns SEC-reported actuals and,
deliberately, nothing else:

```json
"next_earnings_date": null,
"next_earnings_note": "no earnings-calendar source configured",
"estimates": "UNAVAILABLE - no consensus-estimate source is configured;
              estimates are never inferred"
```

Two separate gaps: the **next report date**, and **consensus estimates**.
Without the second there is no beat, no miss and no surprise — so the
beat/miss sequence is `UNKNOWN` for every company, and
`agent/company/earnings.py` keeps `REPORTED_VALUE`,
`CONSENSUS_ESTIMATE` and `DERIVED_SURPRISE` apart rather than filling the
hole.

Neither gap blocks the intraday strategy. Both are prerequisites for any
holding strategy (`docs/HOLDING-STRATEGY-INPUTS.md`), and the next report
date is needed before the agent can avoid holding into a release.

## Sources already available to this project

### SEC EDGAR — cannot answer it, and that is structural

Already wired, highest provenance, free. Backward-looking by
construction: an 8-K Item 2.02 announces results that have *happened*. A
company's intention to report on a future date is usually a press
release, not a filing. Consensus estimates are analyst output and never
appear in filings at all.

### Alpaca — corporate actions, not earnings

Verified against the live payload today: the corporate-actions buckets
are cash and stock dividends, splits, mergers, spin-offs, name and symbol
changes, redemptions and rights distributions. Alpaca's own
[Corporate Actions API announcements](https://alpaca.markets/learn/corporate-actions-api-announcements/)
describes exactly that scope — "previous and upcoming dividends, mergers,
spinoffs, and stock splits" — with history back to April 2020. **No
earnings calendar and no estimates.**

Worth noting: it does carry *upcoming* dividends, which is what makes
`next_ex_date` possible now that the fetch window reaches forward.

## Comparison

Rate limits and prices below come from current vendor documentation and
comparison write-ups (sources at the end). Treat them as **documented,
not verified against an account** — only the Alpaca and SEC rows were
exercised directly today. Confirm against the vendor before relying on a
number.

| Source | API | Earnings date | Report time (BMO/AMC) | Consensus EPS | Consensus revenue | Free tier | Paid from | Licensing note |
|---|---|---|---|---|---|---|---|---|
| **SEC EDGAR** | XBRL / submissions | ✗ (after the fact) | ✗ | ✗ | ✗ | unlimited, free | n/a | Public domain filings; already wired |
| **Alpaca** | corporate actions | ✗ | ✗ | ✗ | ✗ | included in current plan | n/a | Already entitled; wrong data type |
| **Alpha Vantage** | `EARNINGS`, `EARNINGS_CALENDAR` | ✓ | ✓ where supplied | ✓ (`estimatedEPS`) | limited | **25 req/day** | ~$29.99/mo (75 req/min) | **Excluded for Company Intelligence**: key is production's, enforced by test |
| **Finnhub** | earnings calendar | ✓ | ✓ | ✓ on free tier; advanced estimates premium | premium | 60 req/min | paid tiers vary | **Preferred candidate**, pending licensing review |
| **Financial Modeling Prep** | earnings calendar, analyst estimates | ✓ | ✓ | ✓ | ✓ | 250 req/day | paid tiers vary | **Off the shortlist** unless a paid plan is chosen deliberately |
| **Eulerpool** | earnings calendar, estimates, filings | ✓ | ✓ | ✓ | ✓ | 100,000 req/month | paid raises volume / real-time | New vendor, least familiar to this project |

### Why Alpha Vantage is the obvious candidate and still blocked

`agent/company/earnings.py` already has `from_alpha_vantage()`, written
to map `reportedEPS` to the actual and `estimatedEPS` to the estimate
**by name, never by position**, treating the string `"None"` as missing
rather than zero. It is tested and unused.

The obstacle is not the endpoint, it is the key: 25 requests/day on the
free tier, and `stock-chatbot/alphavantage-api-key` belongs to the
**production chatbot**, which spends that same budget serving user
queries. The bulk calendar is about one request a day — negligible in
isolation, and drawn from production's allowance. A quota exhausted by
the agent would surface as a production outage.

That is why the agent calls Alpha Vantage not at all today.

### Why FMP or Finnhub might be better anyway

Both have materially larger free tiers (250/day and 60/min against
25/day) and both publish an earnings calendar *with* estimates on the
free tier. Either would avoid touching production's quota entirely. The
cost is a new account and a licensing read: a free tier that forbids
storing or redistributing estimates matters here, because this project
*persists* what it fetches and shows it on a dashboard.

## DECISION TAKEN 2026-10-01

Directed by the account holder:

- **No provider is wired.** Upcoming earnings stays `UNKNOWN` until a
  dedicated provider is deliberately selected. An unknown date is a fact;
  a date inferred from a filing cadence is a guess wearing a fact's
  clothes.
- **Alpha Vantage is not to be used for Company Intelligence at all**,
  because its key is production's and its free tier is 25 requests a day.
  This is now enforced rather than remembered: `agent/company/` may not
  import or construct `AlphaVantageProvider`, and
  `tests/test_company_quota_boundary.py` asserts it. `from_alpha_vantage()`
  stays as a pure normaliser over a payload someone else fetched — it has
  no way to fetch anything.
- **Finnhub is the preferred next candidate**, pending a licensing and
  terms review (see below). Nothing is signed up for.
- **FMP is off the shortlist** unless a paid plan is intentionally
  selected.
- Sequencing: finish Company Intelligence and the real-time paper
  cutover first.

### What the Finnhub review has to answer

Not "does it have the data" — it does. The questions that decide it:

1. **Persistence.** This project stores what it fetches, with
   provenance, in DynamoDB. Does the free tier permit storing estimates
   rather than only displaying them transiently?
2. **Display.** The dev dashboard renders the data. Is that
   "redistribution" under their terms?
3. **Derived values.** The agent computes a surprise from actual minus
   estimate and shows it. Is a derived figure covered?
4. **Attribution.** Is a visible credit required where the data appears?
5. **Commercial use.** The project is personal today. A free tier
   restricted to non-commercial use would constrain what this could ever
   become, which is worth knowing before building on it.
6. **Report time.** Is BMO/AMC actually populated, or frequently null?
   A calendar without the time cannot support an earnings-blackout rule
   that must act before a release.

Until those are answered, (a) below is in force.

## USER ACTION REQUIRED — pick one

- **(a) Do nothing.** Earnings dates stay `null`, beat/miss stays
  `UNKNOWN`. Honest, and the intraday strategy does not need them. In
  force by default.
- **(b) Second free Alpha Vantage key for the agent** — free, separate
  account, no payment. Stored as a new secret so production's budget is
  untouched. Smallest change, uses code that already exists and is
  tested.
- **(c) Finnhub free tier — the preferred candidate.** 60 requests a
  minute, a calendar with estimates, and no contact with production's
  quota. Needs a new account and the licensing review above.
- **(d) A paid plan**, if per-symbol estimate coverage matters more than
  any free tier allows — this is the only route by which FMP returns to
  the shortlist. A purchase and an account agreement: a stop condition,
  not something this system will arrange.

## If any of (b) to (d) is chosen

Verification before trusting any of it — the same discipline that found
three SEC normalisation defects on 2026-10-01:

1. Confirm the calendar's actual field names and horizon against a live
   response, not documentation.
2. Record **which** consensus it is, with a retrieval timestamp. An
   estimate without provenance is a number with an opinion attached.
3. Keep the separation: a missing estimate stays `None`, and the
   `invent-a-missing-estimate` mutation in
   `scripts/falsifying_controls.py` must keep failing the suite.
4. Treat a calendar date as a **relayed claim**, not a measurement.
   Companies move report dates; re-read rather than cache a date as fact.
5. Check the licence against what this project does with the data:
   persist it, display it, and derive a surprise from it.

## Explicitly excluded

Scraping Yahoo Finance, or any site's HTML, for dates or estimates. The
application must not depend on it. Yahoo's layout remains useful only as
a manual reference for what a company page should *show*.

Sources: [Alpaca corporate actions announcements](https://alpaca.markets/learn/corporate-actions-api-announcements/),
[free stock API comparison](https://qveris.ai/guides/stock-api-free-comparison/),
[earnings calendar API overview](https://eulerpool.com/blog/earnings-calendar-api-2),
[Yahoo Finance API alternatives](https://daytradingz.com/yahoo-finance-api-alternatives/).

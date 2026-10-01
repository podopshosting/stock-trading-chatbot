# A forward earnings calendar: what the project could use

Opened 2026-10-01. **Needs a decision; nothing has been wired.**

## What is missing and why it matters

`GET /agent/company/{symbol}/earnings` returns reported actuals from SEC
XBRL and, deliberately, nothing else:

```json
"next_earnings_date": null,
"next_earnings_note": "no earnings-calendar source configured",
"estimates": "UNAVAILABLE - no consensus-estimate source is configured;
              estimates are never inferred"
```

Two separate gaps sit behind that:

1. **The next report date.** Needed to avoid holding into a release, and
   to raise an upcoming-earnings caution with a real number instead of
   `None` (`HoldingContext` has the factor; it currently never fires).
2. **Consensus estimates.** Without them there is no beat, no miss and no
   surprise. `agent/company/earnings.py` already keeps
   `REPORTED_VALUE`, `CONSENSUS_ESTIMATE` and `DERIVED_SURPRISE` apart
   and reports `UNKNOWN` rather than inventing the missing side; the
   beat/miss sequence is `UNKNOWN` for every company today.

Neither is urgent for the intraday strategy. Both are prerequisites for
any holding strategy (`docs/HOLDING-STRATEGY-INPUTS.md`).

## Sources already available to this project

### SEC EDGAR — cannot answer it

SEC is the project's highest-provenance source and is already wired for
filings and XBRL. It is backward-looking by construction: an 8-K Item
2.02 announces results that have *happened*. A company's intention to
report on a future date is usually a press release, not a filing. SEC
can confirm a report after the fact and cannot schedule one.

Consensus estimates are analyst output and never appear in SEC filings
at all.

### Alpaca — does not provide it

Alpaca's market-data API covers quotes, bars, trades, news and corporate
actions. Corporate actions is the endpoint this project already uses for
dividends and splits, and its action types do not include earnings dates
or estimates. Verified against the live payload: the buckets returned are
cash/stock dividends, splits, mergers, spin-offs, name and symbol
changes, redemptions and rights distributions.

### Alpha Vantage — the realistic candidate, with a quota problem

Alpha Vantage exposes both pieces:

- a bulk earnings calendar (whole market, horizon of several months),
  which is **one request** rather than one per symbol
- per-symbol quarterly history carrying both the reported and the
  estimated EPS — the consensus side this project is missing

`agent/company/earnings.py` already has `from_alpha_vantage()`, written
to map `reportedEPS` to the actual and `estimatedEPS` to the estimate
**by name, never by position**, treating the string `"None"` as missing
rather than zero. It is tested and unused: nothing calls it, because no
key is configured for the agent.

**The constraint is the key, not the endpoint.** The free tier allows
**25 requests per day**, and the key held in
`stock-chatbot/alphavantage-api-key` belongs to the **production
chatbot**, which spends that same budget serving user queries. A bulk
calendar pull is about one request a day, which is negligible in
isolation — but it would be drawn from production's allowance, and a
quota exhausted by the agent would show up as a production outage.

That is why the agent currently calls Alpha Vantage not at all.

## USER ACTION REQUIRED — pick one

- **(a) Do nothing.** Earnings dates stay `null` and beat/miss stays
  `UNKNOWN`. Honest, and the intraday strategy does not need them. This
  is in force by default.
- **(b) Provision a second free Alpha Vantage key for the agent**
  (free, separate account, no payment). Store it as a new secret — e.g.
  `stock-agent/alphavantage-api-key` — so production's budget is
  untouched. One daily bulk calendar pull plus a handful of per-symbol
  estimate pulls fits inside 25/day for a universe of this size.
- **(c) Authorise a paid plan** if per-symbol estimate coverage matters
  more than the daily cap allows. A purchase and an account agreement:
  a stop condition, not something this system will arrange.
- **(d) Name a different vendor.** Several (Finnhub, Financial Modeling
  Prep, Polygon, Nasdaq Data Link) publish earnings calendars on free or
  paid tiers. Each needs a new account and, for anything redistributed,
  a licence check. None has been evaluated in detail because (b) uses
  infrastructure the project already understands.

## If (b) or (c) is chosen

Verification required before trusting any of it — the same discipline
that found three SEC normalisation defects on 2026-10-01:

1. Confirm the bulk calendar's actual field names and horizon against a
   live response, not documentation.
2. Confirm that the per-symbol estimate is a consensus and record which
   consensus it is, with a retrieval timestamp. An estimate without
   provenance is a number with an opinion attached.
3. Keep the existing separation: a missing estimate stays `None`, and the
   `invent-a-missing-estimate` mutation in
   `scripts/falsifying_controls.py` must keep failing the suite.
4. Treat a calendar date as a **relayed claim**, not a measurement. A
   company can move its report date, and the agent should re-read the
   calendar rather than cache a date as fact.

## Explicitly excluded

Scraping Yahoo Finance, or any other site's HTML, for earnings dates or
estimates. The application must not depend on it. Yahoo's layout remains
useful only as a reference for what a company page should *show*.

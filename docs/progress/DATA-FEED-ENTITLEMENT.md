# Market-data entitlement and what the paper evidence can prove

Opened 2026-10-01. **Contains a decision only the account holder can make.**

## The finding

The agent's paper fills are computed against quotes that are about
**15 minutes old**. This is a property of the data plan, not a bug.

Verified today, 2026-10-01 14:39 UTC, from the agent's own output
(`GET /agent/analysis?symbol=MSFT`):

```
data_quality.is_delayed = true
```

The configured feeds are the `AlpacaProvider` defaults; nothing overrides
them:

| Use | Feed | Meaning |
|---|---|---|
| Quotes (entries, exits, stop checks) | `delayed_sip` | consolidated tape, 15 minutes late |
| Bars (indicators) | `sip` | consolidated tape, windowed to end >15 min ago |

Why this matters more than it looks: the quote-age control
(`max_quote_age_seconds = 120`) measures **how long ago we fetched the
quote**, not how old the market data inside it is. A quote fetched 3
seconds ago can describe the market as it was 15 minutes ago and will
pass that check. The guard is working as written; it was never measuring
what the name suggests.

So the current record supports one claim and not another:

- **Supported:** the machinery runs — scheduler, scan, signals,
  hypotheses, Risk Governor, order placement, position management,
  reconciliation, EOD flatten.
- **Not supported:** that the strategy would have obtained these fills,
  or this P&L, in the live market.

This is now enforced rather than remembered. Each cycle records the feed
quality it used; each session carries an evidence class; and
`demonstrated_edge`, `stops_hold` and `strength_is_predictive` refuse to
be met by anything that is not real-time, single-runtime evidence. An
unrecorded feed reads as `UNKNOWN`, never as real-time.

```
REAL_TIME_STRATEGY_EVIDENCE   may support a performance gate
OPERATIONAL_VALIDATION_ONLY   proves the machinery runs, nothing more
VOID                          cannot be attributed to one runtime
```

Current classification of the live record: **OPERATIONAL_VALIDATION_ONLY**.

## The options

Established from `docs/TRADING-DATA-SOURCES.md`, which records live
verification against this account on 2026-09-30 (a request for recent SIP
data returned 403 *"subscription does not permit querying recent SIP
data"* — the free-plan boundary).

### 1. Keep `delayed_sip` — no action, no cost

Continue accumulating operational evidence, labelled
`OPERATIONAL_VALIDATION_ONLY`. Scheduler reliability, reconciliation,
exit handling and EOD flatten are all genuinely exercised. Strategy
performance stays undemonstrated.

### 2. Switch to `feed=iex` — no cost, but a different distortion

IEX is real-time on the free plan. It is also roughly **2.5% of US
volume**, so its best bid/offer is not the national one. Spreads read
wider and thinner than the consolidated market, which would change what
the 0.5% spread limit admits and would make fills optimistic or
pessimistic in ways that are hard to characterise. Real-time, but still
not representative. Not recommended as a substitute for a consolidated
feed, and it would need its own evidence label rather than being treated
as equivalent to real-time SIP.

### 3. Upgrade to real-time SIP — **requires the account holder**

Alpaca **Algo Trader Plus, $99/month**: real-time full SIP, no 15-minute
restriction, 10,000 requests/minute, unlimited WebSocket symbols.

This is a paid subscription and an account agreement. It is a
`STOP CONDITION`: this system will not purchase, enable or change a
subscription.

## USER ACTION REQUIRED

Pick one:

- **(a) Accept delayed data for now.** Nothing to do. Cohorts continue to
  be labelled `OPERATIONAL_VALIDATION_ONLY` and no performance gate can
  be satisfied from them. The readiness report already says so.
- **(b) Authorise the $99/month Alpaca Algo Trader Plus subscription**
  (your purchase, your account). Afterwards, tell this project it is
  active; the feed constant changes to `sip` for quotes and the next
  cohort will classify as `REAL_TIME_STRATEGY_EVIDENCE`.
- **(c) Direct a different real-time source.** Say which, and whether its
  licence permits this use.

Until one of these is chosen, option (a) is in force by default, because
it is the only one that requires nothing and claims nothing.

## Related, unchanged

The quote-age guard keeps its current meaning (freshness of *our fetch*)
and is not being redefined mid-cohort. If real-time data is authorised, a
true data-age check becomes possible using the provider's own `as_of`
timestamp, and should be added then.

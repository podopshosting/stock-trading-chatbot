# FIDELITY_AUTOMATED_EXECUTION_UNAVAILABLE

**Finding:** Fidelity does not offer a supported retail API capable of
placing securities orders. Automated execution through Fidelity is
therefore unavailable to this project.

Researched 2026-09-30.

## What was checked

| Path | Status |
|---|---|
| Public retail trading API from Fidelity | **Does not exist.** Fidelity's public position is that no public retail API is offered; API access is directed at institutional clients. |
| Fidelity developer / Fidelity Access | **Read-only.** Available to approved third-party integrators for positions and balances. Does not place trades. |
| SnapTrade (approved Fidelity integrator) | **Read-only.** SnapTrade's Fidelity integration explicitly does not support placing trades, unlike its integrations with several other brokers. |
| Institutional FIX / direct market access | Exists for hedge funds, proprietary trading firms and large family offices. Not available to a retail account, and out of scope. |
| Third-party browser automation (e.g. `fidelity-api` on PyPI / GitHub) | **Excluded by instruction and by judgement.** See below. |

## Why browser automation is excluded

A Playwright-based package exists that drives the Fidelity website to
place orders. It is the only programmatic path that reaches order entry,
and it is not being used.

The project instruction is explicit: do not screen scrape Fidelity, do
not automate its website, do not reverse engineer private endpoints, do
not bypass authentication restrictions.

That instruction also happens to be the correct engineering call, for
reasons worth recording rather than leaving as deference:

- **It is unauthorised.** Driving a logged-in session programmatically
  is outside the terms a retail account is granted under. The risk is
  not a rejected order, it is a closed account.
- **It has no contract.** A private web flow can change without notice.
  An automation that silently stops working is survivable; one that
  silently half-works — submitting without confirming, or confirming
  the wrong field — is not.
- **It cannot satisfy the live contract.** Several requirements in
  `agent/broker/live_contract.py` are impossible through a scraped UI:
  there is no reliable broker-side honouring of a client order id, no
  dependable order-status stream, and no way to distinguish "the cancel
  was rejected because it already filled" from "the page changed".
  Without broker-side duplicate protection, a retried submission can
  double a position, which is the single most expensive failure this
  system is built to prevent.

## Consequence for the project

The trading agent has a complete decision pipeline, a realistic paper
broker, and a running paper pilot. It has **no route to real-money
execution at Fidelity**, and none can be built without violating either
the instruction or the live contract.

Any real-money execution would require a different venue.

## Brokers that do offer a supported retail execution API

Recorded for completeness. **None of these has been set up, and setting
any of them up is outside what may be done autonomously** — each
requires opening or authorising an account, accepting brokerage
agreements, supplying credentials, identity verification, and in some
cases market-data agreements. Those are account actions for the account
holder alone.

| Broker | Retail order API | Notes |
|---|---|---|
| Alpaca | Yes | Already the market-data provider for this project; its trading API is separate from the data API used here. Offers a paper endpoint. |
| Interactive Brokers | Yes | Client Portal API and TWS API. Most capable, most complex. |
| Tradier | Yes | REST, brokerage account required. |
| Schwab | Yes | Developer portal with trading scopes, application approval required. |

This project's market-data credentials for Alpaca grant **data access
only** and are not trading credentials; data access is not an execution
capability and no code here treats it as one.

## What is in place instead

`agent/broker/live_contract.py` defines the sixteen requirements a live
adapter must satisfy beyond the `BrokerAdapter` shape — asynchronous
fills, authoritative state reads, halted symbols, pattern day trader
rules, settlement, broker-side duplicate protection, a kill switch that
cancels working orders, and the rest — each with a written rationale
describing what goes wrong without it.

`AdapterAssessment.ready_for_real_money` is a derived property with no
setter and no partial credit. A paper adapter can never satisfy it, and
a declared capability is recorded as declared rather than counted as
verified.

So the boundary is drawn and documented, and the decision logic is
unchanged by which venue sits behind it. What is absent is a venue —
and the absence is the finding, not an oversight.

## Status

```
FIDELITY_AUTOMATED_EXECUTION_UNAVAILABLE
```

No further work on a Fidelity execution adapter is possible or
appropriate.

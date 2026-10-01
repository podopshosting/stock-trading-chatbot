# Milestone 17 — Broker Execution Adapter

Status: complete, with the outcome
`FIDELITY_AUTOMATED_EXECUTION_UNAVAILABLE`.
27 live-contract tests.

## The finding

Fidelity does not offer a supported retail API capable of placing
securities orders. Two independent checks confirmed it: Fidelity's
public position directs API access to institutional clients, and the
read-only developer access that exists for approved integrators (SnapTrade)
explicitly cannot place trades.

Full research record, including the brokers that *do* offer retail
execution APIs: [FIDELITY_AUTOMATED_EXECUTION_UNAVAILABLE.md](../FIDELITY_AUTOMATED_EXECUTION_UNAVAILABLE.md)

A Playwright-based package exists that drives the Fidelity website. It
is excluded by instruction, and the instruction is also the correct
call: it is unauthorised, it has no contract so it can silently
half-work, and it cannot provide broker-side duplicate protection —
without which a retried submission can double a position, the single
most expensive failure this system is built to prevent.

No account was opened and no credentials were supplied. The project's
existing Alpaca credentials grant **data access only**; data access is
not an execution capability and no code treats it as one.

## What was built instead

`agent/broker/live_contract.py` draws the boundary. The
`BrokerAdapter` protocol from Milestone 9 describes the *shape* of a
broker, and the paper broker satisfies it completely while being unable
to lose a cent — so conformance is not evidence of live readiness.

Sixteen requirements, each with a written rationale describing what goes
wrong without it:

- **Order lifecycle** — asynchronous fills (a real submission returns an
  acknowledgement, not a fill, so code written against the paper broker
  will record positions that do not yet exist), status polling, cancel
  confirmation, partial-then-reject
- **Truth about state** — authoritative position and cash reads,
  unreachable broker halts, and duplicate protection **honoured by the
  broker** rather than by us, since our own map is lost if our state is
- **Market conditions the simulator ignores** — halted symbols,
  auctions and locked markets, pattern day trader rules (which an
  intraday strategy under $25,000 makes likely rather than incidental),
  settlement and good-faith violations
- **Operational** — credential rotation, rate-limit backoff, a full
  request audit trail, and a kill switch that cancels **working**
  orders, which today's does not

`AdapterAssessment.ready_for_real_money` is derived with no setter and
no partial credit. A paper adapter can never satisfy it even while
declaring every requirement, and a declared capability is recorded as
declared rather than counted as verified.

A test also asserts that no module under `agent/` references
`fidelity.com` or `playwright`, so the excluded path cannot reappear
quietly.

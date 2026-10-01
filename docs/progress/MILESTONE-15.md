# Milestone 15 — Live Paper-Trading Pilot

Status: deployed and scheduled. 35 persistence tests, 18 integration
tests, 1,201 total.

The pilot runs `stock-agent-dev-cycle` every 5 minutes during US market
hours. It was enabled while the market was closed, so the first live
cycle is at the next open.

## What this milestone actually required

The orchestrator from Milestone 13 could run a cycle. It could not run
a *pilot*, because `PaperBroker` and `PositionManager` both held state
in memory and Lambda is stateless. Every invocation would have started
with full cash and no positions — producing a stream of unrelated
one-cycle experiments while looking like a continuous record.

So most of this milestone is persistence, and the failures worth
guarding against are all silent:

| Failure | Consequence |
|---|---|
| Cash resets to starting value | The agent appears to have unlimited money |
| Reserved cash is released | The same dollar funds two positions |
| A position loads without its exit plan | An unbounded loss with no stop |
| The high-water mark is lost | The trailing stop silently un-ratchets |
| An unreadable store returns "empty" | The agent believes it holds nothing while the broker holds real positions |
| A client order id survives but its order does not | A retry after a cold start places a second order |

Every one of those is now a test.

## Fail closed, in the right direction

`BrokerStateStore.load()` and `PositionStore.load_open()` **raise** on a
read error rather than returning empty. An unreadable account is not an
empty account: handing back a fresh broker with full starting cash would
look like a clean slate while real positions sat unmanaged.

`from_item()` refuses to reconstruct a position with no stored stop
price. A position without a stop is an unbounded loss, and defaulting
one would apply a number nobody chose to real exposure.

It also refuses a stop **wider** than the position's own recorded
history. If storage were rolled back or edited, restoring the older
wider stop would quietly increase risk on a live position — the one
thing `tighten_stop` exists to make impossible.

## Optimistic concurrency

The broker state store uses a revision guard, so two cycles that both
load, both decide and both save cannot silently lose one set of fills.
Combined with the Milestone 13 cycle lock this is belt and braces, and
deliberately so: the lock prevents the overlap, and the revision guard
catches the case where the lock itself was wrong.

## Defects found

**Persisting only open orders broke idempotency across a restart.** My
first design kept the full client-id map but only the open orders, so
after a cold start a duplicate submission looked up a client id, found
an order id, and crashed on a `KeyError`. Idempotency has to *return*
the existing order, which means the order must still exist. Now all
orders in a session are persisted — bounded in practice, since the risk
limits cap new positions at a few per day.

`submit_order` also now fails closed if the map is ever inconsistent:
if a client id is known but its order is missing, it **refuses** rather
than placing, because placing would do precisely what the client id
exists to prevent.

**An exact float comparison would have halted the agent on a good
position.** `stop_history` rounds `to` to six places while
`plan.stop_price` keeps the raw float, so a trailing stop at
`116.39999999999999` read as "wider" than its own recorded `116.4`. The
loosened-stop guard fired on floating-point dust. Found by the
double-round-trip test, which exists because a lossy field often
survives one pass and drifts on the second. Now compared with a
tolerance of 1e-4 — far below any meaningful price move, far above
six-decimal rounding — with a test proving a *real* loosening is still
refused.

**One of my integration tests was vacuous.** The trailing-ratchet test
collected stops only `if open_positions`, and its hardcoded prices went
straight through the profit target, closing the position after one
observation. `[101.92] == sorted([101.92])` passed while testing
nothing. Now the price path is derived from the position's **own**
target, and the test asserts it collected four observations with more
than one distinct value — so it cannot pass by never exercising the
ratchet, and it will not break again when the stop distance changes.

**Two more guessed constructors.** `agent.providers.credentials` does
not exist; the credential loader lives inline in the API handler. And
`DynamoDBScannerStore` uses `latest_run()`. Both verified against the
real signatures this time rather than assumed — the same discipline
Milestone 14 had to learn.

## Safety posture of the running pilot

- **Paper only, asserted not configured.** `IS_LIVE = False` is a module
  constant, the only broker imported is `PaperBroker`, and it is
  constructed directly rather than selected by configuration. No
  environment variable can point it at a real brokerage.
- `AGENT_TRADING_ENABLED` and `AGENT_EXECUTION_AVAILABLE` are **true on
  the cycle Lambda only**. The read API still has both unset, and no
  broker adapter at all.
- Phase gating means entries happen only in `INTRADAY`; the market
  being unreachable short-circuits the cycle entirely.
- The pre-close cycle flattens, so no position is carried overnight.
- `$100` paper cash, `$50` daily capital ceiling, 2 concurrent
  positions, 3 new positions per day.

## Known limitation

**The live-data path has not been exercised under live market
conditions.** The market was closed throughout this milestone, so every
cycle so far has correctly returned `phase=CLOSED` and skipped. The
composition is covered by 18 integration tests that discard in-memory
state between cycles exactly as Lambda does, but integration tests use
synthetic quotes.

Specifically unverified: `_quote_loader` and `_hypothesis_loader`
against real Alpaca responses inside a cycle, and the scanner-candidate
read. This goes to the Milestone 18 readiness gate rather than being
left implicit.

A 5-minute cycle also means a polled stop is checked every 5 minutes.
`stop_gap_disclosure()` already states what that does and does not
promise; the pilot inherits it.

## Files

```
agent/broker/store.py               paper account persistence
agent/positions/store.py            managed position persistence
lambda-micro/agent-cycle/handler.py the pilot cycle
tests/test_persistence.py           35 tests
tests/test_pilot_integration.py     18 tests, state discarded per cycle
```

## AWS resources created (dev only)

```
Lambda    stock-agent-dev-cycle
DynamoDB  stock-agent-dev-broker, -positions, -journal
Rule      stock-agent-dev-pilot-cycle  cron(*/5 13-20 ? * MON-FRI *)
```

Production `/chatbot` untouched; the RSI fix remains undeployed.

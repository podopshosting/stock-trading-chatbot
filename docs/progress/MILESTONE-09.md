# Milestone 9 — Paper Broker & Order Management

Status: complete. 57 broker tests, 852 total, 42/42 mutations caught.

## What this is for

Every claim this system will eventually make about whether the strategy
works rests on the paper record. If the simulation is optimistic, the
paper record is worse than no record at all, because it will be compared
with live results and the gap will be blamed on the market rather than on
the simulator.

So the design goal here was not "make orders work". It was: **make the
simulation cost what trading costs.**

## The costs that are modelled

| Cost | How | Why it matters |
|---|---|---|
| Spread | A buy lifts the ask, a sell hits the bid | Filling at the mid hands the strategy half the spread on entry and half on exit. At three round trips a day that is larger than most edges. |
| Slippage | `slippage_bps`, always against the trader | Size moves price. A simulation without it rewards oversizing. |
| Partial fills | Probabilistic, configurable | A strategy that assumes complete fills will size positions it cannot actually get. |
| Cash reservation | Working buy orders reserve notional | Two orders must not be sized against the same dollar — the classic way a paper broker flatters itself. |

The load-bearing test is `test_a_flat_round_trip_loses_money`: buying and
selling at an unchanged price **must** lose money. A simulator where that
breaks even is lying, and the failure would be invisible in aggregate
performance numbers.

## Deliberate omissions

**No market orders.** `OrderType` offers only `LIMIT` and
`MARKETABLE_LIMIT`. A market order is an instruction to accept any
price, and this system's entire risk model rests on knowing the worst
case *before* committing. A marketable limit buys the same immediacy
with a bound on how bad the fill may be.

**No shorting, no margin.** Selling a name that is not held is refused as
`SHORTING_NOT_PERMITTED`, whatever it is called at the call site. Buying
power never exceeds cash.

## The execution gate

`submit_approved` is the only path from a risk decision to an order. It
checks four things separately and explicitly:

1. `execution_available` — the kill switch
2. the decision is approved (and `approved` is derived from the reason
   codes, so it cannot be forged)
3. the decision's hypothesis matches the hypothesis supplied — an
   approval is for **one** hypothesis; reusing it is how an approval for
   a good setup ends up executing a different one
4. the proposal is buildable

The client order id is a SHA of `decision_id:intent`. That is what makes
a retry idempotent.

## Defects found by the falsifying controls

Three findings, all of which would have read as coverage:

**1. `order_types` was a hand-kept literal.** `capabilities()` returned
`["LIMIT", "MARKETABLE_LIMIT"]` as a hardcoded list. Adding `MARKET` to
the `OrderType` enum would have made `submit_order` accept market orders
while `capabilities()` still declared they were unsupported — a lying
capability declaration that the risk model would have relied on. Now
derived from the enum.

**2. The retry test could not catch identity-based derivation.** The test
called `build_proposal` twice with the *same* decision object, so a
client order id derived from `id(decision)` still matched. But the retry
that actually matters happens in a **new Lambda invocation**, where the
decision is deserialised from storage — a different object carrying the
same `decision_id`. Added
`test_a_rebuilt_decision_yields_the_same_client_order_id` using a
deep copy, plus the converse (two real decisions must not collide).

**3. The approval check in `submit_approved` was unreachable.**
`build_proposal` refuses an unapproved decision too, so removing
`submit_approved`'s own check left the suite green — the test asserted
only that *something* raised. Both layers are kept deliberately, their
wording now differs so the trail records which fired, and the test
asserts on the message to pin the layer. Same pattern as the Milestone 8
`DAILY_CAPITAL_EXCEEDED` finding: a second layer of defence that nothing
exercises reads as coverage without being any.

Also fixed a test of mine that was vacuous by construction
(`assertIn(x, [...] + [x])`) and a precedence-fragile ternary inside a
`raise`.

## Files

```
agent/broker/models.py       orders, fills, positions, accounts, quotes
agent/broker/base.py         BrokerAdapter protocol
agent/broker/paper.py        the simulator
agent/broker/execution.py    the decision -> order gate
tests/test_broker.py         57 tests
```

## Still true

- `trading_enabled=false`, `execution_available=false`
- Production `/chatbot` untouched; the RSI fix remains undeployed
- No real-money order is reachable from any code path in this milestone

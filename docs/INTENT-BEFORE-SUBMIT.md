# Intent before submit

The rule, stated once: **an order that reaches an external venue is
durably recorded before it is sent, and if it cannot be recorded it is
not sent.**

This exists because of a specific incident. On 2026-10-02 the agent held
0.48247 DRAM at 62.204, filled at 13:37:43 from order
`cli_1d1d1063b70a461b9e9e`, and nothing in this system had any record of
having asked for it. The position was real, the fill was real, and the
agent's own books were empty. See
[INCIDENT-2026-10-02-ORPHANED-FILL.md](progress/INCIDENT-2026-10-02-ORPHANED-FILL.md).

## Why the old shape could not avoid it

`submit_approved` built a proposal and called `broker.submit_order`.
Where the broker was the in-process simulator that was sufficient,
because `agent/broker/store.py` serialises the simulator's own `_orders`
and a submitted order survives the cycle. Where the broker is Alpaca it
was not sufficient at all: the order existed only at the venue, and the
window between "sent" and "recorded locally" had no record in it. A
crash, a timeout, or a Lambda that simply ended in that window produced a
fill nothing had asked for.

The window was not a bug in any one line. It was the absence of a step.

## The shape now

```
build_proposal            deterministic client_order_id from the decision
    |
record_intent             DURABLE. Fails -> ExecutionRefused, nothing sent
    |
broker.submit_order       the only network call
    |
record_observation        best-effort; the order already exists
```

Three properties, each with a test that fails when it is removed:

**The write precedes the send.** Not "both happen" - the ordering is the
property, because the whole defect lives between them.
`test_the_intent_is_recorded_before_the_order_is_sent` asserts the event
sequence, and the `submit-first-record-intent-after` mutation reverses it
and is caught.

**A failed write prevents the send.** If the intent cannot be persisted
the order is refused. Submitting anyway would be strictly worse than not
trading: it reintroduces the orphan knowingly.

**A ledger is not optional for an external venue.**
`BrokerAdapter.is_external_venue` defaults to `False`, and
`AlpacaPaperBroker` sets it `True`. An external submit with no ledger is
refused before any network call. The default is the restrictive one by
intent: a new adapter has to declare itself in-process deliberately
rather than inherit the laxer path by silence.

## Recovery, which is the point of a deterministic id

`client_order_id = "cli_" + sha256(f"{risk_decision_id}:{intent}")[:20]`

When an intent already exists for this id, the agent does not resubmit on
a guess. It asks the venue:

| The venue says | What happens | Why |
|---|---|---|
| the order exists | adopt it, record the observation, send nothing | this is the lost-response case the id exists for |
| absence is **confirmed** | send the same id again | the earlier attempt never arrived; no second logical order is created |
| it cannot answer | `ExecutionRefused` | an unanswerable question is not a "no" |

The middle row depends on `AlpacaPaperBroker.find_by_client_order_id`
cross-checking a 404 against the order list, which it already did before
this work: a 404 from an endpoint that does not behave as assumed would
otherwise read as "never placed", and that misreading is exactly what
produces a duplicate.

Where a broker offers no lookup at all and the ledger says the outcome
was already observed, resubmission is refused rather than risked.

## What is deliberately NOT fatal

A failed `record_observation` after a successful submit. By then the
order exists at the venue, so raising would report a failure for an order
that was accepted and the caller's error handling would be about the
wrong thing. The intent record is already durable and carries the client
order id, so the observation is recoverable by polling. It is logged as
`order_observation_not_recorded` because it is a real integrity gap -
just not one that unsends an order.

`make-a-lost-observation-unsend-the-order` mutates this in the
*over-strict* direction and is also caught, which matters: the behaviour
is pinned in both directions, not only against laxity.

## What this does not yet do

Being explicit, because the chain is only as good as its weakest
unimplemented link:

- **No polling.** Non-terminal orders are recorded and are not yet
  followed up. An order accepted and never filled stays non-terminal in
  the ledger, which is correct but inert until a poller reads it.
- **No position adoption.** `agent/broker/provenance.py` can establish
  that a discovered position is the agent's own, and nothing calls it
  from the cycle yet. DRAM therefore remains unadopted.
- **No Risk Governor integration.** `committed_exposure()` exists and the
  Governor does not consult it, so an accepted-but-unfilled order does
  not yet reserve room.
- **No DynamoDB table in dev.** `DynamoDBOrderLedger` is written and
  untested against a real table.
- **Exits are not recorded.** Only entries pass through
  `submit_approved`; `PositionManager` calls `broker.close_position`
  directly, so an exit order gets no ledger row and no lost-response
  recovery. That is tolerable only while exits are driven from
  positions the agent already holds in its own store, and it stops
  being tolerable the moment adoption lands - an adopted position's
  exit is precisely the order that most needs a durable record.

Each of those is a separate step, and none of them is a reason to soften
this one.

## Files

| File | Role |
|---|---|
| `agent/broker/execution.py` | the single submission chokepoint |
| `agent/broker/order_ledger.py` | the durable record |
| `agent/broker/provenance.py` | whose a discovered position is |
| `agent/broker/base.py` | `is_external_venue`, defaulting to False |
| `agent/orchestration/day.py` | supplies the ledger, session date and cohort |
| `tests/test_broker.py` | `TestIntentBeforeSubmit`, 16 tests |
| `tests/test_provenance.py` | `TestTheTwoDerivationsCannotDrift` |
| `scripts/falsifying_controls.py` | 10 controls, all caught |

## The id is derived twice, and that is pinned

`execution.py` builds the client order id; `provenance.py` reconstructs
it. Provenance cannot import the execution path, because the read API
must not be able to reach it, so the rule is written in both places.

Nothing compared them until now. Had either changed, the ledger would
key orders under one id while provenance reconstructed another, adoption
would quietly stop recognising the agent's own positions, and every
existing test would still have passed - each side is self-consistent.
`TestTheTwoDerivationsCannotDrift` compares them over canonical fixed
decision ids (including the real DRAM one) across four intents, and
three mutations drift each side independently.

One of those mutations is worth naming: putting `reference_price` into
the id. It looks harmless and it would mean a retry after a single tick
produced a different id, so the venue could no longer dedupe the retry -
turning the idempotency mechanism into a duplicate generator.

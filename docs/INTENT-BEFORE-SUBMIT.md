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
- ~~Exits are not recorded.~~ **Done.** See below.

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

## Exits go the same way, and they could not before

An exit used to call `broker.close_position`, which on Alpaca is
`DELETE /v2/positions/{symbol}`. That endpoint accepts **no client order
id**. An exit sent through it therefore could not be made idempotent: if
the response was lost there was no handle to ask the venue about, and
the only choices were to leave the position open or risk selling twice.
This was not a missing feature in the exit path - it was unreachable
through the endpoint the exit path used.

An external exit now goes out as a deterministic marketable-limit SELL
through the same chokepoint an entry uses, so intent-before-submit,
lost-response recovery, polling, partial fills and restart recovery all
apply to it unchanged. The id is
`sha256(f"{risk_decision_id}:EXIT")[:20]`, which is exactly what
`provenance.client_order_id_for(decision, "EXIT")` reconstructs - so an
exit order is recognisable as the agent's own too.

A retry reuses its attempt's id, so the venue suppresses the duplicate.
A **remainder** left by a partial fill is a different matter: it is a new
logical order, and reusing the first id for it would be suppressed and
leave the remainder open. Later attempts therefore carry a suffixed
intent (`EXIT#2`, `EXIT#3`), bounded at `MAX_EXIT_ATTEMPTS` - a position
that cannot be closed in three attempts is a state for a human to look
at rather than one to keep retrying. Those suffixes are in
`provenance.DEFAULT_INTENTS`; leaving them out would mean a position
closed on a second attempt could not be proved to be the agent's own,
and a mutation checks it.

The in-process simulator keeps `close_position`: its orders are
serialised with its own state, so the durability is already there.

### A boundary that nearly broke

The obvious implementation imports `submit_exit` into
`agent/positions/manager.py`. That makes the submission path reachable
from the read API, which imports positions - and a test asserts that
importing the API handler loads no broker and no orchestrator. It
failed immediately. A lazy import would have hidden the failure while
leaving the path reachable, so the submitter is **injected** by the
orchestrator instead and `manager.py` imports nothing from
`agent/broker/`. The API never supplies it, so the API cannot submit.

### What this did not fix

`session_date` is set on the position manager per cycle rather than at
construction, because a warm Lambda's manager outlives a session and a
stale date would file an exit under the wrong day. When it was not set
at all the exit was *refused* and the position stayed open with
`EOD_FLATTEN_FAILURE` raised - correct fail-closed behaviour, and still
a bug, which is how it was found.

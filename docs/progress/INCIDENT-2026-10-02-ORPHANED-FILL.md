# Incident: an order was filled and never recorded

2026-10-02. Paper money only. No real-money exposure at any point.
**The safety machinery worked**; the journal integrity guarantee did not.

## Timeline, from CloudWatch and the venue's own order history

```
13:37:41  broker_selected       alpaca_paper, authoritative, paper host
13:37:42  reconciliation        matched: true          <- book was clean
13:37:43  order_submitted       DRAM BUY 0.48247 MARKETABLE_LIMIT
                                order_id d7eaecdd-4c25-4273-8685-8085e60d48b4
13:37:43  cycle_aborted         'AlpacaPaperBroker' object has no attribute
                                '_account'             <- same second
   (venue fills the order: 0.48247 @ 62.204)
13:42:41  reconciliation        matched: false, broker_only: ['DRAM']
13:42:42  emergency_stop_engaged  RECONCILIATION_MISMATCH
13:42:42  cycle_aborted         same defect
   ... 21 further mismatches through 15:13
```

There is **no** `order_filled`, `position_opened`, `trade_recorded` or
`position_state_saved` event. The order left the agent, the venue
executed it, and the agent kept no record of it.

## Why

The fifth defect in the external-broker path.
`agent/broker/store.py` persists the internal simulator by reaching into
its private attributes, and the cycle handed it the external adapter, so
`_persist` raised `AttributeError` on `broker._account`.

`_persist` runs **after** the orchestrator has already submitted orders.
So the crash landed in the window between "order submitted" and
"position written", which is the one window where a fill can be lost.

## What worked, and should be said plainly

- Reconciliation detected the orphan on the very next cycle
- `EMERGENCY_STOP` latched and `UNEXPECTED_BROKER_POSITION` was raised
- New exposure was refused from 13:42 onward; `entries_permitted: false`
- The position was **not** auto-closed, by design: closing a position of
  unknown origin would risk being the second mistake
- No duplicate was submitted. Had the agent kept trading with no record
  of DRAM, re-entering it was a live possibility - the emergency stop is
  what prevented that

The orphan was detectable only because reconciliation compares the
broker's book against the agent's rather than trusting its own.

## The architectural finding

This is not only a missing attribute. **Persistence happens at the end
of a cycle, after orders have been submitted.** Any failure in that
window loses the record of a fill that has already happened.

The guarantee that matters is: *an order that has left the agent must be
recoverable from durable state before anything else can fail.* Today it
was not. `client_order_ids` - the idempotency guard - is itself part of
the broker state that failed to save, so the mechanism that prevents a
duplicate was lost in the same crash that created the need for it.

This wants fixing before the first strategy cohort, and it is a
trading-path change, so it needs its own regression proof and gate.

## State as at 15:15Z

| | |
|---|---|
| Venue position | DRAM 0.48247 @ 62.204, mv $29.66, unrealized −$0.35 |
| Venue orders today | 1, filled |
| Agent's internal position store | empty, and **correctly** reports itself readable and empty |
| Health | HALTED; entries refused, exits permitted |
| Latched conditions | `RECONCILIATION_MISMATCH`, `EMERGENCY_STOP`, `UNEXPECTED_BROKER_POSITION` |
| Deployed cycle | `6e71adf` at 15:12:51Z - the defect is fixed; cycles now complete |

## Consequences

**2026-10-02 is permanently operational/non-cohort.** Regular-market
trading occurred on `c4a9d50` before the verified runtime was deployed,
which the cohort charter already excluded in advance.

**EOD flatten will not close DRAM.** `flatten_all` iterates the internal
manager's open positions, and DRAM is not among them. It will remain
open overnight unless closed deliberately, which breaches
NO_OVERNIGHT_POSITIONS through a path the policy cannot reach.

**The first cohort cannot open until the book is flat and the two books
reconcile.** That is unchanged by the fix: a clean cohort needs a proven
flat start, and the start is not flat.

## Two actions only a person can take

1. **Clear the latched `EMERGENCY_STOP`.** It records `cleared_by` by
   design, because a latch that software can clear is not a latch. The
   cause is now understood and fixed, so clearing it is appropriate -
   but it is a named human act.
2. **Decide the DRAM position.** Its origin is no longer unknown: order
   `d7eaecdd-4c25-4273-8685-8085e60d48b4`, submitted by the agent at
   13:37:43 and filled. Closing it restores a flat book. The agent has
   deliberately not closed it.

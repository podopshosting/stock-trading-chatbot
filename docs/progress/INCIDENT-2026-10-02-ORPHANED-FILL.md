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

## Why — CORRECTED

My first reading of this blamed the `_account` crash for losing the
record of a fill. **That was wrong**, and the decision log says so.

The agent's own decision log holds a DRAM row with outcome
`ORDER_NOT_FILLED`, `filled_quantity: 0.0`, `average_fill_price: None`,
against the same order id. So the real sequence is:

1. the agent submits a MARKETABLE_LIMIT order for DRAM
2. it reads the order back immediately: **zero filled**
3. it records `ORDER_NOT_FILLED` and moves on - correct, by its own
   information at that instant
4. the cycle later crashes on `_account`, which is a separate defect
5. **the venue fills the order afterwards**
6. the next cycle sees a position the agent never opened

The `_account` crash is incidental. The orphan would have happened
without it.

### The actual defect

`agent/orchestration/day.py` abandons an unfilled entry order:

```python
if (order.get("filled_quantity") or 0) <= 0:
    self._record_decision(result, symbol, "ORDER_NOT_FILLED", ...)
    continue
```

There is no cancel. `cancel_order` does not appear anywhere in that
module. The order is left **working at the venue**.

Against the internal simulator this is harmless, because it fills
synchronously or never - so "not filled on read-back" really does mean
"will never fill". A real venue does not work that way: a marketable
limit can fill milliseconds after the agent looks. The assumption was
true of the simulator and false of the market, and nothing had ever run
this path against a real venue.

That is why this surfaced on the first external-broker session and not
in any of 2109 tests.

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

**An order that has left the agent is exposure, whether or not it has
filled yet.** The code treated a submitted-but-unfilled order as
nothing at all: not tracked, not cancelled, not reconciled against.
There is no state in which that is safe with a real venue.

A secondary weakness stands regardless: persistence runs at the end of
a cycle, after orders have been submitted, and `client_order_ids` - the
idempotency guard - is part of the broker state that failed to save. So
the mechanism that prevents a duplicate was lost in the same crash that
would have created the need for it.

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

---

## Fix

`_cancel_abandoned_entry` now cancels any entry order that reads back
unfilled, and the decision detail records whether the cancel succeeded.
If the cancel fails - including because the order filled while the
cancel was in flight - `UNCERTAIN_ORDER_STATE` is raised as a condition
and an alert, because exposure is then unknown and must not be reported
as absent. Reconciliation on the next cycle is what resolves it.

Five regression tests, with both controls:

- an order really was submitted (without this, the cancel assertion
  would pass for a cycle that never traded)
- the unfilled order is cancelled
- no position is claimed
- the cycle still completes
- **a filled order is NOT cancelled** - cancelling a filled entry would
  close a position the agent had just correctly opened

The defect test fails against the unfixed orchestrator with
`Lists differ: [] != ['ord_1']`.

## What this says about the test suite

2114 tests did not catch it, and no amount of care in writing them
would have. Every test ran against the internal simulator, where
"unfilled on read-back" genuinely means "will never fill". The
assumption was true of the fake and false of the market.

The `StrictExternalBroker` and `UnfillingBroker` doubles added today are
the first things in the suite that behave like a venue rather than like
the simulator: public interface only, and asynchronous fills. Both
defects found this afternoon were invisible until something in the
tests stopped pretending to be the simulator.

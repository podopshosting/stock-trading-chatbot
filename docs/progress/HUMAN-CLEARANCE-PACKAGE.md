# Human clearance package — 2026-10-02

Three health conditions are latched. The agent cannot clear them:
`clear_condition` refuses a latching condition unless `cleared_by` names
a person, and the agent's own cycle passes `None`. That is the mechanism
that stops a system talking itself back into the state that halted it,
and it is working as intended.

Nothing here is a request to weaken it. This is the evidence a person
needs in order to decide.

## Current state

| | |
|---|---|
| Health | `HALTED` — entries refused, exits permitted |
| Latched since | 2026-10-02T13:42:41Z (two) and 13:42:42Z (one) |
| Occurrences | 37 each, and still counting every cycle |
| Venue position | DRAM 0.48247 @ 62.204, unrealized ≈ −$0.26 |
| Agent's internal store | empty, and correctly reports itself readable and empty |

## Condition 1 — `RECONCILIATION_MISMATCH`

**Original cause.** At 13:42:41 reconciliation found the broker holding
DRAM while the agent's position store held nothing. The books disagreed
about real exposure.

**Why it happened.** At 13:37:43 the agent submitted a MARKETABLE_LIMIT
order for DRAM, read it back with `filled_quantity: 0`, recorded
`ORDER_NOT_FILLED`, and moved on — leaving the order **working at the
venue**. The venue filled it afterwards. The orchestrator contained no
`cancel_order` call anywhere, so an unfilled entry was abandoned rather
than cancelled. Against the internal simulator that was safe, because it
fills synchronously or never; against a real venue it is not.

**Remediation.** `_cancel_abandoned_entry` now cancels any entry order
that reads back unfilled, and records whether the cancel succeeded. A
failed cancel — including one that filled while the cancel was in
flight — raises `UNCERTAIN_ORDER_STATE` rather than reporting absence.

**Regression test.** `TestAnUnfilledEntryOrderIsNotAbandoned`, five
cases. Fails against the unfixed orchestrator with
`Lists differ: [] != ['ord_1']`. Two controls: that an order really was
submitted, and that a **filled** order is not cancelled.

**Mutation proof.** Gate on `2918714` (which contains the fix):
163/163 caught, 0 survivors, 0 stale anchors, clean restore.

**Deployed verification.** `fee3be5` deployed 16:15:38Z; cycles now
complete rather than abort.

**Remaining risk.** The cancel is a mitigation, not the requirement. The
agent still does not poll a submitted order to a terminal state, so
`ASYNC_FILL_RECONCILIATION` and `ORDER_STATUS_POLLING` remain unmet. An
order that fills between the submit and the cancel would still surprise
it. The order ledger built today is the foundation for closing that, and
is not yet wired.

## Condition 2 — `EMERGENCY_STOP`

**Original cause.** Engaged automatically at 13:42:42 by condition 1.
The agent may engage it and may never clear it.

**Remediation.** None of its own — it is a consequence, and clears when
the operator is satisfied the cause is understood.

**Remaining risk.** None independent of condition 1 and 3.

## Condition 3 — `UNEXPECTED_BROKER_POSITION` (DRAM)

**Original cause.** The orphaned fill described above.

**Status: NOT remediated, because remediation is a position decision.**

The position is still open. Its origin is **no longer unknown**: order
`d7eaecdd-4c25-4273-8685-8085e60d48b4`, client order id
`cli_1d1d1063b70a461b9e9e`, submitted by this agent at 13:37:43 and
filled by the venue. The "do not close what you cannot explain" rule
was the right default and no longer applies.

**EOD flatten will not close it.** `flatten_all` iterates the internal
manager's open positions, and DRAM is not among them. It will sit open
overnight through a path the no-overnight-positions policy cannot reach.
The agent has deliberately not closed it.

## What a person needs to do

**1. Decide the DRAM position.** It is paper money, about $29 of
notional. Closing it restores a provably flat book, which the cohort
preflight requires. I have not closed it, because adopting or
liquidating a position the agent did not record is a decision about
real-looking exposure rather than a mechanical step.

**2. Clear the three conditions, after the position is resolved.**
Clearing them while DRAM is still held would simply re-latch on the next
cycle — reconciliation would find the same mismatch — so the order
matters. Each requires `cleared_by` naming you.

The conditions live at `PK=CONTROL#HEALTH, SK=STATE` in
`stock-agent-dev-journal`. There is currently **no administrative
endpoint or script** that calls `clear_condition` with `cleared_by`: the
only callers in the codebase pass `None` and are refused by design. So
clearing today means either a short administrative script or a direct
record edit, and I would rather build the former than suggest the
latter.

**I have not built it yet**, deliberately: a tool that clears latching
safety conditions is exactly the kind of thing that should be written
with its audit trail and its authorisation argument considered, not
added at the end of a long session. It is the first item I would build
next, with `cleared_by` mandatory, every clearance journalled, and a
test proving it refuses an empty or absent identity.

## What stays true regardless

- Production untouched
- Normal strategy remains PAPER
- Autonomous real money remains DISABLED
- 2026-10-02 is permanently `OPERATIONAL_VALIDATION_ONLY`
- The agent refused all new exposure from 13:42 onward, which is the
  behaviour that made this a recorded incident rather than a loss

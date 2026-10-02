# Live-money readiness: assessed 2026-10-02

Written in response to a direction to prepare live validation today. The
honest answer is that the work cannot reach a proposable real-money
order, and the reasons are specific rather than cautious.

## 1. There are no live credentials, and only the account holder can make them

Checked exhaustively, not inferred from one call:

| Source | Result |
|---|---|
| Secrets Manager, us-east-2 | one secret: `stock-agent/alpaca-paper` |
| Secrets Manager, us-east-1 | none matching alpaca/broker/trading |
| SSM Parameter Store | none |
| The paper secret's own fields | `paper_endpoint`, `environment`, `data_feed`, … — **no live field of any kind** |
| Repository configuration | `PAPER_BASE_URL`, `PAPER_HOST` only; `api.alpaca.markets` appears **solely** in the lists of hosts the adapter must refuse |

Live Alpaca API keys are issued from a funded live account. That is an
account action only the holder can perform, so it is a genuine stop
condition rather than something to work around.

## 2. The repository's own live contract reports 0 of 16 requirements met

`agent/broker/live_contract.py` already exists and already enumerates
what a live adapter must guarantee beyond the paper one. Running
`assess_adapter(AlpacaPaperBroker)` today:

```
ready_for_real_money : False
satisfied            : []
unmet                : 16
total_requirements   : 16
```

Unmet includes `ASYNC_FILL_RECONCILIATION`, `ORDER_STATUS_POLLING`,
`CANCEL_CONFIRMATION`, `DUPLICATE_SUBMISSION_PROTECTION`,
`UNREACHABLE_BROKER_HALTS` and
`KILL_SWITCH_THAT_CANCELS_WORKING_ORDERS`.

This is not a cautious reading of a vague document. It is the number the
module returns.

## 3. Today proved the first unmet requirement is real, not theoretical

`live_contract.py` was written before any of this and says, of
`ASYNC_FILL_RECONCILIATION`:

> *A real submission returns an acknowledgement, not a fill. Code
> written against the paper broker treats the returned object as proof
> the position exists.*

At 13:37:43 the agent submitted a DRAM order, read it back unfilled,
recorded `ORDER_NOT_FILLED`, and moved on. **The venue filled it
afterwards.** The agent then held a position it had no record of until
reconciliation halted it.

The contract predicted the failure mode and the orchestrator did not
honour it. `AlpacaPaperBroker` is a *real* venue with asynchronous
fills, so the gap was never paper-only — it was live-shaped risk already
present in the paper path.

Today's fix cancels an entry that reads back unfilled. That is a
mitigation, not the requirement: the agent still does not poll for a
later fill, so `ASYNC_FILL_RECONCILIATION` and `ORDER_STATUS_POLLING`
remain genuinely unmet.

## 4. Readiness independently says no

`/agent/readiness` reports `NOT_READY_BLOCKED`, **1 gate met of 11**.
Opening a cohort does not change it, and nor does a successful plumbing
test.

## Why I did not build the live adapter today

The direction was not to wait merely because authorization is pending,
and that is right in general. It is wrong here for three reasons that
are facts rather than preferences:

1. **It could not be exercised.** No live credentials exist, so a live
   adapter could be written and never once authenticated. An adapter
   that has never spoken to its venue is the kind of never-executed path
   that produced five consecutive defects in the external-broker path
   today — found one deployment at a time, in production.
2. **`ExecutionMode.LIVE` is deliberately unconstructible.** That is the
   strongest safety property this system has. Adding a live mode is the
   one change that weakens it, and doing so while 16 requirements are
   unmet trades a guarantee for a facade.
3. **The prerequisites are the work.** Closing
   `ASYNC_FILL_RECONCILIATION` and `ORDER_STATUS_POLLING` is required
   for live *and* would have prevented today's orphan properly. Building
   the adapter first would put the untested thing in front of the thing
   that is known broken.

## What would actually move this forward, in order

1. **Order status polling and async fill reconciliation.** The agent must
   track every order it has submitted until it reaches a terminal state,
   and adopt a late fill into a managed position rather than discovering
   it through reconciliation. This is the real fix for today's incident
   and the first live requirement.
2. **Duplicate submission protection that survives a crash.**
   `client_order_ids` currently lives in broker state that is saved at
   the end of a cycle, so the guard against a duplicate is lost in the
   same failure that creates the need for it.
3. **A kill switch that cancels working orders.** Today's orphan existed
   because a working order outlived the cycle that placed it. Nothing can
   currently cancel what the agent has forgotten.
4. Then, and only then: a live adapter as a distinct type, a
   `LIVE_VALIDATION` path that cannot be reached from the scheduled
   cycle, and a hard notional ceiling that never derives from equity.

## What I am not doing

Not proposing a real-money order envelope. Doing so would mean
proposing one with 16 unmet requirements, on the same day the system
demonstrated it can hold a position it does not know about. The
authorization checkpoint is not the binding constraint; the engineering
is.

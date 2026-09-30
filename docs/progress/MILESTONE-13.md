# Milestone 13 — Autonomous Market-Day Orchestration

Status: complete. 47 orchestration tests, 1,098 total, 100/100 mutations
caught.

## The ordering rule is the design

```
reconcile  ->  exits  ->  entries
```

**Reconciliation first**, because acting on a position set that
disagrees with the broker is acting on fiction.

**Exits second**, because if the process dies at any point after that —
and on Lambda it will, eventually, at the worst possible moment — risk
has already been reduced before the failure.

**Entries last**, because they are the only step that *adds* risk, and
the only step that is safe to skip entirely.

Every failure path in the orchestrator leads to the same place: exits
continue, entries stop. There is no failure mode in which the agent
keeps opening positions while something is wrong.

`CycleResult.risk_was_managed` reports whether the protective half
completed, because after a failure "the cycle failed" is not useful
information — "reconciliation succeeded, exits succeeded, the scan
failed" is.

## Asymmetric caution

Refusing to enter costs an opportunity. Refusing to exit costs money
without limit. The two are not symmetrical, and the phase model encodes
that:

| Phase | Entries | Exits |
|---|---|---|
| `PRE_MARKET` | no | no (market unreachable) |
| `OPENING` (first 5 min) | **no** | yes |
| `INTRADAY` | **yes** | yes |
| `PRE_CLOSE` (last 30 min) | **no** | yes + flatten |
| `CLOSED` | no | no |
| `UNKNOWN` | no | no |

`permits_new_exposure` is derived from the phase with no flag to
override it, and `INTRADAY` is the only phase that returns true.

- The **opening minutes** are excluded because spreads are widest and
  the first print is often not a price anyone could have traded.
- **Pre-close** is excluded because a position opened there cannot be
  given time to work before it must be flattened.
- **Open but unknown time to close** resolves to `PRE_CLOSE`, not
  `INTRADAY`: without knowing how long is left, the flatten window
  cannot be respected. Exits stay safe.
- **Unknown market status is never treated as open.** A provider that
  cannot tell us whether the market is trading is not evidence that it
  is.

`CycleResult.new_exposure_permitted` requires **both** the right phase
and an empty halt list, so a halt cannot be cleared by changing the
phase and a wrong phase cannot be excused by an empty halt list. It is
derived, with no setter.

## Idempotency

EventBridge delivers at-least-once and Lambdas overlap. Two cycles
passing the risk checks against the same capital would both enter,
producing double the intended exposure from a single day's signal.

`CycleLock` prevents that. The DynamoDB implementation uses a
conditional put with a lease timestamp, so:

- acquisition is atomic
- only the holder may release — a late cycle releasing a lock it does
  not own would let a third cycle in alongside the holder
- a crashed Lambda releases by lease expiry rather than deadlocking
  trading for the rest of the session
- **an unreadable lock refuses the cycle.** If we cannot tell whether
  another cycle is running, skipping one is better than two entering the
  same position.

## Failure containment

- **A cycle never raises into the scheduler.** A retry would rerun the
  whole thing, and the parts that already succeeded would run twice.
- **An injected provider that raises cannot stop the exits.**
  `_safe_call` returns `None`, which every downstream check treats as
  missing data and therefore as a reason not to trade — while the exits
  above it have already run.
- **A journalling failure is distinguished from an exit failure.** The
  position *is* closed; only the record is missing. Conflating them
  would make someone believe risk was still live.
- **A fill that cannot be given an exit plan is flagged loudly.** The
  broker then holds a position the agent is not managing. Reconciliation
  catches it on the next cycle and halts, so the failure is loud rather
  than silent — the best available outcome, not a good one.

## Defects and weak tests found

**Three test fixtures of mine were testing nothing.** `exits_failed`,
the entry-failure containment test and the journal-failure test all set
up a position with a stop *below* the current price, so no exit intent
ever fired and `submit_exit` was never reached. Each assertion passed
against a cycle that did nothing. Fixed with a falling-quote provider —
`open_from_order` refuses a stop at or above the entry, so a position
cannot be constructed already in breach; the price has to come down to
it.

**The pre-close flatten test could not distinguish two layers.** The
exit engine has its own `END_OF_DAY` rule which fires first, so the
orchestrator's fallback flatten never ran. Added a case where the
position's plan has no end-of-day rule at all, making the orchestrator's
flatten the only thing that can close it.

Both are the same failure as Milestones 8, 9, 10 and 12: a second layer
of defence that nothing exercises reads as coverage without being any.

## Files

```
agent/orchestration/models.py   phases, outcomes, halt reasons, CycleResult
agent/orchestration/day.py      the cycle, in safe order
agent/orchestration/lock.py     at-least-once protection
tests/test_orchestration.py     47 tests
```

## Still true

- `trading_enabled=false`, `execution_available=false` are the defaults
  on the orchestrator constructor, and both must be explicitly passed
  true for any order to be attempted
- Production `/chatbot` untouched; the RSI fix remains undeployed
- Nothing is scheduled: no EventBridge rule points at this yet

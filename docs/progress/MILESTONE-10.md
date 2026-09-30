# Milestone 10 — Position Manager & Exit Engine

Status: complete. 79 position tests, 931 total, 56/56 mutations caught.

## The asymmetry this milestone rests on

For a **new entry**, the safe direction is to do nothing. For an **open
position**, the safe direction is to get out.

These are not the same kind of caution, and conflating them is how
automated systems lose more than they planned to. Missing data blocks an
entry; missing data *forces* an exit. A position whose price cannot be
established is carrying unbounded risk in exchange for nothing, so
`evaluate()` closes it rather than holding blind.

The same logic applies to an unreadable halt state: if an operator may
have halted trading and the agent cannot see it, the assumption is
halted.

## Stops may only ever tighten

`ManagedPosition.tighten_stop()` raises `StopWidened` on any attempt to
move a stop away from price, including by a hair, and including by
setting it to `None`.

Moving a stop to avoid taking a loss converts a bounded loss into an
unbounded one. It is the single most destructive discretionary act in
retail trading, so the code makes it impossible rather than merely
discouraged. Every move is recorded in `stop_history` with its reason.

The trailing stop ratchets the hard stop up through the same method, so
it inherits the guarantee: if the trailing level is *wider* than the
current stop it is ignored, and the tighter of the two always wins.

Trailing does not begin until the trade is up `TRAILING_ACTIVATION_PCT`
(1%). Trailing from the first tick converts ordinary noise into an exit
and guarantees the strategy never holds a winner.

## Exit reasons stay separate

Several rules can fire at once. All of them are recorded on the
`ExitIntent`; the most protective becomes `primary_reason` by rank in
`EXIT_PRIORITY`. They are never summed into a score, so the record can
still answer "which exit rule actually earns its keep".

`PROTECTIVE_REASONS` marks the exits that reduce risk. These must never
be gated by the checks that govern new exposure: a system that can open
a position but not close one is far more dangerous than one that can do
neither.

## What a stop does not guarantee

`stop_gap_disclosure()` states it plainly, because this is the most
common way a paper record overstates a strategy:

- An **engine-polled** stop is only checked when a cycle runs. If the
  agent is not running, the stop does not exist. Between cycles price
  can pass straight through it, so the realised loss may exceed the
  planned one.
- A **broker-resting** stop triggers without the agent, but still fills
  at the market, not at the stop price.

Neither mechanism guarantees a fill price. A test asserts that no
mechanism ever claims one.

## The broker is authoritative

`reconcile()` compares the agent's view with the broker's. Divergence
halts the manager and is **not** repaired automatically — adopting the
broker's numbers would discard the plan attached to the position, and
adopting the agent's would ignore reality. Both ways divergence arises
mean the risk arithmetic is wrong in an unknown direction, which cannot
be corrected by guessing.

`ReconciliationResult.safe_to_trade` is a derived property with no
setter, the same pattern as `RiskDecision.approved`.

`total_open_risk()` returns `None` if any position cannot be priced. A
total that silently omits an unpriceable position understates risk, and
an understated risk total is worse than no total at all.

## Defects found while building this

**1. A routine partial fill would have halted the whole system.**
Discovered because a test fixture happened to hit a partial fill. An
entry order for 2 shares filling 1.4 leaves 0.6 still working. The
managed position records 1.4; when the remainder fills later the broker
holds 2.0, reconciliation detects the mismatch and halts everything —
over an entirely ordinary market event. And the remainder could never
have been managed anyway, because `open_from_order` refuses a second
position in the same name.

`open_from_order` now cancels the remainder as part of opening, so the
managed position is exactly what is held, and fails closed if the
cancel is refused. Four tests cover it, including that a partial entry
reconciles cleanly afterwards.

**2. `_primary` was picking the first reason found, not the highest
ranked — and nothing caught it.** The append order in `evaluate()`
mirrors `EXIT_PRIORITY` closely enough that `reasons[0]` is *usually*
the most protective. There is exactly one place they diverge:
`PROFIT_TARGET` is detected before `THESIS_INVALIDATED` but ranks below
it.

That case matters. If the thesis collapsed and price merely happened to
touch the target, recording the exit as `PROFIT_TARGET` would credit the
strategy with a win its own logic did not earn — which would then
corrupt the strategy evaluation in Milestone 16. Added a specific test
for it plus a general one asserting the primary is chosen by rank rather
than detection order.

## Files

```
agent/positions/models.py     ManagedPosition, ExitPlan, ExitIntent,
                              ReconciliationResult, the stop invariant
agent/positions/exits.py      pure exit rules, trailing logic, disclosure
agent/positions/manager.py    lifecycle, reconciliation, submission
tests/test_positions.py       79 tests
```

## Still true

- `trading_enabled=false`, `execution_available=false`
- Production `/chatbot` untouched; the RSI fix remains undeployed
- No real-money order is reachable from any code path in this milestone

# Milestone 18 — Live-Readiness Gate

Status: complete. 33 readiness tests. **Current answer:
`NOT_READY_BLOCKED`.**

Live: `GET /agent/readiness`, and the first panel on the dashboard.

## One question, derived answer

May this system place a real-money order? The gate answers it from
eleven prerequisites and nothing else.

Three design choices carry the bias, and the bias is deliberate: a false
"not ready" costs delay, a false "ready" costs money — and an automated
system loses money not through one large mistake but through a small one
repeated faster than anyone notices.

**The verdict is derived, never assigned.** `ready` is a property
computed from the unmet gates. No field, no override, no `force`
parameter. A test scans the module source for `force=`, `override=`,
`skip_gates`, `bypass` to keep it that way.

**Gates do not trade off.** A gate is a prerequisite, not a score
contribution. Ten of eleven is not a pass, because each gate is an
independent way to lose money and a strong showing elsewhere does not
close an open one.

**An UNKNOWN gate counts as unmet.** The system cannot be ready in a
respect it has not checked. Treating unknown as satisfied would make the
gate weaker the less it knew, which is exactly backwards — and it is why
`assess()` with no arguments returns not-ready rather than vacuously
passing.

## The eleven gates and where they stand

| Gate | Status | Why it is not met |
|---|---|---|
| `demonstrated_edge` | UNMET | No completed trades. A strategy whose edge is undemonstrated is gambling with extra steps. |
| `stops_hold` | UNKNOWN | Nothing measured. A 0% breach rate over zero trades is not evidence that stops hold. |
| `strength_is_predictive` | UNMET | Position size is scaled by hypothesis strength; if strength does not order outcomes the sizing is arbitrary. |
| `live_adapter_exists` | UNMET | No live adapter. `BrokerAdapter` conformance is not evidence — the paper broker satisfies it fully while unable to lose a cent. |
| `execution_venue_available` | **BLOCKED** | Fidelity offers no retail execution API. Cannot be met from here. |
| `pilot_ran_unattended` | UNKNOWN | Needs 20 completed sessions. The pilot is scheduled but has not yet run one. |
| `live_data_path_exercised` | UNKNOWN | The market was closed throughout Milestone 15; `_quote_loader` and `_hypothesis_loader` have never run against real responses inside a cycle. |
| `kill_switch_reaches_live_risk` | UNMET | The current switch stops **new** orders. A resting order is live risk it does not reach. |
| `reconciliation_clean` | UNKNOWN | No sessions to assess. |
| `explicit_authorisation` | UNMET | Cannot be satisfied by evidence, only by a decision from the account holder. |
| `capital_at_risk_agreed` | UNMET | The amount that may be lost must be decided in advance, not discovered afterwards. |

`BLOCKED` is distinct from `UNMET` on purpose: it records that the gate
cannot be closed by more engineering. It needs an account at a venue
with a supported execution API, which is an account action for the
account holder.

## Honest limitations this gate surfaces

Two of the UNKNOWNs are things I could not verify rather than things
left undone, and the gate names them rather than letting them pass as
implicitly fine:

- **The live-data path has never run.** The market was closed for the
  whole of Milestone 15, so every cycle correctly returned
  `phase=CLOSED`. 18 integration tests cover the composition using
  synthetic quotes, which is not the same thing.
- **The kill switch does not cancel working orders.** It stops new
  ones. In a paper pilot with no resting orders that is adequate; with
  real money it would be advertising protection it does not provide.

## What would have to change

Closing the engineering gates is weeks of pilot data plus a live
adapter. Closing `execution_venue_available` requires a brokerage
relationship this system cannot arrange. And `explicit_authorisation`
is not an engineering task at all.

A `READY` verdict would still not be advice to trade, and the summary
says so: "Every gate is met. This is a statement about the checklist,
not a recommendation to trade."

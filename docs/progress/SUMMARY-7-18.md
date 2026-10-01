# Milestones 7–18 — consolidated record

Branch `feature/trading-agent-v1`, from baseline `73792e1` (682 tests).

| | Baseline | Now |
|---|---|---|
| Tests | 682 | **1,302** |
| Falsifying-control mutations | 18 | **140** |
| Production `/chatbot` | untouched | **untouched** (ETag `dcbc02f1…`) |

## What was built

| Milestone | Deliverable |
|---|---|
| 7 | Trade hypothesis engine — strategy selection, contradictions, itemised strength |
| 8 | Risk governor — derived approval, frozen limits, global halt store |
| 9 | Paper broker — spread, slippage, partial fills, cash reservation, idempotent orders |
| 10 | Position manager & exit engine — stops that only tighten, gap disclosure, reconciliation |
| 11 | Trade journal & analytics — R-multiples, Wilson intervals, sample-adequacy gating |
| 12 | Historical replay — structural anti-lookahead, next-bar fills |
| 13 | Market-day orchestration — reconcile → exits → entries, cycle lock |
| 14 | Agent dashboard — five separate pipeline stages, no composite score |
| 15 | Paper pilot — state persistence, deployed and scheduled |
| 16 | Evaluation & calibration — selection-noise floor, chronological holdout |
| 17 | Broker contract — `FIDELITY_AUTOMATED_EXECUTION_UNAVAILABLE` |
| 18 | Readiness gate — `NOT_READY_BLOCKED`, 0 of 11 gates met |

## The recurring defect class

Eleven of the defects found across these milestones were the same thing
in different clothing: **a guard that could not fail**.

- A disclaimer scanned as an assertion (M9, M14) — `"carries no order,
  quantity or price"` matched a search for "quantity"; `place_order`
  matched inside `"can_place_orders": False`
- A second layer of defence nothing exercised (M8, M9, M10, M13) — the
  daily-capital ceiling, the approval check in `submit_approved`,
  `_primary`'s priority walk, the orchestrator's pre-close flatten
- A test whose fixture moved with the constant it guarded (M16)
- A test accepting two outcomes, so neither was tested (M16)
- An assertion true by construction (M9) — `assertIn(x, [...] + [x])`
- A test that passed because the situation never arose (M12, M15) — no
  position ever survived to the last bar; the ratchet closed after one
  observation

Each was found by mutation, not by review. The pattern is strong enough
to state as a rule: **after writing a guard, ask what proves it
executes.**

## The worst incident

During Milestone 14 I found `elif False:  # MUTATION` in
`agent/risk/governor.py` — a leaked mutation that had been packaged and
deployed to the dev Lambda, disabling a spread check. I confirmed it by
downloading the deployed artifact.

The harness defect behind it was worse than the leak. Its snapshot came
from the working tree, so a pre-existing mutation was recorded as the
"original", faithfully restored, and certified **"restored cleanly"** on
every subsequent run. The checksum only ever proved *unchanged since
this run started* — never *matches the real source*.

Fixed with a pre-flight contamination check and a post-run marker sweep,
both verified by planting a mutation and watching the harness refuse
with exit 3. This was the guards-that-cannot-fail problem applied to the
guard infrastructure itself.

## Design decisions worth keeping

**Derived properties instead of flags.** `RiskDecision.approved`,
`ReconciliationResult.safe_to_trade`, `Metric.is_evidence`,
`CycleResult.new_exposure_permitted`, `ReplayResult.valid`,
`AdapterAssessment.ready_for_real_money`, `ReadinessReport.ready`. None
has a setter. An approval cannot be forged, only earned.

**Asymmetric fail-closed.** For a new entry the safe direction is to do
nothing; for an open position it is to get out. Missing data blocks an
entry and *forces* an exit.

**Sample-size gating on every claim.** A 100% win rate over twelve
trades has a 95% lower bound of 76% and says almost nothing about the
thirteenth. `describe()` returns `NO_EDGE_DEMONSTRATED` for any
inadequate sample whichever way the numbers point.

**No composite score.** Scanner score, signal agreement, signal
magnitude, evidence materiality, evidence novelty, hypothesis strength
and the risk verdict are reported separately, everywhere.

## What is NOT true

- No real-money order has been placed, and no code path can place one.
- The strategy has demonstrated nothing. Zero completed trades.
- The live-data path has never run — the market was closed throughout
  Milestone 15.
- The RSI production fix remains undeployed, as instructed.

Milestone 19 has not begun and will not without explicit authorisation.

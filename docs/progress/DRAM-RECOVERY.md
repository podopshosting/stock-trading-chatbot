# DRAM recovery

**Status 2026-10-04:** adopted, exit submitted and queued, **awaiting the
market open at 2026-10-05 09:30 ET to fill.**
Steps 1–10 complete. Steps 11–15 cannot complete until the fill.

## The position

Alpaca PAPER account `PA3XG705F1PL`.

| | |
|---|---|
| Symbol | DRAM |
| Quantity | 0.48247 |
| Average entry | 62.204 |
| Opened | 2026-10-02T13:37:43Z |
| Entry client order id | `cli_1d1d1063b70a461b9e9e` |
| Broker order id | `d7eaecdd-4c25-4273-8685-8085e60d48b4` |

## Root cause: a module with zero call sites

`agent/positions/adoption.py` was written, tested, and **never
invoked** — zero call sites in the cycle handler.

So when the agent opened DRAM and failed to record it, nothing ever
closed the gap. Reconciliation saw a broker position the agent did not
hold, raised `RECONCILIATION_MISMATCH` and `EMERGENCY_STOP`, and halted
— correctly, and permanently.

**A module that is never invoked passes all of its own unit tests.**

## Step 1–2: provenance, proven

By the deployed classifier, not by hand:

```
origin    AGENT_CREATED
evidence  RECONSTRUCTED_CLIENT_ID
proven    true
detail    the venue's client order id cli_1d1d1063b70a461b9e9e is
          reproduced exactly by sha256('risk_adef08c5015b:ENTRY'),
          which only this agent's own risk decision could produce
```

The first classification attempt returned `CLIENT_ID_PREFIX_ONLY` with
`proven: false`, because `decisions` was passed as a list of rows while
`adopt_external_positions` calls `.for_session` on it. Every read
failed, the evidence degraded, and **it adopted nothing** — reporting
DRAM as `unknown_origin` with `blocks_new_exposure: true` and five read
errors listed. Fail-closed, with the reason named.

## Step 3–5: adoption

| | |
|---|---|
| Position id | `pos_e84ee9249b8840ca` |
| Quantity | 0.48247 — **exact** match with the broker, not rounded |
| Reconstructed stop | 58.0587 |
| Stop origin | `RECONSTRUCTED_FROM_RISK_LIMIT` |
| Bounded loss | (62.204 − 58.0587) × 0.48247 = **$2.0000** = `max_trade_risk` |
| Integrity | COMPLETE, zero errors |

### Idempotency depends on ORDER, and that is now a test

`PositionManager` holds positions in memory; the cycle persists through
a separate `DynamoDBPositionStore`. So `adopt_external_positions`
reported `adopted=1` three times in a row, each with a **new** position
id, while the store held nothing — true of the manager, false of the
store.

Idempotency comes from loading the store into the manager **first**,
which is what the cycle does at `handler.py:380`. Verified against the
real paper account:

```
pass 1   adopted=1  already_managed=0  pos_e84ee9249b8840ca
pass 2   adopted=0  already_managed=1  pos_e84ee9249b8840ca
pass 3   adopted=0  already_managed=1  pos_e84ee9249b8840ca
```

A test asserts the load precedes the adoption **in the source**,
because the two orderings are indistinguishable from the adoption
result alone.

## Step 6–9: the exit

```
client order id   cli_2f9aa45e7808d1b2d8f5
derived from      sha256('risk_adef08c5015b:EXIT')
side              SELL
quantity          0.48247
order type        MARKETABLE_LIMIT
limit price       61.5947   (0.3% below the broker's 61.78)
time in force     DAY
submitted         2026-10-04T20:49:13Z
status            SUBMITTED / raw "accepted"
```

Intent was persisted **before** submission, per
`docs/INTENT-BEFORE-SUBMIT.md`. `submit_exit` is deliberately not gated
on `execution_available` or on any halt: exits are always permitted,
because a position that cannot be closed is unmanaged risk.

`close_position` and close-all were **not** used. `DELETE
/v2/positions/{symbol}` accepts no client order id, so it cannot be
made idempotent — which is the whole reason exits go through
`submit_order`.

### Exit idempotency, proven

Resubmitting logged:

```
order_recovered_by_client_id — "intent existed; the venue already has
this order, so it was not sent again"
```

The broker holds **exactly two orders, ever**: the original BUY
(filled) and **one** SELL (accepted).

## Current state

| | |
|---|---|
| Broker position | DRAM 0.48247, still held |
| Broker open orders | 1 — the SELL, `accepted` |
| Ledger (2026-10-05) | 1 recorded, 1 outstanding, `read_integrity: COMPLETE` |
| Ledger outcome known | `true`, 4 observations |
| Committed exposure | $29.806997, **known** |
| Managed positions | 1, `risk_known_for: 1 of 1` |
| Market | CLOSED — next open 2026-10-05T09:30:00-04:00 |

## Steps 11–15: blocked on the fill

These **cannot** be completed before the open, and are not claimed:

11. prove final broker position flat
12. prove no open DRAM order
13. prove external ledger terminal
14. prove journal complete
15. prove reconciliation clean

### A gap risk that applies to this exit

The limit is 61.5947. If DRAM opens **below** that, the order does not
fill and the position stays open. That is the same gap exposure
measured throughout this work, and it is why `EXIT#2` and `EXIT#3`
exist as deterministic retry ids — already computed and absent from the
ledger:

```
attempt 2   EXIT#2   cli_c165f99ab7627dc9e40d
attempt 3   EXIT#3   cli_ac1aca4c83520d2736bc
```

A market order would have removed the gap-fill risk and added an
arbitrary-fill risk instead. The limit was chosen deliberately.

## The durable fix

Adoption now runs **every cycle**, after the store load and before
orchestration, sharing the orchestrator's order ledger — adoption must
read the evidence the exit will be written into, or the two could
disagree about what has already been sent, and that is how a duplicate
order happens.

Failure is logged and never raised: a failed adoption leaves the
position unmanaged, which is the state it was already in, and raising
would also stop the **exits**.

Only `proven` provenance adopts. `PREEXISTING_EXTERNAL` and
`UNKNOWN_ORIGIN` are never adopted and never auto-closed — they block
new exposure instead.

Tests: `tests/test_dram_adoption_wiring.py`, 10 tests, including a
falsifying control proving a reconstructed id **is** proven, so the
gate is not passing by being unsatisfiable.

## Latches: NOT cleared

The three latching conditions remain untouched. Per the standing rule
they stay until DRAM is flat, broker and order state are readable,
reconciliation is clean, the underlying defect is fixed and no
recurrence is observed.

The defect is fixed and deployed. **DRAM is not yet flat.** The human
clearance package is therefore not ready, and claiming otherwise to
make pre-market readiness green would be the exact inversion this rule
exists to prevent.

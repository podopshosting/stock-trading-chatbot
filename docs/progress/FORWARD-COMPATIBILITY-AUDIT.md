# Forward-compatibility audit: persisted enum-backed records

Prompted by a real failure on 2026-10-02. A `BROKER_UNAVAILABLE` alert
written by a newer cycle made the older API's alert read raise, and
`/agent/autonomy` returned `alerts: []` with the error in
`read_errors`. The endpoint named the problem, which is the design
working — but an operator saw *no alerts*, and a CRITICAL one in the
same session would have been equally invisible.

**Invariant.** A newer writer must never make an older reader hide the
rest of a safety-critical collection.

## The finding that changes the shape of the work

Tolerance is **not** the right answer everywhere, and applying the alert
fix uniformly would introduce a worse defect than the one it removed.

Each collection has a different safe failure:

| Collection | Safe failure | Why |
|---|---|---|
| **Alerts** | **Degrade** — show every readable row | Missing one alert is bad; showing none is worse. An operator who sees an unfamiliar kind can act; one who sees an empty list cannot. |
| **Orders** | **Fail closed** | An order that cannot be parsed is an order that might be submitted twice. Skipping the row destroys the idempotency guarantee. A dropped order is more dangerous than a stopped agent. |
| **Positions** | **Fail closed** | The agent must never believe it holds nothing while holding something. Every risk figure is computed from this collection. |
| **Health conditions** | **Fail closed to HALTED** | An unreadable health record is not a clean one. |
| **Journal trades** | **Degrade, and say so** | Used for daily counters and evidence. A partial read must be labelled partial, never summed as if complete. |
| **Reconciliation / cohort records** | **Degrade, and say so** | Evidence attribution. A missing row must reduce the claim, not the count silently. |

So the audit is not "make every reader tolerant". It is: **decide the
safe failure per collection, verify the code actually does that, and
make the degraded case explicit either way.**

## Enum coercion from stored data — every site

Found by searching for `Enum(stored_value)` across `agent/` and
`lambda-micro/`.

| Site | Field | Current behaviour |
|---|---|---|
| `agent/autonomy/alerts.py:220` | `AlertKind` | **FIXED 2026-10-02** — per-row, unknown kept as a raw string, unreadable rows skipped and logged |
| `agent/broker/store.py:151` | `OrderSide` | raises → cycle aborts |
| `agent/broker/store.py:152` | `OrderType` | raises → cycle aborts |
| `agent/broker/store.py:156` | `OrderStatus` | raises → cycle aborts |
| `agent/broker/store.py:~158` | `TimeInForce` | raises → cycle aborts |
| `agent/autonomy/health.py:277` | `Condition` | raises → API falls back to HALTED; cycle aborts |
| `agent/positions/store.py:105` | `PositionState` | raises → cycle aborts |
| `agent/positions/store.py:117` | `ExitReason` | raises → cycle aborts |

**In the cycle these all fail closed**, which is correct for orders and
positions. `restore_broker` is called unguarded at
`lambda-micro/agent-cycle/handler.py:323`, so a ValueError propagates
and the invocation now terminates as `ABORTED` with a health failure
recorded — visible, which it was not before today.

The alert reader was the outlier: it was the one that failed **open**.

## The systemic exposure is `_safe(..., [])`, not the enums

`_safe` in the dev API turns any failing read into its default plus a
marker. Twelve call sites pass `[]`:

```
def _safe(fn, default=None):
    try:
        return fn(), None
    except Exception as exc:
        return default, f"{type(exc).__name__}: {str(exc)[:120]}"
```

The error is recorded, so nothing is silent — but the **collection
itself** reads as a successful empty read to any consumer that does not
cross-check the error dictionary. That is the masquerade the invariant
forbids, and it is twelve places wide rather than one.

Two consumers are already correct, both fixed today:

- `/agent/autonomy` → `daily` returns `None` for every figure with a
  `basis` naming the failed read, rather than zeros
- `/agent/positions` → `open_count: None` and `source: "unreadable"`
  with a note that says *"This is not a report of zero"*

The remaining ten need the same treatment: a consumer that sums,
counts, or renders "none" from one of these lists must first establish
that the list is complete.

## Required test matrix, per collection

1. known row
2. unknown enum / status row
3. malformed row
4. known + unknown together
5. known + malformed together

Required outcomes: readable rows survive where degrading is correct;
the collection fails closed where that is correct; a degraded read is
reported explicitly; and `[]` is returned **only** when a successful
read proved zero records.

## Status

- Alerts: **done**, with the five-case matrix
- Cycle lifecycle and the `daily` contract: **done** 2026-10-02
- Orders, positions, health conditions, journal, reconciliation, cohort
  records: **audited above, not yet hardened**

Next, in this order, because it follows the risk: positions and orders
first (they feed risk arithmetic), then journal and reconciliation
(evidence), then cohort records.

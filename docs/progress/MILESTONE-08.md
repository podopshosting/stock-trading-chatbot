# Milestone 8 — Risk Governor

**Status:** COMPLETE
**Tests:** 728 → 795 (+67)
**Falsifying controls:** 30/30 caught (8 new)

## The structural guarantee

`RiskDecision.approved` is a **property derived from the absence of
rejection codes**, not a field. There is no setter, no force flag and no
override parameter. A language model, a prompt, a config value or a
caller cannot turn a rejection into an approval, because `approved` is
not a thing that can be set at all.

```python
>>> d = RiskDecision(..., reason_codes=[RejectionCode.SPREAD_TOO_WIDE])
>>> d.approved
False
>>> d.approved = True
AttributeError
```

A test asserts both that `approved` is absent from the dataclass fields
and that assignment raises.

## Fail closed

Missing data is a **rejection**, never a pass. An unknown spread is not
a tight spread; an absent quote age is not a fresh quote; unknown time
to close is not plenty of time. Every `None` routes to a reject, because
the alternative is that a provider outage quietly becomes permission to
trade.

## The Milestone 3 deferral, fixed

The emergency stop previously lived on the **daily session record**, so
a new session began with a fresh record and the halt silently
evaporated overnight. A stop that expires by itself is not a stop.

`GlobalHaltState` now lives in its own record with **no session date**:

```
PK = CONTROL#GLOBAL   SK = HALT
```

It persists until explicitly cleared, clearing requires naming who did
it, and writes are revision-guarded so two concurrent clears cannot both
succeed. `DynamoDBHaltStore.get` **fails closed**: if the halt state
cannot be read it returns halted, because an outage might be hiding a
stop.

## Configured limits

| | Value | Why |
|---|---|---|
| Daily capital | **$50** (max configurable $100) | a ceiling, never a target |
| Single-name cap | 60% of daily = $30 | concentration |
| Per-trade risk | $2.00 | |
| Daily loss limit | $5.00 → `DAILY_RISK_LOCK` | |
| Max concurrent | 2 | |
| **Max new/day** | **3** | with 2 concurrent and $50, more than 3 entries means churning the same capital and paying the spread twice each round trip |
| Max spread | 0.50% | |
| Min dollar volume | $20M | |
| Max quote age | 120s | |
| No entries within | 30 min of close | a position opened at 15:55 must be flattened by 16:00 — that is a round trip, not a trade |
| Min hypothesis strength | 0.35 | |

`RiskLimits` is **frozen**. A limit that the code it constrains could
mutate is not a limit. The prohibited-instrument flags cannot be enabled
by configuration at all: `validate()` raises if any of options, margin,
leverage, shorting, crypto, OTC or averaging-down is set true.

## Sizing is by risk, not conviction

The position is whatever amount puts `max_trade_risk` at stake given the
stop distance, capped by concentration and by what remains of the day.
Sizing on hypothesis strength would mean the most confident-looking
setups carry the largest losses — precisely backwards. A test asserts a
0.40-strength and a 0.99-strength hypothesis size identically.

## Unrealised losses count

`should_engage_risk_lock` counts realised **and** unrealised P&L.
Waiting for a loss to be realised would mean the limit is only enforced
after the damage is taken.

## Finding: an unreachable guard

The `remove-daily-capital-ceiling` mutation initially **survived**.
`position_size` already caps at the remaining allocation, so the
`DAILY_CAPITAL_EXCEEDED` check inside `_check_capital` is unreachable
through `evaluate`. It is legitimate defence in depth — the cap lives in
a different function and a future sizing change must not breach the
ceiling silently — but an unreachable guard that nothing tests is a
guard that will rot. It is now exercised directly, and the property that
makes it unreachable is asserted separately.

## Purity

The governor performs no I/O, reads no clock and fetches no market data.
Every input is supplied and snapshotted onto the decision, so a verdict
can be replayed exactly — which is what makes "why was this trade
allowed" answerable months later.

## Next

Milestone 9 — Paper Broker & Order Management.

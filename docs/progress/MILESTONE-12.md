# Milestone 12 — Historical Replay / Backtesting

Status: complete. 45 replay tests, 1,051 total, 83/83 mutations caught.

## Why this milestone is mostly about refusing to cheat

A backtest is the easiest thing in this project to get wrong in a way
that looks right. Lookahead does not make results slightly optimistic —
it makes them **arbitrary**, because a strategy that can see the next bar
can be made to return any number you like. And the output is
indistinguishable from a good result: the equity curve just looks
better.

So the guards here are structural, not conventional. Reaching past `now`
raises `LookaheadError`, and a run that raises is reported as
`VOID_LOOKAHEAD` rather than returned with a caveat. A contaminated
backtest cannot be partially trusted, because the amount of
contamination is unknown.

`ReplayResult.valid` is a derived property with no setter, so a run
cannot be marked valid after the fact by code that would prefer a
result.

## The two structural protections

**1. Nothing past the clock is reachable.**
`ReplayClock` only moves forward, and refuses to stand still.
`PointInTimeSeries` serves closes up to and including the current bar.
`PointInTimeEvidence` filters on publication time — news dated 14:30 is
not visible to a decision made at 14:00, which is where lookahead is
easiest to introduce by accident because a headline feels like context
rather than data. An item with **no** timestamp is never served: it
might have been published after the decision, and assuming otherwise is
the whole mistake.

Bars are validated as strictly increasing in time on construction.
Unsorted input would silently defeat the clock, because index order
would stop corresponding to time order.

**2. Decisions and fills are separated in time.**
An order decided on bar N fills at bar N+1's open. A backtest that
decides on a close and fills at that same close has used the fill price
as an input to the decision, which makes *every* strategy profitable.

`next_open()` is deliberately the only way to obtain a forward price,
and it returns a single number rather than a bar, so a caller cannot
reach the next bar's high and size a position against information it
will not have.

An order decided on the **final** bar is rejected with `NO_NEXT_BAR`
rather than filled. Inventing a fill there adds a trade that could not
have happened — and it will be one of the largest in the sample if the
run ends on a spike.

## Stop fills model gaps

A stop is filled at the **worse** of the stop price and the next bar's
open. Gapping through a stop is normal, and a backtest that fills every
stop exactly at its level understates losses systematically.

## One source of truth

The loop drives the real `signals.evaluate`, `hypothesis.generate`,
`risk.evaluate`, `PositionManager` and `journal` modules. It contains no
second, simpler model of the strategy — that would measure the model
rather than the system that will trade, and the two diverge exactly
where it matters.

This is also why the replay inherits the Milestone 11 adequacy gating
for free: a 2-trade run reports `NO_EDGE_DEMONSTRATED`, because it is
handing its trades to the same metrics code as live rather than a more
flattering version.

## Honesty in what a run reports

- **A missing regime is reported, never defaulted.** Without one the
  risk posture fails closed to `NO_NEW_TRADES`, which is correct — but a
  reader seeing zero trades would conclude the strategy found no setups
  when it was never allowed to look. Supplying a permissive default
  would be the replay relaxing a safety gate to manufacture trades.
- **Rejections are counted by code.** A heavily gated strategy must be
  distinguishable from a broken pipeline.
- **Warmup is enforced.** Deciding on bar 3 of a 200-bar moving average
  is not a strategy decision, it is a division by a number that happens
  to exist.
- **Unclosed positions are counted and warned about.** Their P&L is
  missing from the results, so the report says the performance is
  incomplete rather than presenting a partial figure.
- **Discarded evidence is reported**, so a run cannot proceed on a
  thinner feed than expected without anyone knowing.

## Wiring defects found

Three, all caught by the pipeline's own fail-closed behaviour rather
than by producing wrong numbers — which is the design working:

1. **Freshness defaulted to `UNKNOWN`**, a blocking contradiction, so no
   hypothesis was ever actionable. Replay may legitimately assert
   `FRESH`: at a bar's close, that bar *is* the current observation.
2. **I was hand-assembling the signal payload** from `aggregate()`, which
   omits `data_quality` — a field the hypothesis engine reads to decide
   whether a reading is fresh enough to act on. Narrowing the input to a
   safety gate by hand is precisely the kind of divergence rule 1 exists
   to prevent. Now routed through the same `signals.evaluate` entry point
   the live service uses.
3. **`RiskContext` field names were wrong** (`capital_used_today` vs
   `capital_deployed_today`), which Python accepted silently as unknown
   kwargs would not have been caught without checking the signature.

## Two weak tests of my own, found by mutation

- **The gap test asserted `fill < 97.0`** — which passed even when the
  gap was ignored, because the broker's slippage nudges the fill under
  the stop by itself. Slippage was masking the entire behaviour under
  test. Now asserts the fill reflects the gap (near 90).
- **`entries_filled == exits_filled` was vacuous.** With the default 3%
  trailing stop, no position ever survives to the last bar, so the
  end-of-data flatten was never exercised at run level. Added a case
  with the exit rules disabled, where the flatten is the only way out,
  plus a direct unit test of `_flatten_at_end`.

Both are the same failure: a test that passes because the situation
never arises is not coverage.

## Files

```
agent/replay/clock.py    ReplayClock, LookaheadError
agent/replay/data.py     Bar, PointInTimeSeries, PointInTimeEvidence
agent/replay/broker.py   next-bar fills, gap-aware stop resolution
agent/replay/engine.py   the loop, driving the live modules
tests/test_replay.py     45 tests, mostly attempts to cheat
```

## Still true

- `trading_enabled=false`, `execution_available=false` in the live path
- Production `/chatbot` untouched; the RSI fix remains undeployed
- Every replay trade is `is_paper=true`

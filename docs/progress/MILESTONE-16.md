# Milestone 16 — Strategy Evaluation & Calibration

Status: complete. 41 evaluation tests, 132/132 mutations caught.

Both modules in `agent/evaluation/` spend most of their effort refusing
to conclude things, which is the job.

## Calibration: is hypothesis strength predictive?

Strength is a claim — higher numbers should be followed by better
outcomes — and until it is tested the score is decoration. Worse, the
risk governor scales position size by it, so if strength does not order
outcomes the sizing is arbitrary.

`assess()` buckets trades by strength and compares the bands. It almost
always returns `INSUFFICIENT_DATA`, and the wording is deliberate: "not
tested" is different from "does not work", and the second would justify
removing the score.

The trap being avoided: seeing high-strength trades beat low-strength
ones over thirty trades, concluding the score works, and raising size on
high-strength signals. The difference between two 15-trade buckets is
almost entirely sampling variation. `MONOTONIC_BUT_NOT_SIGNIFICANT`
exists precisely for the common case where the ordering looks right and
the intervals overlap.

Trades with no recorded strength are **excluded**, not binned at zero.
An unknown strength is missing data, and placing it at zero would
fabricate a reading the system never made — and make the lowest band
look worse than it was.

The correlation function reports its interval and no p-value. A
coefficient from forty points has an interval so wide that quoting the
number alone is misleading, and no variation returns `None` rather than
zero — zero would claim there is no relationship when the question is
undefined.

## Sweeps: arguing against your own results

A parameter sweep is the most reliable way to produce a number that
looks like an improvement and is not one, for a reason that is pure
arithmetic: try twenty settings and the best will look good **even if
every setting is identical in truth**, because you are reporting the
maximum of twenty noisy measurements.

`expected_best_of_n()` quantifies that. With twenty arms and a 0.2R
spread, selection alone is expected to produce a 0.34R gap. A winner
that beats the field by less than that has demonstrated the selection,
not the parameter.

The module then requires three things in sequence, and reports which one
failed:

1. Arms of at least 30 trades (higher than a single metric's bar,
   because sweeps are the most overfitting-prone thing here)
2. A winner's gap exceeding the selection-noise floor
3. The winner surviving a **chronological** holdout

The holdout is deliberately not random. Market conditions cluster in
time, so a randomly split holdout is contaminated by the same regime the
selection was fitted to.

The strongest possible return value is `CANDIDATE_FOR_CHANGE`, and the
detail says in words that this is not an approved change.
`permits_change` is derived from the recommendation.

## Two self-defeating tests of mine, found by mutation

**A test that moved with the constant it guarded.** My small-arms test
used `MIN_TRADES_PER_ARM - 1` trades, so lowering the threshold from 30
to 1 left it passing — it could never catch a change to the very thing
it was protecting. Now uses a literal, plus a separate test asserting
the constant stays at or above 30.

**A test that accepted two outcomes.** My within-noise test used random
data and accepted either `MONOTONIC_BUT_NOT_SIGNIFICANT` or
`NOT_MONOTONIC`. When the sample came out unordered, a bug that ignored
interval overlap entirely was invisible. Now built deterministically,
and it asserts the fixture **is** monotonic before testing the verdict —
so it cannot pass on data that does not exercise the comparison.

Both are the same mistake in different clothing: an assertion loose
enough to be satisfied without the behaviour under test occurring.

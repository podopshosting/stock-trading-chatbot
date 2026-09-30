# Milestone 11 — Trade Journal & Performance Analytics

Status: complete. 75 journal tests, 1,006 total, 70/70 mutations caught.

## The problem this milestone is really about

Computing a win rate is trivial. Refusing to believe it is the hard
part.

With two concurrent positions and a $50 daily ceiling, this system will
take a few trades a day at most. After a month that is perhaps forty
trades. **Forty trades cannot distinguish a real edge from luck.**

The arithmetic is not intuitive. A 100% win rate over twelve trades has
a 95% lower bound of 76% — which sounds like a strategy and says almost
nothing about the thirteenth trade. A 60% win rate over twenty trades
has an interval of roughly 39%–78%, which contains 50%.

The natural failure mode is to look at fifteen profitable trades,
conclude the strategy works, and scale up. So every metric here carries
its sample size and an explicit judgement of what may be said about it.

## Adequacy gating

| Adequacy | Trades | What may be said |
|---|---|---|
| `INSUFFICIENT` | <20 | Report the number, claim nothing |
| `DIRECTIONAL` | ≥20 | May state a sign, not a magnitude |
| `ESTIMATE` | ≥30 | May quote a value with an interval |
| `DEMONSTRATED` | ≥100 | May describe an edge as demonstrated |

`Metric.is_evidence` requires **both** `DEMONSTRATED` adequacy **and**
a confidence interval that excludes the no-edge value. Both are derived
properties with no setters, so neither can be asserted independently of
the interval it describes.

`describe()["verdict"]` is the field to read. It returns
`NO_EDGE_DEMONSTRATED` for any sample that cannot support a claim,
whichever way the numbers happen to point, and its summary says
explicitly that the figures must not be used to justify increasing size.

The numbers are always reported. Withholding them would be its own
dishonesty — what changes with sample size is the *claim*, not the
disclosure. And the gate is passable: a test confirms that 120 trades
with a genuine 2R/-1R profile does reach
`POSITIVE_EDGE_DEMONSTRATED`, because a gate that can never open is a
refusal to measure rather than a standard.

Intervals use the **Wilson score** interval, not the normal
approximation, which is badly wrong at these sample sizes and wrong in
the direction of looking more certain than it is. Validated against
published reference values in the test suite.

## Honest denominators

- **Scratches are not wins.** A move smaller than 0.1R is noise.
  Counting scratches as wins is a cheap way to inflate a win rate, and
  including them in the denominator lets a strategy that mostly goes
  nowhere dilute its losses.
- **Unknown outcomes are excluded from statistics.** A trade whose
  result could not be determined is missing data, not a neutral result.
- **Profit factor is `None` when there are no losses yet.** An infinite
  profit factor is not a good sign; it is a sign of too small a sample.
- **Groups below 15 trades are marked `comparable: false`.** Slicing
  forty trades by strategy and time of day produces cells of two or
  three, and a "best performing strategy" chosen from those is noise
  with a label on it.

## R-multiples

Results are measured in R — P&L divided by the risk accepted at entry —
because it is the only fair way to compare trades of different sizes. A
$10 win on $5 of risk and a $100 win on $50 of risk are the same trade;
raw P&L says otherwise, which would make sizing changes look like
performance changes.

Critically, R is measured against **the stop as it stood at entry**, not
the trailed stop. Using the final stop would rewrite history and make
every trailing exit look like a 0R scratch, hiding that the trade risked
something when it was taken.

## Stop integrity — the most important diagnostic

`stop_integrity()` reports how often a loss came in worse than −1R.

If that is not rare, the stops are not holding, every upstream risk
calculation is wrong, and position sizes are systematically too large.
A breach is logged as an alert the moment it is recorded rather than
left to be noticed in a report later.

`max_adverse_excursion_r` exists for the converse: a winner that spent
time well below its stop level did not win because the plan worked, it
won because the stop was not enforced.

## The defect this found

**Money totals were excluding real money.** `total_net_pnl`,
`max_drawdown` and `total_costs` all filtered through `_counted()`,
which drops trades whose outcome is `UNKNOWN`.

But a trade with no measurable R **still moved real cash**. Excluding it
made the reported P&L disagree with the actual account balance — a worse
failure than an incomplete statistic, and one that would have been
mystifying to debug later. "Cannot compute R" and "did not happen" are
different things.

Split into `_counted()` for statistical denominators and
`_all_with_money()` for account facts. Three tests now assert the money
total reconciles with the sum of every trade.

The mutation harness also exposed that my original test for unknown
outcomes proved nothing: `_decisive`, `expectancy_r` and
`stop_integrity` all independently re-filter on `None`, so they are
immune to the filter being removed. The metrics that genuinely leak are
`trades_counted`, `profit_factor` and `group_by`, and I was asserting
none of them.

## Files

```
agent/journal/models.py     TradeRecord, R-multiples, outcome rules
agent/journal/metrics.py    adequacy gating, Wilson intervals, attribution
agent/journal/store.py      append-only, in-memory and DynamoDB
agent/journal/recorder.py   position -> journal, preserving provenance
tests/test_journal.py       75 tests
```

Also added `low_water_price` tracking to `ManagedPosition` so adverse
excursion is recorded rather than inferred.

## Still true

- `trading_enabled=false`, `execution_available=false`
- Production `/chatbot` untouched; the RSI fix remains undeployed
- Every journalled trade so far is `is_paper=true`, and `describe()`
  reports `paper_only` so a mixed history cannot be presented as live

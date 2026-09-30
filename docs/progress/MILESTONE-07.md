# Milestone 7 — Trade Hypothesis Engine

**Status:** COMPLETE
**Tests:** 682 → 728 (+46)
**Falsifying controls:** 22/22 caught (5 new)

## What it does

Combines the scanner context, the quantitative reading, published
evidence and the market regime into a structured argument. It produces
no order, no quantity and no approval; the Risk Governor comes next and
is the only component that can approve anything.

## The design rule

The quantitative engine and the evidence engine may disagree, and that
disagreement is information. Three cases are all expressible:

| Inputs | Result |
|---|---|
| quant BUY + no catalyst | MOMENTUM hypothesis, labelled as having no published reason |
| quant BUY + material NEGATIVE | **BLOCKING** contradiction, no hypothesis |
| quant NEUTRAL + evidence POSITIVE | **not** a trade — the market saw the news and declined to act |

That last one matters most. Good news on a stock that is not moving is
a story. A system that bought every positive headline would be trading
the news wire rather than the market.

## Strategies

`MOMENTUM` · `MOMENTUM_CATALYST` · `BREAKOUT` · `MEAN_REVERSION` ·
`CATALYST_CONTINUATION` · `NO_VALID_STRATEGY`

Deliberately small. Dozens of strategies would be unfalsifiable: each
would have too few trades to evaluate, and the set would encode
curve-fitting rather than a thesis.

## hypothesis_strength is not an opaque score

It is a combination, but every input that produced it is carried
alongside, the contributions are itemised in `strength_components`, and
the contradictions that reduced it are listed individually rather than
netted. The components sum exactly to the score, and a test asserts it.

```
signal_agreement       +0.3500
signal_magnitude       +0.1900
evidence_support       +0.2025
market_regime          +0.1350
contradiction_penalty  +0.0000
TOTAL                  +0.8775
```

## Blocking vs advisory contradictions

**BLOCKING** — stale price data, strongly bearish regime, blocking risk
posture, material published negative. No hypothesis is generated.

**MAJOR / MINOR** — weak agreement, weak magnitude, conflicting
evidence, an opposing signal group, an unfavourable or unknown regime,
no catalyst, evidence not collected. Recorded, penalised, proceeds.

"We did not look" and "we looked and found nothing" are stored as
different contradictions, because only one of them is an observation.

## Abstract risk inputs, not orders

`reference_price`, `suggested_stop_distance_pct` and
`reference_volatility_pct` exist because the Risk Governor cannot judge
risk without a notion of how far wrong the idea could go. The stop
distance is expressed in units of the security's own volatility — a flat
2% stop is noise on one name and a catastrophe on another — and nothing
turns it into a quantity.

## Provenance

Every hypothesis is stamped with `config_version` (`hypothesis-v1.0.0`)
and the scanner, signal and evidence run ids that fed it. Without those
a later review cannot reproduce why the hypothesis existed.

## Defect found

My own test scanned the serialised payload for banned order fields and
flagged **its own disclaimer**, which legitimately says "carries no
order, quantity or price". Same denial-in-prose class as the Milestone 5
UI scan. The test now checks the JSON **keys**, with a falsifying
control proving it can still fail.

## AWS changes

None. Storage is modelled (`DynamoDBHypothesisStore`) but no table was
created yet; the next milestone determines the final schema needs.

## Next

Milestone 8 — Risk Governor.

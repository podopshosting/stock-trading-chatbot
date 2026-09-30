# Market Regime Engine

Quantitative classification of the broad market environment, evaluated
before any individual security is considered. Milestone 3.

**Deterministic and LLM-free.** The engine is a pure function from
normalised data to a structured result: no provider types, no network, no
model. The same inputs always produce the same classification, which is
what makes it testable and what makes a later explanation trustworthy —
the LLM describes this result, it never decides it.

---

## 1. Pipeline

```
AlpacaProvider ──▶ CachedProvider ──▶ MarketRegimeService
                                            │  normalises to IndexInput
                                            ▼
                                   MarketRegimeEngine  (pure)
                                            │
                                            ▼
                                   MarketRegimeResult
                                            │
                                            ▼
                                    AgentStateService
```

All provider knowledge lives in `MarketRegimeService`. The engine never
sees a `Quote`, a `BarSet` or an HTTP error, so a provider swap leaves it
untouched.

---

## 2. Instruments

| Symbol | Role |
|---|---|
| `SPY` | Broad large-cap |
| `QQQ` | Large-cap technology |
| `IWM` | Small-cap |

Configurable via `RegimeConfig.instruments`. `DIA`, a VIX source, sector
ETFs, Treasury yields and the dollar index can be added later; scope is
deliberately not expanded before there is a reason.

**No VIX value is invented.** Volatility is computed from realised price
behaviour, because no VIX source is integrated.

---

## 3. Inputs per instrument

| Input | Source | Purpose |
|---|---|---|
| price | batched snapshot | current level |
| session open | snapshot daily bar | intraday direction |
| previous close | snapshot previous daily bar | fallback baseline |
| 60 daily closes | `1day` bars | SMA20, SMA50, realised volatility |
| 78 × 5-minute bars | `5min` bars | session VWAP |

**Request cost: 7 per evaluation** — one batched snapshot covering all
three symbols, then one daily and one intraday call each. Measured, not
estimated. A cached repeat inside the TTL costs 0.

---

## 4. Features

### Trend score, per instrument → `[-1, +1]`

The mean of up to three sub-signals, each squashed against a reference
size:

| Sub-signal | Formula | Reference |
|---|---|---|
| Price vs short average | `(price − SMA20) / SMA20` | 2.0% |
| Average spread | `(SMA20 − SMA50) / SMA50` | 1.5% |
| Session direction | `(price − open) / open` | 1.0% |

`scaled(x, ref) = clamp(x / ref, −1, +1)`

The references are judgement calls about what counts as a decisive move
for a broad index — not measured constants. They are the most obvious
thing to revisit once there is a performance record.

A sub-signal that cannot be computed is omitted rather than zeroed, so
missing history does not read as a flat market.

### VWAP

`Σ(typical_price × volume) / Σ(volume)` over the session's bars, where
`typical_price = (high + low + close) / 3`.

Returns `None` when total volume is zero. Falling back to an unweighted
mean would produce a number that looks like a VWAP but is not one.

**Deadband:** within ±0.05% of VWAP, the instrument counts as *at* VWAP —
neither above nor below. Without this, `price == vwap` contributed a full
+1 to the alignment term and pushed an exactly flat market to `BULLISH`.
That was a real defect, found by a test.

### Cross-index confirmation → `[-1, +1]`

`(n_positive − n_negative) / n_usable`, with a ±0.05 dead zone on each
trend score so noise does not count as a direction.

### VWAP alignment → `[-1, +1]`

`(n_above − n_below) / n_with_vwap`.

### Realised volatility

Annualised from daily closes:

```
r_i  = ln(close_i / close_{i−1})          last 20 returns
vol  = stdev_sample(r) × sqrt(252)
```

Sample standard deviation (÷ n−1), because these returns are a sample of a
process rather than a whole population. Returns `None` below 21 closes —
zero volatility would read as a calm market, which is not the same as not
knowing.

### Dispersion

`max(trend_scores) − min(trend_scores)`. Drives the `MIXED` override.

---

## 5. Weights

| Component | Weight |
|---|---|
| SPY trend | 0.30 |
| QQQ trend | 0.25 |
| IWM trend | 0.20 |
| Cross-index confirmation | 0.15 |
| VWAP alignment | 0.10 |
| **Total** | **1.00** |

Validated to sum to 1.0 at construction; a bad set raises rather than
silently skewing every score. Override with
`AgentConfig.with_weights(...)`, which re-validates.

### Volatility has no directional weight

A violent selloff and a violent rally both raise realised volatility, so
giving it a directional weight would be a category error. It acts as a
confidence penalty and, at the extreme, takes the label.

### Rationale for the ordering

SPY carries the most weight as the broadest measure. QQQ is next because
index-level moves are heavily influenced by large-cap technology. IWM is
weighted lowest individually but earns its place by disagreeing — a
divergence between QQQ and IWM is the signal that produces `MIXED` and
`NARROW_LARGE_CAP`, which a SPY-only view would miss entirely.

**None of these weights is validated against out-of-sample results.**

---

## 6. Scoring

```
trend_component = Σ (trend_score_i × weight_i / Σ weights_of_usable)

raw = trend_component × (w_spy + w_qqq + w_iwm)
    + confirmation     × w_cross
    + vwap_alignment   × w_vwap

raw_score = clamp(raw / active_weight, −1, +1)
```

`active_weight` excludes terms that could not be computed, so the score
stays comparable across evaluations with different data availability.

**Weights of unusable instruments are redistributed** across the rest. An
index we could not price is excluded, not scored as zero — scoring it flat
would drag the result toward `NEUTRAL` and impersonate genuine
indecision. A test pins that a missing index preserves the *direction* but
lowers *confidence*.

---

## 7. Taxonomy

### Primary regime, from `raw_score`

| Score | Regime | Trend |
|---|---|---|
| ≥ +0.60 | `STRONG_BULLISH` | `UP` |
| ≥ +0.20 | `BULLISH` | `UP` |
| > −0.20 | `NEUTRAL` | `FLAT` |
| > −0.60 | `BEARISH` | `DOWN` |
| ≤ −0.60 | `STRONG_BEARISH` | `DOWN` |

### Overrides

| Condition | Result |
|---|---|
| realised vol ≥ 0.40 | `VOLATILE` — the environment is better described by its violence than by a direction |
| dispersion ≥ 0.90 and regime ∈ {NEUTRAL, BULLISH, BEARISH} | `MIXED` |
| usable instruments < 2 | `UNKNOWN`, confidence 0 |

Every override is reported in `reasons`, naming the label it replaced.

### Separate dimensions

Reported alongside, never collapsed into one label:

| Dimension | Values |
|---|---|
| `trend` | `UP` · `FLAT` · `DOWN` · `UNKNOWN` |
| `risk_mode` | `RISK_ON` · `NEUTRAL` · `RISK_OFF` · `UNKNOWN` |
| `volatility` | `NORMAL` · `ELEVATED` · `EXTREME` · `UNKNOWN` |
| `breadth_proxy` | `BROAD_POSITIVE` · `BROAD_NEGATIVE` · `NARROW_LARGE_CAP` · `SMALL_CAP_LEADERSHIP` · `NEUTRAL` · `UNKNOWN` |

`breadth_proxy` is named a proxy because it is one: real breadth needs
advance/decline data this system does not have. It compares QQQ against
IWM.

---

## 8. Confidence

```
confidence = agreement × completeness × freshness
```

| Factor | Definition |
|---|---|
| agreement | `1 − dispersion / 2` — do the instruments point the same way? |
| completeness | `usable / requested` |
| freshness | `1 − stale_share × (1 − 0.5)` |

Multiplicative because each can independently invalidate the
classification.

### Confidence is not a probability

It measures **agreement among the inputs and the quality of the data
behind them**. A confidence of 0.9 means "the evidence is consistent and
fresh". It does **not** mean "a 90% chance the market rises". Nothing in
this engine forecasts anything.

The serialised result carries a `confidence_meaning` field saying exactly
this, because a number labelled "confidence" sitting next to a direction
invites precisely the wrong reading.

---

## 9. Freshness and missing data

| Label | Condition |
|---|---|
| `FRESH` | age ≤ 300s, or age unknown but price present |
| `STALE` | 300s < age ≤ 1800s |
| `MISSING` | no price, an error, or age > 1800s |

Age is measured from when the **provider** produced the value, not from
the cache read — `Provenance.retrieved_at` is preserved through the cache
precisely so this stays honest.

Consequences:

- `STALE` inputs halve their contribution to confidence and add a warning.
- `MISSING` inputs are excluded from the score entirely.
- Fewer than 2 usable instruments → `UNKNOWN`, confidence `0`, posture
  `NO_NEW_TRADES`. One index is not a market.
- Data beyond the stale ceiling is treated as missing, not used with a
  discount.

Stale data is never silently substituted for fresh data.

---

## 10. Regime transitions

A transition is recorded when the label changes **and** the change is
meaningful:

```
label_changed AND (
      |new_score − old_score| ≥ 0.10
   OR either side is UNKNOWN
   OR a previous score is absent
)
```

A label flip driven by a 0.01 move across a boundary is a wobble, not a
new environment; the label still updates but the history stays readable.

Movement to or from `UNKNOWN` is **always** recorded — that reflects data
availability changing, which matters regardless of magnitude.

Stored per transition: previous and new regime, both scores, timestamp and
the primary reason.

---

## 11. Risk posture

An **input to the future Risk Governor**, not a decision. The Risk
Governor decides what any of it means operationally.

| Condition | Posture |
|---|---|
| `UNKNOWN` | `NO_NEW_TRADES` |
| `STRONG_BEARISH`, or volatility `EXTREME` | `RESTRICTED` |
| `BEARISH`, `MIXED`, `VOLATILE` | `CAUTIOUS` |
| confidence < 0.35 | `CAUTIOUS` |
| otherwise | `NORMAL` |

---

## 12. Worked example — live, 2026-09-30 mid-session

| Instrument | Price | Session | vs SMA20 | SMA spread | VWAP | Trend |
|---|---|---|---|---|---|---|
| SPY | 768.79 | +0.31% | +0.45% | +0.33% | above | **+0.249** |
| QQQ | 744.50 | +0.58% | +2.72% | +1.36% | above | **+0.830** |
| IWM | 279.51 | −0.24% | −2.45% | −2.25% | below | **−0.745** |

```
raw_score   +0.2166      → BULLISH by score
dispersion   1.5755      → ≥ 0.90, override to MIXED
volatility   0.1268      → NORMAL
confidence   0.2122      → low: the instruments disagree
breadth      NARROW_LARGE_CAP
posture      CAUTIOUS
```

A genuinely divergent tape — technology up, small caps down — and the
engine declined to call it bullish. That is the override doing its job.

---

## 13. Known limitations

1. **Reference magnitudes are judgement, not measurement.** The 2.0% /
   1.5% / 1.0% scaling references and every threshold are starting
   defaults chosen for explainability. None is validated out-of-sample.
2. **No VIX, no term structure, no yields, no breadth data.** `breadth_proxy`
   is two ETFs compared, not advance/decline.
3. **Quote data is 15 minutes delayed** on the free consolidated feed. For
   a regime view that is acceptable; it would not be for execution timing.
4. **Realised volatility is backward-looking** over 20 days and reacts
   slowly. A gap that opens violently today will not show as `EXTREME`
   until it persists.
5. **Three correlated instruments.** SPY, QQQ and IWM overlap
   substantially, so "agreement" among them is weaker evidence than three
   independent measurements would be. Confidence should not be read as if
   the inputs were independent.
6. **No intraday regime memory.** Each evaluation is independent apart
   from the transition record; there is no smoothing across the session.
7. **The `MIXED` and `VOLATILE` thresholds have never been triggered by a
   crisis tape**, only by synthetic fixtures and one ordinary divergent
   session.

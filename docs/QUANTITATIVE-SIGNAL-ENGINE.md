# Quantitative Signal Engine

**Signal agreement is not a probability of profit.** Neither is signal
magnitude. Neither is the two combined. Nothing in this document
describes a likelihood that a price will move, and nothing here is
investment advice.

The engine answers one question:

> What direction do the independent market signals support, how strongly
> do they support it, and where do they disagree?

It does not answer whether to trade, at what price, in what size, or
with what stop. There is no entry, target, stop, position size or
expected return anywhere in its output, and no broker adapter exists in
this build.

---

## Where the logic lives

```
agent/signals/
├── models.py          types: SignalResult, SignalGroupResult,
│                      QuantitativeSignalResult, SignalRun, enums
├── indicators.py      the arithmetic; pure functions over closes
├── normalization.py   magnitude normalisation
├── engine.py          indicator -> signal -> group -> aggregate -> regime
├── reasons.py         deterministic explanation
├── store.py           persistence
└── service.py         orchestration, scanner integration, events
```

This is the **one** canonical engine. `agent/analysis.py` renders it for
the UI and holds no thresholds of its own; the review UX consumes the
rendered structure and recomputes nothing.

### The one known duplicate, and why it is pinned

`lambda-micro/chatbot-router/ml_agent_lite.py` carries an equivalent
copy of the arithmetic because it is what the deployed production
`/chatbot` Lambda runs, and rebuilding that artifact is a production
change this milestone is not authorised to make.

Two copies of arithmetic drift — not as a risk but as a certainty, given
enough edits. `tests/test_signal_equivalence.py` pins them to each other
on randomised inputs and fails if they diverge to more than 1e-9.
Deliberate differences are named in that file, never left implicit.

---

## Three quantities, kept apart

Compressing these into one "confidence" percentage is what made the
previous output unreadable.

| | What it measures | What moves it |
|---|---|---|
| **direction** | which way the evidence points | group votes |
| **signal agreement** | how many *independent* groups concur | structure only |
| **signal magnitude** | how far readings sit beyond their thresholds | size only |

Agreement contains no magnitude term, so it cannot rise because a
reading got bigger. Magnitude contains no agreement term, so it cannot
rise because more groups concurred. A result can be:

- BUY, agreement 1.00, magnitude 0.51 — everything agrees, nothing
  emphatically *(GOOG, 2026-09-30)*
- BUY, agreement 0.50, magnitude 0.88 — one group shouting, the rest
  contesting it *(IOVA, same run)*

Those are different pieces of evidence. The old model multiplied them
and reported a single number, so they were indistinguishable.

---

## Signal vocabulary

`BUY` · `SELL` · `NEUTRAL` · `NO_SIGNAL`

`STRONG_BUY` / `STRONG_SELL` are deliberately **absent**. Strength is
measured separately and continuously; two more discrete direction levels
would invent granularity the data does not support.

**NEUTRAL and NO_SIGNAL are different claims.**

- `NEUTRAL` — the indicator computed and reports no direction. RSI 52 is
  a measurement.
- `NO_SIGNAL` — the indicator could not compute. Not enough history, or
  unusable data. **Absence of evidence, not evidence of balance.**

Counting a `NO_SIGNAL` as a neutral vote would let missing data look
like considered agnosticism.

Groups add a fifth state, `MIXED`: the group's own members oppose each
other with equal strength, so it casts no vote. Distinct again from
NEUTRAL (members agreed there is no direction).

---

## Indicators

Every indicator returns the same shape: direction, normalised strength
in [0, 1], the raw values it used, the thresholds it applied, a reason,
freshness and a timestamp. Downstream code never needs to understand an
indicator-specific payload.

### MACD (12 / 26 / 9) — group: momentum

**Accepted baseline. The arithmetic must not be altered.**

```
MACD line = EMA12 - EMA26
signal    = EMA9(MACD line)          <- an EMA of the LINE, not of price
histogram = MACD line - signal
```

Needs `26 + 9 - 1 = 34` closes; returns None below that.

| | |
|---|---|
| **Direction** | `MACD > signal` → BUY; `MACD < signal` → SELL |
| **Neutral band** | `abs(histogram) <= 1e-6 × price` → NEUTRAL |
| **Strength** | `(abs(histogram) / price) / realised_vol`, saturating, scale 0.25 |

The crossover **is** the indicator. `macd > 0` is a trend comparison and
carries none of it.

> This once set `signal = macd × 0.9`. The consequence was not cosmetic:
> histogram became `0.1 × macd`, so it always carried the MACD line's
> sign and `macd > signal` reduced to `macd > 0` — i.e. to `EMA12 >
> EMA26`. Over 300 random walks the histogram sign matched the line's
> sign 300 times out of 300, and MACD was silently voting as a second
> moving-average crossover while appearing independent.

The neutral band is new and does **not** touch the arithmetic. Without
it, a frozen series where line and signal are both exactly `0.0` fails
`line > signal` and reads as a **bearish crossover** — a halted security
casting a SELL vote for having done nothing.

### RSI (14) — group: momentum

```
RSI = 100 - 100 / (1 + avg_gain / avg_loss)     over the last 14 changes
```

| Reading | Direction |
|---|---|
| `< 30` | BUY |
| `> 70` | SELL |
| `40–60` | NEUTRAL |
| `30–40`, `60–70` | NEUTRAL (no vote) |
| exactly 30 or 70 | NEUTRAL — the rule is *strictly* beyond |

**Strength** = distance past the threshold, saturating, scale 12 RSI
points. RSI 72 is weaker than RSI 86, which the old constant 0.85 could
not express. RSI needs no volatility normalisation: it is a bounded
0–100 oscillator and scale-free by construction.

**A reading of 66 is not a buy.** Translating relative strength into a
directional vote would double-count trend, which RSI is measured to
track (r = −0.571 against 10-day momentum).

> **Defect fixed:** `avg_loss == 0` short-circuited to RSI 100. With no
> gains *and* no losses the series did not move and RSI is 0/0 —
> undefined, not maximal. A flat series now returns **50**. A series
> that only rose still returns 100.

### Moving-average crossover (20 / 50) — group: trend

| | |
|---|---|
| **Direction** | `SMA20 > SMA50 × 1.02` → BUY; `< 0.98` → SELL; else NEUTRAL |
| **Strength** | `(abs(SMA20 − SMA50) / price) / realised_vol`, scale 0.60 |

The 2% band exists because two averages within 2% are effectively the
same line, and voting on that produces a signal that flips on noise.

### Golden / death cross (50 / 200) — group: trend

| | |
|---|---|
| **Direction** | `SMA50 > SMA200` → BUY, else SELL |
| **Strength** | `(abs(SMA50 − SMA200) / price) / realised_vol`, scale 0.60 |

Needs 200 closes. Kept separate from the 20/50 crossover: they are
correlated (r = +0.237) but not the same observation, and the trend
group is what handles that correlation.

**Known asymmetry:** this indicator has **no neutral band**, so it
always votes — it fires on 100% of evaluations where 200 closes exist,
including when the two averages are within 0.01% of each other. The
magnitude normalisation mitigates it (a meaningless separation scores at
the 0.25 floor) but the vote still counts toward direction. Adding a
band would be an unvalidated tuning change and has not been made. See
*Limitations*.

### 10-day momentum — group: momentum

```
momentum = (close[-1] - close[-11]) / close[-11] × 100
```

Positive means price is higher than ten sessions ago. Votes only beyond
±5%; a smaller move is inside the ordinary range of most liquid names
and would fire constantly.

**Strength** = excess beyond the threshold, in volatility units, scale
1.20. A 10% ten-day move is unremarkable on a name that swings 6% a day
and notable on one that swings 0.5%.

Returns `None` for short history rather than `0.0`: "no change" and
"cannot tell" are different claims.

### Bollinger Bands (20, 2σ) — group: mean reversion

| | |
|---|---|
| **Direction** | close **above** the upper band → SELL; **below** the lower → BUY; inside → NEUTRAL |
| **Strength** | excess beyond the band ÷ band half-width, scale 0.35 |

**Being near a band is not a signal.** The rule is a close *outside* the
band. Making proximity directional would fire the mean-reversion group
almost continuously and it would stop being independent of trend. This
group is silent (NEUTRAL) roughly 88% of the time by design.

Self-normalising: the band half-width already embeds two standard
deviations of the security's recent dispersion.

---

## Normalisation

Two rules govern all of it.

**Never compare raw currency across securities.** A $1 MACD histogram is
enormous on a $10 stock and noise on a $1,000 one. Every magnitude is a
ratio — to price, to the security's own volatility, or to the width of
its own bands.

**A signal that has just crossed its threshold is weak, not absent.**
Each map has a floor of 0.25, so a reading one tick past its cutoff
produces a small positive magnitude. Zero is reserved for "not
directional at all".

All maps saturate rather than clip:

```
saturate(r, k) = r / (r + k)        smooth, bounded [0, 1), 0.5 at r = k
strength       = 0.25 + 0.75 × saturate(ratio, scale)
```

Volatility is realised daily volatility — the standard deviation of the
last 20 daily returns — floored at 0.003. Below that a security is
halted, barely traded, or has bad data, and dividing by it would turn
rounding error into a maximal signal. A *missing* volatility falls back
to the floor rather than inflating every magnitude for that symbol.

The scale constants are **choices, not discovered truths**. They were
set so a typical crossing lands mid-range on liquid large caps and have
**not** been validated out of sample. Tests assert properties —
monotonicity, bounds, volatility invariance, price-level invariance —
rather than pinning the numbers, so they can be revised without
rewriting the suite.

---

## Correlation groups

```
trend            ma_crossover, golden_cross
momentum         rsi, macd, momentum_10d
mean_reversion   bollinger
```

Membership was set from **measured** pairwise correlation of directional
votes over random-walk series, not from intuition.

A group contributes **at most one independent vote**, however many of its
members fire. That is the entire reason groups exist: three momentum
indicators agreeing is one observation seen three times, and counting it
as three is how a single fact came to be reported as independent
agreement.

### Within a group

1. Opposing members **net** against each other rather than both counting:
   `net = Σ (vote × strength)`.
2. `net > 0` → BUY, `net < 0` → SELL, `net == 0` with opposing members →
   MIXED (no vote), no directional members → NEUTRAL, nothing computable
   → NO_SIGNAL.
3. Members that conflict set `internal_agreement = 0.5` — the group's
   influence is **halved**. Internal disagreement is real information
   about the quality of that group's opinion.
4. `magnitude` = the **strongest** member supporting the group's
   direction. A mean would let a weak agreeing member dilute a strong
   one.
5. `weight = magnitude × internal_agreement`.

### Across groups

```
opinionated  = groups with a directional vote
winning      = the majority side
agreement    = (len(winning) / len(opinionated)) × mean(internal_agreement of winning)
magnitude    = mean(magnitude of winning)
```

An **even split** returns NEUTRAL with agreement 0.0. That is genuine
indecision; reporting a direction would invent a tiebreak the evidence
does not contain. The group counts remain visible, so a conflict is
distinguishable from silence.

---

## Regime adjustment

The market regime (Milestone 3) scales **magnitude only**. It never
creates, removes or reverses a direction — a hostile market can make
evidence count for less, but it can never make evidence appear.

Raw and adjusted values are both retained: `signal_magnitude` and
`regime_adjusted_magnitude`. Agreement is untouched, because a hostile
market does not change how many groups agreed.

| Regime | BUY | SELL |
|---|---|---|
| STRONG_BULLISH | 1.00 | 0.55 |
| BULLISH | 1.00 | 0.70 |
| NEUTRAL | 0.95 | 0.95 |
| MIXED | 0.85 | 0.85 |
| VOLATILE | 0.75 | 0.85 |
| BEARISH | 0.70 | 1.00 |
| STRONG_BEARISH | 0.55 | 1.00 |
| UNKNOWN | 0.70 | 0.70 |

`UNKNOWN` is discounted like a hostile regime. Not knowing the market is
not the same as a calm one.

**The project is long-only operationally.** A SELL reading is analysis —
"the indicators lean down" — and must never be read as a short
instruction.

---

## Missing and stale data

Every indicator returns `NO_SIGNAL` rather than a substitute when it
cannot compute. There is no default RSI, no assumed moving average and
no zero-filled history.

| Condition | Behaviour |
|---|---|
| fewer than 50 closes | no analysis at all; `analysis_available = false`, stated in a warning |
| MACD with < 34 closes | NO_SIGNAL |
| SMA200 unavailable | golden cross NO_SIGNAL, warning lists it |
| degenerate bands | NO_SIGNAL |
| stale quote | evaluated, with a stale warning |
| unknown freshness | evaluated, with a caution warning — **never treated as fresh** |
| provider failure | a stated absence, not an exception; one bad symbol cannot abort a run |

**The system prefers `NO_SIGNAL` over fake certainty.**

---

## Reasons

Deterministic. The same result always produces the same sentences. No
language model: an explanation that can drift from the numbers it claims
to explain is worse than none, because it reads as authoritative while
being unfalsifiable.

Chat questions about signals (`agent/chat_grounding.py`) are answered
from the stored result for the same reason — a fabricated MACD value is
indistinguishable from a real one to the reader.

---

## Persistence

DynamoDB single-table, `stock-agent-dev-signals`:

```
PK = SIGRUN#<id>     SK = META                run metadata
PK = SIGRUN#<id>     SK = RESULT#<symbol>     one symbol
PK = SYMBOL#<sym>    SK = SIGNAL#<ts>#<run>   per-symbol history
PK = SESSION#<date>  SK = SIGRUN#<ts>#<run>   run index
```

Raw bars are **not** stored: large, reproducible from the provider, and
they go stale while looking authoritative. What is stored is the reading
and the numbers it was derived from.

Run ids carry a nonce. Second-resolution timestamps collided in the
scanner and the second run silently overwrote the first.

---

## Scanner integration

```
scanner (14,388 symbols -> 25 candidates)
   -> top N by rank
      -> signal engine (2 provider calls per symbol)
```

`N` is configurable (`SignalService(top_n=…)`, default 10). Deep
analysis costs a quote and a bars request per symbol, so it runs on the
scanner's top N rather than the universe — the scanner exists precisely
to decide what is worth that.

Signal-engine provider calls are tracked **separately** from the
scanner's. A single combined number would make neither controllable.

---

## API

Read-only. No route triggers an evaluation that a stranger could use to
spend provider quota; the one admin route in this API is disabled by
default.

| Route | Returns |
|---|---|
| `GET /agent/signals?symbol=X` | canonical result — groups, per-indicator raw values and thresholds, regime adjustment, reasons |
| `GET /agent/analysis?symbol=X` | the same result rendered for the UI |
| `GET /agent/signals/latest` | most recent **stored** run |
| `GET /agent/signals/latest?symbol=X` | most recent stored result for one symbol |
| `GET /agent/scanner/signals` | signals for a scanner run's candidates |

---

## Observability

`signal_engine_started` · `signal_engine_completed` · `signal_engine_failed`
· `indicator_no_signal` · `indicator_error` · `group_disagreement` ·
`signal_direction_changed` · `signal_result_persisted` ·
`regime_adjustment_applied`

Raw time series are never logged.

---

## Statistical controls

`scripts/analyze_signal_statistics.py` runs the engine over a synthetic
population and reports what unit tests structurally cannot see:
duplicate indicators, dead indicators, dominance, impossible states and
sign errors. `tests/test_signal_statistics.py` turns those into
regression tripwires.

This is the harness that exposed the original MACD defect, which every
individual case had looked reasonable under.

`scripts/falsifying_controls.py` mutates the real source to reintroduce
eight defects and asserts the suite turns red for each. A mutation that
survives marks a guard that cannot fail — worse than no guard, because
it reads as coverage.

---

## Limitations

- **Nothing here has been validated out of sample.** Every threshold and
  scale constant is a starting hypothesis.
- **Daily bars only.** No intraday signal, no volume confirmation, no
  fundamentals, no catalysts.
- **The golden cross never abstains** (see above).
- **Correlation groups were measured on random walks**, not on real
  market data across regimes. The relationships may differ in a trending
  or crisis market.
- **Group membership is fixed, not learned.** A new indicator requires a
  human to choose its group.
- **Mean reversion is a single indicator**, so that group has no internal
  netting to do and cannot exhibit internal disagreement.
- **Magnitude floors at 0.25** for any directional signal, so a barely
  crossed threshold is never scored at zero. That is deliberate, and it
  means magnitude is not proportional to evidence near a boundary.

---

## What this is not

- Not a probability of a price move.
- Not a probability of a profitable trade.
- Not an expected return.
- Not a forecast.
- Not investment advice.
- Not executable. There is no broker adapter, paper or live, and
  `execution_available` is `false` in every response.

# Controlled Market Scanner

Milestone 4. A disciplined funnel that answers one question:

> **Which liquid securities deserve deeper analysis right now, and why?**

It does not answer "what should I buy". A `Candidate` carries no side,
entry, target or stop, and nothing in `agent/scanner/` references a broker
adapter — a test asserts that. `scanner_score` ranks *research priority*,
never expected return.

**No execution exists in this build.** Every scanner run serialises
`execution_available: false`.

---

## 1. Pipeline

```
/v2/assets ──────────▶  UNIVERSE                    1 request, 14,388 assets
                            │                       (no price, no volume)
                            ▼
                        STATIC FILTERS              0 requests
                        exchange, tradable, status,
                        asset class, type, leveraged
                            │  12,308 survive
                            ▼
                        BATCHED SNAPSHOTS          ~14 requests, 1,000/req
                        price, volume, bid/ask,
                        session VWAP, prev close
                            │
                            ▼
                        DYNAMIC FILTERS             0 requests
                        price band, dollar volume,
                        spread, freshness
                            │  ~1,750 survive
                            ▼
                        SHORTLIST CAP               0 requests
                        top 300 by turnover
                            │
                            ▼
                        DAILY + INTRADAY BARS      ~7 requests
                        20d ADV, 5/15/30m returns
                            │
                            ▼
                        FEATURES ─▶ SCORING ─▶ REGIME GATE ─▶ RANKING
                            │
                            ▼
                        PERSISTED SCANNER RUN       DynamoDB
```

The cheap stages see everything; only `max_universe_size` symbols reach
per-symbol work. **Widening the universe cannot make the scan explode.**

---

## 2. Universe policy

All configurable in `agent/config.py` → `UniverseConfig`.

| Setting | Default | Why |
|---|---|---|
| `min_price` | $5.00 | below this a one-cent tick is a large percentage move |
| `max_price` | $2,000 | stops a data error dominating a ranking |
| `min_dollar_volume` | $20,000,000 | dollar volume, not shares: 1M shares of a $6 stock and of a $600 stock are not comparable |
| `min_avg_daily_volume` | 500,000 | enforced when 20-day ADV is available |
| `max_spread_pct` | 0.50% | a spread is paid on entry and again on exit |
| `max_data_age_seconds` | 1,800 | beyond this a quote cannot support an intraday decision |
| `max_universe_size` | 300 | cap on the expensive stage |
| `max_candidates` | 25 | cap on the output |
| `allow_equities` | true | |
| `allow_etfs` | true | ordinary liquid ETFs |
| `allow_otc` | **false** | |
| `allow_penny_stocks` | **false** | |
| `allow_leveraged_etfs` | **false** | long-only research |
| `allowed_exchanges` | NASDAQ, NYSE, ARCA, BATS, AMEX | |
| `allowed_asset_classes` | `us_equity` | no options, crypto or futures |
| `require_fractionable` | false | would matter at a $50/day allocation |

Long-side research only. **No short candidates are ever produced**, in any
regime.

---

## 3. Rejection reasons

Every rejected symbol carries machine-readable reasons, and **all** failed
checks are collected rather than short-circuiting on the first: knowing a
symbol failed on both price and spread is more useful than knowing it
failed one.

```
INACTIVE  NOT_TRADABLE  UNSUPPORTED_ASSET_CLASS  UNSUPPORTED_EXCHANGE
OTC_EXCLUDED  PENNY_STOCK_EXCLUDED  PRICE_BELOW_MINIMUM
PRICE_ABOVE_MAXIMUM  VOLUME_BELOW_MINIMUM  DOLLAR_VOLUME_BELOW_MINIMUM
SPREAD_TOO_WIDE  SPREAD_UNKNOWN  STALE_QUOTE  MISSING_QUOTE
MISSING_BAR_DATA  LEVERAGED_ETF_EXCLUDED  ETF_EXCLUDED  EQUITY_EXCLUDED
NOT_FRACTIONABLE  UNKNOWN_SECURITY_TYPE  BELOW_REGIME_THRESHOLD
UNIVERSE_CAP_REACHED
```

`SPREAD_UNKNOWN` is distinct from `SPREAD_TOO_WIDE` on purpose: an unknown
spread is not a tight one, and the two are different diagnoses.

`UNKNOWN_SECURITY_TYPE` rejects. A security that cannot be classified
cannot be checked against the type policy at all.

Rejections are logged **in aggregate** — one line per symbol would be
~13,000 CloudWatch entries per scan. Full reason counts plus a bounded
25-symbol sample are stored on the run.

---

## 4. Leveraged and inverse detection

Name matching, with a real precision ceiling, so two things keep it
honest.

**It applies only to ETFs.** The pattern hits 12 operating companies in
the live universe — *Ultra Clean Holdings*, *10x Genomics*, *Ultragenyx* —
and excluding those would be wrong. Leveraged and inverse products are
funds, so gating on ETF removes that whole class of false positive.

**Where still ambiguous it errs toward exclusion.** Admitting a 3x inverse
product into a long-only funnel is a real problem; dropping a
short-duration bond fund that would never rank anyway is not.

Two traps found the hard way:

- `\bultra\b` **fails on "UltraShort" and "UltraPro"** — the word boundary
  needs a non-word character after "ultra". That let SDS, QID, TQQQ, UPRO
  and UDOW through. A prefix match (`\bultra\w*`) fixes it.
- **"Short" is ambiguous.** *ProShares Short S&P500* is inverse;
  *Schwab Short-Term U.S. Treasury ETF* and *PIMCO Enhanced Short Maturity*
  are ordinary bond funds. A naive `\bshort\b` match would have excluded
  **217 legitimate funds**. A negative lookahead on
  `term|duration|maturity|dated` separates them.

The first live scan put **ProShares Short Russell2000 (RWM) in the top 5**
of a long-only funnel. That is what prompted this work.

Validated against the live universe: **0 false negatives across 22 known
leveraged/inverse products, 0 false positives across 20 ordinary ones.**
890 products rejected per scan.

---

## 5. Features

| Feature | Definition |
|---|---|
| `session_change_pct` | vs session open, falling back to previous close |
| `return_5m` / `15m` / `30m` | from 5-minute bars, oldest-first |
| `relative_volume` | **see below** |
| `distance_from_vwap_pct` | vs the provider's session VWAP (`dailyBar.vw`) |
| `above_vwap` | with a ±0.05% deadband — sitting *on* VWAP is neither |
| `range_position` | 0 = low of day, 1 = high of day |
| `market_relative_strength` | session change minus the benchmark's, in percentage points |

### Relative volume states its own denominator

Comparing a partial session's volume against a full-day average is not a
ratio of comparable quantities, and calling that "relative volume" would
be a quiet lie. What is computed is:

```
relative_volume = (session_volume / elapsed_session_fraction) / 20d_ADV
```

— projected full-session volume over the 20-day average. Every record
carries `relative_volume_basis` saying exactly that, including that it is
**not** a same-time-of-day comparison, which would need intraday volume
history this system does not collect.

Returns `None` when the elapsed session fraction is unknown, rather than
projecting from a guess. **Early in the session the projection is noisy.**

The 20-day ADV **excludes the current partial session** — including it
would drag the average down all morning and make every symbol look
unusually active.

---

## 6. Scoring

Weights in `ScannerWeights`, validated to sum to 1.0 at construction.

| Component | Weight |
|---|---|
| Liquidity (turnover 65% / spread tightness 35%) | 0.30 |
| Relative volume | 0.20 |
| Short-term momentum (5m 40% / 15m 35% / 30m 25%) | 0.20 |
| Market-relative strength | 0.15 |
| VWAP and range position | 0.10 |
| Data quality | 0.05 |

Each component normalises to [0, 1] before weighting, so a 0.30 weight
really does contribute at most 30 points. Dollar volume is log-scaled
because it spans orders of magnitude — a linear scale would give every
ordinary name ~0 and let a few mega-caps own the range.

**Missing components are excluded and their weight redistributed**, not
scored as zero. A missing feature must not be indistinguishable from a
genuinely poor one. `active_weight` is stored so the redistribution is
visible.

Every component value and its point contribution is persisted, so a
ranking can be audited after the fact.

### What the score means

A score of 85 says *"this symbol is liquid, unusually active and moving,
so analyse it before the one scoring 40."*

It does **not** say the price will rise. It is not a probability, not an
expected return, and not a recommendation. `score_meaning` travels with
every candidate and every API response for exactly this reason.

### Ranking

Sorted by `(-score, symbol)`. The symbol tie-break makes the ordering
total and therefore reproducible: two symbols with identical scores must
not swap places between runs on the same data, or a ranking cannot be
audited.

---

## 7. Regime gating

The Milestone 3 regime is an input to every scan, not decoration. A
hostile regime makes the scanner **pickier** — it never produces short
ideas.

| Regime | min score | spread × | $ volume × | momentum weight × |
|---|---|---|---|---|
| `STRONG_BULLISH` | 0 | 1.00 | 1.00 | 1.15 |
| `BULLISH` | 0 | 1.00 | 1.00 | 1.05 |
| `NEUTRAL` | 0 | 1.00 | 1.00 | 0.90 |
| `MIXED` | 45 | 0.80 | 1.00 | 0.80 |
| `VOLATILE` | 55 | 0.60 | 1.50 | 0.70 |
| `BEARISH` | 55 | 0.80 | 1.00 | 0.70 |
| `STRONG_BEARISH` | 70 | 0.60 | 2.00 | 0.50 |
| `UNKNOWN` | 60 | 0.70 | 1.00 | 0.50 |

`UNKNOWN` is treated as hostile, not calm. **Not knowing the regime is not
the same as a benign one.** Candidates are still produced so the funnel
stays inspectable, but the bar is raised and every candidate carries the
warning.

The active gate is stored on each run, so a past ranking can be read in
the light of the rules that produced it.

---

## 8. Freshness

| Label | Condition |
|---|---|
| `FRESH` | age ≤ 300s |
| `STALE` | 300s < age ≤ 1,800s |
| `MISSING` | no price, an error, or age > 1,800s |

Age is measured from the **provider's** event time, never from the cache
read — `Provenance.retrieved_at` is preserved through the cache so this
stays honest.

Stale quotes are **rejected** by default (`allow_stale_candidates: false`).
When explicitly allowed, the score is multiplied by
`stale_score_penalty` (0.5) and the candidate carries a warning naming its
age. Stale data is never ranked silently beside fresh data.

---

## 9. Provider usage

Measured live on the full universe, 2026-09-30:

| Stage | Requests |
|---|---|
| `/v2/assets` | 1 |
| Batched snapshots (1,000/request) | 14 |
| Daily bars — shortlist only | 5 |
| Intraday bars — shortlist only | 9 |
| **Total** | **≈22, in ~23–33s** |

Against a 200 requests/minute allowance. Four measurement and efficiency
defects were found only by running this live:

1. **`max_symbols_per_request` was 100**, set in Milestone 2 before
   anything had been measured. It silently re-split every 1,000-symbol
   batch into 10 requests, turning 13 snapshot requests into **125**.
   Now 1,000, with 2,000 verified working. Total 142 → 31 requests,
   89s → 33s.
2. **`provider_calls` was counted per call site**, so a paginating batch
   read as one request: a scan reported 18 against 161 actual. It is now
   read from the provider's own counter. A wrong efficiency number is
   worse than none, because it is the evidence the scan does not explode.
3. **Cache hits were counted per symbol**, so a 13-request scan reported
   12,427 "misses" — which reads as a broken cache rather than one batched
   fetch. Now per request.
4. **Batched intraday bars derived their window from `limit × slack`**,
   asking for ~5.5 days of 5-minute bars across 300 symbols. That blew
   past the page cap, so most symbols returned no recent bars and their
   5m/15m/30m returns were **silently absent** — 1 of 25 candidates had
   them. With an explicit lookback, 25 of 25 do.

Reference data is cached for 6 hours; it changes at most daily and is the
largest single response.

---

## 10. Persistence

**Table:** `stock-agent-dev-scanner` (DynamoDB, on-demand, `us-east-2`)

```
PK = RUN#<scanner_run_id>   SK = META                       run metadata
PK = RUN#<scanner_run_id>   SK = CANDIDATE#<rank>#<symbol>   one candidate
PK = SESSION#<date>         SK = RUN#<started_at>#<run_id>   run index
```

Rank is **zero-padded to 4 digits**. Lexicographic ordering on an unpadded
number puts `CANDIDATE#10` before `CANDIDATE#2` and silently corrupts the
ranking on read.

The session index exists because "the latest scan" is the common query,
and scanning the table for it would get slower every day.

Metadata is written **last**. If a write is interrupted partway, a run
without metadata is invisible to `latest_run` rather than appearing as a
complete scan with candidates missing.

Run ids include a nonce. Hashing only date and a second-resolution
timestamp collided, so two scans in the same second produced the same id
and the second overwrote the first — a retry was enough to trigger it.

Per-symbol history walks recent runs rather than using a GSI: a secondary
index would cost write capacity on every candidate for a query that is
diagnostic, not hot.

---

## 11. Failure behaviour

| Condition | Status | Candidates |
|---|---|---|
| Market closed or pre-market | `MARKET_CLOSED` | 0, **and no provider requests** |
| Regime gate disabled | `SCAN_DISABLED` | 0 |
| Universe provider down | `PROVIDER_ERROR` | 0 |
| All snapshot batches fail | `PROVIDER_ERROR` | 0 |
| **One** batch fails | `COMPLETE` | rest of the universe, with a warning |
| Bars unavailable | `COMPLETE` | snapshot-only features |
| Universe empty / all ineligible | `EMPTY_UNIVERSE` | 0 |
| DynamoDB write fails | `COMPLETE` | returned, with a warning |

Candidates are never fabricated from stale cached data unless
`allow_stale_candidates` is explicitly set. A partial outage degrades the
affected symbols only — dropping the whole universe because one batch
failed would turn a partial outage into a total one.

A persistence failure does not discard the scan result; the caller still
receives the run with the failure recorded.

---

## 12. Scheduling

| Resource | Value |
|---|---|
| Rule | `stock-agent-dev-scanner-schedule` |
| Expression | `cron(0/5 14-20 ? * MON-FRI *)` |
| Target | `stock-agent-dev-scanner` |

Every 5 minutes, 14:00–20:55 UTC, Mon–Fri. The window is in UTC and
deliberately wider than the 09:30–16:00 ET session so it stays correct
across daylight-saving changes; **the Lambda checks the broker clock and
no-ops when the market is shut**, spending no provider quota. Correctness
lives in the code, not in the cron expression.

Five minutes because the free consolidated feed is 15 minutes delayed —
scanning faster than the data arrives would add cost and no information.

One iteration per invocation. No loop, no long-running process.

---

## 13. API

Read-only, on `stock-agent-dev-api`:

```
GET /agent/scanner/latest   ?limit= &session_date=
GET /agent/scanner/run      ?run_id=
GET /agent/candidates       ?limit= &min_score= &symbol= &run_id=
GET /agent/candidate        ?symbol= &limit=
```

Reads serve stored state and spend no provider quota. Every response
carries `execution_available: false` and `score_meaning`.

Manual scan triggering is **not** exposed. The scanner is invoked by its
schedule or with AWS credentials; an unauthenticated endpoint that spends
provider quota on demand is an obvious way for a stranger to drain the
budget.

---

## 14. Known limitations

1. **Every threshold is a starting default**, chosen for explainability and
   validated against nothing. The scoring reference magnitudes
   (`$200M` turnover, `0.10%` spread, `2x` relative volume, `2%` momentum)
   are judgement calls, not measurements.
2. **The score has never been tested against outcomes.** It ranks
   research priority; whether high-scoring symbols behave differently from
   low-scoring ones is unknown and is not what this milestone claims.
3. **Quote data is 15 minutes delayed** on the free consolidated feed.
   Adequate for choosing what to analyse; not for execution timing.
4. **Relative volume is a projection**, noisy early in the session, and
   not a same-time-of-day comparison.
5. **Leveraged/inverse detection is name-based** and cannot be complete. A
   newly launched product with an unusual name could slip through. The
   ETF-only gating and exclusion bias limit the damage; they do not
   eliminate it.
6. **Security type is inferred from the name and exchange**, which is why
   `UNKNOWN_SECURITY_TYPE` rejects rather than passes.
7. **No same-time-of-day volume history**, no sector classification, no
   short-interest, no borrow availability, no halt detection.
8. **The shortlist cap is by turnover**, so a less liquid but genuinely
   interesting symbol can be cut before features are ever computed. That
   is a deliberate trade for bounded cost.
9. **Momentum on a 5-minute grid** cannot see anything faster than one
   bar.
10. **Pre-market and after-hours are not scanned** at all.

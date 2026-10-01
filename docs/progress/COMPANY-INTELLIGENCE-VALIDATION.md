# Company Intelligence: live validation

Run 2026-10-01 against the dev API with real SEC and Alpaca data, across
six symbols chosen to cover the cases that behave differently: a dividend
payer with falling fundamentals (GIS), a payer with a split history
(AAPL), a payer whose dividend is immaterial (NVDA), a non-payer (TSLA),
a company whose SIC sits next to semiconductors (GE), and a payer whose
fiscal calendar leaves its last filing months old (COST).

**The suite was green throughout.** 1952 tests passed while peer
selection was pairing computers with tractors. That is the finding worth
recording ahead of any of the others: a green suite established that the
code did what the tests described, and the tests described the wrong
thing. Only running the real surface against real symbols found it.

## Matrix as first observed

| | GIS | AAPL | NVDA | TSLA | GE | COST |
|---|---|---|---|---|---|---|
| SIC | 2040 | 3571 | 3674 | 3711 | 3600 | 5331 |
| Dividend | ACTIVE | ACTIVE | ACTIVE | NO_DIVIDEND | ACTIVE | ACTIVE |
| History rows | 40 | 40 | 40 | 0 | 40 | 40 |
| TTM / yield | 2.44 / 7.70% | 1.06 / 0.32% | 0.52 / 0.22% | — | 1.30 / 0.42% | 5.54 / 0.60% |
| Cadence (days) | 91 | 91 | 92 | — | 91 | 91 |
| Splits | 0 | 1 | 2 | 2 | 1 | 0 |
| Corporate actions | 40 | 41 | 43 | 2 | 44 | 43 |
| Earnings periods | 12 | 12 | 12 | 12 | 12 | 12 |
| Fundamentals freshness | CURRENT | CURRENT | CURRENT | CURRENT | CURRENT | **AGING** |
| Period end | 2026-08-30 | 2026-06-27 | 2026-07-26 | 2026-06-30 | 2026-06-30 | 2026-05-10 |
| Peers | SJM KHC HSY KDP CAG | **CAT CSCO DE IBM** | AMD AVGO **GE** INTC MU | **BA HON** | **NVDA AMD AVGO INTC MU** | WMT |
| Holding | UNFAVORABLE | FAVORABLE | FAVORABLE | UNFAVORABLE | FAVORABLE | **FAVORABLE** |

Bold entries are the defects. Everything else was correct, including the
two cases below that looked wrong and were not.

## Defect 1 — peer selection matched on the 2-digit SIC major group

Apple (3571, electronic computers) came back as a peer of Caterpillar
(3531, construction machinery) and Deere (3523, farm machinery). NVIDIA
(3674, semiconductors) came back as a peer of General Electric (3600,
electrical equipment), and the relation held in both directions. Tesla
(3711, motor vehicles) paired with Boeing (3721, aircraft).

Each list is plausible enough to pass a glance — large American
industrials, sensibly sized — which is why a green suite and a readable
output were not sufficient to notice.

Fixed by limiting major-group matching to an explicit allowlist of groups
that genuinely compete with themselves: food (20), depository
institutions (60), insurance carriers (63). Three-digit and exact SIC
matching are unchanged, so the food peers above still work. The allowlist
is short on purpose, and a test asserts it stays short: a long one would
amount to claiming that many whole major groups are single industries,
which is usually false.

Verified on real profiles after the fix:

| Subject | Before | After |
|---|---|---|
| AAPL | CAT, CSCO, DE, IBM | CSCO |
| NVDA | AMD, AVGO, GE, INTC, MU | AMD, AVGO, INTC, MU |
| GE | NVDA, AMD, AVGO, INTC, MU | *(none in this set)* |
| TSLA | BA, HON | *(none in this set)* |

IBM (3570) shares Apple's three-digit group and is still excluded, as a
market-cap outlier at 22x — existing behaviour, unchanged here.

## Defect 2 — a FAVORABLE holding verdict on data of unknown age

COST read FAVORABLE while its fundamentals were AGING, resting on a
quarter ending 2026-05-10. Freshness is deliberately held out of the
factor tally, because it is a fact about our data rather than about the
company, but it was then ignored entirely, so a favourable reading could
rest on a stale quarter.

Writing the test for that exposed a third case the first fix missed:
UNKNOWN and absent freshness also promoted to FAVORABLE. The gate is
therefore an allowlist — FAVORABLE requires freshness positively
established as CURRENT — rather than a denylist of known-bad labels,
which would let any newly added label promote by default. Unknown age is
not the same as acceptable age.

STALE already yielded INSUFFICIENT_DATA and still does.

## Two findings that looked like defects and were not

**GIS yields 7.70%.** High for General Mills, and correct: the recorded
basis is `price 31.7 as of 2026-10-01T18:43:45Z`, and 2.44/31.70 = 7.70%.
The stock really is near $32. The yield was checkable in seconds only
because the price and its timestamp are stored alongside the figure,
which is the whole purpose of carrying a basis.

**No upcoming ex-dividend date, for any of the six.** Also correct: the
corporate-actions feed holds zero future rows for them. Companies declare
about a quarter at a time, so this is "not yet announced", not "none
coming". The field was a bare null, though, which could not be
distinguished from "we did not look" — so `next_ex_note` now always
carries the reason, and states that the date is never inferred from the
payment cadence. GIS, AAPL and COST are all on 91 days, so the next date
is easy to guess; that is exactly why the field has to say it is not a
guess.

## What remains unknown, by choice

Upcoming earnings and consensus estimates are `UNKNOWN` for every
company, and no provider is wired. See
[EARNINGS-CALENDAR-SOURCES.md](../EARNINGS-CALENDAR-SOURCES.md): Alpha
Vantage is excluded because its key is production's and its free tier is
25 requests a day, enforced by `tests/test_company_quota_boundary.py`
rather than remembered; Finnhub is the preferred next candidate pending a
licensing review; FMP is off the shortlist unless a paid plan is chosen
deliberately.

Without estimates there is no beat, no miss and no surprise, so the
beat/miss sequence is `UNKNOWN` throughout rather than filled in.

## Method note

Both defects were found by reading real output for symbols chosen to
differ from one another, not by adding tests. The tests came afterwards,
and each was run against the pre-fix code to confirm it fails there.

One of them initially passed pre-fix: with Apple's real market cap,
Caterpillar and Deere are rejected as cap outliers whether the SIC logic
is right or not, so the test would have guarded nothing. It now uses
comparable caps so the SIC path is what decides. A test that passes for
the wrong reason is indistinguishable from coverage until the day it
matters.

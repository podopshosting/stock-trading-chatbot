# What would have to be true before holding a position overnight

Status: **`NO_OVERNIGHT_POSITIONS` remains in force.** Nothing in this
document enables it. This is the list of what is missing, written while
the agent is intraday-only, so that a later decision is made against
requirements rather than against enthusiasm.

The Company Intelligence layer (`agent/company/`) was built partly to
answer questions a holding strategy would need. It is **research
infrastructure**: `HoldingContext` returns a label and the factors behind
it, carries `is_execution_instruction: false`, and has no path to the
order layer. Long-term company quality and a short-term trading signal
are different claims about different horizons, and the system keeps them
apart deliberately.

## 1. The blocker that is structural, not missing data

Every open position carries an exit plan:

```
stop_price, target_price, trailing_stop_pct, flatten_before_close_minutes,
stop_mechanism = ENGINE_POLLED
```

`ENGINE_POLLED` means the stop is enforced by the agent looking at the
price every cycle — currently every five minutes during market hours.
Nothing rests at the broker.

An overnight position's loss does not arrive gradually while something is
watching. It arrives as a gap at the next open, already complete. A
polled stop cannot be hit on the way down because there is no way down:
the price simply is somewhere else. The planned risk per trade
(`max_trade_risk`, currently $2) silently stops being a bound.

**Requirement:** before any position is held past the close, the stop has
to live somewhere that is awake when the agent is not — a resting
broker-side stop order, with its own reconciliation — or the position
size has to be set from the *gap* distribution rather than from the stop
distance. These are different position-sizing models; the current one
would be wrong, not merely conservative.

## 2. Earnings

Holding through an earnings release is a different trade from the one
that was entered. A single report routinely moves a stock several
multiples of a normal day's range, in a direction no indicator in this
system claims to predict.

What exists: reported actuals from SEC XBRL (`agent/company/earnings.py`),
with estimate and actual kept strictly apart.

What is missing: **a forward earnings calendar.** There is no configured
source for the next earnings date. `GET /agent/company/{sym}/earnings`
returns `next_earnings_date: null` and says why. Until that exists the
agent cannot avoid what it cannot see.

**Requirement:** a licensed earnings-calendar source, and a rule — likely
"no new overnight exposure within N sessions of a report, and flatten
before one".

## 3. Ex-dividend dates

A dividend is not free money to a holder across the ex-date. The price
adjusts down by roughly the dividend, and a position held through it has
simply exchanged one for the other, minus any tax difference.

What exists: ex-date, record date, pay date and amount from Alpaca
corporate actions; the next ex-date within a 120-day forward window;
`HoldingContext` raises an `ex_dividend_adjustment` caution inside five
days and states that it is not free return.

**Requirement:** a position-level rule for what to do across an ex-date,
and accounting that credits the dividend rather than recording the price
adjustment as a loss. The journal has no dividend-income concept today,
so a held-through dividend would currently read as a loss of exactly the
dividend.

## 4. Corporate actions

A split changes the share count and the price with no economic event;
a merger or spin-off can change what the position even is. The intraday
agent never holds long enough to care.

What exists: splits with direction derived from the ratio, mergers,
spin-offs, symbol and name changes, redemptions, rights distributions.

**Requirement:** position records that survive a share-count change, and
a refusal to hold through an announced merger, redemption or
reorganisation — the position's terms are being rewritten by someone
else.

## 5. Overnight and weekend gap risk

Risk does not accrue in proportion to market hours. A Friday close to a
Monday open carries three calendar days of news into one price, and a
long weekend four. The current limits
(`daily_capital_limit` $50, `daily_loss_limit` $5) are daily by
construction: they reset each session and assume exposure ends with it.

**Requirement:** an explicit overnight and weekend exposure limit,
separate from the daily one, plus the empirical gap distribution for the
candidate universe — not an assumption that it resembles intraday
volatility.

## 6. After-hours liquidity

A decision to exit made at 16:05 cannot be executed the way a 14:00
decision can. Spreads widen, depth thins, and the 0.5% spread limit
(`max_spread_pct`) was calibrated against regular-hours quotes.

**Requirement:** verified after-hours quote and execution data, and
either separate limits for that session or a rule that exits wait for the
regular open — which is itself a decision to accept the gap.

## 7. Data quality

The agent currently trades on Alpaca's `delayed_sip` feed, about 15
minutes behind the consolidated tape — see
`docs/progress/DATA-FEED-ENTITLEMENT.md`. For an intraday strategy this
already limits what the evidence can prove. For a holding strategy it
also removes the ability to see the close accurately, which is when the
decision to hold is actually made.

**Requirement:** real-time data, or an explicit statement that hold/flatten
decisions are made on stale prices.

## 8. Catalyst persistence

The hypothesis engine forms intraday hypotheses: a momentum or
mean-reversion reading with a horizon of hours. Nothing in it claims that
a signal observed at 14:00 still means anything at tomorrow's open.
Holding on an intraday signal is holding on a reason that has expired.

**Requirement:** evidence that a specific catalyst class persists
overnight, measured — not assumed from the fact that the position is
still open. This is a calibration question of the same kind as
`strength_is_predictive`, and it needs its own sample.

## 9. Maximum hold period and exit discipline

An intraday position has a natural terminator: the close. Remove it and
"when do we give up" becomes an open question, and an unanswered one
tends to resolve as "when it hurts enough".

**Requirement:** a maximum hold period, and a stop methodology
appropriate to it. A 2% trailing stop that makes sense within a session
will be hit by ordinary multi-day noise.

## 10. Macro events

Scheduled macro releases — rate decisions, CPI, employment — move whole
sectors at a known time, often outside market hours.

**Requirement:** a macro calendar and a rule for exposure across those
times. There is no such source configured.

## 11. Evidence before capability

The readiness gate already refuses real money on eleven grounds. A
holding strategy would be a **new strategy**, not an extension of the
current one: a different horizon, a different risk model, different
exits. Its evidence cannot be inherited from intraday paper trading, and
pooling the two would produce a record describing neither — the same
error the cohort machinery exists to prevent
(`agent/autonomy/versions.py`, `agent/autonomy/evidence_class.py`).

**Requirement:** a separate strategy version, a separate cohort, and its
own sample large enough to judge. Starting a new cohort is cheap;
pretending an old one applies is not.

## Summary

| Input | Status |
|---|---|
| Broker-side resting stops | **absent** — polled stops only |
| Gap-based position sizing | **absent** |
| Forward earnings calendar | **absent** — no source configured |
| Macro calendar | **absent** |
| Ex-dividend handling | partial — dates known, no rule, no dividend accounting |
| Corporate-action handling | partial — actions known, no position-level rules |
| Overnight/weekend exposure limits | **absent** — limits are daily by construction |
| After-hours liquidity data | **unverified** |
| Real-time data | **absent** — 15-minute delayed |
| Catalyst persistence evidence | **absent** |
| Maximum hold period / multi-day stops | **absent** |
| Separate strategy version and cohort | **not created** |

Every row would need an answer, and the first one needs a change to how
risk is enforced rather than a new data source. Until then the agent
flattens before the close, which is the only honest thing it can do with
a stop that is only enforced while it is looking.

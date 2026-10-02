# Cohort: REALTIME_SIP_STRATEGY_EVIDENCE — charter and start conditions

Written 2026-10-02 before the cohort exists, so the conditions cannot be
chosen after seeing the result.

## Why this cohort is not starting on 2026-10-02

Two earlier cutovers are permanently VOID (21:06:32Z and 00:01:43Z, see
`REALTIME-COHORT-CUTOVER.md`). The third deployment carries the
observability and forward-compatibility fixes and lands mid-session,
around 14:15Z.

The rule, fixed in advance:

> If any regular-market trading activity occurs on `c4a9d50` before
> `430a4ac` is deployed, all of 2026-10-02 is operational/non-cohort
> activity, and the first cohort starts at the 2026-10-03 regular-session
> open.

A deployment at 14:15 would leave a session with two runtimes and
same-day prior activity to explain. Tomorrow's open gives one runtime
from before the first cycle, a known-flat starting book, a known
real-time feed, and nothing earlier in the day to account for. The
cleaner boundary is worth a day.

As at 13:11Z the paper venue is flat: 0 open orders, 0 orders of any
status since 00:00Z, 0 positions, $100,000 equity. If it is still
provably flat at deployment, a same-day cohort would be technically
valid — and is still declined, for the reason above.

## Pinned configuration

Nothing here may change inside the cohort. A change to any of it closes
the cohort and starts a new one at the new deployment timestamp.

| | |
|---|---|
| Code SHA | `430a4ac` (both API and cycle) |
| Data feed | `ALPACA_QUOTE_FEED=sip` — real-time consolidated tape |
| Broker | `AGENT_BROKER=alpaca_paper`, authoritative; internal `PaperBroker` as shadow |
| Execution mode | `PAPER`. LIVE is unconstructible; real money DISABLED |
| Overnight positions | DISABLED |
| Strategy / risk / exits / orchestration | `hypothesis-v1.0.0` / `risk-v1.0.0` / `exits-v1.0.0` / `orchestration-v1.1.0` |
| Schedules | cycle `cron(2/5 13-21 ? * MON-FRI *)`, scanner `cron(0/5 13-21 ? * MON-FRI *)` |

## Start conditions — all 29 preflight checks, plus these

The cohort opens only when every one of these is **proven**, not merely
absent. An unreadable collection fails the check; it never counts as
empty.

1. API and cycle both report `430a4ac`; no redeploy since the boundary
2. `ALPACA_QUOTE_FEED=sip`, `AGENT_BROKER=alpaca_paper`,
   `AGENT_EXECUTION_MODE=PAPER`
3. Adapter base URL exactly `https://paper-api.alpaca.markets`; every
   live host refused
4. Paper account ACTIVE, not blocked
5. SIP source timestamp current, data age inside the limit, judged on
   the **data's** timestamp and not on fetch time
6. Paper open orders: **read succeeded** and count 0
7. Paper positions: **read succeeded** and count 0
8. Internal position store: **read succeeded** and count 0
9. `positions_known` true — the book is known flat, not merely empty
10. Current-session figures established, not inherited from a prior
    session
11. `read_integrity.evidence_complete` true
12. Emergency stop clear, global halt clear, health permits entries
13. Production router unchanged from its recorded baseline

## What this cohort may be used to claim, and when

Nothing, until the sample-adequacy gates pass. `readiness` currently
reports `NOT_READY_BLOCKED` with 1 gate met of 11, and a cohort does not
change that by existing.

Operational reliability may be reported from the first session:
cycle completion, reconciliation, EOD flatten, terminal-state coverage.
Strategy performance may not be reported at all until the gates say the
sample supports it, and a single session never will. Profitability is
not evidence of an edge at any sample size this cohort will reach
quickly.

Cohorts are never pooled. Evidence from `c4a9d50`, from either VOID
cutover, or from 2026-10-02 does not enter this one.

## Closing conditions

- any change to the pinned configuration above
- a redeploy of the cycle, whatever the reason
- a runtime version change inside a session
  (`RUNTIME_VERSION_CHANGED_MID_SESSION`)
- a proven defect in the trading path requiring a fix

On any of these: close the cohort, record why, and open a new one at the
new deployment timestamp. Do not extend a cohort across a fix.

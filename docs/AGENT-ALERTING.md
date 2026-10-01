# Alerting

Design. **No transport is configured**, which is why this document exists:
`AlertSink` records alerts to DynamoDB and the dev dashboard shows them,
so an alert raised today is visible only if someone looks. Choosing and
subscribing a transport is an account action — see the end.

## The rule that shapes everything

**A normal paper trade raises no alert.** An alerting path that fires on
ordinary operation is one that gets muted, and a muted path is worse than
none because it reads as coverage. A test asserts this
(`test_a_normal_paper_trade_raises_no_alert`).

Alerts are for states a human must decide about. Everything else is a
journal entry.

## What is raised, and why

### CRITICAL — the agent has stopped or its books disagree

| Alert | Condition | Why a human is needed |
|---|---|---|
| `EMERGENCY_STOP` | engaged by reconciliation mismatch or an unresolvable state | Latching: the agent may engage it and may never clear it. Only a named person can, via `cleared_by`. |
| `RECONCILIATION_MISMATCH` | agent and broker disagree on positions | The risk arithmetic was wrong in an unknown direction. New exposure stops; exits continue. |
| `UNEXPECTED_BROKER_POSITION` | broker holds something the agent did not open | Deliberately **not** auto-closed: closing a position whose origin is unknown could be the second mistake. |
| `DUPLICATE_ORDER_ATTEMPT` | idempotency guard caught a second submission | The guard worked, but something upstream tried twice. |
| `EOD_FLATTEN_FAILURE` | a position survived the close attempt | Overnight exposure is forbidden, so this is a policy breach in progress. |
| `UNCERTAIN_ORDER_STATE` | a submission's outcome is unknown after a transport failure | The agent does not adopt an orphan fill. A person reconciles it. |

### HIGH — the agent is degraded but safe

| Alert | Condition | Notes |
|---|---|---|
| `DAILY_RISK_LOCK` | daily loss limit hit | The day is locked. Working as designed; worth knowing the same day. |
| `REPEATED_CYCLE_FAILURE` | 3 consecutive failed cycles | One failure is noise. Three is a pattern. |
| `BROKER_UNAVAILABLE` | broker unreadable, or the Alpaca adapter could not be constructed | Entries stop, exits continue. Also fires on a silent downgrade to the internal simulator, which would otherwise mislabel evidence. |
| `MARKET_DATA_UNAVAILABLE` | provider failing across the universe, not one symbol | Distinguished from one bad symbol on purpose. |
| `STALE_MARKET_DATA` persisting | data age beyond the limit across cycles | After the freshness fix this means the feed itself is lagging. |
| `JOURNAL_PERSISTENCE_FAILURE` | a trade could not be recorded | A trade that happened and was not recorded corrupts every later number. Latching. |

### INFO — rhythm, not incident

`SESSION_STARTED`, `SESSION_COMPLETED` (with the close-out verdict), and
`COHORT_STARTED` (a deploy that begins a new evidence cohort).

INFO should be deliverable to a different channel, or digested daily. If
INFO and CRITICAL arrive the same way, CRITICAL stops being read.

## Deduplication, already implemented

`AlertSink` dedupes per kind per session, so a condition recurring across
40 cycles produces one alert with a count rather than 40 messages. That
behaviour is the difference between a usable channel and a flood, and it
is tested.

Secrets are redacted by `_safe()` before an alert is stored: an alert
about a credential failure must not contain the credential.

## Transport options

Evaluated against: no new vendor, no purchase, works while nobody is
watching a dashboard.

### 1. SNS topic with an email subscription — recommended

One topic, say `stock-agent-dev-alerts`. The Lambda publishes; a
confirmed email subscription delivers.

- Cost: effectively nil at this volume (first 1,000 email notifications
  free each month, then cents).
- Already permitted? **No** — the Lambda role has no `sns:Publish`. One
  statement to add, scoped to that topic ARN only.
- Requires: creating the topic, and the owner **confirming the email
  subscription** from their inbox. AWS will not confirm it for them.

### 2. SNS topic with SMS

Same mechanism, higher urgency, and it costs per message. Worth it only
for the CRITICAL set, which would mean two topics. Defer until the
CRITICAL rate over a few weeks is known — it should be zero.

### 3. CloudWatch alarm on a metric filter

The cycle already emits structured JSON events, so a metric filter on
`alert_raised` could drive an alarm without touching the Lambda's
permissions at all.

- Advantage: no code change, no new IAM on the Lambda, and it also
  catches a Lambda that stopped running entirely — which an
  application-level alert by definition cannot.
- Disadvantage: the alert body is not carried, only the fact of it.

**Both 1 and 3 are worth having, for different failures.** Option 1 tells
you what happened; option 3 tells you when nothing is happening.

### 4. EventBridge to a third-party sink

Rejected for now: a new vendor and an outbound dependency for a system
that currently alerts a handful of times a month at most.

## Proposed implementation order

1. CloudWatch alarm on "no `cycle_complete` event in 30 minutes during
   market hours". This catches the silent death that no in-process alert
   can, and needs no code or Lambda IAM change.
2. SNS topic plus one `sns:Publish` statement scoped to it, and an
   `SnsAlertSink` behind the existing `AlertSink` interface — the
   interface exists precisely so this is a transport swap, not a
   rewrite.
3. Owner confirms the email subscription.
4. A falsifying control: with the sink configured, a deliberately
   engaged test condition must produce exactly one delivery, and a normal
   paper trade must produce none. An alert path that has never been seen
   to fire is not evidence that it works.

## USER ACTION REQUIRED

Nothing here is wired, and two steps need you:

- Decide whether to create `stock-agent-dev-alerts` and add the scoped
  `sns:Publish` statement.
- **Confirm the email subscription**, which only the mailbox owner can do.

Until then, alerts are recorded and visible on the dev dashboard only,
and that is the honest description of the current state.

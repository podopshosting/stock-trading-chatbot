# The live cycle is running a pre-fix artifact

Observed 2026-10-01 during read-only Track A observation. No change was
made to the cycle: it is frozen for the duration of the paper session by
design, and this note records what that freeze is currently costing.

## What happened

One cycle of 66 aborted. From CloudWatch:

```
{"event": "cycle_aborted", "cycle_id": "cycle_e6429e2eab0a43a4",
 "detail": "'>' not supported between instances of 'method' and 'float'"}
{"event": "cycle_complete", "outcome": "ABORTED",
 "risk_was_managed": true, "exits_submitted": 0, "entries_submitted": 0,
 "halt_reasons": ["UNHANDLED_ERROR"]}
```

`risk_was_managed: true` and nothing submitted, so the abort was safe:
the cycle stopped rather than acting on a comparison it could not make.

## Why it is already fixed, and still happening

The message is a bound method compared with a float.
`Provenance.age_seconds` is a method, and returning it uncalled put the
method object itself into the staleness comparison. That was found and
fixed earlier in this branch; `lambda-micro/agent-cycle/handler.py` at
HEAD carries the fix with the reason written at the call site, and the
Risk Governor now compares `context.source_age_seconds`.

The deployed cycle predates it. Its `versions.code_sha` reads `8674df7`
and the session tally reports `sha_source: versions.code_sha (single
value; per-cycle SHAs not recorded)`, which is itself evidence of age:
per-cycle SHA recording was added in the same body of work as the fix,
so an artifact that cannot report per-cycle SHAs necessarily predates
it.

So the defect is fixed in the repository and live in production-equivalent
dev. Nothing about the fix helps until the artifact is replaced.

## Why it was not deployed on finding it

Track A holds the cycle, scanner, signal engine, Risk Governor, broker,
position manager and schedules frozen for the duration of the session. A
redeploy mid-session would also void the session as evidence, because a
runtime version change inside a session is recorded as
`RUNTIME_VERSION_CHANGED_MID_SESSION` and the session is then excluded
from aggregation. Trading one aborted cycle in 66 for a voided session
would be a poor exchange, and the abort is safe.

## What clears it

The scheduled cutover. Deploying the cycle with `ALPACA_QUOTE_FEED=sip`
and `AGENT_BROKER=alpaca_paper` replaces the artifact, so the fix lands
as part of a change that was already going to happen at a cohort
boundary. That is the right moment: a new artifact starts a new evidence
cohort either way.

This is therefore a second, independent reason for the cutover, beyond
the feed. Worth stating plainly, because "the data feed was not
recorded" was the only reason on file, and it understates what the
current artifact is missing.

## The general point

A green suite and a fixed repository say nothing about what is running.
The gap between `git log` and the deployed artifact is invisible from the
code, and here it was visible only in a CloudWatch line and a missing
field in a session tally. Any claim about agent behaviour has to name the
artifact it is a claim about.

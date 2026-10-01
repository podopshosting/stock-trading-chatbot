# Today's session spans two runtimes, and cannot tell

Observed 2026-10-01 by read-only Track A observation. Nothing was
changed: the cycle is frozen for the duration of the session by design.

**This file previously claimed the deployed cycle still carried a
staleness-comparison defect. That was wrong, and the error is recorded
below rather than quietly replaced, because the way I got it wrong is
the more useful finding.**

## What actually happened

One cycle of 66 aborted:

```
13:32:42  COMPLETED  cycle_3c68f2557adc4c
13:38:44  ABORTED    cycle_e6429e2eab0a43   <- UNHANDLED_ERROR
13:39:50  COMPLETED  cycle_6c926afe9fa54e
13:42:44  COMPLETED  cycle_a84029d4980244
```

The abort detail was `'>' not supported between instances of 'method'
and 'float'` — a bound method compared with a float, because
`Provenance.age_seconds` is a method and was being returned uncalled.
`risk_was_managed: true` and nothing was submitted, so the abort was
safe: the cycle stopped rather than acting on a comparison it could not
make.

The cycle Lambda was redeployed at **13:39:39** — 55 seconds after that
abort — carrying the fix. Every one of the 64 cycles since has
completed, and reconciliation is 66/66 clean. The defect is not live.

## The error I made

I read the session tally, which reports `code_shas: ["8674df7"]`, and
concluded that the running artifact was `8674df7` and therefore predated
the fix. The deployed configuration actually reads:

```
LastModified  2026-10-01T13:39:39Z
AGENT_CODE_SHA  66989c4
```

The tally was not lying; it was answering a different question. It
records the SHA it saw when the session opened at 13:32:42, which was
indeed `8674df7`. I inferred what was *running now* from a value that
describes what was running *then*, when the deployed configuration was
one read away.

A session tally is a record of a session. The authority on what is
deployed is the deployment.

## The real finding, which is worse

Today's session spans **two artifacts**: two cycles on `8674df7`, then
64 on `66989c4` after the 13:39:39 redeploy. A runtime change inside a
session is precisely the condition `RUNTIME_VERSION_CHANGED_MID_SESSION`
exists to catch, and such a session is excluded from aggregation as
unattributable.

It was not caught. The tally reports `single_runtime_version: null` and
`sha_source: "versions.code_sha (single value; per-cycle SHAs not
recorded)"`, and its classification gives exactly one reason — "the data
feed was not recorded". The runtime change is absent from the reasons.

The cause is self-referential: per-cycle SHA recording was added in the
same body of work as the staleness fix, so the artifact running when the
session opened could not record per-cycle SHAs. **The guard against
mid-session redeploys could not see the mid-session redeploy that
installed the guard.**

## Consequences for the close-out

- The practical outcome is unchanged: the session is already
  `counts_toward_strategy_gates: false`, so nothing downstream treats it
  as strategy evidence.
- The **reason** on file is wrong, or at least incomplete. The session
  should be read as unattributable because it ran on two runtimes, not
  merely as delayed-data. The close-out states this explicitly, since
  the tally cannot.
- `OPERATIONAL_VALIDATION_ONLY` remains a fair label for the operational
  numbers — 66 cycles, 66/66 reconciliation, zero emergency stops, zero
  risk locks, zero EOD flatten failures — because those are claims about
  whether the machinery ran, and it did, on both artifacts.
- This is a one-off. From the next cohort the running artifact records
  per-cycle SHAs, so a mid-session redeploy will flag itself.

## The general point

A green suite and a fixed repository say nothing about what is running,
and a session's own record of itself is not an authority on its runtime.
Both gaps were invisible from the code and visible in one CloudWatch
line, one deployed-configuration field, and one missing entry in a list
of reasons. Any claim about agent behaviour has to name the artifact it
is a claim about — and check that name against the deployment rather
than against the claim.

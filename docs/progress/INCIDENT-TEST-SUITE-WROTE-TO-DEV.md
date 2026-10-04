# Incident: the test suite wrote to the deployed dev tables

**Discovered** 2026-10-03, during an unrelated investigation into two
order records that looked like a mystery.
**Status** remediated and verified. Residual rows retained deliberately.
**Production (`stock-chatbot-router`, `/chatbot`)** never affected.

## What happened

`tests/test_cycle_handler.py` invokes the real cycle `lambda_handler`
from `lambda-micro/agent-cycle/handler.py`. That handler builds real
DynamoDB clients. When AWS credentials were present in the environment,
those clients worked, and the unit suite wrote to the **deployed dev
tables**.

Measured, with a probe installed below the boundary and the whole suite
run: **68 AWS calls**, every one of them naming a deployed resource.

| | count | what |
|---|---|---|
| `GetItem` | 31 | reads of deployed tables |
| `Query` | 23 | reads of deployed tables |
| `PutItem` | 5 | **writes** |
| `GetSecretValue` | 1 | **a read of the live Alpaca credential** |

The writes landed in three places:

1. **Order intents** in `stock-agent-dev-journal` — two per full-suite
   run, for symbol `XYZ` at $20 and $30, which together reserve the
   **entire $50 daily capital envelope** as committed exposure.
2. **Terminal cycle snapshots**, via `agent/autonomy/snapshot.py:69`.
3. **Health streak records**, via `agent/autonomy/health.py:250` and
   `:317`.

The third is the serious one. Health gates trading. A test run was
mutating the agent's own health accounting, and it produced a
`REPEATED_CYCLE_FAILURE` condition and a `consecutive_failures` count
that no cycle had caused.

## Why the suite appeared hermetic

Because it was — **by accident, not by construction.**

Without credentials, the DynamoDB client constructions fail, the
handler's broad exception handling absorbs the failure, and the tests
pass. The suite's correctness did not depend on reaching AWS, so
nothing ever drew attention to the fact that it was trying.

The behaviour was therefore **credential-dependent**: identical code,
identical tests, different side effects depending on whether a shell
happened to have `AWS_PROFILE` exported. "It passes locally" and "it is
hermetic" are different claims, and only the second one matters.

## Why `deploy_agent_dev.sh` amplified it

The deploy script exports `AWS_PROFILE` so that it can deploy, and then
ran the test gate **in that same environment**. So the gate that was
supposed to protect a deploy was itself the most reliable writer:
**every deploy added two phantom order intents** and a fresh set of
snapshot and health writes.

The test phase and the deploy phase have genuinely different authority,
and the script did not distinguish them.

## Why it was hard to find

- **Module-by-module bisection found nothing.** Running one test module
  at a time produced no writes, because the write needed credentials
  that were present in one shell and not another.
- **Import-only of every test module produced no writes**, ruling out
  an import-time side effect.
- Only the full suite, with a profile set, reproduced it: the journal
  went 4 rows → 6 → 8 across successive runs.

It was found by **instrumenting the boundary** instead of guessing
again: patching `botocore.client.BaseClient._make_api_call`, trapping
every `PutItem` naming the real table, and printing the stack. That
gave file and line for all three write paths immediately.

## Resources affected

| Resource | Effect |
|---|---|
| `stock-agent-dev-journal` | 8 phantom order intents; cycle snapshots; health records |
| `stock-agent/alpaca-paper` | read once per suite run |
| `stock-agent-dev-state` | read, and written via snapshot/state sync |
| `stock-agent-dev-positions` | no contamination found |
| `stock-agent-dev-broker` | no contamination found |
| **`stock-chatbot-router`** | **not touched; last modified 2026-09-30** |

## Phantom order impact

Eight `EXTORDERS#2026-10-01` records, symbol `XYZ`, with
`submitted_at = None`, zero observations and
`submission_outcome_known = false`.

Because an accepted-but-unfilled order reserves its notional, these
reserved **$50.00** of committed exposure — the entire daily limit — and
the cycle correctly refused new entries with
`entries_blocked_unknown_exposure`. The safety machinery behaved
properly on corrupt input; the input should not have existed.

**Confirmed absence** was established from the venue, not inferred:

- `GET /v2/orders:by_client_order_id` → **404** for every id
- `GET /v2/orders?status=all` on account `PA3XG705F1PL` → **exactly one
  order has ever existed**, the real `DRAM` fill

Resolved with `agent/broker/order_poller.poll_outstanding`, the
canonical mechanism, **not** by editing rows: it asks the venue, honours
the absence grace period, and refuses to mark any row carrying a fill or
a prior observation. Result: all 8 `NEVER_PLACED` with a confirming
observation each, exposure **$50.00 → $0.00**, read integrity
`COMPLETE`.

The poller also demonstrated its own design on the first attempt: run
without `requests` installed it returned `integrity: PARTIAL` and
`never_placed: 0` rather than inferring absence from a failed lookup.

## Health impact

`consecutive_failures` reached 4 and `REPEATED_CYCLE_FAILURE` was
raised, neither caused by a real cycle.

Recovering it exposed a **second, pre-existing defect**: the condition
is documented non-latching, but it had two raise paths
(`handler.py:231`, added because an early abort never reaches
orchestration, and `day.py:513`) and only one clear path
(`day.py:519`, inside orchestration). A market-closed skip returns
before orchestration, so it reset the streak and could never clear the
condition. A non-latching condition whose cause has passed is a latching
condition nobody declared.

Fixed in the skip path, with the clear gated on the streak actually
reaching zero. Verified on the live dev agent:

```
before   consecutive_failures 4, REPEATED_CYCLE_FAILURE active
after    consecutive_failures 0, REPEATED_CYCLE_FAILURE cleared
```

The three latching conditions (`RECONCILIATION_MISMATCH`,
`EMERGENCY_STOP`, `UNEXPECTED_BROKER_POSITION`) **remained active and
untouched**, which is correct: they require a named human.

## Was strategy evidence affected?

**No conclusion about the strategy was corrupted, and no conclusion
could have been.**

- The phantom orders produced **zero fills**. The journal holds 2 real
  completed trades for 2026-10-01 (`MSFT`), unchanged.
- The affected cohort `cohort-c68ffe3fd9` was **already classified
  `VOID`** for independent reasons, so it was never admissible as
  strategy evidence.
- What the phantom rows did distort is **operational** accounting:
  outstanding-order counts, committed exposure, and the health record.

This matters because operational reliability is exactly what the
current cohort was being used to measure. Any claim about cycle
reliability over 2026-10-01 to 2026-10-03 must be treated as
contaminated.

## Remediation

Four layers, because the incident was a single-layer failure and
repeating that shape would be learning the wrong lesson.

1. **Credentials neutralised** (`tests/__init__.py`) — unusable key
   values, named profiles cleared, credentials file pointed at
   `/dev/null`, instance metadata disabled.
2. **Endpoints unreachable** — every AWS endpoint pointed at
   `127.0.0.1:1`, which refuses immediately.
3. **The call itself blocked** (`tests/support/aws_seal.py`) — botocore
   is patched so that *any* API call from an ordinary test raises
   before a socket is opened. This holds even if layers 1 and 2 are
   deleted, the credentials are real and the profile is an
   administrator.
4. **Integration tests must opt in** with
   `RUN_AWS_INTEGRATION_TESTS=1`. The presence of credentials is not
   permission — that inference is the root cause.

Reads are blocked as well as writes: a read against a deployed table
proves the boundary is open just as well, and the next person to add a
write would find the door already unlocked.

The block is a plain `RuntimeError` subclass, deliberately **not** a
botocore exception type, because this codebase catches
`ClientError` and bare `Exception` widely and a botocore-shaped block
would be silently swallowed in exactly the tests most likely to need
it.

**Deploy script**: the test phase now runs under `env -u` with every
credential variable removed (`env -u` rather than empty values, because
an empty `AWS_PROFILE` is still a profile name to botocore).

Side effect worth recording: suite runtime fell from **14.8s to 4.8s**.
It had been making real network calls all along.

## Regression controls

- `scripts/verify_test_hermeticity.py` — installs a probe **below** the
  seal, runs the whole suite, and fails if any call escapes.
  Three-state: PASS / FAIL / NOT RUN, where NOT RUN is not a pass.
  Wired into `deploy_agent_dev.sh`.
- `tests/test_aws_seal.py` — 17 tests. Includes a **differential
  control** that substitutes a sentinel beneath the seal and proves the
  call reaches it with the seal off and does not with the seal on.
  (The first version made a *real* call to prove the difference; the
  hermeticity gate correctly failed it. The fix was to stop the control
  needing a network, not to teach the gate an exemption.)
- `tests/test_suite_is_hermetic.py` — 11 tests, including one that
  *sets* `AWS_PROFILE` and proves the guard clears it. An earlier
  version only asserted the variable was absent, which passed
  trivially in any shell that never set it — and the corresponding
  mutation **survived** until it was rewritten.
- `tests/test_health_recovery.py` — 10 tests, asserting the
  non-latching clear works *and* that all seven latching conditions
  refuse an unnamed clear.
- Four mutation controls in `scripts/falsifying_controls.py`.

## Remaining known contaminated rows

Retained on purpose. Deleting them would destroy the evidence of the
incident, and they are now in a terminal, zero-exposure state.

| Rows | Location | State |
|---|---|---|
| 8 | `EXTORDERS#2026-10-01` | `NEVER_PLACED`, exposure $0, integrity `COMPLETE` |
| some | cycle snapshots / `SESSIONS#` | indistinguishable from real skipped cycles; harmless |

Audited 2026-10-04 across `journal`, `positions`, `broker` and `state`:
no other unit-test contamination. The 10 `REPLAYRUN#` rows that mention
`XYZ` are legitimate simulation records — `XYZ` is the synthetic symbol
the scenario library uses.

One caution for anyone repeating this audit: DynamoDB JSON nests values
as `{"symbol": {"S": "XYZ"}}`. The first version of the audit script
searched for `"symbol": "XYZ"` and reported **zero contamination** while
eight known rows sat in the table. A clean result from a broken probe is
worse than a dirty one.

## Lessons

1. **A guard that cannot fail is not a guard.** Both of the first
   remediation attempts — the endpoint variable, and the
   `AWS_PROFILE`-absence assertion — passed without proving anything.
   Only the version that attempts a real call and requires it to fail
   is evidence.
2. **Absence of credentials is not isolation.** Any property that holds
   only because of an environment accident will stop holding.
3. **Instrument the boundary.** Three guesses found nothing; one probe
   at `_make_api_call` gave file and line.
4. **A mystery is usually attribution, not mystery.** The XYZ rows were
   investigated as an unexplained anomaly for some time. They were a
   test run.

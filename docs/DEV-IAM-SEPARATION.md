# Separating development IAM from production

Design. **No IAM change has been made**, and none should be made while a
paper session is running.

## What is shared today

Verified 2026-10-01. Four Lambdas, one role:

| Function | Role | Purpose |
|---|---|---|
| `stock-chatbot-router` | `stock-chatbot-lambda-role` | **PRODUCTION** `/chatbot` |
| `stock-agent-dev-api` | `stock-chatbot-lambda-role` | dev read API |
| `stock-agent-dev-cycle` | `stock-chatbot-lambda-role` | dev paper trading cycle |
| `stock-agent-dev-scanner` | `stock-chatbot-lambda-role` | dev scanner |

Attached policies: `AWSLambdaBasicExecutionRole`,
`AmazonDynamoDBFullAccess`, `SecretsManagerReadWrite`.

## Why this matters more than it looks

Two AWS-managed policies are doing the work, and both are far wider than
anything here needs:

- **`AmazonDynamoDBFullAccess`** grants `dynamodb:*` on **every table in
  the account**, including `DeleteTable`. The dev cycle writes to seven
  dev tables and needs no ability to delete any of them. Any of these
  four functions could destroy the others' evidence.
- **`SecretsManagerReadWrite`** grants write and, depending on version,
  `DeleteSecret` on **every secret**, including the production
  Alpha Vantage key and the Alpaca paper credentials. The dev cycle needs
  to *read* one secret.

The practical risk is not malice, it is a mistake: a dev deploy with a
wrong table name has the permissions to do real damage to production
data, and production code has the permissions to damage the evidence the
agent is accumulating. Shared roles also make CloudTrail ambiguous —
"who wrote this row" has four possible answers.

## Proposed roles

Four roles, each with only what the function demonstrably uses. Verified
against the code, not guessed.

### `stock-agent-dev-api-role` — read-only

- `dynamodb:GetItem`, `Query`, `Scan` on the seven `stock-agent-dev-*`
  tables and their indexes. **No write actions at all**: the API is a
  read surface and a test already asserts it imports no broker.
- `secretsmanager:GetSecretValue` on `stock-agent/alpaca-paper` only.
- Basic execution (CloudWatch Logs).

The one wrinkle: `podops-templates-api`-style surprises aside, one dev
endpoint still creates a table on first use if absent — if that is kept,
it needs `CreateTable`/`DescribeTable` and the "read-only" claim must be
qualified. Preference: pre-create the table and drop the permission, so
the read API really is read-only.

### `stock-agent-dev-cycle-role` — the only writer

- `dynamodb:GetItem`, `PutItem`, `UpdateItem`, `Query`, `Scan`,
  `ConditionCheckItem` on the seven dev tables. **No `DeleteItem`**: the
  cycle never deletes, and immutable history is the point.
  **No `DeleteTable`, no `UpdateTable`.**
- `secretsmanager:GetSecretValue` on `stock-agent/alpaca-paper` only.
- Later, if alerting lands: `sns:Publish` on the alerts topic only.
- Basic execution.

### `stock-agent-dev-scanner-role`

- Read/write on `stock-agent-dev-scanner` and `stock-agent-dev-state`;
  read on nothing else.
- `secretsmanager:GetSecretValue` on the Alpaca secret.
- Basic execution.

### Production stays as it is, for now

`stock-chatbot-router` keeps `stock-chatbot-lambda-role`. Narrowing
production's permissions is a separate, production-affecting change and
is explicitly **not** part of this one. The win here is that dev stops
sharing production's authority; tightening production is a second step
with its own approval.

## Sequencing (nothing destructive)

1. Create the three dev roles with the scoped policies above. Creating a
   role changes nothing that is running.
2. Repoint **one** dev function — `stock-agent-dev-api`, the read-only
   one — and exercise every endpoint. The dev API has 31 routes and a
   full test surface, so a missing permission shows up immediately as a
   `read_error` in the payload rather than as silence.
3. Repoint the scanner. Verify a scan completes and is stored.
4. Repoint the cycle **between sessions, never during one**, and verify a
   full cycle: lock, reconcile, scan read, decision write, journal write,
   session tally, snapshot.
5. Leave the old role attached to nothing but production. Do not delete
   it: it is production's role.

Each step is reversible by repointing the function back, which is why
this order is safe. Step 4 is the only one that can interrupt trading, so
it belongs in the same window as a cohort boundary — and because it
changes the runtime's identity, it is worth treating as a new cohort.

## What would prove it worked

Not "the deploy succeeded". A missing permission is usually silent until
the specific code path runs. So:

- every dev API endpoint returns 200 with no `read_error`
- a scanner run completes and is readable
- a cycle completes with `state_saved: true`, a decision row, a session
  tally and a snapshot — the four writes it must make
- a deliberate check that the dev cycle **cannot** read a non-Alpaca
  secret and **cannot** delete a dev table, because a scoped policy that
  has never been seen to deny anything has not been shown to be scoped

That last item is the falsifying control. Without it, "least privilege"
is an assertion about a JSON document rather than a property of the
system.

## Not doing now, and why

No IAM change during an active paper session. A permission error
mid-session would abort cycles and contaminate the evidence cohort, which
costs more than the exposure it would fix. The exposure has existed for
weeks and is not newly urgent.

## USER ACTION

Creating IAM roles and repointing functions needs an authorised
principal. `claude-deployment` can read IAM but has not been used to
create roles here, and whether it should be able to is itself a decision
worth making deliberately.

# Paper evidence cohort 1 — frozen configuration

Recorded 2026-10-01 12:25 UTC, before the first scheduled live-market cycle
(13:02 UTC; market opens 13:30 UTC / 09:30 ET).

- Deployed cycle Lambda: `stock-agent-dev-cycle`, last modified 2026-10-01T01:15:47Z
- Code SHA pinned in Lambda env: `8674df7`
- Execution mode: `PAPER` (LIVE unconstructible; real money disabled)
- Broker: internal `PaperBroker` (AlpacaPaperBroker unwired; credential scope unverified)
- Strategy `hypothesis-v1.0.0`, risk `risk-v1.0.0`, exits `exits-v1.0.0`,
  orchestration `orchestration-v1.1.0`, metrics `metrics-v1.0.0`
- Schedules: cycle `cron(2/5 13-21 ? * MON-FRI *)`, scanner `cron(0/5 13-21 ? * MON-FRI *)`, both ENABLED
- Commits after 8674df7 (ef0f222, fdc09c0) touch only dashboard, docs and the new
  `agent/company/` package; nothing in the cycle's import closure.

## Freeze rules
- The cycle and scanner Lambdas are NOT redeployed during the cohort.
- Company Intelligence ships only to the dev API Lambda and dev UI.
- A bug fix to the execution path starts a new cohort.

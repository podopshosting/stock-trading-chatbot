# Milestone 19A — autonomous paper operation (status)

Mode: `AUTONOMOUS_PAPER`. `ExecutionMode` is DISABLED/PAPER only; LIVE is
unconstructible and unknown values fail closed to DISABLED. Real money
and overnight positions are disabled.

## Built and deployed (dev)
- Autonomy policy, health (HEALTHY/DEGRADED/HALTED, latching conditions),
  alerts, decision log, version cohorts, session reports, state sync.
- Scheduled cycle Lambda (`cron(2/5 13-21 ? * MON-FRI *)`) running the real
  orchestrator against the internal paper broker.
- API: /agent/autonomy, decisions, sessions, session-report, ask (GET).
  Grounded deterministic explainer; no LLM.
- Dashboard: autonomous-paper panel, health, limits, decisions, alerts, ask box.
- 1611 tests passing.

## Defect found by live verification
Health, halt store, cycle lock, alerts and snapshot were pointed at the
session-keyed state table (PK/SK mismatch). Moved to the journal table;
guard tests added.

## Not done / blocked
- AlpacaPaperBroker and shadow comparison unwired: credential trading
  scope unverified (probe was denied). USER ACTION.
- Alert transport (SNS/email) not subscribed. USER ACTION (optional).
- First live session, 19B, and Milestone 20 need market days.
- API `execution_mode` shows UNKNOWN (API Lambda lacks the env var).
- Readiness: NOT_READY_BLOCKED; gates unchanged.

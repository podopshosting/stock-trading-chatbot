# Stock Trading Agent — Agent Handoff

Read this first. It is self-contained. Machine-readable twin: `docs/agent-handoff-state.json`.
Short start prompt: `docs/NEXT-AGENT-PROMPT.md`.

> Facts below were verified on **2026-10-01 ~13:45 UTC** against Git, AWS and the
> live dev API. Anything time-varying (paper session numbers, next invocation) must be
> re-queried — commands are in section 20.

## 0. What changed after this handoff was written

This document is a snapshot of **2026-10-01 ~13:45 UTC** and has not been
rewritten. Work continued the same day; where the two disagree, the
documents below are later and win.

| Topic | Read |
|---|---|
| Company Intelligence validated live; two defects a green suite missed | [progress/COMPANY-INTELLIGENCE-VALIDATION.md](progress/COMPANY-INTELLIGENCE-VALIDATION.md) |
| The 2026-10-01 session straddles two artifacts and counts toward no cohort | [progress/CYCLE-ARTIFACT-LAG.md](progress/CYCLE-ARTIFACT-LAG.md) |
| Real-time SIP cutover: runbook, preflight 16/16, **not executed** | [progress/REALTIME-COHORT-CUTOVER.md](progress/REALTIME-COHORT-CUTOVER.md) |
| A mutation run was voided by concurrent edits; two guards added | [progress/MUTATION-HARNESS-CONCURRENCY-INCIDENT.md](progress/MUTATION-HARNESS-CONCURRENCY-INCIDENT.md) |
| No earnings provider is wired, by instruction; Finnhub is next candidate | [EARNINGS-CALENDAR-SOURCES.md](EARNINGS-CALENDAR-SOURCES.md) |
| Current counts, SHAs, blockers | [agent-handoff-state.json](agent-handoff-state.json) |

Tests are now **2019 run: 2018 passing, 1 skipped**, not the 1738 below.
The mutation-control figure below is **stale** and needs a full re-run on
a committed tree.

## 1. Handoff checkpoint

| | |
|---|---|
| Boundary time | 2026-10-01 ~13:45 UTC (market open since 13:30 UTC / 09:30 ET) |
| Branch | `feature/trading-agent-v1` (never `main`) |
| Final SHA / tag | recorded in `docs/agent-handoff-state.json` (`head_sha`, `handoff_tag`); tag `agent-handoff-2026-10-01` |
| Local repo | `/Users/Brian 1/Documents/GitHub/stock-trading-chatbot` |
| Remote | `git@github.com:podopshosting/stock-trading-chatbot.git` |
| Tests | **1738 run: 1737 passing, 0 failing, 1 skipped** (`python3 -m unittest discover -s tests`) |
| Working tree | clean at handoff (backup lives outside the repo) |
| Earlier tags | `recovery-2026-09-30`, `paper-cohort-1` (see section 8: cohort 1 is VOID) |

## 2. Primary objective

A risk-governed personal trading agent that scans markets autonomously, evaluates
quantitative signals and catalyst evidence, forms explicit hypotheses, applies
deterministic risk controls, **paper trades autonomously**, manages positions and exits,
journals everything, and only *eventually* may support real-money execution after
explicit user approval and passing every readiness gate.

**REAL MONEY DISABLED.** `ExecutionMode` has DISABLED/PAPER only; LIVE is
unconstructible (`LiveExecutionRefused`); unknown values fail closed to DISABLED.
Overnight positions DISABLED. Readiness gate: `NOT_READY_BLOCKED`, 0 of 11 gates met —
correct and must never be bypassed.

## 3. Completed milestones (7–18, then 19A)

(Milestones 1–6 are the original chatbot; recovery tag `recovery-2026-09-30` restored it.)
Detail: `docs/progress/MILESTONE-07..19A.md`, `docs/progress/SUMMARY-7-18.md`.

- **Recovery** — production chatbot restored/verified; untouched since.
- **Provider abstraction/cache** — `agent/providers` (Alpaca, Alpha Vantage, cache, typed errors, provenance).
- **Agent state** — `agent/state` (transition graph; EMERGENCY_STOP terminal).
- **Market regime** — `agent/market`.
- **Scanner** — `agent/scanner` (own Lambda + 5-min schedule).
- **Quantitative signals** — `agent/signals`.
- **Evidence/catalysts** — `agent/evidence` (SEC EDGAR + Alpaca news; LLM only interprets).
- **Trade hypothesis** — `agent/hypothesis` (deterministic).
- **Risk Governor** — `agent/risk` (every rejection reason reported; derived approval).
- **Paper broker** — `agent/broker/paper.py` (internal) ; `alpaca_paper.py` exists but is **unwired**.
- **Position management** — `agent/positions` (exits, reconciliation).
- **Journal/analytics** — `agent/journal` (sample-adequacy gating; reports `NO_EDGE_DEMONSTRATED`).
- **Historical replay** — `agent/replay`.
- **Orchestration** — `agent/orchestration/day.py` (lock → reconcile → exits → regime → scan → signals → evidence → hypotheses → Risk Governor → paper orders → persist).
- **Dashboard** — `web/agent/index.html` (dev UI, includes autonomy panel).
- **Paper pilot** — scheduled cycle Lambda (19A).
- **Strategy evaluation** — `agent/evaluation`.
- **Broker research** — `docs/BROKER-INTEGRATION-OPTIONS.md`.
- **Readiness gate** — `agent/readiness.py` (11 gates; derived, no override).
- **19A autonomy** — `agent/autonomy/*` (policy, health, alerts, decisions, versions/cohorts, sessions, grounded explainer, schedule).

Counts: tests 1738 run (1737 pass, 1 skipped); falsifying-control mutations **140 at the last full harness run**
(not re-run during this handoff; see section 14); readiness 0/11; **completed paper
trades: 0**.

## 4. Company Intelligence status (package `agent/company/`)

The "slice at `fdc09c0`" (models, dividend logic, split logic, 16 tests) is now only the
start. **Much more is committed after it**, all tested (1738-test suite green (1 skipped)):

DONE and committed:
- models with provenance; dividends (status/history/special-vs-regular/next ex-date/yield price basis); splits (type from ratio, split-aware moves)
- `providers/alpaca_corporate_actions.py` — Alpaca `/v1/corporate-actions`, all action types, no inferred dates, page-cap raises (no silent truncation) — **validated live on GIS** (real dividend history, yield, cadence)
- `crosscheck.py` — `CORPORATE_ACTION_SOURCE_CONFLICT` preserved, never resolved
- `providers/sec_companyfacts.py` + `fundamentals.py` — period selection, TTM (4 sequential quarters only), freshness from period end (CURRENT/AGING/STALE), `PERIOD_MISMATCH`, date-aligned ratios — **validated live on GIS**; live validation found and fixed 3 real data defects (wrong concept chosen, DEF 14A facts overriding 10-K, quarter labelled FY)
- `earnings.py` — REPORTED_VALUE vs CONSENSUS_ESTIMATE vs DERIVED_SURPRISE, never swapped; SEC actuals only; Alpha Vantage normaliser exists but is **not wired** (shares the production chatbot's 25/day key)
- `peers.py` (+ `classification.py`, `universe.py`) — structured SIC-based peers, explicit rejections, LLM cannot create peers (`validate`, `refine_with_llm`) — **validated live** (GIS → SJM/KHC/HSY/KDP/CAG/CPB/LW/MDLZ)
- `compare.py` — peer median/range/percentile, PERIOD_MISMATCH exclusion, split-aware price performance
- `holding_context.py` — labels FAVORABLE…INSUFFICIENT_DATA, named factors, no composite, never an instruction, overnight stays DISABLED
- `store.py` — immutable history facts + versioned snapshots (journal table, PK `COMPANY#SYM`); `service.py`
- Dev API: `GET /agent/company/{symbol}[/dividends|splits|corporate-actions|earnings|fundamentals|peers|peer-comparison|holding-context]` (read-only; 405 on writes)
- Isolation: tests prove the cycle and scanner handlers load **no** `agent.company` code.

NOT done (do in this order):
1. `/agent/company/{sym}/peer-comparison` exceeds the 90 s Lambda limit on a cold cache (needs fundamentals for ~9 companies). Warm by repeat calls, or make it incremental/async. **Not yet live-validated end to end.**
2. Live validation of AAPL, NVDA, a non-dividend company, a forward-split company, a reverse-split company, an irregular/special-dividend company; deeper GIS validation write-up; HoldingContext live check.
3. Dashboard Company area (Overview/Dividends/Earnings/Financials/Competitors/Corporate Actions/History), dividend/split/earnings/competitor UX.
4. Chat grounding for company questions (extend `agent/autonomy/explain.py`; deterministic, no LLM facts).
5. `docs/HOLDING-STRATEGY-INPUTS.md` (not written).
6. Remaining falsifying controls into `scripts/falsifying_controls.py` (EPS swap, fiscal mismatch, unrelated peer, LLM-invented peer, stale-labelled-current) — tests for the behaviours exist; mutation entries do not.
7. Earnings calendar / consensus estimates: no source configured (next-earnings date shows unavailable). Needs a decision on a legitimate source (Alpha Vantage shares production quota).

## 5. Architecture

```
Market Data (Alpaca)
   ↓
Scanner  (own Lambda, every 5 min)  → stored ScannerRun
   ↓
Quant Signals → Evidence (SEC/news) → Hypothesis
   ↓
Risk Governor (deterministic, every reason reported)
   ↓
Paper Broker (internal PaperBroker; AlpacaPaperBroker unwired)
   ↓
Position Manager (exits, reconciliation) → Journal / Analytics → Readiness gate
```
Cycle Lambda `stock-agent-dev-cycle` runs the orchestrator every 5 min in market hours,
reading the stored scan/regime (age-checked ≤15 min).

```
Company Intelligence (agent/company, dev API only)
   ↓
Dividend / Earnings / Fundamentals / Peers / Corporate Actions
   ↓
HoldingContext  (longer-horizon context; NOT an execution instruction)
```
Company Intelligence is contextual. It is **not** a dependency of intraday execution
and is enforced not to be importable by the cycle/scanner handlers.

## 6. Current execution policy (from `RiskLimits()` defaults, `risk-v1.0.0`)

Paper mode ON; live OFF; overnight positions OFF; long-only; no shorting, options,
margin or leverage. Daily capital limit $50 (cumulative gross; hard max $100); max
position 60% of daily; min position $5; max 2 concurrent; max 3 new/day; daily loss
limit $5 (also: remaining loss budget must cover trade risk); max trade risk $2; max
spread 0.5%; min dollar volume $20M; max quote age 120 s; no new entries within 30 min
of close; EOD flatten. Same-session re-entry cooldown. Emergency stop is latching: only a
named human clears it (`cleared_by`); the agent may engage but never clear protections.

## 7. Scheduler

- EventBridge `stock-agent-dev-pilot-cycle`: `cron(2/5 13-21 ? * MON-FRI *)` ENABLED
- EventBridge `stock-agent-dev-scanner-schedule`: `cron(0/5 13-21 ? * MON-FRI *)` ENABLED
- UTC window 13–21 covers both EDT and EST sessions; the cycle self-gates on the market clock (`cycle_skipped_market_closed`).
- Cycle Lambda pinned `AGENT_CODE_SHA=66989c4`, `AGENT_EXECUTION_MODE=PAPER`.
- Next invocation: `GET /agent/autonomy` → `next_cycle_at`.

## 8. First live-market paper session — IN PROGRESS (2026-10-01)

Started 13:32 UTC. What happened (full honesty):
- **13:32** first cycle OK (OPENING, reconciliation matched).
- **13:37** cycle ABORTED: `KeyError 'conditions'`. Root cause: the first cycle's `record_cycle()` created the health item with only `streak`; the next read indexed a missing attribute. Fixed (`agent/autonomy/health.py`), regression tests added, deployed.
- **13:38** next abort: `'>' not supported between 'method' and 'float'`. Root cause: handler returned `Provenance.age_seconds` (a method) uncalled into the Risk Governor. Fixed in `lambda-micro/agent-cycle/handler.py`; dollar volume now derived from session volume (Quote has no dollar_volume field, so every candidate would have been refused). Fixed, tested with real `Quote`/`Provenance` types, deployed.
- **13:39:39** fixed code live. Since then cycles COMPLETE: 8 symbols scanned, 8 hypotheses, **0 approvals** (rejections: HYPOTHESIS_TOO_WEAK, HYPOTHESIS_NOT_ACTIONABLE, NOT_LONG_ONLY, INSUFFICIENT_CAPITAL), 0 orders, 0 positions. Zero trades is a valid outcome; do not retune.

Consequences: **cohort 1 (`paper-cohort-1`, SHA 8674df7) is VOID** — two defects meant it could not have traded. **Cohort 2 starts at the 13:39:39 deploy, code SHA `66989c4`.** Do not pool evidence across cohorts (`aggregate_evidence` already refuses). Today's session tally mixes cohorts in `cohort-c68ffe3fd9` for the aborted cycles; treat today as a partial/diagnostic session and read per-cycle logs. The formal session report is written at the close (20:00 UTC EDT) by the cycle; collect it with `GET /agent/session-report`.

Not yet verified for today: EOD flatten, final report, P&L (no positions so far).

## 9. AWS dev resources (account 899383035514, us-east-2, profile `mypodops`)

| Resource | Type | Purpose | Active | Production dependency |
|---|---|---|---|---|
| stock-agent-dev-cycle | Lambda | scheduled paper-trading cycle | yes | no |
| stock-agent-dev-scanner | Lambda | scanner + regime refresh (**older package**, deployed 2026-09-30 17:55, not from current SHA) | yes | no |
| stock-agent-dev-api | Lambda (Function URL) | read-only dev API incl. company endpoints | yes | no |
| stock-agent-dev-pilot-cycle | EventBridge rule | cycle schedule | yes | no |
| stock-agent-dev-scanner-schedule | EventBridge rule | scanner schedule | yes | no |
| stock-agent-dev-state | DynamoDB (key `session_date`) | agent state | yes | no |
| stock-agent-dev-journal | DynamoDB (PK/SK) | journal, decisions, sessions, health, alerts, snapshot, halt, lock, company data | yes | no |
| stock-agent-dev-broker | DynamoDB | internal paper broker state | yes | no |
| stock-agent-dev-positions | DynamoDB | positions | yes | no |
| stock-agent-dev-scanner / -signals / -evidence | DynamoDB | scans / signals / evidence | yes | no |
| stock-agent-dev-ui | S3 website | dev dashboard (`/agent/`, not the root) | yes | no |
| stock-agent/alpaca-paper | Secret | paper trading creds — **scope unverified, never read** | n/a | no |
| stock-chatbot-lambda-role | IAM role | **SHARED with production** (DynamoDB full access, Secrets Manager) | yes | **YES** |

Safe to delete later: nothing now; all dev resources hold evidence. Dev API URL is
discoverable with `aws lambda get-function-url-config --function-name stock-agent-dev-api`.
Dev dashboard: http://stock-agent-dev-ui.s3-website.us-east-2.amazonaws.com/agent/

## 10. Production state (UNTOUCHED by this work)

- Chatbot API: `https://lmi4hshs7h.execute-api.us-east-2.amazonaws.com/prod/chatbot` (GET → 403, expected)
- Frontend: `http://stock-chatbot-web.s3-website.us-east-2.amazonaws.com` (HTTP 200), bucket `stock-chatbot-web`, `index.html` ETag `dcbc02f151a223fbec6a5e4ebc794678`, last modified 2026-09-30T15:05:29Z
- Router Lambda `stock-chatbot-router` CodeSha256 `W4mlJEFVHdGMIoPC7azqBzJMkq2uQ7Wt29HPf6sEyGE=`, last modified 2026-09-30T18:20:31Z
- **`PRODUCTION_FIX_PENDING_APPROVAL`**: RSI flat-series correction is NOT deployed to production. Do not deploy without the user.
- Shared risk: the dev API and production router use the same IAM role.

## 11. Data providers

- **Alpaca** — market data (quotes, bars, clock, calendar), news, corporate actions. **Quote feed is `delayed_sip` (15-minute delay)** and `Provenance.age_seconds()` measures time since *our* retrieval, not data age → the 120 s quote-age guard cannot see the delay. See section 15.
- **SEC** — EDGAR filings (evidence) and Company Facts XBRL (fundamentals). Requires contact User-Agent; ≤8 req/s paced.
- **Alpha Vantage** — low frequency only; 25 req/day free tier; the key is **shared with the production chatbot**, so the agent does not call it.
- **OpenAI** — text interpretation/explanation only; never decides, never a data source.

## 12. Fidelity finding

`FIDELITY_AUTOMATED_EXECUTION_UNAVAILABLE` — Fidelity offers no supported retail trading API;
browser automation/scraping is intentionally prohibited. Researched candidates and criteria:
`docs/BROKER-INTEGRATION-OPTIONS.md`, `docs/FIDELITY_AUTOMATED_EXECUTION_UNAVAILABLE.md`.

## 13. Safety properties

Approval/readiness/health/new-exposure are derived properties (no setters). Asymmetric
fail-closed: unknown ⇒ refuse entries, but exits are never gated. No hidden composite
score (HoldingContext returns `composite_score: null`). Sample-adequacy gating; reports
`NO_EDGE_DEMONSTRATED`. Persistent latching emergency halt. Reconciliation mismatch ⇒
EMERGENCY_STOP; unexpected broker position alerts and is not auto-closed. Idempotent
orders (client_order_id; lookup before POST; never retry a POST; quarantine uncertain).
EOD flatten. No real execution path (`tests/test_live_contract.py` guards modules/hosts).

## 14. Known defect history

- **Leaked mutation**: the falsifying-controls harness once left a mutation in deployed dev code. Lesson, ask after every guard: **WHAT PROVES THIS GUARD EXECUTES?** Harness now: pre-flight refuses a tree containing `MUTATION` markers; baselines checksummed against Git; post-run restores from Git, verifies checksum, sweeps markers. Never treat the working tree as an unquestioned clean snapshot.
- Handoff check (this session): no `MUTATION` markers in `agent/`, `lambda-micro/`, `scripts/` (outside the harness); no `if False`/`and False`/`or True`; deployed cycle/API code diffed against repo `agent/` — identical (cycle lacks `agent/company` by design). Harness not re-run (it edits source).
- **Same-class bug found today**: component tests passed while the live path crashed twice (fakes returned floats where the provider returns objects; a DynamoDB record created by a different write path). Prefer tests that use real provider types and a fake that mimics DynamoDB write ordering.
- 19A found ~12 latent live-path defects only visible through end-to-end handler tests.

## 15. Current blockers (real ones only)

1. **Paper-evidence validity: 15-minute delayed quotes.** Paper fills and the quote-age guard run on `delayed_sip` quotes; evidence on those fills would not describe real-time trading. Needs a decision (real-time feed entitlement or accept/label as delayed). Not fixed here because it alters the evidence cohort.
2. **AlpacaPaperBroker/shadow comparison cannot start**: credential trading scope for `stock-agent/alpaca-paper` unverified; an attempt to probe the secret was denied by the permission system and must not be retried by another route. User must confirm/provide authorization.

## 16. External user actions

- Revoke the exposed old Alpha Vantage key (`docs/SECURITY_ACTION_REQUIRED.md`) — still active as of 2026-09-30; key is in public Git history.
- Confirm the Alpaca *paper* credentials may be used for the trading endpoints (blocker 2).
- Optional: subscribe an SNS/email transport for alerts (`AlertSink` has none configured).
- Optional: separate IAM role for dev vs production Lambdas.

## 17. Pending decisions (do not make silently)

Live broker selection; any real-money authorization; overnight holds; real-time data feed
(blocker 1); production RSI deployment; production UX migration; an earnings
calendar/consensus source (Alpha Vantage quota is shared with production); whether
dividend/earnings-hold strategies are ever built (`docs/HOLDING-STRATEGY-INPUTS.md` to be written first).

## 18. Immediate next tasks (exact order)

0. Re-verify the live session health (section 20). Do not touch cycle/scanner config.
1. Collect today's formal session report after 20:00 UTC; append to `docs/progress/` (zero trades is a valid result; do not retune).
2. Decide/raise blocker 1 with the user.
3. Company Intelligence, in order: (a) make peer-comparison fit the Lambda budget and live-validate GIS fully; (b) live validation AAPL, NVDA, non-dividend, forward-split, reverse-split, special-dividend names; (c) dashboard Company area; (d) chat grounding; (e) `docs/HOLDING-STRATEGY-INPUTS.md`; (f) remaining falsifying controls into the harness (run it with the pre-flight discipline); (g) dev-only deployment.
4. Then 19B (internal vs external paper comparison — blocked on blocker 2) and Milestone 20 (evidence accumulation).

Autonomous paper operation continues independently of all of this.

## 19. Files to read first

1. this file; 2. `docs/progress/SUMMARY-7-18.md`, `docs/progress/MILESTONE-19A.md`, `docs/progress/PAPER-COHORT-1-FREEZE.md`; 3. `docs/AGENT-ARCHITECTURE.md`; 4. `agent/autonomy/policy.py`, `health.py`; 5. `agent/orchestration/day.py`; 6. `lambda-micro/agent-cycle/handler.py`; 7. `agent/risk/governor.py`, `models.py`; 8. `agent/readiness.py`; 9. `docs/QUANTITATIVE-SIGNAL-ENGINE.md`, `docs/EVIDENCE-SOURCES.md`; 10. `agent/company/service.py`, `fundamentals.py`, `peers.py`; 11. `scripts/deploy_agent_dev.sh`, `scripts/falsifying_controls.py`; 12. `docs/SECURITY_ACTION_REQUIRED.md`.

## 20. Commands to run first

```bash
cd "/Users/Brian 1/Documents/GitHub/stock-trading-chatbot"
git status --short; git branch --show-current; git log --oneline -5
git tag -l 'agent-handoff*'
python3 -m unittest discover -s tests 2>&1 | grep -E '^(FAIL|ERROR):|^Ran|^OK|^FAILED'
# contamination (read-only; do NOT run falsifying_controls.py casually: it edits source)
git grep -nE "# MUTATION" -- agent lambda-micro   # expect no output
git diff --quiet HEAD && echo "tree == HEAD"
export AWS_PROFILE=mypodops          # default profile is a DIFFERENT account (Panda)
aws sts get-caller-identity --query Account --output text   # expect 899383035514
aws events list-rules --region us-east-2 --name-prefix stock-agent --query 'Rules[].[Name,State,ScheduleExpression]' --output text
U=$(aws lambda get-function-url-config --function-name stock-agent-dev-api --region us-east-2 --query FunctionUrl --output text)
curl -s "${U}agent/autonomy" | python3 -m json.tool | head -80
curl -s "${U}agent/readiness"
aws logs tail /aws/lambda/stock-agent-dev-cycle --region us-east-2 --since 15m | grep -E 'cycle_aborted|cycle_complete|risk_decision'
```
Deploy (dev only): `./scripts/deploy_agent_dev.sh [lambda|cycle|ui]`. **Redeploying `cycle` changes the evidence cohort** (new code SHA); only for a proven blocker. Never touch `stock-chatbot-*`.

## 21. Definition of safe continuation

Stay on `feature/trading-agent-v1`. No production deploy without the user. Keep paper and
live separated; never add a LIVE path. Keep autonomous paper collection running; a bug fix
to the execution path starts a new cohort — record it. Stop before any first real-money
execution. Preserve provenance and versioning. Use `AWS_PROFILE=mypodops`. No
`Co-Authored-By`/AI attribution in commits. Do not use `grep` for negatives on ignored
files (`/usr/bin/grep`). No force-push, no history rewrite.

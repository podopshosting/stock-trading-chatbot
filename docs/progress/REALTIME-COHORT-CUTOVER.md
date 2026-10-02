# Cutover to the real-time SIP paper cohort

Prepared 2026-10-01. **The cycle Lambda has NOT been redeployed.** Today's
session is still running the delayed-data artifact, deliberately: switching
the feed and the broker mid-session would split one session across two
runtimes, which the evidence rules correctly treat as unattributable.

## What is already verified (against the live service, not documentation)

| Check | Result |
|---|---|
| Paper account auth, `paper-api.alpaca.markets/v2/account` | **200 ACTIVE**, trading not blocked, $100,000 cash |
| Algo Trader Plus real-time SIP, `data.alpaca.markets ?feed=sip` | **200**, source timestamp `2026-10-01T17:14:00.146Z`, **data age 0.1 s** |
| Same through the dev API | `is_delayed=false`, `price_as_of` 17:07:51.28Z for a 17:07:51 request |
| Credential pair | the saved file names the **same key pair already in Secrets Manager** (fingerprint match); the stored copy has the secret half |
| Adapter refuses live hosts | `api.alpaca.markets`, `broker-api.alpaca.markets` both rejected |
| Deployed cycle mode | `AGENT_EXECUTION_MODE=PAPER`, no live URL in config |

For contrast, the delayed feed this morning returned data about 17 minutes
old (`price_asof` 12:16:20Z fetched at 12:33).

## Run this first

```bash
cd "/Users/Brian 1/Documents/GitHub/stock-trading-chatbot"
AWS_PROFILE=mypodops python3 scripts/preflight_realtime_cohort.py
```

16 checks, all of which must pass. A check that cannot be performed counts
as a failure, so an unknown blocks the deploy rather than being skipped.

## Then, AFTER the close (20:00 UTC) and before the next open

```bash
export AWS_PROFILE=mypodops

# 1. Confirm today's session finalised and flattened.
U=$(aws lambda get-function-url-config --function-name stock-agent-dev-api \
      --region us-east-2 --query FunctionUrl --output text)
curl -s "${U}agent/session-report" | python3 -m json.tool | head -40

# 2. Select the real-time feed and the external paper venue.
aws lambda get-function-configuration --function-name stock-agent-dev-cycle \
  --region us-east-2 --query 'Environment.Variables' --output json \
  > /tmp/cycle-env.json
python3 - <<'PY'
import json
env = json.load(open("/tmp/cycle-env.json")) or {}
env["ALPACA_QUOTE_FEED"] = "sip"
env["AGENT_BROKER"] = "alpaca_paper"
json.dump({"Variables": env}, open("/tmp/cycle-env-new.json", "w"))
PY
aws lambda update-function-configuration --function-name stock-agent-dev-cycle \
  --region us-east-2 --environment file:///tmp/cycle-env-new.json

# 3. Deploy the code (this also pins the new AGENT_CODE_SHA).
./scripts/deploy_agent_dev.sh cycle

# 4. Record the deployment time to the second - it is the cohort boundary.
date -u +"%Y-%m-%dT%H:%M:%SZ"
aws lambda get-function-configuration --function-name stock-agent-dev-cycle \
  --region us-east-2 \
  --query '[LastModified,Environment.Variables.AGENT_CODE_SHA,Environment.Variables.AGENT_BROKER,Environment.Variables.ALPACA_QUOTE_FEED]' \
  --output text
```

## Also pending: the dev API Lambda

Separate from the cohort boundary, and safe to do at any time, because
the API places no orders and imports no broker — a test asserts it.

```bash
export AWS_PROFILE=mypodops
cd "/Users/Brian 1/Documents/GitHub/stock-trading-chatbot"
./scripts/deploy_agent_dev.sh lambda
```

The target is **`lambda`**, not `api`. The valid targets are
`lambda|cycle|ui|all`; `api` exits 2 with a usage message, which is how
this was found on 2026-10-01 - the instruction said `api` and the deploy
did nothing.

The deploy script runs the test suite first and refuses a red one;
`AGENT_DEPLOY_SKIP_TESTS=1` overrides it and prints a warning.

What that deploy carries, none of which is live yet:

- the two Company Intelligence fixes found by live validation — peer
  selection no longer matches on the 2-digit SIC major group, and a
  FAVORABLE holding verdict now requires fundamentals positively
  established as CURRENT
- dividend growth over 1, 3 and 5 years, restated into current share
  terms
- `next_ex_note`, so a null next ex-date says why it is null
- an honest reason when a cohort has no finalised sessions yet, instead
  of "no sessions recorded" while a session is mid-flight
- two new dashboard panels: session close-out and shadow orders

**Verify after deploying**, since the peer fix is the one with a
user-visible before and after:

```bash
U=$(aws lambda get-function-url-config --function-name stock-agent-dev-api \
      --region us-east-2 --query FunctionUrl --output text)
for S in AAPL NVDA GE TSLA; do
  echo -n "$S peers: "
  curl -s "${U}agent/company/$S/peers" \
    | python3 -c "import json,sys;print([p['symbol'] for p in (json.load(sys.stdin).get('peers') or [])])"
done
```

Expected: AAPL no longer lists CAT or DE, and NVDA no longer lists GE.
Before the fix, AAPL returned `CAT, CSCO, DE, IBM` and NVDA returned
`AMD, AVGO, GE, INTC, MU`.

### Known blocker

On 2026-10-01 this deploy could not be run from the agent session: the
sandbox permission classifier refused `scripts/deploy_agent_dev.sh` as a
production deploy, although it targets only `stock-agent-dev-*`. It
needs to be run by the account holder, or the permission widened
deliberately for the dev functions.

## The cohort this creates

Fill in from step 4; do not guess any of it.

| Field | Value |
|---|---|
| Cohort start | the deployment timestamp from step 4 |
| Code SHA | pinned by the deploy script |
| Data feed | `sip` — real-time consolidated tape |
| Execution mode | `PAPER` (LIVE unconstructible) |
| Broker | `AlpacaPaperBroker`, authoritative; internal `PaperBroker` as shadow |
| Strategy / risk / exits / orchestration | `hypothesis-v1.0.0` / `risk-v1.0.0` / `exits-v1.0.0` / `orchestration-v1.1.0` |
| Expected class | first candidate `REAL_TIME_STRATEGY_EVIDENCE` |

Earlier cohorts stay as they are and must not be upgraded retroactively:

- **Cohort 1** (SHA `8674df7`): **VOID** — two defects meant it could not trade.
- **Cohort 2** (SHA `66989c4`, 2026-10-01 13:39:39): `OPERATIONAL_VALIDATION_ONLY` — it ran on the 15-minute delayed tape.

**The 2026-10-01 session straddles both.** Its first cycle ran at
13:32:42 on `8674df7`; the redeploy landed at 13:39:39, and the
remaining 64 cycles ran on `66989c4`. A runtime change inside a session
is `RUNTIME_VERSION_CHANGED_MID_SESSION` and makes the session
unattributable, so it must not be counted toward either cohort's
strategy evidence.

The session's own tally does not say so: per-cycle SHA recording shipped
in the same work as that redeploy, so the artifact running at session
open could not record it, and the tally reports a single SHA with the
feed as its only reason. See
[CYCLE-ARTIFACT-LAG.md](CYCLE-ARTIFACT-LAG.md). From the cohort created
below this is self-reporting, because the deployed artifact records
per-cycle SHAs.

## What will change in behaviour, and why

The Risk Governor now judges freshness on the **data's** timestamp rather
than on when we fetched it. On the delayed tape that means every entry
would be refused, which is correct and is why the feed and the rule ship
together. On real-time SIP a sub-second data age passes comfortably.

Expect the first real-time session to trade *less* than today's, not more:
the governor has one more thing it can refuse on. A zero-trade day remains
a valid outcome and is not a reason to retune.

## First order on the external venue

Do not force a trade. When the strategy produces an approved hypothesis,
`AlpacaPaperBroker` submits it automatically. Capture: hypothesis id, risk
decision id, client order id, symbol, side, quantity, order type, submit
time, broker order id, broker status, fills — and the internal shadow's
expected fill alongside, for the slippage comparison. Redact the account
number beyond its last four characters.


---

## VOID cohort attempt — 2026-10-01 21:06:32Z

The first cutover attempt deployed and then failed its own verification.
Recorded rather than overwritten, because the attempt happened and the
next attempt's timestamp has to be distinguishable from it.

| | |
|---|---|
| Deployed | 2026-10-01T21:06:32Z, SHA `522aa5f` |
| Config | `AGENT_BROKER=alpaca_paper`, `ALPACA_QUOTE_FEED=sip`, mode `PAPER` |
| Verification | **FAILED** — `ValueError: unknown log event 'broker_unavailable'` |
| Cycles on this artifact | 2 (21:06:42 verify, 21:07:41 scheduled), **both ABORTED** |
| Trades | none — market already closed, and the abort precedes any order |
| Evidence value | **none.** No cohort was opened. |

### What was actually wrong

Not the adapter. `AlpacaPaperBroker` constructs correctly against the
paper host — verified directly afterwards with the real credentials.

Neither `broker_selected` nor `broker_unavailable` was in
`observability.EVENTS`, and the logger fails closed on an unknown event.
The whole `AGENT_BROKER=alpaca_paper` branch therefore raised whichever
way it went, and it had never run before, so nothing had exercised it.

Worse, the success log sat **inside** the `try`, so the sequence was:

1. the adapter constructs fine
2. `log_event("broker_selected", ...)` raises
3. `except Exception` catches its own logging failure and treats it as a
   broker failure
4. `log_event("broker_unavailable", ...)` raises too, uncaught → abort

Had only the second name been missing, the result would have been worse
than a crash: a silent downgrade to the internal simulator, filing
simulator fills under a cohort labelled external.

### Second defect, found while investigating the first

`/agent/autonomy` reported **HEALTHY, no failure streak, no conditions,
no alerts** across both aborted cycles. `health.record_cycle` is called
from `agent/orchestration/day.py`, which an early exception never
reaches. The agent could abort every cycle and keep describing itself as
healthy.

Both are fixed, with regression tests proven to fail first:

- both events registered, and a general guard asserts that **every**
  `log_event` name anywhere in `agent/` and both handlers is registered
- only the adapter construction is inside the `try`, so a logging fault
  can never be reported as a broker fault
- an aborted cycle now records a health failure, best-effort and last,
  so a broken health store cannot mask the abort

### The next attempt starts a fresh boundary

The 21:06:32Z deployment is void and must not be cited as a cohort
start. `REALTIME_SIP_STRATEGY_EVIDENCE` begins at the timestamp of the
redeploy that carries these fixes, on the SHA that deploy pins.


---

## VOID cutover attempt 2 — 2026-10-02 00:01:43Z

The second attempt deployed and failed its verification on the next
statement of the same never-executed path. Recorded permanently
alongside the first; neither is a partial success.

| | |
|---|---|
| Deployed | 2026-10-02T00:01:43Z, SHA `a1bccb8` |
| Config | `AGENT_BROKER=alpaca_paper`, `ALPACA_QUOTE_FEED=sip`, mode `PAPER` |
| Verification | **FAILED** — `AttributeError: 'DynamoDBAlertSink' object has no attribute 'send'` |
| Cycles on this artifact | aborted; market closed, no order reached |
| API deployed in the same window | 00:02:06Z — **this half succeeded** and the peer fix went live |
| Evidence value | **none.** No cohort was opened. |

### What it actually was

The CloudWatch line above the abort named the real cause:
`NameError: name 'creds' is not defined`. `creds` was a local of
`_provider()` and had never been in scope in the broker branch, so
construction always raised, and the fallback then broke on its own
second and third statements.

Four defects in the same four lines, none of which 2042 tests could
see, because the branch ran only when `AGENT_BROKER` selected it and
nothing ever had:

1. `broker_selected` / `broker_unavailable` not in `observability.EVENTS`
2. `creds` undefined
3. `alerts.send` — the method is `emit`
4. `AlertKind.BROKER_UNAVAILABLE` did not exist

The first was found by the 21:06 deploy, the second and third by this
one, and the fourth only after the branch was extracted into
`_select_broker` so a test could call it. Fixing the statement each
error named, three times, was the mistake: an unexecuted path has no
reason to contain only one fault.

### Why both attempts stay VOID

Neither produced a cycle that completed. The second attempt's API
deployment did succeed and is not void, but a cohort is a property of
the trading runtime, and that runtime aborted. Reinterpreting either as
a partial cohort start would put a timestamp on evidence that does not
exist.

`REALTIME_SIP_STRATEGY_EVIDENCE` begins at the first regular-market
cycle on a runtime that has been verified end to end, not at either of
these timestamps.

---

## VOID cutover attempt 3 — 2026-10-02 14:09:47Z

| | |
|---|---|
| Deployed | 2026-10-02T14:09:51Z, SHA `d07c6a3` |
| Config | `alpaca_paper`, `sip`, `PAPER` — all correct |
| API half | 14:08:20Z, **verified and kept**; all 10 semantic checks passed |
| Verification | **FAILED** — `AttributeError: 'AlpacaPaperBroker' object has no attribute '_account'` |
| Trades | none. The venue stayed flat: 0 orders of any status, 0 positions |
| Evidence value | **none.** No cohort was opened. |

The fifth defect in the external-broker path, and the same class as the
four before it: `agent/broker/store.py` persists the internal simulator
by reaching into its private attributes, and the cycle handed it the
external adapter. Nothing had ever run a cycle with an external broker
selected, so every line assuming the simulator's internals was
unverified.

Note where it surfaced: off-hours cycles completed fine, because broker
state is only persisted on the intraday path. The deploy's verification
runs one cycle, and at 14:09 that cycle was intraday — which is why this
appeared now rather than during last night's closed-market checks.

Fixed by passing the internal simulator to the state store, which is
also the correct semantics: the external venue is authoritative for its
own state and is queried live, so caching a second copy locally would
create a second source of truth about real exposure.

The structural fix is the test, not the one-line change. A
`StrictExternalBroker` double now exposes only the public adapter
surface - no `_account`, `_positions`, `_orders` or `_client_ids` - and
the cycle is run against it both off-hours and intraday. Any future
reach into the simulator's internals fails in the suite.

### All three attempts remain VOID

21:06:32Z, 00:01:43Z and 14:09:47Z. None produced a completed trading
cycle. The API deployments at 00:02:06Z and 14:08:20Z both succeeded and
are not void, but a cohort is a property of the trading runtime.

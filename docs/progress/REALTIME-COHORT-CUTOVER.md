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
./scripts/deploy_agent_dev.sh api
```

The deploy script now runs the test suite first and refuses a red one;
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

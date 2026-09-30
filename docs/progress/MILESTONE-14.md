# Milestone 14 — Complete Agent Dashboard

Status: complete. 50 dashboard tests, 1,148 total, 109/109
mutations caught. Deployed to dev; production untouched.

Live: `http://stock-agent-dev-ui.s3-website.us-east-2.amazonaws.com/agent/`

## No composite score

The pipeline is shown as five separate stages:

1. **Quantitative** — direction, signal agreement, signal magnitude,
   regime-adjusted magnitude, freshness, group counts
2. **Evidence** — collected or not, materiality, novelty, independent
   and primary source counts, conflict flag
3. **Regime** — regime, confidence, risk posture
4. **Hypothesis** — strategy, actionability, strength, and strength's
   contributions **itemised**
5. **Risk** — approved or refused, and *every* reason code

`composite_score` is present in the API response and is always `null`,
with a `composite_score_detail` field explaining why. Each stage gets
its own bar on its own track, never summed.

The reason this matters is specific, not stylistic: a blended number
makes a strong signal with no catalyst look identical to a mediocre
signal with a good one. When the agent refuses a trade you want to know
*which* stage refused, and when it takes one you want to know which
stage was carrying it. A single number destroys both.

Hypothesis strength *is* a combination, so the dashboard breaks out its
contributions rather than leaving one number — otherwise it would be
exactly the opaque score this design avoids.

## The API cannot place an order

Not "does not by default" — cannot. Tests assert that no broker adapter,
no orchestrator and no order-submitting call site is imported anywhere
in the Lambda. There are 23 routes; 22 are GET and the single POST only
forces a regime evaluation.

Both kill switches default to **false**: a missing environment variable
is not permission. An unreadable global-halt state is reported as
**halted**, not "unknown" — showing unknown would invite someone to
assume it was clear.

`/agent/positions` returns `total_open_risk: null`, not `0.0`. Zero
would claim there is no risk; null says we do not know. And it explains
its empty result, because "no positions" and "position store not wired"
look identical otherwise.

## Presentation honesty

- A sticky `PAPER ONLY — NO EXECUTION PATH` banner that cannot scroll
  away.
- Every metric row shows value, 95% interval, sample size, adequacy,
  and **is this evidence** — the only cell that should gate a decision
  to increase size.
- The page states that both conditions are required, so a striking
  number from a handful of trades does not read as a finding.
- Where a stop price is displayed, the page states that an
  `ENGINE_POLLED` stop does not exist when the agent is not running.
- Prose is checked with denials stripped first, so a page saying "no
  guarantee" is not reported as claiming one.
- No CDN. A dashboard that cannot render without an external host is a
  dashboard that fails when it is most needed.

## Incident: a leaked mutation reached a deployed artifact

While deploying I found `agent/risk/governor.py` modified in the working
tree:

```python
-    elif context.spread_pct > limits.max_spread_pct:
+    elif False:  # MUTATION
```

A mutation from the falsifying-control harness had been left behind, and
it **had been packaged and deployed** to `stock-agent-dev-api`. I
confirmed this by downloading the deployed artifact and finding the
marker in it.

Blast radius was limited — that Lambda has no broker adapter,
`trading_enabled` is false, and a disabled spread check cannot cause a
trade there. It was corrected immediately and the redeployed artifact
verified clean. But a disabled safety check has no business in any
deployed artifact.

**The harness defect was worse than the leak.** Its snapshot came from
the working tree:

```python
originals = {p: p.read_text() for p in {m.path for m in MUTATIONS}}
original_sums = {p: checksum(p) for p in originals}
```

So a pre-existing mutation was recorded as the "original", faithfully
restored afterwards, and certified by the checksum as **"restored
cleanly"** — on every subsequent run, indefinitely. The checksum only
ever proved *unchanged since this run started*, which is not the same as
*matches the real source*, and nothing surfaced the difference.

Two fixes, both verified by planting a mutation and watching the harness
refuse:

1. **Pre-flight contamination check.** Every mutation writes a
   `MUTATION` marker, so its presence *before* any mutation is applied
   proves a previous run did not clean up. The harness aborts with exit
   3 (NOT RUN — neither pass nor fail) and prints the `git checkout`
   command to fix it.
2. **Post-run marker sweep**, independent of the checksum comparison,
   because that comparison can only detect drift from a snapshot which
   might itself be contaminated.

This is the guard-that-cannot-fail problem applied to the guard
infrastructure itself. The harness had been reporting a clean result it
could not actually verify.

## Other defects found

**I broke the dev Lambda and my own deploy script caught it.** My first
package omitted the vendored dependencies, so every provider-backed
endpoint failed with `No module named 'requests'` while the endpoints
that did not need them kept returning 200s — a partial failure that
looks exactly like a working deployment. The deploy script now installs
from `requirements.txt`, fails if `requests` is absent from the package,
and verifies after deploying by calling an endpoint that *needs* the
dependencies.

**I guessed at four constructors instead of reading them.**
`AlpacaProvider()`, `DynamoDBHaltStore()`, `MarketSessionService()` and
`EvidenceService.evaluate_symbol()` all took different arguments than I
assumed. The module already had factories (`_provider()`,
`_signal_service()`, `_evidence_service()`) that read credentials from
Secrets Manager and share one cached provider; I should have used them
from the start rather than constructing my own.

**I was evaluating the regime live instead of reading the stored one.**
That spends provider quota on every page view, and would show a regime
the agent never actually decided on. Now reads session state, as
`handle_market_regime` does.

**I broke nine production chatbot tests with a `sys.path` insert.**
Both Lambdas have a module called `handler`, and `sys.modules` caches
the first one imported — so `import handler` after a path insert makes
whichever test file runs first win, and the other silently exercises the
wrong module. `test_agent_api.py` had already hit this and fixed it by
loading via `importlib` under the distinct name `agent_api_handler`,
with a comment explaining the symptom appears *only when the whole suite
runs together*. I reintroduced a bug the codebase had already documented.

Fixed by using the same loader and module name, and added a guard test
that scans the test directory for bare `import handler` so a third
occurrence fails immediately rather than at suite level.

**Two of my own tests were weak, found by mutation.** A bare
`assertIn("m.is_evidence", page)` passed even with that table cell
replaced by a dash, because the field name also appears on the adjacent
colour-picking line — now pinned to the rendering expression. And my
unreadable-halt test was asserting against the *store's* internal
fail-closed behaviour while appearing to test the *handler's*: the store
never raises, so the handler's except block never ran, and both tests
passed only because both layers use the word "unreadable". Added a test
that injects a raising store, which is the only way to reach that layer,
plus a control proving the handler can report a clear halt.

**A substring scan matched a denial.** My test for "no order-placing
code" scanned for `place_order`, which matched inside
`"can_place_orders": False` — a field *denying* the capability being
read as evidence of it. Fixed to match call sites (`.place_order(`)
rather than bare substrings. Identical in kind to the Milestone 9
disclaimer problem and the Milestone 5 UI scan.

## Files

```
lambda-micro/agent-api/handler.py   8 new read endpoints (23 routes)
web/agent/index.html                the dashboard
web/agent/config.js                 API base, replaced at deploy
scripts/deploy_agent_dev.sh         dev-only deploy, with verification
tests/test_agent_dashboard.py       43 tests
```

## Production separation verified

`stock-chatbot-web/index.html` ETag is still
`dcbc02f151a223fbec6a5e4ebc794678`, matching the baseline. The deploy
script names only `stock-agent-dev-api` and `stock-agent-dev-ui`, so no
argument to it can reach production. The RSI fix remains undeployed.

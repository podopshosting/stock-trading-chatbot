# Analysis UX — review build

Status: **review only.** Deployed to `stock-agent-dev-ui`, not to production.

## The problem this addresses

The production page presented a result as:

```
BUY — 72.5% confidence
```

Three things are wrong with that line.

1. **It is not a confidence, and it is not a probability.** The number is
   the share of independent signal *groups* voting the winning direction,
   scaled by whether the signals inside those groups agreed with each
   other. It says nothing about the chance of a price move, and nothing
   about the chance of a profitable trade. Calling it "confidence"
   invites both readings.
2. **It hides its own structure.** The same 72.5% could come from two
   independent categories agreeing or from one category speaking twice.
   Those are very different pieces of evidence. After the correlation fix
   they score differently, but the old display could not show why.
3. **It cannot express disagreement.** When MACD and the moving averages
   pointed opposite ways, the page showed a slightly smaller number. It
   never said they conflicted.

## What the review build shows instead

```
BUY    2 ▲ groups support Buy
Strong 0 ▼ groups support Sell
       1 ■ neutral or no signal
       Independent groups agree
       4 indicators fired, grouped into 2 independent opinions of 3 categories.

SIGNAL AGREEMENT  Strong (0.72)
```

The leading claim is now a count of independent agreeing categories. The
0.72 remains, secondary, relabelled, and with an inline explanation.

### Independent signals

Three category cards — Trend, Momentum, Mean Reversion — each showing its
direction, the member indicators that produced it, and whether those
members disagreed.

A group whose members conflict is marked `MIXED` and states that its
influence is halved. Previously this was invisible; a conflicted group and
a unanimous one looked identical.

A group that produced no signal is shown as `NO SIGNAL` and marked *not
counted*, with the note that this is not the same as neutral. Silence and
neutrality were previously indistinguishable.

### Technical detail

| Panel | What changed |
|---|---|
| MACD | Line, signal line and histogram shown separately, with the relation (`MACD > Signal`) and the note that the signal line is a 9-period EMA. A positive MACD line can now visibly be a bearish crossover — a state that was unreachable before the signal-line fix. |
| RSI | Shows the engine's *actual* vote, including `NONE`, and states that the engine only votes below 30 or above 70. A reading of 66 no longer looks like a buy signal. |
| Moving averages | Price/SMA20/SMA50 with the structural relation spelled out. |
| Bollinger | Position in the range, plus the note that being near a band is not itself a signal — the engine only votes on a close outside one. |

### Data quality and market context

Source, freshness, age and the 15-minute delay are stated on every
result. `STALE` raises a visible banner rather than silently serving old
numbers. The market regime the stock is being judged against is shown
alongside, with the indices that produced it.

### What it says it does not mean

A closing card states in plain terms that signal agreement is not a
probability, that this is technical analysis of past prices, that a
recommendation is not advice, and that no order can be placed.

## Data contract

`GET /agent/analysis?symbol=X` returns named fields. **The UI never parses
prose to recover a number.** Built by `agent/analysis.py` from an
unmodified `ml_agent_lite` result.

Key fields:

```
recommendation          BUY | SELL | HOLD
signal_strength         Strong | Moderate | Weak | Mixed
signal_agreement        0.0–1.0
agreement_meaning       inline explanation text
agreement_summary       buy_groups, sell_groups, neutral_or_silent,
                        verdict, raw_signals_fired, groups_with_opinion
groups[]                key, label, direction, summary,
                        internal_disagreement, members[]
explanation             headline, lines[]   (deterministic, not generated)
indicators              macd, rsi, moving_averages, bollinger, …
data_quality            freshness, sources, age_seconds, is_delayed
market_context          regime, risk_posture, indices, …
```

The explanation is deterministic: the same inputs always produce the same
sentences. It is not model output.

## Deployment

| | |
|---|---|
| Review UI | `stock-agent-dev-ui` → http://stock-agent-dev-ui.s3-website.us-east-2.amazonaws.com |
| Review API | `stock-agent-dev-api` Lambda Function URL |
| Production | **untouched** — `stock-chatbot-web` and the `/chatbot` route are unchanged |

Admin routes are disabled on the review API (`AGENT_ADMIN_ENABLED=false`);
`POST /agent/regime/evaluate` returns 403 over the public URL.

There is no broker adapter, paper or live. No order can be placed.

## Tests

`tests/test_review_ui.py` (35 tests) extracts the shipped `<script>` block
and runs the real render functions under node against fixtures of the
contract, so the assertions are about what the page actually says.

Covered: recommendation states, agreement wording, banned terminology,
internal disagreement, silent groups, MACD display, honest indicator
readings, price sign, data freshness, market context, accessibility,
and that numbers render with no prose present.

Five mutations were used to confirm the guards can fail. One did not:
the accessibility check survived a glyph being removed, because it only
asserted that a glyph existed somewhere on the page. It now asserts
per-element, and both a badge-only and a tally-only strip turn it red.

## Known gaps

- Positions, Decisions and Journal are placeholders marked *Coming in
  Trading Agent*. They are not simulated.
- Chat remains on the production app; this build does not duplicate it.
- The review bucket is public-read over HTTP for review convenience.
  It should not become a durable surface.

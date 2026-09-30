# Trading Agent v1 — Architecture

**Status:** design + Milestone 2 implemented. Nothing here is wired to
production; the live chatbot is untouched.

**Scope boundary:** this phase stops at *paper* trading. No real-money order
ever leaves this system in Phase 1.

---

## 1. The principle that shapes everything

> Up to $50–$100 is *available* each day when an opportunity meets the
> system's criteria. Zero trades is a valid and often correct day.

The architecture is built so that "do nothing" is the cheap default path and
trading is the exception that must earn its way through a series of gates.
Anything that would make the system trade more often in order to feel busy is
a design error.

A second principle follows from the recovery work:

> Absence of data is not evidence, and a rate limit is not a fact about a
> stock.

Every value the agent handles carries provenance, and every "unknown" stays
distinguishable from "false".

---

## 2. Data flow

```
                    ┌─────────────────────────────────────────────┐
                    │            MARKET DATA LAYER                │
                    │  MarketDataProvider (interface)             │
                    │    AlphaVantageProvider │ AlpacaProvider…   │
                    │  CachedProvider ─ TieredCache(mem→DynamoDB) │
                    │  every value carries Provenance             │
                    └───────────────────┬─────────────────────────┘
                                        │ quotes, bars, status
                    ┌───────────────────▼─────────────────────────┐
                    │          MARKET REGIME ENGINE               │
                    │  SPY / QQQ / IWM / VIX → regime label        │
                    │  measured, not inferred by an LLM            │
                    └───────────────────┬─────────────────────────┘
                                        │ regime
   ┌────────────────┐   ┌───────────────▼─────────────────────────┐
   │ UNIVERSE RULES │──▶│              SCANNER                     │
   │ liquidity,     │   │  ranks the universe on cheap features    │
   │ price, spread  │   │  OUTPUT: Candidate[]  (never a trade)    │
   └────────────────┘   └───────────────┬─────────────────────────┘
                                        │ candidates
                    ┌───────────────────▼─────────────────────────┐
                    │            SIGNAL ENGINE                    │
                    │  momentum │ RS vs SPY │ VWAP │ MA │ RSI      │
                    │  MACD │ volume │ opening range │ ATR         │
                    │  pure functions: same bars → same output     │
                    │  declares signal CORRELATION, not just value │
                    └───────────────────┬─────────────────────────┘
                                        │ signal set + score
                    ┌───────────────────▼─────────────────────────┐
                    │       EVIDENCE / CATALYST ENGINE            │
                    │  SEC EDGAR │ news │ earnings │ econ calendar │
                    │  dedup by story, not by article             │
                    │  LLM summarises; primary doc is retained    │
                    └───────────────────┬─────────────────────────┘
                                        │ evidence[]
                    ┌───────────────────▼─────────────────────────┐
                    │           TRADE HYPOTHESIS                  │
                    │  entry zone, invalidation, target, horizon  │
                    │  a hypothesis — never a prediction          │
                    └───────────────────┬─────────────────────────┘
                                        │
                    ┌───────────────────▼─────────────────────────┐
                    │             RISK GOVERNOR                   │
                    │  DETERMINISTIC. ABSOLUTE VETO.              │
                    │  no LLM input on this path                  │
                    │  APPROVE(size) │ REJECT(reason)             │
                    └───────────────────┬─────────────────────────┘
                                        │ approved order only
                    ┌───────────────────▼─────────────────────────┐
                    │      BrokerAdapter (interface)              │
                    │   PaperBroker  │  future AlpacaBroker       │
                    │   Fidelity: NOT automatable (see below)     │
                    └───────────────────┬─────────────────────────┘
                                        │ fills
                    ┌───────────────────▼─────────────────────────┐
                    │  POSITION MANAGER → EXIT ENGINE             │
                    │  stop │ target │ trail │ thesis invalidated │
                    │  regime break │ max hold │ EOD flatten      │
                    └───────────────────┬─────────────────────────┘
                                        │
                    ┌───────────────────▼─────────────────────────┐
                    │  TRADE JOURNAL  (taken AND rejected)        │
                    │            ↓                                │
                    │  PERFORMANCE ANALYTICS                      │
                    └─────────────────────────────────────────────┘

   The chat sits BESIDE this pipeline and only ever reads from it:

        User ──▶ Chat ──▶ [agent state, evidence, journal, positions]
                            (read-only; chat cannot place orders)
```

---

## 3. The safety property

This must be structurally impossible:

```
LLM says BUY ──▶ order executes
```

It is impossible because the `BrokerAdapter` is only ever called by the Risk
Governor, and the Risk Governor takes **no LLM input**. The LLM's output is
data that enters a hypothesis; the hypothesis is then judged by deterministic
rules. The LLM cannot widen a limit, resize a position, or bypass a veto,
because it is never on that code path.

Two independent kill switches sit above everything:

| Switch | Effect |
|---|---|
| `TRADING_ENABLED=false` | no new entries; existing positions still managed |
| `EMERGENCY_STOP=true` | no new entries; flatten on next cycle; agent → `EMERGENCY_STOP` |

Paper trading obeys the same controls as live trading would, so the controls
are exercised continuously rather than first tested with real money.

---

## 4. Component audit of the existing application

### RETAIN (as-is)

| Component | Why |
|---|---|
| API Gateway `lmi4hshs7h` + `/chatbot` | works; the agent adds routes beside it |
| S3 static frontend | the dashboard extends this page |
| `stock-chatbot-lambda-role` | reusable execution role |
| `scripts/build_lambda_package.sh` | verified packaging; extend for packages |
| `_av_request` throttle logic | proven in recovery; **already lifted** into `AlphaVantageProvider` |
| CloudWatch log groups | observability baseline |

### MODIFY

| Component | Change |
|---|---|
| `handler.py` Alpha Vantage calls | replaced by `MarketDataProvider`; the handler becomes a chat surface over agent state |
| `ml_agent_lite` indicators (SMA/EMA/RSI/BB) | sound; move into the signal engine as pure functions **with the MACD defect fixed** |
| Frontend | keep; add the agent dashboard around it |
| DynamoDB | `stock-chatbot-predictions` is empty and unused; new agent tables rather than overloading it |

### REPLACE

| Component | Reason |
|---|---|
| `MLTradingAgent.analyze_stock` vote aggregation | **measured defect — see §5** |
| `calculate_macd` signal line | `signal = macd * 0.9` is not a 9-period EMA; carries no crossover information |
| `extract_stock_symbols` | naive; *"Explain dollar cost averaging"* resolves to **COST** (Costco) and fetches real quote data for a general question |
| Request-driven analysis model | the agent must scan on a schedule, not wait for `POST /chatbot` |

### DEFER

| Component | Reason |
|---|---|
| `stock-data-service`, `stock-news-service`, `stock-prediction-service` | unreachable, no caller; leave until the agent needs those roles |
| `shared/`, `lambda-layer/` (yfinance) | legacy architecture |
| `lambda/` (older handlers) | superseded |

---

## 5. Measured defect in the inherited scoring

The existing `analyze_stock` treats its six signals as independent votes and
averages their confidences. Two of them are not independent.

Measured over random-walk price series:

| Measurement | Result |
|---|---|
| `histogram` sign == `macd` sign | **300/300 trials** |
| `signal / macd` ratio | **exactly 0.900 always** |
| P(MACD buy) | **0.48** |
| P(MACD buy \| MA-crossover buy) | **0.93** |
| lift | **1.95×** |

Because `signal = macd * 0.9`, the test `macd > signal` reduces to `macd > 0`,
i.e. `EMA12 > EMA26` — a plain trend check. It duplicates the moving-average
signal instead of adding crossover information.

**Consequence:** the "ML Confidence: 72.5%" shown for AAPL was the average of
two confidences derived from *one* underlying observation, counted twice. It
is not a probability and must never be used for position sizing.

**Design rule carried forward:** the signal engine declares a correlation
group per signal, and the aggregator counts correlated signals once. The
number that reaches the Risk Governor must mean something.

---

## 6. Storage plan

New tables, namespaced `stock-agent-dev-*`. Nothing shares the production
table.

| Table | PK / SK | Purpose | TTL |
|---|---|---|---|
| `stock-agent-dev-cache` | `cache_key` | provider response cache | `expires_at` |
| `stock-agent-dev-state` | `agent_id` / `session_date` | agent state, regime, kill switches | — |
| `stock-agent-dev-candidates` | `session_date` / `ts#symbol` | scanner output | 30d |
| `stock-agent-dev-evidence` | `symbol` / `ts#source#id` | evidence with provenance | 180d |
| `stock-agent-dev-decisions` | `session_date` / `ts#id` | hypotheses + risk verdicts, **including rejections** | — |
| `stock-agent-dev-orders` | `order_id` | paper orders and fills | — |
| `stock-agent-dev-positions` | `agent_id` / `symbol` | open positions | — |
| `stock-agent-dev-trades` | `session_date` / `ts#id` | closed round-trips with MFE/MAE | — |

Rejected trades are first-class records. Without them there is no way to
learn whether the Risk Governor is protecting the account or strangling it.

---

## 7. Risk Governor design

Deterministic, pure, independently testable. Input: hypothesis + portfolio +
account + regime. Output: `APPROVE(quantity)` or `REJECT(reason_code)`.

Checks are ordered cheapest-and-most-absolute first, and **every** check must
pass:

| # | Check | Default |
|---|---|---|
| 1 | `EMERGENCY_STOP` not set | — |
| 2 | `TRADING_ENABLED` | true |
| 3 | Agent not in `DAILY_RISK_LOCK` | — |
| 4 | Market open, session allowed | regular only |
| 5 | Daily capital not exhausted | $50 (max $100) |
| 6 | Concurrent positions | ≤ 2 |
| 7 | New positions today | ≤ 3 |
| 8 | Daily loss limit | −$5 (10% of allocation) → lock |
| 9 | Per-trade risk | ≤ $2 |
| 10 | Stop present, on the correct side, non-zero distance | — |
| 11 | Spread | ≤ 0.5%, and **unknown spread rejects** |
| 12 | Liquidity / ADV | configurable floor |
| 13 | Earnings or high-impact event imminent | block window |
| 14 | Not already long the symbol (no averaging down) | — |
| 15 | Data freshness | reject stale quotes |
| 16 | LONG only | shorts/margin/options rejected |

Position size derives from **risk**, not from the allocation:

```
risk_per_share = entry - invalidation
quantity       = min(
                   max_trade_risk / risk_per_share,     # risk cap
                   remaining_daily_capital / entry,     # capital cap
                 )
```

If `quantity` rounds to zero, that is a REJECT, not a minimum-size trade.

The thresholds above are **starting defaults, not validated parameters.**
They will be revisited once paper data exists.

---

## 8. Paper trading design

`PaperBroker` implements `BrokerAdapter` and must be pessimistic — an
optimistic simulator produces a strategy that only works in the simulator.

- fills at the **far side** of the spread where bid/ask is known, never at the
  signal price
- configurable slippage, default adverse
- fractional shares (necessary at $50/day)
- rejects: insufficient buying power, market closed, invalid symbol
- partial fills where the data supports it
- realised and unrealised P&L, MFE/MAE per position

**Known limitation to state plainly:** the current data provider supplies no
bid/ask, so early paper fills use a modelled spread. Paper results are
therefore an *estimate*, and that estimate must not be quoted as performance.

---

## 9. AWS architecture

Nothing long-running. Four scheduled entry points via EventBridge:

| Schedule | Lambda | Work |
|---|---|---|
| 08:45 ET weekdays | `stock-agent-dev-premarket` | build universe, regime, earnings/econ calendar, reset session |
| every 5 min, 09:30–16:00 ET | `stock-agent-dev-scan` | scan → signals → evidence → hypothesis → risk → paper order |
| every 1 min, 09:30–16:00 ET | `stock-agent-dev-positions` | manage open positions and exits |
| 16:15 ET weekdays | `stock-agent-dev-eod` | flatten, reconcile, compute metrics, journal |

Step Functions is deliberately **not** used yet: the flow is a single
sequential pass per cycle, and the state machine would add operational
surface without removing any. Revisit if cycles need fan-out or retries with
distinct backoff.

Cadence is bounded by the data provider, not by ambition — a 5-minute scan on
a 25-request/day provider is impossible, which is precisely what the provider
abstraction and cache exist to fix.

---

## 10. Broker position

Confirmed by research and independently spot-checked:

**Fidelity offers no official retail API for programmatic order entry.**
Fidelity Access is read-only aggregation via approved aggregators; FIX and
Integration Xchange are institutional. The available `fidelity-api` packages
are browser automation or reverse-engineered private endpoints.

**Standing decision: Fidelity is never automated by this system**, in any
environment including tests. The risks are live credentials inside an
automated process, silent breakage on a money path (a mis-parsed page
producing a wrong order is worse than a crash), and account termination.

The architecture is therefore broker-agnostic behind `BrokerAdapter`:

- `PaperBroker` — Phase 1, the only implementation built
- `AlpacaBroker` — candidate for a future live phase; stateless REST suits
  Lambda, free paper environment, fractional shares, $0 minimum
- `FidelityBroker` — **only** if an official API appears
- Manual path — the agent can emit a trade ticket for the user to enter by
  hand at Fidelity, which needs no automation and breaks no terms

See `BROKER-INTEGRATION-OPTIONS.md` for the evidence.

---

## 11. Live trading readiness gate (future phase)

Live trading is out of scope. The gate is recorded now so it is not invented
later under pressure to ship:

1. a statistically meaningful number of paper trades — the number to be
   derived from observed variance, not asserted up front
2. positive expectancy with a confidence interval that excludes zero
3. maximum drawdown within a pre-declared tolerance
4. no unresolved operational failures over a sustained window
5. provider reliability measured, not assumed
6. Risk Governor veto behaviour proven by tests **and** by real rejections
7. paper-vs-live fill correlation measured on a small live sample
8. an officially supported broker API
9. kill switch exercised end-to-end
10. complete audit trail for every decision

**No threshold above is filled in yet.** Choosing numbers before data exists
would be fitting the gate to the hope. They will be proposed from the paper
record and reviewed before any live phase.

---

## 12. Milestones

| # | Scope | Depends on | Key tests | Exit criteria |
|---|---|---|---|---|
| 1 | Recovery baseline preserved | — | existing suite | ✅ pushed + tagged `recovery-2026-09-30` |
| 2 | Provider abstraction + cache | 1 | 33 offline tests | ✅ quota reduction demonstrated |
| 3 | Agent state + market regime | 2 | regime classification is deterministic | regime computed from real data, stored |
| 4 | Universe + scanner | 3 | illiquid rejected; missing data safe; stale recognised | ranked candidates on a schedule |
| 5 | Signal engine | 4 | known-value indicator tests; no lookahead; correlation groups honoured | deterministic signal set per candidate |
| 6 | Evidence model + SEC/news connectors | 4 | provenance retained; duplicates collapsed; source outage graceful | evidence attached to candidates |
| 7 | Trade hypothesis | 5, 6 | hypothesis only when both signal and evidence qualify | hypotheses produced and journalled |
| 8 | **Risk Governor** | 7 | every limit vetoes; **a high-scoring candidate is provably rejected** | no order can reach a broker unapproved |
| 9 | PaperBroker | 8 | fills, slippage, rejects, partials, fractional, P&L | round-trip paper trade executes |
| 10 | Position manager + exits | 9 | stop, target, timed, EOD | positions open and close unattended |
| 11 | Journal + analytics | 10 | expectancy, MFE/MAE, rejected-trade capture | a day's session fully reconstructable |
| 12 | Scheduled operation | 11 | premarket/scan/positions/EOD; provider and LLM outages | unattended paper day |
| 13 | Dashboard + grounded chat | 12 | chat answers only from stored state | "why did you buy X?" answered from the journal |

---

## 13. Endpoints (planned)

| Method | Path | Auth needed before any public exposure |
|---|---|---|
| GET | `/agent/status` | read |
| GET | `/agent/market-regime` | read |
| GET | `/agent/candidates` | read |
| GET | `/agent/positions` | read |
| GET | `/agent/orders` | read |
| GET | `/agent/trades` | read |
| GET | `/agent/performance` | read |
| GET | `/agent/evidence/{symbol}` | read |
| GET | `/agent/decision/{id}` | read |
| POST | `/agent/scan` | **write — must be authenticated** |
| POST | `/agent/paper/start` \| `/stop` | **write — must be authenticated** |
| POST | `/agent/emergency-stop` | **write — must be authenticated, and must work when everything else is broken** |

The existing `POST /chatbot` is unchanged.

No control endpoint ships publicly without authentication. `emergency-stop`
is the one endpoint that must remain reachable when the rest is degraded.

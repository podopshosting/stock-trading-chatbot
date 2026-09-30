# Agent State

How the trading agent's operational state is modelled, validated and
persisted. Milestone 3.

**This build has no order execution of any kind** — no broker adapter,
paper or live. The capital and P&L fields exist so the record's shape is
stable for later milestones; they are zero because nothing can trade.

---

## 1. States

```
OFFLINE ─────────────┬──▶ PRE_MARKET ──▶ SCANNING ──┬──▶ WATCHING
        │            │                              │
        └──▶ MARKET_CLOSED ◀───────────────────────  ┴──▶ TRADE_CANDIDATE
                    │                                        │
                    └──▶ OFFLINE                             ▼
                                                   PAPER_ORDER_PENDING
                                                             │
                                                             ▼
                                                     POSITION_OPEN
                                                             │
                                                             ▼
                                                    POSITION_EXITING

   from ANY state:  ──▶ EMERGENCY_STOP        (terminal for the process)
   from ANY state:  ──▶ DAILY_RISK_LOCK       (holds for the session)
```

| State | Meaning | Used in Milestone 3 |
|---|---|---|
| `OFFLINE` | Not operating; the default for a new session | yes |
| `PRE_MARKET` | Trading day, before the regular open | yes |
| `SCANNING` | Regular session open | yes |
| `WATCHING` | Tracking candidates | defined only |
| `TRADE_CANDIDATE` | A hypothesis is under evaluation | defined only |
| `PAPER_ORDER_PENDING` | A paper order is working | defined only |
| `POSITION_OPEN` | A paper position is open | defined only |
| `POSITION_EXITING` | Winding a position down | defined only |
| `DAILY_RISK_LOCK` | No new exposure for the rest of the session | yes |
| `MARKET_CLOSED` | Outside the regular session | yes |
| `EMERGENCY_STOP` | Halted | yes |

The later states are declared so the model is complete, but nothing in
this milestone drives the agent into them.

### A state is never a string

`AgentState.parse` raises on anything it does not recognise. An agent that
can be put into `"SCANNNIG"` by a typo does not have a state machine, so
arbitrary strings are rejected at the boundary rather than stored.

---

## 2. Transition rules

Every transition is either in the table or refused with
`InvalidTransition`. Re-asserting the current state is a no-op and records
nothing.

### Safety states are reachable from everywhere

`EMERGENCY_STOP` and `DAILY_RISK_LOCK` can be entered from any state. A
halt that first requires the agent to be in a tidy state is not a halt.

### `EMERGENCY_STOP` is not exited by a transition

It is terminal for the running process. Leaving it requires
`clear_emergency_stop()`, a deliberate, separately named act — and
**clearing it does not re-enable trading**. Restarting after a halt is a
decision, not a side effect.

### The risk lock blocks new exposure, not exits

`DAILY_RISK_LOCK` refuses every state in `EXPOSURE_INCREASING_STATES`
(`SCANNING`, `WATCHING`, `TRADE_CANDIDATE`, `PAPER_ORDER_PENDING`,
`POSITION_OPEN`) for the remainder of the session.

It deliberately still permits `POSITION_EXITING`. A lock that also blocked
exits would trap the agent in its open positions, which is the opposite of
risk control.

---

## 3. `trading_enabled` semantics

Defaults to `false` in `AgentSession`, in `AgentConfig`, and in the
deployed Lambda's environment.

It is necessary but **not sufficient** for trading. In this build there is
no execution path at all, so setting it to `true` changes nothing
operationally — `/agent/status` reports `execution_available: false`
independently of the flag, because that field describes the code, not the
configuration.

It is forced back to `false` by `trigger_emergency_stop()` and stays
`false` after `clear_emergency_stop()`.

---

## 4. Emergency stop semantics

| Property | Behaviour |
|---|---|
| Reachable from | any state |
| Effect | `emergency_stop = true`, `trading_enabled = false` |
| Idempotent | a repeated call re-asserts both flags rather than short-circuiting |
| Exit | only `clear_emergency_stop()`, which lands in `OFFLINE` |
| Survives | a status poll on an open market will not override it |

The idempotence matters: an "already stopped, nothing to do" shortcut
would let a bad write leave `trading_enabled = true` underneath an active
halt.

---

## 5. Persistence

**Table:** `stock-agent-dev-state` (DynamoDB, on-demand, `us-east-2`)
**Key:** `session_date` (S), one record per trading day

`stock-chatbot-predictions` is untouched. Overloading it would mix an
unrelated, empty legacy record with live agent state.

### Why not memory

Scheduled Lambdas share no process memory, and for this system losing
state means losing the record of a halt. `InMemoryStateStore` exists for
tests and local runs only.

### Optimistic concurrency

Each record carries a `revision`. Writes are conditional on it:

```
put_item(..., ConditionExpression="revision = :expected")
```

A write built from a stale read is rejected with `ConcurrentUpdate`, and
`AgentStateService` re-reads and re-applies the change. Without this, two
overlapping invocations could each read revision *n* and the second could
erase an emergency stop set by the first. Verified against real DynamoDB,
not only the in-memory fake.

Reads use `ConsistentRead=True` for the same reason: an eventually
consistent read could miss a halt written moments earlier.

### Mutation goes through the service

`AgentStateService._mutate` takes a function describing the change rather
than a session object the caller has been holding. That removes the class
of bug where a stale in-memory copy overwrites someone else's work.

### Session dates are market-local

`today_market_date()` uses `America/New_York`. A UTC day would roll over at
20:00 ET and split a live session across two records.

### Storage shape

The record is read and written whole and never queried by inner field, so
the document is stored as one JSON blob under `payload`. The fields an
operator would want to see in the console — `agent_state`,
`market_regime`, `market_status`, `trading_enabled`, `emergency_stop`,
`revision`, `updated_at` — are duplicated as top-level attributes.

`AgentSession.from_dict` ignores unknown keys, so a record written by a
newer version still loads.

---

## 6. History

Two histories are kept on the session:

- `state_history` — every state change, with source, target, timestamp and
  reason
- `regime_history` — regime changes only when the change is *meaningful*
  (see `MARKET-REGIME-ENGINE.md` for the hysteresis rule)

---

## 7. Structured log events

Emitted as one JSON object per line, so CloudWatch Logs Insights can query
fields directly. The event name is a closed set — `log_event` raises on an
unknown name, because an unqueryable event silently never matches a
dashboard filter.

```
agent_state_created        agent_state_transition     agent_state_write_retry
market_session_changed     regime_evaluated           regime_changed
regime_unknown             market_data_stale          market_data_missing
provider_error             emergency_stop_triggered   emergency_stop_cleared
daily_risk_lock_set
```

Any field whose name looks credential-shaped (`key`, `secret`, `token`,
`password`, `credential`, `apikey`) is replaced with `<redacted>`, so a
careless call site cannot repeat this project's earlier habit of logging
Authorization headers.

---

## 8. Known limitations

1. **Transitions are driven by the status endpoint, not a scheduler.**
   Milestone 12 adds EventBridge schedules. Until then the agent only
   advances when something calls it.
2. **`DAILY_RISK_LOCK` is never set automatically** — no P&L exists to
   trigger it. The mechanism is tested; the trigger arrives with the Risk
   Governor.
3. **No cross-session carry-over.** Each day starts at `OFFLINE`. Fine
   while there are no overnight positions, which is also the intended
   default later.
4. **The emergency stop is per session record.** A halt set today does not
   persist into tomorrow's session. That is a deliberate gap to close when
   scheduling lands, and it should be closed before anything can execute.

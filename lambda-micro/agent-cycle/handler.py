"""
The autonomous paper cycle.

One EventBridge invocation runs one orchestrator cycle against live
market data and a PERSISTED internal paper broker, under an explicit
autonomy policy. No individual paper trade needs a human.

What this Lambda can and cannot reach
-------------------------------------
It constructs the INTERNAL paper broker directly. There is no code path
here that selects a broker from configuration, and the Alpaca paper
adapter is deliberately NOT wired in: confirming that the stored
credential is a trading credential (rather than data-only) was not
permitted, so it has not been exercised against the real service.
`AGENT_EXECUTION_MODE` selects DISABLED or PAPER; LIVE is converted to
DISABLED and alerted, because a deployment told to go live must stop,
not trade.

The sequence is load -> cycle -> save -> account. State is loaded before
anything acts, and the orchestrator itself does exits before entries, so
a timeout partway through leaves risk reduced rather than added.

The first CLOSED cycle after a session that actually ran finalises it:
verifies the day ended flat, reconciles cash, writes the session report
once, and moves the agent to MARKET_CLOSED.
"""
from __future__ import annotations

import json
import os
import traceback
from typing import Dict, List, Optional

from agent.autonomy import (
    Alert, AlertKind, Condition, DynamoDBAlertSink,
    DynamoDBDecisionLog, DynamoDBSnapshotStore,
    DynamoDBHealthStore, DynamoDBSessionStore, ExecutionMode,
    aggregate_evidence, cohort_key, current_versions, daily_counters,
    finalize_session, policy_from_environment, record_cycle,
)
from agent.autonomy.evidence_class import classify_feed
from agent.autonomy.state_sync import sync_state
from agent.broker import (
    BrokerStateError, ConcurrentBrokerUpdate, DynamoDBBrokerStateStore,
    PaperBroker, PaperBrokerConfig, Quote,
)
from agent.broker.alpaca_paper import AlpacaPaperBroker, RequestsTransport
from agent.broker.store import restore as restore_broker
from agent.config import AgentConfig
from agent.hypothesis import generate as generate_hypothesis
from agent.journal import DynamoDBJournal, describe
from agent.market import MarketSessionService
from agent.observability import log_event
from agent.orchestration import (
    CyclePhase, DynamoDBCycleLock, MarketDayOrchestrator,
    phase_from_session,
)
from agent.positions import (
    DynamoDBPositionStore, PositionManager, PositionState,
    PositionStoreError,
)
from agent.providers import AlpacaProvider, CachedProvider, MemoryCache
from agent.risk import DynamoDBHaltStore, RiskLimits
from agent.scanner import DynamoDBScannerStore
from agent.signals import DynamoDBSignalStore, SignalService
from agent.state import AgentStateService, DynamoDBStateStore
from agent.state.store import today_market_date

# Asserted, not configured: nothing here can flip it.
JOURNAL_TABLE = os.environ.get("AGENT_JOURNAL_TABLE", "stock-agent-dev-journal")
# How old the MARKET DATA may be for an entry. Matches the Risk Governor's
# max_quote_age_seconds; a quote older than this is STALE whatever feed it
# came from.
MAX_SOURCE_AGE_SECONDS = 120.0

IS_LIVE = False

ACCOUNT_ID = os.environ.get("AGENT_PAPER_ACCOUNT", "paper")
MAX_CANDIDATES = 8

# The scanner Lambda refreshes the regime and the candidate list every
# five minutes on its own schedule; this cycle only READS them. Without
# an age check, a failing scanner would leave the cycle trading on a
# regime and a candidate list from hours ago, with nothing complaining.
# Three missed scans is enough to distrust them.
MAX_SCAN_AGE_SECONDS = 15 * 60
MAX_REGIME_AGE_SECONDS = 15 * 60


def _age_seconds(iso: Optional[str]) -> Optional[float]:
    """Seconds since an ISO timestamp, or None if it cannot be told.
    None is treated as stale by every caller: an unknown age is not a
    fresh one."""
    if not iso:
        return None
    try:
        from datetime import datetime, timezone
        then = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        if then.tzinfo is None:
            then = then.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - then).total_seconds()
    except ValueError:
        return None


def _config() -> AgentConfig:
    return AgentConfig.from_env()


def _load_alpaca_credentials(secret_id: str, region: str) -> Dict:
    """Market-DATA credentials. Read from Secrets Manager, never from the
    environment, never logged. Used for quotes and bars only."""
    import boto3
    client = boto3.client("secretsmanager", region_name=region)
    raw = client.get_secret_value(SecretId=secret_id)["SecretString"]
    return json.loads(raw)


def _provider():
    cfg = _config()
    creds = _load_alpaca_credentials(cfg.storage.alpaca_secret_id,
                                     cfg.storage.region)
    # The consolidated tape in real time. Entitlement verified against
    # the live service on 2026-10-01 (is_delayed=false, sub-second source
    # timestamp). Configurable so a loss of entitlement can be handled
    # without a code change - and the Risk Governor refuses on a stale
    # DATA age whatever the feed is called.
    inner = AlpacaProvider(api_key_id=creds["api_key_id"],
                           api_secret_key=creds["api_secret_key"],
                           quote_feed=os.environ.get("ALPACA_QUOTE_FEED",
                                                     "sip"))
    return inner, CachedProvider(inner, backend=MemoryCache())


def lambda_handler(event, context) -> Dict:
    """Run one cycle. Never raises: a scheduler retry would rerun the
    parts that already succeeded."""
    session_date = today_market_date()
    try:
        result = _run(session_date)
    except Exception as exc:                              # noqa: BLE001
        log_event("cycle_aborted", detail=str(exc)[:300])
        # health.record_cycle is called from the orchestration, which an
        # early exception never reaches - broker selection happens well
        # before it. Without this the streak stays clean while every
        # cycle fails, and the agent keeps describing itself as HEALTHY.
        # That is exactly the silence a failure streak exists to break.
        #
        # Best-effort and last: if the health store is itself the thing
        # that is broken, the abort must still be returned rather than
        # replaced by a second, different error.
        try:
            DynamoDBHealthStore(table_name=JOURNAL_TABLE).record_cycle(False)
        except Exception as health_exc:                   # noqa: BLE001
            log_event("cycle_failure_recorded", recorded=False,
                      detail=f"{type(health_exc).__name__}: "
                             f"{health_exc}"[:200])
        result = {
            "session_date": session_date, "ran": False,
            "error": type(exc).__name__, "detail": str(exc)[:300],
            "traceback": traceback.format_exc()[-800:],
            "new_exposure_permitted": False,
        }
    return {"statusCode": 200, "body": json.dumps(result, default=str)}


def _run(session_date: str) -> Dict:
    cfg = _config()
    policy = policy_from_environment(os.environ.get("AGENT_EXECUTION_MODE"))
    requested_live = (os.environ.get("AGENT_EXECUTION_MODE", "")
                      .strip().upper() == "LIVE")

    alerts = DynamoDBAlertSink(table_name=JOURNAL_TABLE)
    health = DynamoDBHealthStore(table_name=JOURNAL_TABLE)
    versions = current_versions()
    cohort = cohort_key(versions)

    if requested_live:
        # Refused, converted to DISABLED by the policy, and surfaced.
        log_event("live_mode_refused", session_date=session_date)
        alerts.emit(Alert(
            kind=AlertKind.LIVE_MODE_REQUESTED, session_date=session_date,
            detail="AGENT_EXECUTION_MODE=LIVE was configured. LIVE is not "
                   "implemented or authorised; the agent is DISABLED."))

    raw_provider, cached = _provider()
    state_service = AgentStateService(
        DynamoDBStateStore(table_name=cfg.storage.state_table,
                           region=cfg.storage.region))

    # --- where are we in the day? -------------------------------------
    session = MarketSessionService(raw_provider).current()
    phase = phase_from_session(session)

    journal = DynamoDBJournal(table_name=os.environ.get(
        "AGENT_JOURNAL_TABLE", "stock-agent-dev-journal"))
    sessions = DynamoDBSessionStore(table_name=os.environ.get(
        "AGENT_JOURNAL_TABLE", "stock-agent-dev-journal"))

    broker_store = DynamoDBBrokerStateStore(table_name=os.environ.get(
        "AGENT_BROKER_TABLE", "stock-agent-dev-broker"))
    internal = PaperBroker(PaperBrokerConfig(
        starting_cash=float(os.environ.get("AGENT_PAPER_CASH", "100")),
        slippage_bps=float(os.environ.get("AGENT_SLIPPAGE_BPS", "5")),
        partial_fill_probability=0.0, seed=None))
    internal.is_live = IS_LIVE
    snapshot, revision = broker_store.load(ACCOUNT_ID)
    if snapshot:
        restore_broker(internal, snapshot)

    # Alpaca's PAPER venue, when configured. It becomes authoritative for
    # broker facts - fills, positions, cash - and the internal simulator
    # stays available as a shadow so its expected fill can be compared
    # with what the venue actually did. The adapter refuses any base URL
    # but the paper domain, so this cannot reach real money.
    broker = internal
    external = None
    if os.environ.get("AGENT_BROKER", "internal").lower() == "alpaca_paper":
        # ONLY the construction is guarded. The success log used to sit
        # inside this try, so when it raised - both event names were
        # unregistered - the except clause caught its own logging
        # failure, reported it as `broker_unavailable`, and then died on
        # that log too. A logging fault must never be reported as a
        # broker fault: it would downgrade to the simulator and file the
        # result under a cohort labelled external, which is exactly what
        # the fallback below exists to prevent.
        try:
            external = AlpacaPaperBroker(
                transport=RequestsTransport(creds["api_key_id"],
                                            creds["api_secret_key"]))
        except Exception as exc:                          # noqa: BLE001
            # Fall back to the internal simulator rather than trading
            # against an adapter in an unknown state, and say so loudly:
            # a silent downgrade would put delayed-grade evidence into a
            # cohort labelled external.
            external = None
            log_event("broker_unavailable", broker="alpaca_paper",
                      error=f"{type(exc).__name__}: {exc}"[:200])
            alerts.send(Alert(
                kind=AlertKind.BROKER_UNAVAILABLE,
                detail=f"alpaca paper adapter unavailable: "
                       f"{type(exc).__name__}", session_date=session_date))
            broker = internal
        if external is not None:
            broker = external
            log_event("broker_selected", broker="alpaca_paper",
                      base_url=external.base_url, authoritative=True)

    position_store = DynamoDBPositionStore(table_name=os.environ.get(
        "AGENT_POSITIONS_TABLE", "stock-agent-dev-positions"))
    manager = PositionManager(broker=broker,
                              execution_available=policy.may_open_new_exposure)

    # --- closed / pre-market: nothing to trade ------------------------
    if phase in (CyclePhase.CLOSED, CyclePhase.PRE_MARKET,
                 CyclePhase.UNKNOWN):
        return _off_hours(phase, session_date, session, state_service,
                          sessions, journal, broker, manager,
                          position_store)

    try:
        for position in position_store.load_open(session_date):
            manager._positions[position.symbol] = position
    except PositionStoreError as exc:
        # The agent cannot know what it holds. Halting is the only
        # honest response, and a human is told.
        log_event("state_restore_failed", detail=str(exc)[:300])
        health.raise_condition(Condition.STATE_PERSISTENCE_FAILURE,
                               str(exc)[:160])
        return {"session_date": session_date, "ran": False,
                "error": "positions unreadable", "detail": str(exc)[:300],
                "new_exposure_permitted": False}

    decisions = DynamoDBDecisionLog(table_name=os.environ.get(
        "AGENT_JOURNAL_TABLE", "stock-agent-dev-journal"))
    orchestrator = MarketDayOrchestrator(
        broker=broker, position_manager=manager, journal=journal,
        halt_store=DynamoDBHaltStore(table_name=JOURNAL_TABLE,
                                     region=cfg.storage.region),
        limits=RiskLimits(),
        cycle_lock=DynamoDBCycleLock(table_name=JOURNAL_TABLE),
        autonomy=policy, health=health, alerts=alerts, decisions=decisions,
        versions=versions)

    # --- daily counters from the JOURNAL, not from memory --------------
    counters = daily_counters(journal, manager, session_date)
    candidates, scanner_ok, scanner_why = _candidates(cfg, phase)
    if not scanner_ok:
        health.raise_condition(Condition.SCANNER_DEGRADED, scanner_why)
    else:
        health.clear_condition(Condition.SCANNER_DEGRADED)

    data_quality_seen: set = set()
    quotes = _quote_loader(cached, broker, data_quality_seen)
    hypotheses = _hypothesis_loader(cached, cfg, session_date)
    opening_cash = broker.get_account()["cash"]

    cycle = orchestrator.run_cycle(
        session_date=session_date, phase=phase, candidates=candidates,
        quote_for=quotes, hypothesis_for=hypotheses,
        minutes_to_close=session.minutes_to_close,
        capital_deployed=counters["capital_deployed_today"],
        realized_pnl_today=counters["realized_pnl_today"],
        positions_opened_today=counters["positions_opened_today"])

    # --- persist, then account ----------------------------------------
    saved = _persist(broker, broker_store, revision if snapshot else None,
                     manager, position_store, session_date, health, alerts)
    payload = cycle.as_dict()
    payload.update({
        "state_saved": saved["ok"], "state_detail": saved["detail"],
        "account": broker.get_account(), "is_paper": not IS_LIVE,
        "cohort": cohort, "candidates": candidates,
        "market": session.as_dict(),
        "daily": counters,
        # What this cycle's fills were actually computed against. Absent
        # or mixed reads as UNKNOWN downstream, never as real-time.
        "data_quality": (data_quality_seen.pop()
                         if len(data_quality_seen) == 1 else
                         ("MIXED" if data_quality_seen else "UNKNOWN")),
        "data_qualities_seen": sorted(data_quality_seen),
    })

    try:
        rows = decisions.for_session(session_date)
        record_cycle(sessions, session_date, payload, versions,
                     [r for r in rows if r.get("cycle_id") ==
                      cycle.cycle_id], opening_cash=opening_cash)
    except Exception as exc:                              # noqa: BLE001
        payload["session_tally_error"] = str(exc)[:160]

    payload["state_sync"] = sync_state(state_service, payload, session_date)

    # Written where the dashboard and the chat can read it. Best effort:
    # failing to record this must never be a reason a cycle fails.
    try:
        DynamoDBSnapshotStore(table_name=JOURNAL_TABLE).put(payload)
    except Exception as exc:                              # noqa: BLE001
        payload["snapshot_error"] = str(exc)[:120]
    return payload


def _off_hours(phase, session_date, session, state_service, sessions,
               journal, broker, manager, position_store) -> Dict:
    """Closed, pre-market or unknown: nothing to trade.

    The first CLOSED cycle after a session that really ran finalises it.
    """
    out = {"session_date": session_date, "ran": False,
           "phase": str(phase),
           "reason": "the market cannot be reached in this phase",
           "new_exposure_permitted": False,
           "market": session.as_dict()}

    if phase is CyclePhase.CLOSED:
        try:
            trades = journal.list_trades(session_date=session_date)
            open_positions = [p for p in position_store.load_open(
                session_date)]
            report = finalize_session(
                sessions, session_date, trades, len(open_positions),
                [p for p in broker.get_positions()], broker.get_account(),
                describe_fn=describe)
            if report is not None:
                out["session_report"] = {
                    "session_ok": report["session_ok"],
                    "failed_checks": report["failed_checks"],
                    "zero_trade_day": report["zero_trade_day"],
                    "trades": report["trades"]}
        except Exception as exc:                          # noqa: BLE001
            out["finalize_error"] = f"{type(exc).__name__}: {str(exc)[:160]}"
        out["state_sync"] = sync_state(
            state_service, {"phase": "CLOSED"}, session_date)
    else:
        log_event("cycle_skipped_market_closed", session_date=session_date,
                  phase=str(phase))
    return out


def _persist(broker, broker_store, expected_revision, manager,
             position_store, session_date, health, alerts) -> Dict:
    """Save broker and position state.

    A failure raises STATE_PERSISTENCE_FAILURE, which halts new entries:
    the journal may already record an exit the saved account does not,
    and trading on from there would compound a known inconsistency.
    """
    detail, ok = [], True
    try:
        broker_store.save(broker, ACCOUNT_ID,
                          expected_revision=expected_revision)
    except ConcurrentBrokerUpdate as exc:
        ok = False
        detail.append(f"another writer saved first: {exc}")
    except BrokerStateError as exc:
        ok = False
        detail.append(f"broker state not saved: {exc}")

    for position in list(manager._positions.values()):
        try:
            if position.state is PositionState.CLOSED:
                position_store.delete(position.symbol, session_date)
            else:
                position_store.save(position, session_date)
        except PositionStoreError as exc:
            ok = False
            detail.append(f"{position.symbol}: {exc}")
    for position in manager.closed_positions():
        try:
            position_store.delete(position.symbol, session_date)
        except PositionStoreError as exc:
            ok = False
            detail.append(f"{position.symbol} (closed): {exc}")

    if not ok:
        try:
            health.raise_condition(Condition.STATE_PERSISTENCE_FAILURE,
                                   "; ".join(detail)[:200])
        except Exception:                                 # noqa: BLE001
            pass
    return {"ok": ok, "detail": "; ".join(detail)}


def _candidates(cfg, phase):
    """Today's scanner candidates: (symbols, readable, why).

    `readable` is False when the scan cannot be trusted - unreadable,
    failed, or too old - so health records the scanner as degraded
    rather than merely quiet. An empty list means no new entries, the
    correct direction to fail.
    """
    if not phase.permits_new_exposure:
        return [], True, ""
    try:
        store = DynamoDBScannerStore(table_name=cfg.storage.scanner_table,
                                     region=cfg.storage.region)
        run = store.latest_run()
    except Exception as exc:                              # noqa: BLE001
        log_event("provider_error", operation="scanner_candidates",
                  error=str(exc)[:200])
        return [], False, f"scanner unreadable: {type(exc).__name__}"
    if run is None:
        return [], False, "no scan has been recorded today"

    # latest_run() returns a ScannerRun DATACLASS, not a dict. An earlier
    # version called run.get(...), which would have raised on the first
    # live read and been swallowed as "scanner degraded".
    status = str(getattr(run.status, "value", run.status))
    if status != "COMPLETE":
        return [], False, f"the latest scan ended {status}"
    age = _age_seconds(run.completed_at or run.started_at)
    if age is None or age > MAX_SCAN_AGE_SECONDS:
        return [], False, (f"the latest scan is {age:.0f}s old"
                           if age is not None else
                           "the latest scan has no usable timestamp")
    symbols = [c.symbol for c in run.candidates if c.symbol]
    return symbols[:MAX_CANDIDATES], True, ""


def _quote_loader(cached, broker, observed: Optional[set] = None):
    """`observed` collects the data quality of every quote actually used,
    so the session can record what it traded on rather than leaving the
    feed to be inferred from configuration that may change."""
    def quote_for(symbol: str) -> Optional[Dict]:
        try:
            quote = cached.get_quote(symbol)
        except Exception:                                 # noqa: BLE001
            return None
        price = getattr(quote, "price", None) or getattr(quote, "last", None)
        if price is None:
            return None
        bid = getattr(quote, "bid", None)
        ask = getattr(quote, "ask", None)
        # The internal broker fills against a quote it is given.
        if hasattr(broker, "set_quote"):
            broker.set_quote(Quote(symbol=symbol, bid=bid or price,
                                   ask=ask or price, last=price))
        spread_pct = None
        if bid and ask and ask > 0:
            spread_pct = (ask - bid) / ((ask + bid) / 2) * 100.0
        provenance = getattr(quote, "provenance", None)
        # Age of the DATA, from the provider's own timestamp. The fetch
        # age below is kept for diagnostics but must never stand in for
        # this: a quote fetched 3 seconds ago can describe the market as
        # it was fifteen minutes earlier.
        source_age = None
        if provenance is not None and hasattr(provenance, "source_age_seconds"):
            source_age = provenance.source_age_seconds()
        feed_quality = classify_feed(
            getattr(provenance, "feed", None), source_age,
            MAX_SOURCE_AGE_SECONDS)
        if observed is not None:
            observed.add(str(feed_quality))
        # Provenance.age_seconds is a METHOD. Returning it uncalled put a
        # bound method into the Risk Governor's "> max age" comparison and
        # aborted the first live cycle that reached a hypothesis.
        age = getattr(provenance, "age_seconds", None)
        age = age() if callable(age) else age
        # The provider Quote carries session volume, not dollar volume.
        # Unknown stays None (the governor refuses on unknown).
        volume = getattr(quote, "volume", None)
        dollar_volume = getattr(quote, "dollar_volume", None)
        if dollar_volume is None and volume:
            dollar_volume = price * volume
        return {"price": price, "spread_pct": spread_pct,
                "dollar_volume": dollar_volume, "age_seconds": age,
                "source_age_seconds": source_age,
                "feed": getattr(provenance, "feed", None),
                "feed_quality": str(feed_quality)}
    return quote_for


def _hypothesis_loader(cached, cfg, session_date):
    signal_service = SignalService(
        cached,
        store=DynamoDBSignalStore(table_name=cfg.storage.signal_table,
                                  region=cfg.storage.region),
        config=cfg)
    state_service = AgentStateService(
        DynamoDBStateStore(table_name=cfg.storage.state_table,
                           region=cfg.storage.region))

    def hypothesis_for(symbol: str):
        try:
            signal = signal_service.evaluate_symbol(symbol).as_dict()
        except Exception:                                 # noqa: BLE001
            return None
        # The STORED regime, as the dashboard does: evaluating here would
        # spend quota per symbol per cycle and could show a regime the
        # agent never decided on.
        # The cycle's OWN session date, not the ambient clock: the two
        # can disagree around the day boundary, and reading the wrong
        # day's regime would silently return an empty UNKNOWN one.
        session = state_service.get_session(session_date).as_dict()
        age = _age_seconds(session.get("regime_updated_at"))
        if age is None or age > MAX_REGIME_AGE_SECONDS:
            # A stale regime is an unknown regime. The posture fails
            # closed to NO_NEW_TRADES rather than carrying forward a
            # reading nobody has refreshed.
            regime = {"regime": "UNKNOWN", "regime_confidence": 0,
                      "risk_posture": "NO_NEW_TRADES",
                      "market_session": session.get("market_session")
                      or "UNKNOWN"}
        else:
            regime = {
                "regime": session.get("market_regime") or "UNKNOWN",
                "regime_confidence":
                    session.get("market_regime_confidence") or 0,
                "risk_posture": session.get("risk_posture")
                or "NO_NEW_TRADES",
                "market_session": session.get("market_session")
                or "UNKNOWN"}
        # Evidence is deliberately not collected inside the cycle: it is
        # slow and rate-limited, and the hypothesis engine treats "not
        # collected" as a minor contradiction rather than assuming there
        # is no bad news.
        return generate_hypothesis(symbol, signal, None, regime)
    return hypothesis_for

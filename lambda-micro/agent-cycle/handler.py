"""
The paper pilot cycle.

One EventBridge invocation runs one orchestrator cycle against live
market data and a PERSISTED paper broker. Nothing here can reach a real
brokerage: the only adapter imported is the paper one, and it is
constructed directly rather than selected by configuration, so there is
no environment variable that could point it somewhere real.

The sequence is load -> cycle -> save, and the save is the part that
needs care. If it fails, the broker's in-memory fills are lost while the
journal may already record them, so the failure is reported loudly and
the next cycle's reconciliation will halt on the mismatch rather than
trade on top of it.

Order of operations mirrors the orchestrator's own rule: state is loaded
before anything acts, and the cycle does exits before entries, so a
timeout partway through leaves risk reduced rather than added.
"""
from __future__ import annotations

import json
import os
import traceback
from typing import Dict, List, Optional

from agent.broker import (
    BrokerStateError, ConcurrentBrokerUpdate, DynamoDBBrokerStateStore,
    PaperBroker, PaperBrokerConfig, Quote,
)
from agent.broker.store import restore as restore_broker
from agent.config import AgentConfig
from agent.hypothesis import generate as generate_hypothesis
from agent.journal import DynamoDBJournal
from agent.market import MarketRegimeService, MarketSessionService
from agent.observability import log_event
from agent.orchestration import (
    CyclePhase, DynamoDBCycleLock, MarketDayOrchestrator, resolve_phase,
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

# The pilot is paper. This is asserted rather than configured: a
# deployment cannot flip it, and `is_live` is what the journal reads to
# decide whether a trade is marked paper.
IS_LIVE = False

ACCOUNT_ID = os.environ.get("AGENT_PAPER_ACCOUNT", "paper")
MAX_CANDIDATES = 8


def _bool_env(name: str) -> bool:
    """A missing variable is not permission."""
    return os.environ.get(name, "false").strip().lower() in (
        "1", "true", "yes", "on")


def _config() -> AgentConfig:
    return AgentConfig.from_env()


def _load_alpaca_credentials(secret_id: str, region: str) -> Dict:
    """Credentials come from Secrets Manager, never from the
    environment, and are never logged."""
    import boto3
    client = boto3.client("secretsmanager", region_name=region)
    raw = client.get_secret_value(SecretId=secret_id)["SecretString"]
    return json.loads(raw)


def _provider():
    cfg = _config()
    creds = _load_alpaca_credentials(cfg.storage.alpaca_secret_id,
                                     cfg.storage.region)
    inner = AlpacaProvider(api_key_id=creds["api_key_id"],
                           api_secret_key=creds["api_secret_key"])
    return inner, CachedProvider(inner, backend=MemoryCache())


def lambda_handler(event, context) -> Dict:
    """Run one cycle. Never raises: a scheduler retry would rerun the
    parts that already succeeded."""
    session_date = today_market_date()
    result: Dict = {"session_date": session_date, "ran": False}

    try:
        result = _run(session_date)
    except Exception as exc:                              # noqa: BLE001
        log_event("cycle_aborted", detail=str(exc)[:300])
        result = {
            "session_date": session_date,
            "ran": False,
            "error": type(exc).__name__,
            "detail": str(exc)[:300],
            "traceback": traceback.format_exc()[-800:],
            "new_exposure_permitted": False,
        }
    return {"statusCode": 200, "body": json.dumps(result)}


def _run(session_date: str) -> Dict:
    cfg = _config()
    trading_enabled = _bool_env("AGENT_TRADING_ENABLED")
    execution_available = _bool_env("AGENT_EXECUTION_AVAILABLE")

    raw_provider, cached = _provider()

    # --- where are we in the day? ------------------------------------
    session = MarketSessionService(raw_provider).current()
    phase = resolve_phase(
        market_status=str(getattr(session, "session", "UNKNOWN")),
        minutes_since_open=getattr(session, "minutes_since_open", None),
        minutes_to_close=getattr(session, "minutes_to_close", None))

    if phase in (CyclePhase.CLOSED, CyclePhase.PRE_MARKET,
                 CyclePhase.UNKNOWN):
        # Nothing to do and nothing at risk to manage: the market cannot
        # be reached, so even exits would fail. Recorded rather than
        # silent, so a day of no activity is distinguishable from a day
        # the scheduler never fired.
        log_event("cycle_skipped_market_closed", session_date=session_date,
                  phase=str(phase))
        return {"session_date": session_date, "ran": False,
                "phase": str(phase),
                "reason": "the market cannot be reached in this phase",
                "new_exposure_permitted": False}

    # --- load persisted state BEFORE anything acts -------------------
    broker_store = DynamoDBBrokerStateStore(
        table_name=os.environ.get("AGENT_BROKER_TABLE",
                                  "stock-agent-dev-broker"))
    broker = PaperBroker(PaperBrokerConfig(
        starting_cash=float(os.environ.get("AGENT_PAPER_CASH", "100")),
        slippage_bps=float(os.environ.get("AGENT_SLIPPAGE_BPS", "5")),
        partial_fill_probability=0.0,
        seed=None))
    broker.is_live = IS_LIVE

    snapshot, revision = broker_store.load(ACCOUNT_ID)
    if snapshot:
        restore_broker(broker, snapshot)
        log_event("state_restored", account_id=ACCOUNT_ID,
                  revision=revision,
                  cash=round(broker.get_account()["cash"], 2),
                  open_positions=len(broker.get_positions()))

    position_store = DynamoDBPositionStore(
        table_name=os.environ.get("AGENT_POSITIONS_TABLE",
                                  "stock-agent-dev-positions"))
    manager = PositionManager(broker=broker,
                              execution_available=execution_available)
    try:
        for position in position_store.load_open(session_date):
            manager._positions[position.symbol] = position
    except PositionStoreError as exc:
        # An unreadable position store means the agent cannot know what
        # it holds. Reconciliation would halt anyway; halting here is
        # earlier and clearer.
        log_event("state_restore_failed", detail=str(exc)[:300])
        return {"session_date": session_date, "ran": False,
                "error": "positions unreadable",
                "detail": str(exc)[:300],
                "new_exposure_permitted": False}

    # --- run the cycle ------------------------------------------------
    orchestrator = MarketDayOrchestrator(
        broker=broker,
        position_manager=manager,
        journal=DynamoDBJournal(
            table_name=os.environ.get("AGENT_JOURNAL_TABLE",
                                      "stock-agent-dev-journal")),
        halt_store=DynamoDBHaltStore(table_name=cfg.storage.state_table,
                                     region=cfg.storage.region),
        limits=RiskLimits(),
        trading_enabled=trading_enabled,
        execution_available=execution_available,
        cycle_lock=DynamoDBCycleLock(table_name=cfg.storage.state_table))

    candidates = _candidates(cfg, phase)
    quotes = _quote_loader(cached, broker)
    hypotheses = _hypothesis_loader(cached, cfg, session_date)

    cycle = orchestrator.run_cycle(
        session_date=session_date,
        phase=phase,
        candidates=candidates,
        quote_for=quotes,
        hypothesis_for=hypotheses,
        minutes_to_close=getattr(session, "minutes_to_close", None),
        capital_deployed=_deployed_today(broker),
        realized_pnl_today=broker.get_account()["realized_pnl"])

    # --- persist AFTER the cycle --------------------------------------
    saved = _persist(broker, broker_store, revision if snapshot else None,
                     manager, position_store, session_date)

    payload = cycle.as_dict()
    payload["state_saved"] = saved["ok"]
    payload["state_detail"] = saved["detail"]
    payload["account"] = broker.get_account()
    payload["is_paper"] = not IS_LIVE
    return payload


def _persist(broker, broker_store, expected_revision, manager,
             position_store, session_date) -> Dict:
    """Save broker and position state.

    A failure here is serious in a specific way: the journal may already
    record an exit that the saved account does not, so the next cycle's
    reconciliation will find a mismatch and halt. That is the correct
    outcome, and it is better than retrying blindly against state whose
    decisions were made from a now-stale snapshot.
    """
    detail = []
    ok = True
    try:
        broker_store.save(broker, ACCOUNT_ID,
                          expected_revision=expected_revision)
    except ConcurrentBrokerUpdate as exc:
        ok = False
        detail.append(f"another writer saved first: {exc}")
        log_event("state_restore_failed",
                  detail=("broker state not saved; another cycle wrote "
                          "first. The next cycle will reconcile and halt "
                          "on any mismatch."))
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

    # Closed positions were removed from the manager by _close, so they
    # must be deleted from storage explicitly or they come back as open
    # on the next cycle and reconciliation halts on a position the
    # broker no longer has.
    for position in manager.closed_positions():
        try:
            position_store.delete(position.symbol, session_date)
        except PositionStoreError as exc:
            ok = False
            detail.append(f"{position.symbol} (closed): {exc}")

    return {"ok": ok, "detail": "; ".join(detail)}


def _deployed_today(broker) -> float:
    """Capital currently in positions, at cost."""
    return sum(p.get("cost_basis") or 0.0 for p in broker.get_positions())


def _candidates(cfg, phase) -> List[str]:
    """Today's scanner candidates.

    Returns [] on any failure. An empty candidate list means no new
    entries, which is the correct direction to fail: the cycle still
    reconciles and still runs exits.
    """
    if not phase.permits_new_exposure:
        return []
    try:
        store = DynamoDBScannerStore(table_name=cfg.storage.scanner_table,
                                     region=cfg.storage.region)
        run = store.latest_run()
        if not run:
            return []
        symbols = [c.get("symbol") for c in (run.get("candidates") or [])]
        return [s for s in symbols if s][:MAX_CANDIDATES]
    except Exception as exc:                              # noqa: BLE001
        log_event("provider_error", operation="scanner_candidates",
                  error=str(exc)[:200])
        return []


def _quote_loader(cached, broker):
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
        # The broker needs a quote to fill against.
        broker.set_quote(Quote(symbol=symbol, bid=bid or price,
                               ask=ask or price, last=price))
        spread_pct = None
        if bid and ask and ask > 0:
            spread_pct = (ask - bid) / ((ask + bid) / 2) * 100.0
        provenance = getattr(quote, "provenance", None)
        return {
            "price": price,
            "spread_pct": spread_pct,
            "dollar_volume": getattr(quote, "dollar_volume", None),
            "age_seconds": getattr(provenance, "age_seconds", None),
        }
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
        # The STORED regime, as the dashboard does. Evaluating here
        # would spend quota per symbol per cycle and could show a regime
        # the agent never decided on.
        session = state_service.get_session().as_dict()
        regime = {
            "regime": session.get("market_regime") or "UNKNOWN",
            "regime_confidence": session.get("market_regime_confidence") or 0,
            "risk_posture": session.get("risk_posture") or "NO_NEW_TRADES",
            "market_session": session.get("market_session") or "UNKNOWN",
        }
        # Evidence is deliberately not collected inside the cycle: it is
        # slow and rate-limited, and the hypothesis engine treats
        # "not collected" as a minor contradiction rather than assuming
        # there is no bad news. A separate scheduled pass populates it.
        return generate_hypothesis(symbol, signal, None, regime)
    return hypothesis_for

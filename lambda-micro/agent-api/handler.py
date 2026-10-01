"""
Agent API - development read endpoints for Milestone 3.

    GET  /agent/status             agent + market + regime state
    GET  /agent/market-regime      detailed quantitative regime data
    POST /agent/regime/evaluate    force a fresh evaluation (ADMIN ONLY)
    GET  /agent/analysis           per-symbol analysis, shaped for the UI
    GET  /agent/signals            canonical signal result for a symbol
    GET  /agent/signals/latest     the most recent stored signal run
    GET  /agent/scanner/signals    signals for a scanner run's candidates

Separate from the production chatbot Lambda on purpose: the live
`/chatbot` route keeps working regardless of anything here.

**The POST endpoint is disabled unless AGENT_ADMIN_ENABLED is set.** The
current API has no authentication, and an unauthenticated endpoint that
spends provider quota on demand is an obvious way for a stranger to
exhaust the budget. It stays off until there is auth in front of it.

Milestone 14 adds read endpoints for the trading pipeline:

    GET  /agent/hypothesis         decomposed hypothesis for a symbol
    GET  /agent/risk/limits        the limits actually in force
    GET  /agent/risk/preview       what the risk governor would decide
    GET  /agent/positions          open positions, plans and open risk
    GET  /agent/journal            completed trades
    GET  /agent/performance        metrics WITH sample adequacy
    GET  /agent/switches           kill-switch state
    GET  /agent/pipeline           every stage for one symbol, separately

**Nothing in this module can place an order.** `/agent/risk/preview`
runs the risk governor and returns its verdict; it does not touch a
broker, and there is no broker adapter wired into this Lambda at all.
The endpoints that would execute are deliberately absent rather than
disabled, so there is no flag anywhere in this file that could turn
execution on.

The dashboard these serve shows every stage SEPARATELY - scanner score,
signal agreement, signal magnitude, evidence materiality, evidence
novelty, hypothesis strength, risk verdict. There is deliberately no
composite "AI score", because a single number would hide which stage
actually drove a decision and make the pipeline impossible to debug or
to distrust intelligently.
"""
from __future__ import annotations

import json
import os
from typing import Dict, Optional, Tuple

from agent.config import AgentConfig
from agent.market import MarketRegimeService, MarketSessionService
from agent.observability import log_event
from agent.providers import AlpacaProvider, CachedProvider, MemoryCache
from agent.providers.base import ProviderError, SymbolNotFound
from agent.analysis import build_analysis
from agent.scanner import DynamoDBScannerStore
from agent.signals import DynamoDBSignalStore, SignalService
from agent.evidence import DynamoDBEvidenceStore, EvidenceService
from agent.evidence.providers import AlpacaNewsProvider, SECProvider
from agent.hypothesis import generate as generate_hypothesis
from agent.journal import DynamoDBJournal, describe as describe_perf
from agent.risk import RiskContext, RiskLimits
from agent.risk import evaluate as evaluate_risk
from agent.risk import DynamoDBHaltStore
from agent.evaluation import assess as calibrate
from agent import readiness
from agent.autonomy import (
    DynamoDBAlertSink, DynamoDBDecisionLog, DynamoDBHealthStore,
    DynamoDBSessionStore, DynamoDBSnapshotStore, aggregate_evidence,
    classify_question, explain as explain_question, next_cycle_time,
)
from agent.positions import DynamoDBPositionStore
from agent.company.service import CompanyService
from agent.company.store import DynamoDBCompanyStore
from agent.company.providers.alpaca_corporate_actions import AlpacaCorporateActions
from agent.company.providers.sec_companyfacts import SECCompanyFacts
from agent.state import (
    AgentState, AgentStateService, DynamoDBStateStore, MarketSession,
)
from agent.state.store import today_market_date

CORS_HEADERS = {
    "Content-Type": "application/json",
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "Content-Type,Authorization",
    "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
}

# Reused across warm invocations so the in-process cache and any
# credentials survive between calls.
_PROVIDER = None
_CONFIG: Optional[AgentConfig] = None


def _config() -> AgentConfig:
    global _CONFIG
    if _CONFIG is None:
        _CONFIG = AgentConfig.from_env()
    return _CONFIG


def _load_alpaca_credentials(secret_id: str, region: str) -> Dict:
    import boto3
    client = boto3.client("secretsmanager", region_name=region)
    raw = client.get_secret_value(SecretId=secret_id)["SecretString"]
    return json.loads(raw)


def _provider():
    """Cached provider. Credentials are read from Secrets Manager, never
    from environment variables, and never logged."""
    global _PROVIDER
    if _PROVIDER is None:
        cfg = _config()
        creds = _load_alpaca_credentials(
            cfg.storage.alpaca_secret_id, cfg.storage.region
        )
        # The quote feed is configurable so the SIP entitlement can be
        # verified against the live service before it becomes the
        # default. "delayed_sip" is the free-plan tape, 15 minutes behind.
        inner = AlpacaProvider(
            api_key_id=creds["api_key_id"],
            api_secret_key=creds["api_secret_key"],
            quote_feed=os.environ.get("ALPACA_QUOTE_FEED", "sip"),
        )
        _PROVIDER = (inner, CachedProvider(inner, backend=MemoryCache()))
    return _PROVIDER


def _journal():
    cfg = _config()
    return DynamoDBJournal(
        table_name=os.environ.get("AGENT_JOURNAL_TABLE",
                                  "stock-agent-dev-journal"))


def _scanner_store():
    cfg = _config()
    return DynamoDBScannerStore(table_name=cfg.storage.scanner_table,
                                region=cfg.storage.region)


def _state_service() -> AgentStateService:
    cfg = _config()
    store = DynamoDBStateStore(
        table_name=cfg.storage.state_table, region=cfg.storage.region
    )
    return AgentStateService(store, cfg)


def _session_date() -> str:
    """Today's session date in MARKET time.

    Reuses the scanner's and state store's existing helper rather than
    deriving a second answer: a run started at 01:00 UTC belongs to the
    previous US trading session, and two functions disagreeing about
    which day it is would file runs under days the market was shut.
    """
    return today_market_date(_config().market_timezone)


def _signal_store():
    """None rather than a raising stub when no table is configured.

    A read route can then say "storage unavailable" instead of
    returning a 500 that looks like a bug in the engine.
    """
    cfg = _config()
    table = os.environ.get("AGENT_SIGNAL_TABLE",
                           getattr(cfg.storage, "signal_table", "") or "")
    if not table:
        return None
    return DynamoDBSignalStore(table_name=table, region=cfg.storage.region)


# SEC asks for a descriptive User-Agent with contact details and refuses
# requests without one.
SEC_USER_AGENT = os.environ.get(
    "SEC_USER_AGENT",
    "stock-trading-chatbot dev (contact: hi@thepodops.com)")


def _evidence_store():
    cfg = _config()
    table = os.environ.get("AGENT_EVIDENCE_TABLE",
                           getattr(cfg.storage, "evidence_table", "") or "")
    if not table:
        return None
    return DynamoDBEvidenceStore(table_name=table, region=cfg.storage.region)


def _evidence_service() -> EvidenceService:
    """Evidence providers, ordered primary-first.

    SEC leads because a filing outranks a story about it when the two
    describe the same event, and deduplication keeps whichever comes
    first in the list as the group seed.
    """
    cfg = _config()
    creds = _load_alpaca_credentials(cfg.storage.alpaca_secret_id,
                                     cfg.storage.region)
    _raw, cached = _provider()
    cache = getattr(cached, "cache", None) or getattr(cached, "backend", None)

    sec = SECProvider(user_agent=SEC_USER_AGENT, cache=cache)
    news = AlpacaNewsProvider(api_key_id=creds["api_key_id"],
                              api_secret_key=creds["api_secret_key"],
                              cache=cache)

    def resolve_name(symbol: str):
        # Used to tell "a story about this company" from "a story that
        # merely mentions it". Best-effort: a failure only weakens the
        # relevance check.
        return sec.cik_for(symbol)[1]

    top_n = int(os.environ.get("EVIDENCE_TOP_N", "5"))
    return EvidenceService([sec, news], store=_evidence_store(),
                           top_n=top_n, name_resolver=resolve_name)


def _signal_service() -> SignalService:
    """The canonical engine, over the CACHED provider.

    Cached specifically: `/agent/analysis` fetches a quote for the
    presentation fields and the service then asks for the same one, so
    without the cache a single page view would cost two identical
    provider calls.
    """
    _raw, cached = _provider()
    return SignalService(cached, store=_signal_store(), config=_config())


def _response(status: int, body: Dict) -> Dict:
    return {
        "statusCode": status,
        "headers": dict(CORS_HEADERS),
        "body": json.dumps(body, default=str),
    }


def _route(event) -> Tuple[str, str]:
    method = (event.get("httpMethod")
              or (event.get("requestContext", {}) or {})
              .get("http", {}).get("method")
              or "GET").upper()
    path = (event.get("path")
            or (event.get("requestContext", {}) or {})
            .get("http", {}).get("path")
            or event.get("rawPath")
            or "/")
    return method, path.rstrip("/") or "/"


def _sync_market_session(state_service: AgentStateService, raw_provider) -> Dict:
    """Refresh the market session and align agent state with it.

    Only the transitions that are valid without a scanner are driven here.
    An invalid move is logged and skipped rather than forced, so the state
    machine stays the authority on what is reachable.
    """
    session_result = MarketSessionService(raw_provider).current()
    state_service.set_market_session(
        MarketSession.parse(session_result.session.value)
    )

    session = state_service.get_session()
    if session.emergency_stop or session.daily_risk_lock:
        return session_result.as_dict()   # safety states are never overridden

    desired = {
        MarketSession.OPEN: AgentState.SCANNING,
        MarketSession.PRE_MARKET: AgentState.PRE_MARKET,
        MarketSession.AFTER_HOURS: AgentState.MARKET_CLOSED,
        MarketSession.CLOSED: AgentState.MARKET_CLOSED,
    }.get(session_result.session)

    if desired is not None and desired is not session.agent_state:
        try:
            state_service.transition(
                desired, f"market session {session_result.session.value}"
            )
        except Exception as e:
            log_event("agent_state_transition",
                      session_date=session.session_date,
                      source=str(session.agent_state), target=str(desired),
                      reason=f"skipped: {e}")
    return session_result.as_dict()


def handle_status(event) -> Dict:
    state_service = _state_service()
    raw_provider, _cached = _provider()

    market = _sync_market_session(state_service, raw_provider)
    session = state_service.get_session()
    d = session.as_dict()
    detail = d.get("regime_detail") or {}

    return _response(200, {
        "session_date": d["session_date"],
        "agent_state": d["agent_state"],
        "market_session": d["market_status"],
        "market": market,
        "trading_enabled": d["trading_enabled"],
        "emergency_stop": d["emergency_stop"],
        "daily_risk_lock": d["daily_risk_lock"],
        "execution_available": False,
        "execution_note": (
            "this build has no broker adapter, paper or live; no order can "
            "be placed"
        ),
        "regime": {
            "regime": d["market_regime"],
            "score": d["market_regime_score"],
            "confidence": d["market_regime_confidence"],
            "confidence_meaning": (
                "agreement among inputs and data quality; NOT a probability "
                "of a market direction"
            ),
            "risk_posture": d["risk_posture"],
            "trend": detail.get("trend"),
            "volatility": detail.get("volatility"),
            "breadth_proxy": detail.get("breadth_proxy"),
            "updated_at": d["regime_updated_at"],
        },
        "data_quality": detail.get("data_quality", {}),
        "capital": {
            "daily_capital_limit": d["daily_capital_limit"],
            "capital_deployed": d["capital_deployed"],
        },
        "pnl": {
            "realized": d["realized_pnl"],
            "unrealized": d["unrealized_pnl"],
        },
        "positions": {
            "open": d["open_positions"],
            "orders_pending": d["orders_pending"],
        },
        "last_scan_at": d["last_scan_at"],
        "updated_at": d["updated_at"],
        "revision": d["revision"],
    })


def handle_market_regime(event) -> Dict:
    """Return the stored regime. Read-only: it spends no provider quota."""
    session = _state_service().get_session()
    d = session.as_dict()
    detail = d.get("regime_detail") or {}

    if not detail:
        return _response(200, {
            "regime": d["market_regime"],
            "evaluated": False,
            "message": (
                "no regime evaluation recorded for this session yet; "
                "POST /agent/regime/evaluate (admin) to produce one"
            ),
        })

    return _response(200, {
        "evaluated": True,
        "session_date": d["session_date"],
        **detail,
        "regime_history": d.get("regime_history", [])[-10:],
    })


def handle_evaluate(event) -> Dict:
    """Force a fresh evaluation. Admin-gated; spends provider quota."""
    if os.environ.get("AGENT_ADMIN_ENABLED", "").strip().lower() not in (
            "1", "true", "yes", "on"):
        return _response(403, {
            "error": "disabled",
            "message": (
                "this endpoint spends market-data quota and the API has no "
                "authentication; set AGENT_ADMIN_ENABLED only behind auth"
            ),
        })

    state_service = _state_service()
    raw_provider, cached = _provider()
    _sync_market_session(state_service, raw_provider)

    service = MarketRegimeService(cached, _config())
    result, stats = service.evaluate()
    state_service.record_regime(
        regime=result.regime, score=result.raw_score,
        confidence=result.confidence, risk_posture=result.risk_posture,
        detail=result.as_dict(),
    )
    return _response(200, {
        "evaluated": True,
        "fetch_stats": stats.as_dict(),
        **result.as_dict(),
    })


def _query(event) -> Dict[str, str]:
    return dict(event.get("queryStringParameters") or {})


def _int_param(params: Dict, name: str, default: int, cap: int) -> int:
    try:
        return max(1, min(cap, int(params.get(name, default))))
    except (TypeError, ValueError):
        return default


def _float_param(params: Dict, name: str) -> Optional[float]:
    try:
        return float(params[name])
    except (KeyError, TypeError, ValueError):
        return None


def _run_summary(run) -> Dict:
    d = run.as_dict(include_candidates=False)
    return {
        **d,
        "candidates": [c.as_dict() for c in run.candidates],
    }


def handle_scanner_latest(event) -> Dict:
    """Most recent scan for the current session. Read-only: no quota."""
    params = _query(event)
    limit = _int_param(params, "limit", 10, 100)
    store = _scanner_store()
    try:
        run = store.latest_run(params.get("session_date"))
    except Exception as e:
        return _response(503, {"error": "scanner store unavailable",
                               "detail": type(e).__name__})
    if run is None:
        return _response(200, {
            "found": False,
            "message": ("no scanner run recorded yet for this session; the "
                        "scheduled scanner runs during market hours"),
        })
    payload = _run_summary(run)
    payload["candidates"] = payload["candidates"][:limit]
    payload["found"] = True
    return _response(200, payload)


def handle_scanner_run(event) -> Dict:
    run_id = (event.get("pathParameters") or {}).get("id") or \
        _query(event).get("run_id")
    if not run_id:
        return _response(400, {"error": "run id required"})
    try:
        run = _scanner_store().get_run(run_id)
    except Exception as e:
        return _response(503, {"error": "scanner store unavailable",
                               "detail": type(e).__name__})
    if run is None:
        return _response(404, {"error": "not found", "scanner_run_id": run_id})
    return _response(200, _run_summary(run))


def handle_candidates(event) -> Dict:
    """Ranked candidates from the latest scan, optionally filtered.

    These are research priorities. They are not recommendations, and the
    response says so rather than leaving it to the caller to assume.
    """
    params = _query(event)
    limit = _int_param(params, "limit", 25, 100)
    min_score = _float_param(params, "min_score")
    symbol = (params.get("symbol") or "").upper() or None

    store = _scanner_store()
    try:
        run = (store.get_run(params["run_id"]) if params.get("run_id")
               else store.latest_run())
    except Exception as e:
        return _response(503, {"error": "scanner store unavailable",
                               "detail": type(e).__name__})
    if run is None:
        return _response(200, {"found": False, "candidates": []})

    candidates = [c.as_dict() for c in run.candidates]
    if symbol:
        candidates = [c for c in candidates if c["symbol"] == symbol]
    if min_score is not None:
        candidates = [c for c in candidates
                      if c["scanner_score"] >= min_score]

    return _response(200, {
        "found": True,
        "scanner_run_id": run.scanner_run_id,
        "session_date": run.session_date,
        "market_regime": run.market_regime,
        "regime_confidence": round(run.regime_confidence, 4),
        "risk_posture": run.risk_posture,
        "execution_available": False,
        "score_meaning": (
            "research priority only: which symbols merit a closer look. "
            "NOT an expected return, a forecast, or a recommendation."
        ),
        "count": len(candidates[:limit]),
        "candidates": candidates[:limit],
    })


def handle_candidate_symbol(event) -> Dict:
    symbol = ((event.get("pathParameters") or {}).get("symbol")
              or _query(event).get("symbol") or "").upper()
    if not symbol:
        return _response(400, {"error": "symbol required"})
    limit = _int_param(_query(event), "limit", 20, 100)
    try:
        history = _scanner_store().candidates_for_symbol(symbol, limit=limit)
    except Exception as e:
        return _response(503, {"error": "scanner store unavailable",
                               "detail": type(e).__name__})
    return _response(200, {
        "symbol": symbol,
        "count": len(history),
        "execution_available": False,
        "history": history,
    })


def _symbol_from(event) -> Tuple[Optional[str], Optional[Dict]]:
    """Validated ticker, or the refusal to return.

    Validation runs before any provider call so a malformed symbol
    cannot spend quota.
    """
    params = _query(event)
    symbol = (params.get("symbol")
              or (event.get("pathParameters") or {}).get("symbol")
              or "").strip().upper()
    if not symbol:
        return None, _response(400, {"error": "symbol required"})
    if not symbol.isalpha() or len(symbol) > 6:
        return None, _response(400, {"error": "invalid symbol",
                                     "symbol": symbol})
    return symbol, None


def _current_regime() -> Tuple[str, float]:
    """The regime to judge a symbol against. UNKNOWN when unavailable -
    never a cheerful default, because "we do not know" and "calm" lead
    to different amounts of caution."""
    try:
        state = _state_service().get_session()
        return (state.market_regime or "UNKNOWN",
                state.market_regime_confidence or 0.0)
    except Exception as e:
        log_event("provider_error", operation="current_regime",
                  error=str(e)[:200])
        return "UNKNOWN", 0.0


def handle_analysis(event) -> Dict:
    """Per-symbol analysis, shaped for the review UI.

    Runs the canonical signal engine and renders it through
    `agent.analysis`, which holds no thresholds of its own. The frontend
    never parses prose to recover a number, and never recomputes one.
    """
    symbol, refusal = _symbol_from(event)
    if refusal:
        return refusal

    _raw, cached = _provider()
    try:
        quote = cached.get_quote(symbol)
    except SymbolNotFound:
        return _response(404, {"error": "symbol not found", "symbol": symbol})
    except ProviderError as e:
        return _response(503, {"error": "market data unavailable",
                               "detail": type(e).__name__})

    regime, regime_confidence = _current_regime()
    result = _signal_service().evaluate_symbol(symbol, regime,
                                               regime_confidence)

    if not result.analysis_available:
        return _response(200, {
            "symbol": symbol,
            "price": getattr(quote, "price", None),
            "analysis_available": False,
            "message": (result.warnings[0] if result.warnings
                        else "analysis unavailable"),
            "warnings": list(result.warnings),
            "execution_available": False,
        })

    return _response(200, build_analysis(result, quote=quote,
                                         market_context=_market_context()))


def handle_signals(event) -> Dict:
    """The canonical signal result for one symbol, unrendered.

    This is the engine's own output rather than a UI shape: group
    structure, per-indicator readings with their raw values and the
    thresholds that were applied, the regime adjustment, and the
    deterministic reasons.
    """
    symbol, refusal = _symbol_from(event)
    if refusal:
        return refusal

    regime, regime_confidence = _current_regime()
    try:
        result = _signal_service().evaluate_symbol(symbol, regime,
                                                   regime_confidence)
    except SymbolNotFound:
        return _response(404, {"error": "symbol not found", "symbol": symbol})

    payload = result.as_dict()
    payload["market_context"] = _market_context()
    return _response(200, payload)


def handle_signals_latest(event) -> Dict:
    """The most recent STORED signal run.

    Reads persistence only - it never triggers an evaluation, so a
    public GET cannot be used to spend provider quota.
    """
    params = _query(event)
    symbol = (params.get("symbol") or "").strip().upper()
    store = _signal_store()
    if store is None:
        return _response(503, {"error": "signal store unavailable"})

    if symbol:
        row = store.latest_for_symbol(symbol)
        if not row:
            return _response(404, {"found": False, "symbol": symbol,
                                   "message": "no stored signal for this "
                                              "symbol"})
        return _response(200, {"found": True, "result": row})

    session_date = (params.get("session_date")
                    or _session_date())
    run = store.latest_run(session_date)
    if not run:
        return _response(404, {"found": False, "session_date": session_date,
                               "message": "no signal run recorded for this "
                                          "session"})
    return _response(200, {"found": True, "run": run})


def handle_scanner_signals(event) -> Dict:
    """Signals for the candidates of one scanner run, from storage."""
    params = _query(event)
    scanner_run_id = (params.get("scanner_run_id")
                      or params.get("run_id") or "").strip()
    store = _signal_store()
    if store is None:
        return _response(503, {"error": "signal store unavailable"})

    session_date = (params.get("session_date")
                    or _session_date())
    run = store.latest_run(session_date)
    if not run:
        return _response(404, {"found": False,
                               "message": "no signal run recorded"})
    if scanner_run_id and run.get("scanner_run_id") != scanner_run_id:
        return _response(404, {
            "found": False, "scanner_run_id": scanner_run_id,
            "message": "no signal run stored for that scanner run",
        })
    return _response(200, {"found": True, "run": run})


def _market_context() -> Dict:
    """Regime context for a single-symbol view.

    A stock does not move in isolation, and a reading presented without
    the market it was taken in invites being read as more decisive than
    it is.
    """
    try:
        state = _state_service().get_session()
        detail = state.as_dict().get("regime_detail") or {}
        return {
            "regime": state.market_regime,
            "regime_agreement": state.market_regime_confidence,
            "risk_posture": state.risk_posture,
            "trend": detail.get("trend"),
            "volatility": detail.get("volatility"),
            "breadth_proxy": detail.get("breadth_proxy"),
            "market_session": str(state.market_status),
            "updated_at": state.regime_updated_at,
            "indices": {
                sym: {
                    "price": info.get("price"),
                    "change_pct": (feat or {}).get("intraday_change_pct"),
                }
                for sym, info in (detail.get("inputs") or {}).items()
                for feat in [(detail.get("features") or {}).get(sym)]
            },
            "agreement_meaning": (
                "Regime agreement measures how well the index readings "
                "agree and how fresh they are, not a probability."
            ),
        }
    except Exception as e:
        log_event("provider_error", operation="market_context",
                  error=str(e)[:200])
        return {"available": False}


def handle_evidence(event) -> Dict:
    """Collect evidence for one symbol, live.

    Read-only in the sense that matters - it places no orders and writes
    no trading state - but it does spend provider calls, so it is capped
    by EVIDENCE_TOP_N elsewhere and by one symbol here.
    """
    symbol, refusal = _symbol_from(event)
    if refusal:
        return refusal

    try:
        result = _evidence_service().collect(symbol)
    except Exception as e:
        log_event("evidence_collection_failed", symbol=symbol,
                  error=type(e).__name__)
        return _response(503, {"error": "evidence collection failed",
                               "detail": type(e).__name__})

    return _response(200, result.as_dict(include_items=True))


def handle_catalysts(event) -> Dict:
    """The catalyst verdict for a symbol, without the full item list."""
    symbol, refusal = _symbol_from(event)
    if refusal:
        return refusal

    try:
        result = _evidence_service().collect(symbol)
    except Exception as e:
        return _response(503, {"error": "evidence collection failed",
                               "detail": type(e).__name__})

    payload = result.as_dict(include_items=False)
    # Enough of the supporting items to show provenance, without
    # reproducing the whole set.
    payload["top_evidence"] = [
        i.as_dict() for i in result.items if i.is_canonical][:5]
    return _response(200, payload)


def handle_evidence_latest(event) -> Dict:
    """Most recent STORED evidence. Never triggers a collection, so a
    public GET cannot be used to spend provider quota."""
    params = _query(event)
    symbol = (params.get("symbol") or "").strip().upper()
    store = _evidence_store()
    if store is None:
        return _response(503, {"error": "evidence store unavailable"})
    if not symbol:
        return _response(400, {"error": "symbol required"})

    catalyst = store.latest_catalyst(symbol)
    if not catalyst:
        return _response(404, {"found": False, "symbol": symbol,
                               "message": "no stored evidence for this symbol"})
    return _response(200, {"found": True, "catalyst": catalyst})


def handle_scanner_evidence(event) -> Dict:
    """Stored evidence for a scanner run's candidates."""
    params = _query(event)
    store = _evidence_store()
    if store is None:
        return _response(503, {"error": "evidence store unavailable"})

    session_date = params.get("session_date") or _session_date()
    run = store.latest_run(session_date) if hasattr(store, "latest_run") else None
    if not run:
        return _response(404, {"found": False, "session_date": session_date,
                               "message": "no evidence run recorded for this "
                                          "session"})
    scanner_run_id = (params.get("scanner_run_id") or "").strip()
    if scanner_run_id and run.get("scanner_run_id") != scanner_run_id:
        return _response(404, {
            "found": False, "scanner_run_id": scanner_run_id,
            "message": "no evidence run stored for that scanner run"})
    return _response(200, {"found": True, "run": run})



# --- Milestone 14: trading pipeline read endpoints -----------------------

def _switch(name: str, default: str = "false") -> bool:
    """Read a kill switch from the environment.

    Both default to FALSE. A missing variable is not permission.
    """
    return os.environ.get(name, default).strip().lower() in (
        "1", "true", "yes", "on")


def handle_switches(event) -> Dict:
    """What the agent is currently allowed to do.

    Exposed so the dashboard can state it plainly rather than implying
    readiness from the presence of data.
    """
    trading = _switch("AGENT_TRADING_ENABLED")
    execution = _switch("AGENT_EXECUTION_AVAILABLE")
    halt = None
    halt_detail = "not checked"
    try:
        cfg = _config()
        state = DynamoDBHaltStore(
            table_name=_autonomy_tables()[1],
            region=cfg.storage.region).get()
        halt = bool(getattr(state, "halted", True))
        halt_detail = getattr(state, "reason", "") or ""
    except Exception as exc:                              # noqa: BLE001
        # Fail closed in the REPORT as well as in the engine: an
        # unreadable halt state is shown as halted, not as unknown.
        halt = True
        halt_detail = f"halt state unreadable, assuming halted: {exc}"

    return _response(200, {
        "trading_enabled": trading,
        "execution_available": execution,
        "global_halt": halt,
        "global_halt_detail": halt_detail,
        "can_place_orders": False,
        "can_place_orders_detail": (
            "This API has no broker adapter. No endpoint in this Lambda "
            "can submit an order, whatever these switches say."),
        "is_paper_only": True,
    })


def handle_risk_limits(event) -> Dict:
    """The limits actually in force, not a description of them."""
    limits = RiskLimits()
    return _response(200, {
        "limits": limits.as_dict() if hasattr(limits, "as_dict")
        else {k: v for k, v in vars(limits).items()},
        "version": limits.version,
        "note": ("These are the values the governor uses. Capital limits "
                 "are a CEILING, not a target - the agent is not expected "
                 "to deploy them."),
    })


def _hypothesis_for(symbol: str):
    """Build a hypothesis from live data, returning the pieces too."""
    # Reuse the module's own factories. They read credentials from
    # Secrets Manager and share one cached provider, so a page view does
    # not pay for the same quote twice.
    signal = _signal_service().evaluate_symbol(symbol)
    signal_dict = signal.as_dict()

    # Read the STORED regime rather than evaluating a fresh one, as
    # handle_market_regime does. Evaluating spends provider quota on
    # every page view, and the orchestrator would use the stored value
    # anyway - so re-evaluating here would also mean the dashboard shows
    # a regime the agent never actually decided on.
    session = _state_service().get_session().as_dict()
    regime_dict = {
        "regime": session.get("market_regime") or "UNKNOWN",
        "regime_confidence": session.get("market_regime_confidence") or 0.0,
        "risk_posture": session.get("risk_posture") or "NO_NEW_TRADES",
        "market_session": session.get("market_session") or "UNKNOWN",
        "regime_updated_at": session.get("regime_updated_at"),
    }

    catalyst = None
    try:
        catalyst = _evidence_service().collect(symbol).as_dict()
    except Exception:                                     # noqa: BLE001
        # Evidence unavailable is distinct from evidence absent, and the
        # hypothesis engine already distinguishes them. Leaving this as
        # None means "not collected", which is the honest value.
        catalyst = None

    hypothesis = generate_hypothesis(symbol, signal_dict, catalyst,
                                     regime_dict)
    return hypothesis, signal_dict, catalyst, regime_dict


def handle_hypothesis(event) -> Dict:
    symbol = _query(event).get("symbol")
    if not symbol:
        return _response(400, {"error": "symbol is required"})
    try:
        hypothesis, _signal, _catalyst, _regime = _hypothesis_for(
            symbol.upper())
    except (ProviderError, SymbolNotFound) as exc:
        return _response(502, {"error": "provider unavailable",
                               "detail": str(exc)[:200]})
    return _response(200, hypothesis.as_dict())


def handle_risk_preview(event) -> Dict:
    """What the governor WOULD decide. Places nothing."""
    symbol = _query(event).get("symbol")
    if not symbol:
        return _response(400, {"error": "symbol is required"})
    symbol = symbol.upper()
    try:
        hypothesis, signal, _catalyst, _regime = _hypothesis_for(symbol)
    except (ProviderError, SymbolNotFound) as exc:
        return _response(502, {"error": "provider unavailable",
                               "detail": str(exc)[:200]})

    raw_provider, _cached = _provider()
    session = MarketSessionService(raw_provider).current()
    quality = signal.get("data_quality") or {}
    context = RiskContext(
        session_date=today_market_date(),
        market_session=str(getattr(session, "session", "UNKNOWN")),
        minutes_to_close=getattr(session, "minutes_to_close", None),
        # Both switches are read from the environment and both default
        # to false, so a preview on an unconfigured deployment correctly
        # shows a refusal rather than an approval.
        trading_enabled=_switch("AGENT_TRADING_ENABLED"),
        execution_available=_switch("AGENT_EXECUTION_AVAILABLE"),
        price=signal.get("price"),
        quote_age_seconds=quality.get("age_seconds"),
    )
    decision = evaluate_risk(hypothesis, context)
    return _response(200, {
        "decision": decision.as_dict(),
        "hypothesis_id": hypothesis.hypothesis_id,
        "note": ("A preview only. This Lambda has no broker adapter and "
                 "cannot submit an order."),
    })


def handle_positions(event) -> Dict:
    """Open positions, read from the store the cycle writes.

    It used to return a fixed empty state explaining that nothing was
    scheduled. That was true when written and became a lie once the
    cycle began running: it reported zero while two positions were open.
    An unreadable store is still reported as unreadable, which is not
    the same as reporting none.
    """
    session_date = _query(event).get("date") or today_market_date()
    store, error = _safe(lambda: DynamoDBPositionStore(
        table_name=os.environ.get("AGENT_POSITIONS_TABLE",
                                  "stock-agent-dev-positions")
    ).load_open(session_date))
    if error:
        return _response(200, {
            "positions": [], "open_count": None, "total_open_risk": None,
            "source": "unreadable", "detail": error,
            "note": ("The position store could not be read. This is not "
                     "a report of zero positions."),
        })
    rows = [p.as_dict() if hasattr(p, "as_dict") else dict(p)
            for p in (store or [])]
    risks = [r.get("open_risk") for r in rows
             if isinstance(r.get("open_risk"), (int, float))]
    return _response(200, {
        "positions": rows,
        "open_count": len(rows),
        "total_open_risk": round(sum(risks), 2) if risks else None,
        "risk_known_for": f"{len(risks)} of {len(rows)}",
        "source": "position store",
        "session_date": session_date,
        "note": ("Stops are polled by the cycle, not resting at a broker, "
                 "which is why positions are flattened before the close."),
    })


def handle_journal(event) -> Dict:
    session_date = _query(event).get("date") or today_market_date()
    try:
        trades = _journal().list_trades(session_date=session_date)
    except Exception as exc:                              # noqa: BLE001
        return _response(200, {
            "trades": [], "count": 0, "session_date": session_date,
            "source": "unavailable",
            "detail": f"journal unreadable: {str(exc)[:200]}",
        })
    return _response(200, {
        "trades": [t.as_dict() for t in trades],
        "count": len(trades),
        "session_date": session_date,
        "source": "dynamodb",
    })


def handle_performance(event) -> Dict:
    """Metrics WITH their sample adequacy.

    The verdict field is the one to read. It reports
    NO_EDGE_DEMONSTRATED for any sample too small to support a claim,
    whichever way the numbers happen to point.
    """
    session_date = _query(event).get("date") or today_market_date()
    try:
        trades = _journal().list_trades(session_date=session_date)
    except Exception as exc:                              # noqa: BLE001
        return _response(200, {
            "verdict": "UNAVAILABLE",
            "detail": f"journal unreadable: {str(exc)[:200]}",
            "trades_counted": 0,
        })
    return _response(200, describe_perf(trades))


def handle_pipeline(event) -> Dict:
    """Every stage for one symbol, kept separate.

    This is the endpoint the dashboard uses. It deliberately returns the
    stages side by side rather than a composite score: a single number
    would hide which stage drove the outcome, and the whole point of the
    decomposition is to be able to distrust one stage without
    distrusting all of them.
    """
    symbol = _query(event).get("symbol")
    if not symbol:
        return _response(400, {"error": "symbol is required"})
    symbol = symbol.upper()

    stages: Dict[str, Dict] = {}
    try:
        hypothesis, signal, catalyst, regime = _hypothesis_for(symbol)
    except (ProviderError, SymbolNotFound) as exc:
        return _response(502, {"error": "provider unavailable",
                               "detail": str(exc)[:200]})

    stages["quantitative"] = {
        "direction": signal.get("direction"),
        "signal_agreement": signal.get("signal_agreement"),
        "signal_magnitude": signal.get("signal_magnitude"),
        "regime_adjusted_magnitude": signal.get(
            "regime_adjusted_magnitude"),
        "strength_band": signal.get("strength_band"),
        "buy_groups": signal.get("buy_groups"),
        "sell_groups": signal.get("sell_groups"),
        "freshness": (signal.get("data_quality") or {}).get("freshness"),
    }
    stages["evidence"] = ({
        "collected": False,
        "detail": ("evidence was not collected; this is distinct from "
                   "having looked and found nothing"),
    } if not catalyst else {
        "collected": True,
        "has_active_catalyst": catalyst.get("has_active_catalyst"),
        "direction": catalyst.get("direction"),
        "materiality": (catalyst.get("primary_catalyst") or {}).get(
            "materiality"),
        "novelty": (catalyst.get("primary_catalyst") or {}).get("novelty"),
        "independent_sources": catalyst.get("independent_source_count"),
        "primary_sources": catalyst.get("primary_source_count"),
        "conflicting": catalyst.get("conflicting_evidence"),
    })
    stages["regime"] = {
        "regime": regime.get("regime"),
        "confidence": regime.get("regime_confidence"),
        "risk_posture": regime.get("risk_posture"),
    }
    stages["hypothesis"] = hypothesis.as_dict()

    raw_provider, _cached = _provider()
    session = MarketSessionService(raw_provider).current()
    quality = signal.get("data_quality") or {}
    decision = evaluate_risk(hypothesis, RiskContext(
        session_date=today_market_date(),
        market_session=str(getattr(session, "session", "UNKNOWN")),
        minutes_to_close=getattr(session, "minutes_to_close", None),
        trading_enabled=_switch("AGENT_TRADING_ENABLED"),
        execution_available=_switch("AGENT_EXECUTION_AVAILABLE"),
        price=signal.get("price"),
        quote_age_seconds=quality.get("age_seconds")))
    stages["risk"] = decision.as_dict()

    return _response(200, {
        "symbol": symbol,
        "stages": stages,
        "composite_score": None,
        "composite_score_detail": (
            "Deliberately absent. Each stage is reported separately so it "
            "is visible WHICH stage drove the outcome; a single blended "
            "number would hide that and make the pipeline impossible to "
            "debug or to distrust selectively."),
        "can_place_orders": False,
    })


def handle_readiness(event) -> Dict:
    """May this system place a real-money order?

    The answer is derived from gates that each default to UNKNOWN, and
    UNKNOWN counts as unmet - so this endpoint cannot return a
    permissive answer because a data source was unavailable.

    The gates themselves are unchanged. What changed is what feeds them:
    session evidence now comes from the accumulated, per-cohort session
    tallies instead of hardcoded unknowns. Evidence from a DIFFERENT
    behavioural cohort is never pooled in.
    """
    cfg, journal_table = _autonomy_tables()
    tallies, _ = _safe(lambda: DynamoDBSessionStore(
        table_name=journal_table).list(), [])
    snapshot, _ = _safe(lambda: DynamoDBSnapshotStore(
        table_name=_autonomy_tables()[1]).get())
    cohort = _current_cohort(snapshot, tallies)
    evidence = (aggregate_evidence(tallies, cohort) if cohort else None)

    # Trades from the CURRENT cohort's sessions only.
    trades = []
    if cohort:
        for tally in tallies:
            if tally.cohort != cohort:
                continue
            got, _ = _safe(lambda d=tally.session_date: _journal()
                           .list_trades(session_date=d), [])
            trades.extend(got)

    # Described rather than assessed, because this Lambda imports no
    # broker at all and must keep it that way - a test asserts that no
    # module capable of reaching a broker is importable here. The absence
    # IS the finding: there is no adapter in this deployment, so the gate
    # is unmet by construction.
    adapter = {
        "adapter_name": "none (this API imports no broker)",
        "is_paper": True,
        "ready_for_real_money": False,
        "unmet_count": None,
    }

    report = readiness.assess(
        performance=describe_perf(trades),
        calibration=calibrate(trades),
        adapter_assessment=adapter,
        switches={"kill_switch_cancels_working_orders": False},
        # The evidence CLASS is forwarded, not just the counts: a
        # performance gate must be able to see that the record behind it
        # was gathered on a delayed feed, or spanned a redeploy. Omitting
        # these keys makes the gates fail closed rather than silently
        # treating the record as real-time.
        pilot=({"sessions_completed": evidence["sessions_completed"],
                "live_data_path_exercised":
                    evidence["live_data_path_exercised"],
                "reconciliation_clean_sessions":
                    evidence["reconciliation_clean_sessions"],
                "evidence_class": evidence["evidence_class"],
                "data_quality": evidence["data_quality"],
                "counts_toward_strategy_gates":
                    evidence["counts_toward_strategy_gates"],
                "evidence_class_reasons":
                    evidence["evidence_class_reasons"]}
               if evidence else
               {"sessions_completed": None,
                "live_data_path_exercised": None,
                "reconciliation_clean_sessions": None}),
        authorisation={"explicit_user_authorisation": False,
                       "capital_at_risk_agreed": False},
        assessed_at=today_market_date())
    body = report.as_dict()
    body["evidence"] = evidence
    body["current_cohort"] = cohort
    return _response(200, body)


# --- Milestone 19A: autonomous paper operation -----------------------------

def _autonomy_tables():
    cfg = _config()
    journal = os.environ.get("AGENT_JOURNAL_TABLE", "stock-agent-dev-journal")
    return cfg, journal


def _safe(fn, default=None):
    """Run a read; a failure becomes `default` plus a marker, never a 500.

    A dashboard that errors when one table is cold is indistinguishable
    from a broken agent, so each panel degrades on its own.
    """
    try:
        return fn(), None
    except Exception as exc:                              # noqa: BLE001
        return default, f"{type(exc).__name__}: {str(exc)[:120]}"


def _current_cohort(snapshot, tallies):
    """The behavioural cohort in force: the last cycle's, else the most
    recent session's. None when neither exists - and then NO sessions
    count toward the evidence, rather than pooling unknown ones."""
    if snapshot and snapshot.get("cohort"):
        return snapshot["cohort"]
    for t in reversed(tallies):
        if t.cohort:
            return t.cohort
    return None


def _autonomy_context(session_date):
    """Everything the dashboard and the chat answer from. Every field
    comes from a STORED record; nothing is recomputed here."""
    cfg, journal_table = _autonomy_tables()
    ctx, errors = {"session_date": session_date}, {}

    snapshot, e = _safe(lambda: DynamoDBSnapshotStore(
        table_name=_autonomy_tables()[1]).get())
    ctx["last_cycle"] = snapshot
    errors["last_cycle"] = e

    health, e = _safe(lambda: DynamoDBHealthStore(
        table_name=_autonomy_tables()[1]).snapshot().as_dict())
    if e:
        # Fail closed in the REPORT: an unreadable health record is not
        # a clean one.
        health = {"state": "HALTED", "entries_permitted": False,
                  "exits_permitted": True, "active": [],
                  "blocking_reasons": [f"health unreadable: {e}"]}
    ctx["health"] = health
    errors["health"] = e

    sessions = DynamoDBSessionStore(table_name=journal_table)
    tally, e = _safe(lambda: sessions.get(session_date))
    ctx["tally"] = tally.as_dict() if tally else None
    errors["tally"] = e
    report, e = _safe(lambda: sessions.get_report(session_date))
    ctx["report"] = report
    errors["report"] = e

    rows, e = _safe(lambda: DynamoDBDecisionLog(
        table_name=journal_table).for_session(session_date), [])
    ctx["decisions"] = rows
    errors["decisions"] = e
    trades, e = _safe(lambda: _journal().list_trades(
        session_date=session_date), [])
    ctx["trades"] = [t.as_dict() for t in trades]
    errors["trades"] = e

    positions, e = _safe(lambda: DynamoDBPositionStore(
        table_name=os.environ.get("AGENT_POSITIONS_TABLE",
                                  "stock-agent-dev-positions"))
        .load_open(session_date), [])
    ctx["positions"] = [p.as_dict() for p in positions]
    errors["positions"] = e

    alerts, e = _safe(lambda: DynamoDBAlertSink(
        table_name=_autonomy_tables()[1]).recent(
            session_date=session_date), [])
    ctx["alerts"] = [a.as_dict() for a in alerts]
    errors["alerts"] = e

    state, e = _safe(lambda: _state_service().get_session(
        session_date).as_dict())
    ctx["agent_state"] = (state or {}).get("agent_state")
    ctx["regime"] = ({k: state.get(k) for k in (
        "market_regime", "market_regime_confidence", "risk_posture",
        "regime_updated_at")} if state else None)
    errors["agent_state"] = e

    limits = RiskLimits()
    ctx["limits"] = {k: v for k, v in vars(limits).items()}
    ctx["daily"] = (snapshot or {}).get("daily")
    ctx["mode"] = (snapshot or {}).get("execution_mode") or "UNKNOWN"
    ctx["errors"] = {k: v for k, v in errors.items() if v}
    return ctx


def handle_autonomy(event) -> Dict:
    """What the agent is doing, in one read. Placed beside nothing that
    can place an order: every field is a stored record."""
    session_date = _query(event).get("date") or today_market_date()
    ctx = _autonomy_context(session_date)
    nxt = next_cycle_time()
    return _response(200, {
        "banner": "AUTONOMOUS PAPER MODE",
        "real_money": "DISABLED",
        "execution_mode": ctx["mode"],
        "live_trading_enabled": False,
        "can_place_orders": False,
        "overnight_positions": "DISABLED",
        "session_date": session_date,
        "agent_state": ctx["agent_state"],
        "health": ctx["health"],
        "regime": ctx["regime"],
        "last_cycle": ctx["last_cycle"],
        "tally": ctx["tally"],
        "report": ctx["report"],
        "positions": ctx["positions"],
        "daily": ctx["daily"],
        "limits": ctx["limits"],
        "alerts": ctx["alerts"],
        "decisions": ctx["decisions"][-25:],
        "trades": ctx["trades"],
        "next_cycle_at": nxt.isoformat(),
        "read_errors": ctx["errors"],
    })


def handle_decisions(event) -> Dict:
    q = _query(event)
    date = q.get("date") or today_market_date()
    cfg, journal_table = _autonomy_tables()
    rows, e = _safe(lambda: DynamoDBDecisionLog(
        table_name=journal_table).for_session(
            date, symbol=(q.get("symbol") or "").upper() or None), [])
    return _response(200, {"session_date": date, "count": len(rows),
                           "decisions": rows, "read_error": e})


def handle_sessions(event) -> Dict:
    """Every session tally, and the evidence the readiness gate reads."""
    cfg, journal_table = _autonomy_tables()
    tallies, e = _safe(lambda: DynamoDBSessionStore(
        table_name=journal_table).list(), [])
    snapshot, _ = _safe(lambda: DynamoDBSnapshotStore(
        table_name=_autonomy_tables()[1]).get())
    cohort = _current_cohort(snapshot, tallies)
    evidence = aggregate_evidence(tallies, cohort) if cohort else None
    return _response(200, {
        "current_cohort": cohort,
        "sessions": [t.as_dict() for t in tallies][-60:],
        "evidence": evidence,
        "note": ("Sessions from other cohorts are listed and counted but "
                 "never pooled into the evidence: they ran different "
                 "behaviour."),
        "read_error": e,
    })


def handle_session_report(event) -> Dict:
    date = _query(event).get("date") or today_market_date()
    cfg, journal_table = _autonomy_tables()
    report, e = _safe(lambda: DynamoDBSessionStore(
        table_name=journal_table).get_report(date))
    return _response(200, {"session_date": date, "report": report,
                           "written": report is not None, "read_error": e})


def handle_ask(event) -> Dict:
    """Answer a question from STORED records. GET, because the API's
    invariant is exactly one POST.

    Deterministic: no language model is involved, so no model can alter
    a stored decision. Each answer names the records it was built from,
    and an answer for something the record does not contain says so.
    """
    q = _query(event)
    query = (q.get("q") or "").strip()
    if not query:
        return _response(400, {"error": "q is required"})
    session_date = q.get("date") or today_market_date()

    # A company question is answered from company intelligence; anything
    # operational from the session records. Both are deterministic, and
    # neither consults a language model: the operational explainer is
    # tried first because "how did today go" is about the agent, not
    # about a company.
    from agent.company import explain as company_explain
    if classify_question(query) is None \
            and company_explain.classify(query) is not None:
        answer = company_explain.explain(
            query, _company_service(), default_symbol=q.get("symbol"))
        answer["session_date"] = session_date
        return _response(200, answer)

    ctx = _autonomy_context(session_date)
    answer = explain_question(query, ctx)
    answer["session_date"] = session_date
    answer["read_errors"] = ctx["errors"]
    return _response(200, answer)


# ---------------------------------------------------------------------------
# Company intelligence (read-only, off the intraday path)
# ---------------------------------------------------------------------------

_COMPANY = None
COMPANY_SECTIONS = {
    "": "overview", "dividends": "dividends", "splits": "splits",
    "corporate-actions": "corporate_actions", "earnings": "earnings",
    "fundamentals": "fundamentals", "peers": "peers",
    "peer-comparison": "peer_comparison",
    "holding-context": "holding_context",
}


def _company_service() -> CompanyService:
    global _COMPANY
    if _COMPANY is None:
        cfg = _config()
        creds = _load_alpaca_credentials(cfg.storage.alpaca_secret_id,
                                         cfg.storage.region)
        inner, cached = _provider()
        sec = SECProvider(user_agent=SEC_USER_AGENT,
                          cache=getattr(cached, "cache", None)
                          or getattr(cached, "backend", None))

        def submissions(symbol):
            cik, _name = sec.cik_for(symbol)
            sub = dict(sec._submissions(cik))
            sub["cik"] = cik
            return sub

        def price(symbol):
            q = cached.get_quote(symbol)
            prov = getattr(q, "provenance", None)
            return q.price, (getattr(prov, "as_of", None)
                             or q.latest_trading_day or "latest quote")

        _COMPANY = CompanyService(
            store=DynamoDBCompanyStore(),
            actions=AlpacaCorporateActions(inner),
            facts=SECCompanyFacts(SEC_USER_AGENT, sec.cik_for),
            submissions=submissions, price=price, cik_for=sec.cik_for)
    return _COMPANY


def handle_company(method: str, path: str, event) -> Dict:
    """GET /agent/company/{symbol}[/section]. Read-only: nothing here can
    place, change or cancel an order, and none of it feeds the cycle."""
    parts = [p for p in path.split("/") if p][2:]       # after agent/company
    if not parts:
        return _response(400, {"error": "symbol required"})
    symbol = parts[0].upper()
    section = parts[1] if len(parts) > 1 else ""
    if not symbol.isalpha() or len(symbol) > 6 or len(parts) > 2:
        return _response(400, {"error": "invalid symbol"})
    fn = COMPANY_SECTIONS.get(section)
    if fn is None:
        return _response(404, {"error": "unknown company section",
                               "sections": sorted(s or "(overview)"
                                                  for s in COMPANY_SECTIONS)})
    if method != "GET":
        return _response(405, {"error": "company endpoints are read-only"})
    # `?refresh=1` re-reads the provider for the sections that cache
    # provider history, so a corrected fetch window or a newly announced
    # action does not wait out a 24-hour TTL. Read-only either way.
    want_refresh = str((_query(event) or {}).get("refresh", "")).lower() \
        in ("1", "true", "yes")
    try:
        method = getattr(_company_service(), fn)
        import inspect
        if want_refresh and "refresh" in inspect.signature(method).parameters:
            body = method(symbol, refresh=True)
        else:
            body = method(symbol)
    except ProviderError as e:
        return _response(502, {"error": "provider unavailable",
                               "detail": str(e)[:200]})
    body["execution"] = {"mode": "PAPER", "real_money": "DISABLED",
                         "overnight_positions": "DISABLED"}
    return _response(200, body)


def handle_cohorts(event) -> Dict:
    """Every evidence cohort, with the dimensions tracked separately.

    Operational reliability and strategy performance are different
    claims and are reported apart: a cohort can be perfectly reliable
    and prove nothing about the strategy, which is exactly the position
    after a delayed-data session. Profitability is reported with its
    sample adequacy attached and without a verdict until the sample
    supports one.

    Cohorts are never combined. A cohort that spanned a redeploy, or ran
    on anything but the real-time consolidated tape, is labelled rather
    than quietly folded in.
    """
    from agent.autonomy.evidence_class import classify_session
    cfg, journal_table = _autonomy_tables()
    tallies, read_error = _safe(lambda: DynamoDBSessionStore(
        table_name=journal_table).list(), [])

    groups: Dict[str, list] = {}
    for tally in tallies or []:
        groups.setdefault(tally.cohort or "unversioned", []).append(tally)

    out = []
    for cohort, rows in sorted(groups.items()):
        rows.sort(key=lambda t: t.session_date)
        trades = []
        for tally in rows:
            got, _ = _safe(lambda d=tally.session_date: _journal()
                           .list_trades(session_date=d), [])
            trades.extend(got)
        classes = [classify_session(t) for t in rows]
        feeds: Dict[str, int] = {}
        for tally in rows:
            for feed, count in (tally.feed_quality_counts or {}).items():
                feeds[feed] = feeds.get(feed, 0) + count
        rejections: Dict[str, int] = {}
        for tally in rows:
            for code, count in (tally.rejections_by_code or {}).items():
                rejections[code] = rejections.get(code, 0) + count
        shas = sorted({sha for t in rows for sha in (t.code_shas or [])
                       if sha})
        out.append({
            "cohort": cohort,
            "sessions": len(rows),
            "first_session": rows[0].session_date,
            "last_session": rows[-1].session_date,
            "versions": rows[-1].versions,
            "code_shas": shas or ["not recorded"],
            "single_runtime": (len(shas) == 1 if shas else None),
            # What this cohort may be used to claim.
            "evidence_class": (
                "VOID" if any(c["evidence_class"] == "VOID" for c in classes)
                else "REAL_TIME_STRATEGY_EVIDENCE"
                if classes and all(c["counts_toward_strategy_gates"]
                                   for c in classes)
                else "OPERATIONAL_VALIDATION_ONLY"),
            "feed_quality_counts": feeds or {"not recorded": 0},
            # --- operational reliability -------------------------------
            "operational": {
                "cycles": sum(t.cycles_total for t in rows),
                "cycles_live_market": sum(t.cycles_live_market for t in rows),
                "cycles_completed": sum(t.cycles_completed for t in rows),
                "cycles_aborted": sum(t.cycles_aborted for t in rows),
                "cycles_halted": sum(t.cycles_halted for t in rows),
                "duplicate_order_attempts": sum(t.duplicate_attempts
                                                for t in rows),
                "provider_quote_failures": sum(t.quotes_missing for t in rows),
                "emergency_stops": sum(t.emergency_stops for t in rows),
                "risk_locks": sum(t.risk_locks for t in rows),
            },
            "reconciliation": {
                "checks": sum(t.reconciliation_checks for t in rows),
                "failures": sum(t.reconciliation_failures for t in rows),
                "clean": all(t.reconciliation_failures == 0 for t in rows),
            },
            "eod_flatten": {
                "failures": sum(t.eod_flatten_failures for t in rows),
                "clean": all(t.eod_flatten_failures == 0 for t in rows),
            },
            "data_rejections": {
                "stale_or_unknown_quotes": sum(t.quotes_stale for t in rows),
                "stale_market_data_refusals":
                    rejections.get("STALE_MARKET_DATA", 0),
                "all_refusal_codes": rejections,
            },
            # --- strategy performance, separately ----------------------
            "strategy": {
                "hypotheses": sum(t.hypotheses for t in rows),
                "risk_approvals": sum(t.risk_approvals for t in rows),
                "entries": sum(t.entries for t in rows),
                "trades_closed": len(trades),
                "performance": describe_perf(trades),
                "note": ("Profitability is not meaningful until the sample "
                         "adequacy gates pass, and it cannot support a "
                         "readiness gate unless the cohort is "
                         "REAL_TIME_STRATEGY_EVIDENCE."),
            },
        })

    return _response(200, {
        "cohorts": out,
        "count": len(out),
        "read_error": read_error,
        "rule": ("Cohorts are never combined: different behaviour, feed or "
                 "runtime makes a different experiment, and adding them "
                 "produces a number describing neither."),
    })


def handle_why_no_trade(event) -> Dict:
    """Why the agent did not trade, by binding constraint.

    Inactivity has to be as explainable as activity, or a quiet day
    looks like a broken one and the strategy gets "fixed" when nothing
    was wrong.
    """
    from agent.autonomy.inactivity import explain_inactivity
    session_date = _query(event).get("date") or today_market_date()
    cfg, journal_table = _autonomy_tables()
    rows, decision_error = _safe(lambda: DynamoDBDecisionLog(
        table_name=journal_table).for_session(session_date), [])
    snapshot, _ = _safe(lambda: DynamoDBSnapshotStore(
        table_name=journal_table).get())
    scanned = (snapshot or {}).get("symbols_scanned")
    body = explain_inactivity(rows or [], scanned=scanned)
    body.update({"session_date": session_date,
                 "read_error": decision_error})
    if decision_error:
        body["headline"] = ("The decision log could not be read, so I "
                            "cannot say why. That is not the same as "
                            "there being no reason.")
    return _response(200, body)


ROUTES = {
    ("GET", "/agent/status"): handle_status,
    ("GET", "/agent/market-regime"): handle_market_regime,
    ("POST", "/agent/regime/evaluate"): handle_evaluate,
    ("GET", "/agent/scanner/latest"): handle_scanner_latest,
    ("GET", "/agent/scanner/run"): handle_scanner_run,
    ("GET", "/agent/candidates"): handle_candidates,
    ("GET", "/agent/candidate"): handle_candidate_symbol,
    ("GET", "/agent/analysis"): handle_analysis,
    ("GET", "/agent/signals"): handle_signals,
    ("GET", "/agent/signals/latest"): handle_signals_latest,
    ("GET", "/agent/scanner/signals"): handle_scanner_signals,
    ("GET", "/agent/evidence"): handle_evidence,
    ("GET", "/agent/evidence/latest"): handle_evidence_latest,
    ("GET", "/agent/catalysts"): handle_catalysts,
    ("GET", "/agent/scanner/evidence"): handle_scanner_evidence,
    # Milestone 14
    ("GET", "/agent/switches"): handle_switches,
    ("GET", "/agent/risk/limits"): handle_risk_limits,
    ("GET", "/agent/risk/preview"): handle_risk_preview,
    ("GET", "/agent/hypothesis"): handle_hypothesis,
    ("GET", "/agent/positions"): handle_positions,
    ("GET", "/agent/journal"): handle_journal,
    ("GET", "/agent/performance"): handle_performance,
    ("GET", "/agent/pipeline"): handle_pipeline,
    ("GET", "/agent/readiness"): handle_readiness,
    # Milestone 19A
    ("GET", "/agent/autonomy"): handle_autonomy,
    ("GET", "/agent/decisions"): handle_decisions,
    ("GET", "/agent/sessions"): handle_sessions,
    ("GET", "/agent/cohorts"): handle_cohorts,
    ("GET", "/agent/why-no-trade"): handle_why_no_trade,
    ("GET", "/agent/session-report"): handle_session_report,
    ("GET", "/agent/ask"): handle_ask,
}


def lambda_handler(event, context):
    method, path = _route(event)

    if method == "OPTIONS":
        return {"statusCode": 200, "headers": dict(CORS_HEADERS), "body": ""}

    if path.startswith("/agent/company/"):
        try:
            return handle_company(method, path, event)
        except Exception as e:
            log_event("provider_error", operation=f"{method} {path}",
                      error=str(e)[:300])
            return _response(500, {"error": "internal error",
                                   "detail": type(e).__name__})

    handler = ROUTES.get((method, path))
    if handler is None:
        return _response(404, {
            "error": "not found",
            "path": path,
            "method": method,
            "available": [f"{m} {p}" for m, p in sorted(ROUTES)],
        })

    try:
        return handler(event)
    except Exception as e:
        # Report the failure without leaking internals into a public body.
        log_event("provider_error", operation=f"{method} {path}",
                  error=str(e)[:300])
        return _response(500, {
            "error": "internal error",
            "detail": type(e).__name__,
        })

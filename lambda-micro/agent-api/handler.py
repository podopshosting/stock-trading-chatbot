"""
Agent API - development read endpoints for Milestone 3.

    GET  /agent/status           current agent + market + regime state
    GET  /agent/market-regime    detailed quantitative regime data
    POST /agent/regime/evaluate  force a fresh evaluation (ADMIN ONLY)

Separate from the production chatbot Lambda on purpose: the live
`/chatbot` route keeps working regardless of anything here.

**The POST endpoint is disabled unless AGENT_ADMIN_ENABLED is set.** The
current API has no authentication, and an unauthenticated endpoint that
spends provider quota on demand is an obvious way for a stranger to
exhaust the budget. It stays off until there is auth in front of it.

Nothing in this module can place an order. There is no broker adapter in
this build at all.
"""
from __future__ import annotations

import json
import os
from typing import Dict, Optional, Tuple

from agent.config import AgentConfig
from agent.market import MarketRegimeService, MarketSessionService
from agent.observability import log_event
from agent.providers import AlpacaProvider, CachedProvider, MemoryCache
from agent.scanner import DynamoDBScannerStore
from agent.state import (
    AgentState, AgentStateService, DynamoDBStateStore, MarketSession,
)

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
        inner = AlpacaProvider(
            api_key_id=creds["api_key_id"],
            api_secret_key=creds["api_secret_key"],
        )
        _PROVIDER = (inner, CachedProvider(inner, backend=MemoryCache()))
    return _PROVIDER


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


ROUTES = {
    ("GET", "/agent/status"): handle_status,
    ("GET", "/agent/market-regime"): handle_market_regime,
    ("POST", "/agent/regime/evaluate"): handle_evaluate,
    ("GET", "/agent/scanner/latest"): handle_scanner_latest,
    ("GET", "/agent/scanner/run"): handle_scanner_run,
    ("GET", "/agent/candidates"): handle_candidates,
    ("GET", "/agent/candidate"): handle_candidate_symbol,
}


def lambda_handler(event, context):
    method, path = _route(event)

    if method == "OPTIONS":
        return {"statusCode": 200, "headers": dict(CORS_HEADERS), "body": ""}

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

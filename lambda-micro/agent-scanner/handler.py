"""
Scanner Lambda — one scan iteration, then exit.

Invoked on a schedule. No loop, no long-running process, no browser, and
no order execution: nothing here imports a broker adapter.

Its own function rather than a route on the agent API, so a scan that
takes 30 seconds and spends provider quota cannot slow down or be
triggered by a status poll.
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Dict, Optional

from agent.config import AgentConfig
from agent.market import MarketRegimeService, MarketSessionService
from agent.observability import log_event
from agent.providers import AlpacaProvider, CachedProvider, MemoryCache
from agent.scanner import (
    AlpacaUniverseProvider, DynamoDBScannerStore, MarketScannerService,
    ScanContext,
)
from agent.state import (
    AgentStateService, DynamoDBStateStore, MarketSession, utcnow,
)

REGULAR_OPEN_MINUTES = 9 * 60 + 30      # 09:30 ET

_PROVIDER = None
_CONFIG: Optional[AgentConfig] = None


def _config() -> AgentConfig:
    global _CONFIG
    if _CONFIG is None:
        _CONFIG = AgentConfig.from_env()
    return _CONFIG


def _provider():
    """Cached across warm invocations. Credentials come from Secrets
    Manager and are never logged or placed in the environment."""
    global _PROVIDER
    if _PROVIDER is None:
        cfg = _config()
        import boto3
        client = boto3.client("secretsmanager", region_name=cfg.storage.region)
        creds = json.loads(
            client.get_secret_value(
                SecretId=cfg.storage.alpaca_secret_id)["SecretString"]
        )
        inner = AlpacaProvider(api_key_id=creds["api_key_id"],
                               api_secret_key=creds["api_secret_key"])
        _PROVIDER = (inner, CachedProvider(inner, backend=MemoryCache()))
    return _PROVIDER


def _minutes_elapsed(market_result) -> Optional[float]:
    """Minutes since the regular open, in market-local time.

    Needed to project session volume for the relative-volume denominator.
    Returns None when it cannot be determined, so the projection is
    skipped rather than computed from a guess.
    """
    try:
        from zoneinfo import ZoneInfo
        if not market_result.as_of:
            return None
        service = MarketSessionService(None)
        now = service._parse_provider_time(market_result.as_of)
        day = market_result.trading_day
        open_time = day.regular_open if day and day.regular_open else None
        if open_time is None:
            return None
        minutes_now = now.hour * 60 + now.minute + now.second / 60.0
        minutes_open = open_time.hour * 60 + open_time.minute
        elapsed = minutes_now - minutes_open
        return elapsed if elapsed > 0 else None
    except Exception:
        return None


def run_once(event: Optional[Dict] = None) -> Dict:
    cfg = _config()
    raw, cached = _provider()

    session_service = AgentStateService(
        DynamoDBStateStore(cfg.storage.state_table, cfg.storage.region), cfg
    )
    scanner_store = DynamoDBScannerStore(cfg.storage.scanner_table,
                                         cfg.storage.region)

    market = MarketSessionService(raw).current()
    session_service.set_market_session(
        MarketSession.parse(market.session.value))

    # The regime is scan context, not a gate on running: a scan that
    # records UNKNOWN and warns is more useful than one that silently
    # does nothing.
    regime_label, regime_confidence, risk_posture = "UNKNOWN", 0.0, "NO_NEW_TRADES"
    if market.is_open:
        try:
            result = MarketRegimeService(cached, cfg).evaluate_and_record(
                session_service)
            regime_label = result.regime
            regime_confidence = result.confidence
            risk_posture = result.risk_posture
        except Exception as e:
            log_event("provider_error", operation="regime_evaluate",
                      error=str(e)[:200])

    context = ScanContext(
        market_session=market.session.value,
        regime=regime_label,
        regime_confidence=regime_confidence,
        risk_posture=risk_posture,
        minutes_elapsed=_minutes_elapsed(market),
        is_open=market.is_open,
    )

    service = MarketScannerService(
        cached, AlpacaUniverseProvider(cached), scanner_store, cfg)
    run = service.run_scan(context)

    try:
        session_service.mark_scan(utcnow())
    except Exception as e:
        log_event("provider_error", operation="mark_scan", error=str(e)[:200])

    return run.as_dict(include_candidates=False) | {
        "top_candidates": [
            {"rank": c.rank, "symbol": c.symbol, "score": c.scanner_score}
            for c in run.candidates[:10]
        ],
    }


def lambda_handler(event, context):
    try:
        summary = run_once(event)
        return {"statusCode": 200, "body": json.dumps(summary, default=str)}
    except Exception as e:
        log_event("scanner_failed", stage="handler", error=str(e)[:300])
        # Re-raise so a scheduled invocation is recorded as a failure
        # rather than reporting success with nothing done.
        raise

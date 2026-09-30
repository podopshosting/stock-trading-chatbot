"""
Structured log events.

One JSON object per line so CloudWatch Logs Insights can query fields
directly. Event names are a closed set, because a log you cannot filter on
is a log nobody reads.

Values are never secrets. The emitter drops any field whose name looks
credential-shaped, so a careless call site cannot leak a key into the logs
the way an earlier Lambda in this project leaked Authorization headers.
"""
from __future__ import annotations

import json
import sys
from typing import Any, Dict

EVENTS = frozenset({
    "agent_state_created",
    "agent_state_transition",
    "agent_state_write_retry",
    "market_session_changed",
    "regime_evaluated",
    "regime_changed",
    "regime_unknown",
    "market_data_stale",
    "market_data_missing",
    "provider_error",
    "emergency_stop_triggered",
    "emergency_stop_cleared",
    "daily_risk_lock_set",
    # scanner
    "scanner_started",
    "scanner_completed",
    "scanner_failed",
    "universe_loaded",
    "symbol_rejected",
    "snapshot_missing",
    "snapshot_stale",
    "candidate_created",
    "candidate_ranked",
    "regime_gate_applied",
    "provider_batch_completed",
    # signal engine
    "signal_engine_started",
    "signal_engine_completed",
    "signal_engine_failed",
    "indicator_no_signal",
    "indicator_error",
    "group_disagreement",
    "signal_direction_changed",
    "signal_result_persisted",
    "regime_adjustment_applied",
    # evidence / catalysts
    "evidence_collection_started",
    "evidence_collection_completed",
    "evidence_collection_failed",
    "evidence_provider_failed",
    "evidence_item_normalized",
    "evidence_duplicate_detected",
    "catalyst_created",
    "catalyst_updated",
    "conflicting_evidence_detected",
    "llm_enrichment_failed",
    "evidence_stale",
    "evidence_result_persisted",
})

_REDACT_HINTS = ("key", "secret", "token", "password", "credential", "apikey")


def _safe(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, dict):
        return {k: _safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe(v) for v in value]
    return str(value)


def log_event(event: str, **fields) -> Dict:
    """Emit one structured event. Returns the record, for tests."""
    if event not in EVENTS:
        # Fail loudly in development rather than emitting an unqueryable
        # event name that silently never matches a dashboard filter.
        raise ValueError(
            f"unknown log event {event!r}; add it to observability.EVENTS"
        )

    record: Dict[str, Any] = {"event": event}
    for name, value in fields.items():
        if any(hint in name.lower() for hint in _REDACT_HINTS):
            record[name] = "<redacted>"
            continue
        record[name] = _safe(value)

    try:
        print(json.dumps(record), file=sys.stdout)
    except Exception:
        print(f"{{\"event\": \"{event}\", \"log_error\": true}}", file=sys.stdout)
    return record

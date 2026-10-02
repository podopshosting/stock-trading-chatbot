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
    # hypothesis
    "hypothesis_generated",
    "hypothesis_rejected",
    "hypothesis_run_started",
    "hypothesis_run_completed",
    "hypothesis_persisted",
    # risk
    "risk_decision",
    "risk_halt_observed",
    "risk_lock_engaged",
    "global_halt_engaged",
    "global_halt_cleared",
    # broker / orders
    "broker_selected",
    "broker_unavailable",
    "order_proposed",
    "order_submitted",
    "order_filled",
    "order_cancelled",
    "order_rejected",
    "order_expired",
    "order_duplicate_suppressed",
    # the durable record of an external order, written before it is sent
    "order_intent_recorded",
    "order_intent_unsent",
    "order_recovered_by_client_id",
    "order_observation_not_recorded",
    # following up orders whose outcome is not yet known
    "order_status_changed",
    "order_never_placed",
    "order_vanished_from_venue",
    "order_poll_failed",
    "order_poll_ledger_unreadable",
    "entries_blocked_unknown_exposure",
    "position_opened",
    "position_reduced",
    "position_closed",
    # position management / exits
    "entry_remainder_cancelled",
    "stop_tightened",
    "exit_intent",
    "exit_submitted",
    "exit_blocked_execution_unavailable",
    "position_closed_managed",
    "flatten_all",
    "reconciliation",
    "position_manager_halted",
    # journal
    "trade_recorded",
    "stop_breach_recorded",
    # replay
    "replay_entry_filled",
    "replay_exit_filled",
    "replay_order_unfillable",
    "replay_lookahead_detected",
    "replay_complete",
    # orchestration
    "cycle_complete",
    "cycle_aborted",
    "cycle_skipped_duplicate",
    "cycle_skipped_market_closed",
    "cycle_lock_contended",
    "exit_failed_position_at_risk",
    "journal_failed_after_exit",
    "position_unmanaged_after_fill",
    # persistence
    "broker_state_saved",
    "position_state_saved",
    "state_restored",
    "state_restore_failed",
    # autonomy (milestone 19A)
    "uncertain_order_state",
    "duplicate_order_attempt",
    "shadow_comparison",
    "alert_raised",
    "alert_persist_failed",
    "alert_unreadable",
    "health_condition_raised",
    "health_condition_cleared",
    "autonomy_mode_resolved",
    "live_mode_refused",
    "emergency_stop_engaged",
    "session_report_written",
    "reconciliation_authoritative",
    "unexpected_broker_position",
    "eod_flatten_result",
    "cycle_failure_recorded",
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

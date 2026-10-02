"""
Operator alerts.

Normal paper trades never alert. An alert means something abnormal has
happened that a human should look at, and the value of the channel is
entirely in that restraint: an alert stream that fires on routine
activity trains its reader to ignore it, and the one alert that mattered
is then ignored too.

There is no notification transport configured. Subscribing an email or
phone to a topic is an account action that needs the owner's
confirmation, so it is not done here. Alerts are persisted and surfaced
on the dashboard, and the `AlertSink` interface is where a real
transport (SNS, email, a pager) plugs in without changing any caller.

Alert payloads never carry credentials or tokens; `_safe` strips
anything whose key looks like one.
"""
from __future__ import annotations

import enum
import hashlib
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional

from ..observability import log_event


class AlertKind(str, enum.Enum):
    EMERGENCY_STOP = "EMERGENCY_STOP"
    DAILY_RISK_LOCK = "DAILY_RISK_LOCK"
    RECONCILIATION_MISMATCH = "RECONCILIATION_MISMATCH"
    UNEXPECTED_BROKER_POSITION = "UNEXPECTED_BROKER_POSITION"
    REPEATED_PROVIDER_OUTAGE = "REPEATED_PROVIDER_OUTAGE"
    REPEATED_CYCLE_FAILURE = "REPEATED_CYCLE_FAILURE"
    POSITION_OPEN_NEAR_CLOSE = "POSITION_OPEN_NEAR_CLOSE"
    DUPLICATE_ORDER_ATTEMPT = "DUPLICATE_ORDER_ATTEMPT"
    JOURNAL_PERSISTENCE_FAILURE = "JOURNAL_PERSISTENCE_FAILURE"
    UNCERTAIN_ORDER_STATE = "UNCERTAIN_ORDER_STATE"
    EOD_FLATTEN_FAILURE = "EOD_FLATTEN_FAILURE"
    LIVE_MODE_REQUESTED = "LIVE_MODE_REQUESTED"
    # Deliberately NOT in CRITICAL_KINDS below: the agent degrades to the
    # internal simulator rather than trading against an adapter in an
    # unknown state, so exposure is not wrong - the evidence label would
    # be, which is serious but not an emergency. docs/AGENT-ALERTING.md
    # describes it under HIGH for that reason.
    BROKER_UNAVAILABLE = "BROKER_UNAVAILABLE"

    def __str__(self) -> str:
        return self.value


class Severity(str, enum.Enum):
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"

    def __str__(self) -> str:
        return self.value


# Which kinds are CRITICAL: money or exposure is, or may be, wrong now.
CRITICAL_KINDS = {
    AlertKind.EMERGENCY_STOP, AlertKind.RECONCILIATION_MISMATCH,
    AlertKind.UNEXPECTED_BROKER_POSITION, AlertKind.DUPLICATE_ORDER_ATTEMPT,
    AlertKind.UNCERTAIN_ORDER_STATE, AlertKind.EOD_FLATTEN_FAILURE,
    AlertKind.POSITION_OPEN_NEAR_CLOSE, AlertKind.LIVE_MODE_REQUESTED,
}

_REDACT = ("key", "secret", "token", "password", "credential", "auth")


def _safe(value):
    if isinstance(value, dict):
        return {k: ("<redacted>" if any(h in str(k).lower()
                                        for h in _REDACT) else _safe(v))
                for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe(v) for v in value]
    return value


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Alert:
    kind: AlertKind
    detail: str
    session_date: str
    severity: Severity = Severity.WARNING
    context: Dict = field(default_factory=dict)
    raised_at: str = field(default_factory=utcnow)
    acknowledged: bool = False

    def __post_init__(self):
        if self.kind in CRITICAL_KINDS:
            self.severity = Severity.CRITICAL
        self.context = _safe(self.context)

    @property
    def dedupe_key(self) -> str:
        """One alert per kind per session, not per occurrence.

        An outage lasting all day should produce one alert that says it
        is still happening, not three hundred.
        """
        return hashlib.sha256(
            f"{self.session_date}:{self.kind}".encode()).hexdigest()[:16]

    def as_dict(self) -> Dict:
        return {"kind": str(self.kind), "severity": str(self.severity),
                "detail": self.detail, "session_date": self.session_date,
                "context": self.context, "raised_at": self.raised_at,
                "acknowledged": self.acknowledged,
                "dedupe_key": self.dedupe_key}


class AlertSink:
    def emit(self, alert: Alert) -> bool:
        """Returns True if this was a NEW alert, False if suppressed."""
        raise NotImplementedError

    def recent(self, limit: int = 50) -> List[Alert]:
        raise NotImplementedError


class InMemoryAlertSink(AlertSink):
    def __init__(self):
        self._alerts: Dict[str, Alert] = {}
        self._counts: Dict[str, int] = {}
        self.transport_calls = 0

    def emit(self, alert: Alert) -> bool:
        key = alert.dedupe_key
        self._counts[key] = self._counts.get(key, 0) + 1
        if key in self._alerts:
            self._alerts[key].context["occurrences"] = self._counts[key]
            return False
        alert.context["occurrences"] = 1
        self._alerts[key] = alert
        self.transport_calls += 1
        log_event("alert_raised", kind=str(alert.kind),
                  severity=str(alert.severity),
                  session_date=alert.session_date)
        return True

    def recent(self, limit: int = 50) -> List[Alert]:
        return sorted(self._alerts.values(), key=lambda a: a.raised_at,
                      reverse=True)[:limit]


class DynamoDBAlertSink(AlertSink):
    """PK = ALERTS#<session_date>, SK = <dedupe_key>.

    Dedupe is a conditional put, so two concurrent cycles cannot both
    report the same alert as new.
    """

    def __init__(self, table_name: Optional[str] = None, client=None):
        self.table_name = table_name or os.environ.get(
            "AGENT_JOURNAL_TABLE", "stock-agent-dev-journal")
        self._client = client

    @property
    def client(self):
        if self._client is None:
            import boto3
            self._client = boto3.client("dynamodb")
        return self._client

    def emit(self, alert: Alert) -> bool:
        try:
            self.client.put_item(
                TableName=self.table_name,
                Item={"PK": {"S": f"ALERTS#{alert.session_date}"},
                      "SK": {"S": alert.dedupe_key},
                      "payload": {"S": json.dumps(alert.as_dict())},
                      "raised_at": {"S": alert.raised_at}},
                ConditionExpression="attribute_not_exists(SK)")
        except Exception as exc:                          # noqa: BLE001
            if "ConditionalCheckFailed" in str(exc):
                return False
            # An alert that cannot be stored must not vanish. The log
            # event is the fallback record.
            log_event("alert_persist_failed", kind=str(alert.kind),
                      error=type(exc).__name__)
            return True
        log_event("alert_raised", kind=str(alert.kind),
                  severity=str(alert.severity),
                  session_date=alert.session_date)
        return True

    def recent(self, limit: int = 50, session_date: Optional[str] = None
               ) -> List[Alert]:
        if session_date is None:
            return []
        response = self.client.query(
            TableName=self.table_name,
            KeyConditionExpression="PK = :pk",
            ExpressionAttributeValues={
                ":pk": {"S": f"ALERTS#{session_date}"}},
            Limit=limit)
        out = []
        for item in response.get("Items", []):
            d = json.loads(item["payload"]["S"])
            out.append(Alert(kind=AlertKind(d["kind"]), detail=d["detail"],
                             session_date=d["session_date"],
                             context=d.get("context") or {},
                             raised_at=d.get("raised_at", "")))
        return out

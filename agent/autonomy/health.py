"""
Agent health.

HEALTHY, DEGRADED or HALTED, derived from a set of named conditions.
The state is never assigned: it is computed from which conditions are
active, so it cannot say HEALTHY while a halting condition is open.

New exposure requires HEALTHY. DEGRADED permits it only for conditions
explicitly listed in TOLERATED_FOR_ENTRIES below, and each exception
carries its reason. The default for an unlisted condition is that it
blocks entries - a new condition added later fails closed until someone
decides otherwise.

Exits are never gated by health. A sick agent must still be able to
close what it holds; refusing to would convert a monitoring problem
into an unmanaged position.
"""
from __future__ import annotations

import enum
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Set


class HealthState(str, enum.Enum):
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    HALTED = "HALTED"

    def __str__(self) -> str:
        return self.value


class Condition(str, enum.Enum):
    # --- halting: new exposure impossible, exits continue -------------
    RECONCILIATION_MISMATCH = "RECONCILIATION_MISMATCH"
    UNEXPECTED_BROKER_POSITION = "UNEXPECTED_BROKER_POSITION"
    EMERGENCY_STOP = "EMERGENCY_STOP"
    DAILY_RISK_LOCK = "DAILY_RISK_LOCK"
    BROKER_UNAVAILABLE = "BROKER_UNAVAILABLE"
    JOURNAL_PERSISTENCE_FAILURE = "JOURNAL_PERSISTENCE_FAILURE"
    STATE_PERSISTENCE_FAILURE = "STATE_PERSISTENCE_FAILURE"
    CYCLE_LOCK_FAILURE = "CYCLE_LOCK_FAILURE"
    UNCERTAIN_ORDER_STATE = "UNCERTAIN_ORDER_STATE"
    DUPLICATE_ORDER_ATTEMPT = "DUPLICATE_ORDER_ATTEMPT"
    EOD_FLATTEN_FAILURE = "EOD_FLATTEN_FAILURE"
    MARKET_DATA_UNAVAILABLE = "MARKET_DATA_UNAVAILABLE"
    STALE_MARKET_DATA = "STALE_MARKET_DATA"
    REPEATED_CYCLE_FAILURE = "REPEATED_CYCLE_FAILURE"

    # --- degraded: some capability lost, but entries may be safe ------
    EVIDENCE_PROVIDER_DEGRADED = "EVIDENCE_PROVIDER_DEGRADED"
    SCANNER_DEGRADED = "SCANNER_DEGRADED"
    LLM_UNAVAILABLE = "LLM_UNAVAILABLE"

    def __str__(self) -> str:
        return self.value


# Conditions that, by themselves, make the state HALTED.
HALTING: Set[Condition] = {
    Condition.RECONCILIATION_MISMATCH, Condition.UNEXPECTED_BROKER_POSITION,
    Condition.EMERGENCY_STOP, Condition.DAILY_RISK_LOCK,
    Condition.BROKER_UNAVAILABLE, Condition.JOURNAL_PERSISTENCE_FAILURE,
    Condition.STATE_PERSISTENCE_FAILURE, Condition.CYCLE_LOCK_FAILURE,
    Condition.UNCERTAIN_ORDER_STATE, Condition.DUPLICATE_ORDER_ATTEMPT,
    Condition.EOD_FLATTEN_FAILURE, Condition.MARKET_DATA_UNAVAILABLE,
    Condition.STALE_MARKET_DATA, Condition.REPEATED_CYCLE_FAILURE,
}

# The documented exceptions: degraded conditions under which a NEW entry
# is still permitted, and why. Anything not here blocks entries.
TOLERATED_FOR_ENTRIES: Dict[Condition, str] = {
    Condition.EVIDENCE_PROVIDER_DEGRADED: (
        "Evidence is not collected inside the cycle at all, and the "
        "hypothesis engine already treats 'not collected' as a minor "
        "contradiction rather than assuming there is no bad news. An "
        "outage therefore cannot make an entry riskier than the "
        "no-evidence case it already handles."),
    Condition.LLM_UNAVAILABLE: (
        "No decision depends on the LLM. It only explains stored "
        "decisions, so losing it degrades chat, not trading."),
    Condition.SCANNER_DEGRADED: (
        "A degraded scanner returns fewer or no candidates, which means "
        "fewer entries. The failure direction is toward doing less."),
}

# Conditions that are cleared automatically once the cause passes, versus
# those that require a human. The agent may never clear a latching one.
LATCHING: Set[Condition] = {
    Condition.RECONCILIATION_MISMATCH, Condition.UNEXPECTED_BROKER_POSITION,
    Condition.EMERGENCY_STOP, Condition.UNCERTAIN_ORDER_STATE,
    Condition.DUPLICATE_ORDER_ATTEMPT, Condition.EOD_FLATTEN_FAILURE,
    Condition.JOURNAL_PERSISTENCE_FAILURE,
}


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class ActiveCondition:
    condition: Condition
    since: str
    detail: str = ""
    count: int = 1

    def as_dict(self) -> Dict:
        return {"condition": str(self.condition), "since": self.since,
                "detail": self.detail, "count": self.count,
                "latching": self.condition in LATCHING,
                "halting": self.condition in HALTING}


@dataclass
class HealthSnapshot:
    active: List[ActiveCondition] = field(default_factory=list)
    assessed_at: str = field(default_factory=utcnow)

    @property
    def state(self) -> HealthState:
        """Derived. No setter."""
        names = {c.condition for c in self.active}
        if names & HALTING:
            return HealthState.HALTED
        if names:
            return HealthState.DEGRADED
        return HealthState.HEALTHY

    @property
    def entries_permitted(self) -> bool:
        """May a NEW position be opened?

        HEALTHY, or DEGRADED only through documented exceptions. An
        unlisted degraded condition blocks, so a condition added later
        fails closed until someone decides it should not.
        """
        state = self.state
        if state is HealthState.HEALTHY:
            return True
        if state is HealthState.HALTED:
            return False
        return all(c.condition in TOLERATED_FOR_ENTRIES
                   for c in self.active)

    @property
    def exits_permitted(self) -> bool:
        """Always true. A sick agent must still be able to close what it
        holds."""
        return True

    def blocking_reasons(self) -> List[str]:
        return [f"{c.condition}: {c.detail}".rstrip(": ")
                for c in self.active
                if c.condition not in TOLERATED_FOR_ENTRIES]

    def as_dict(self) -> Dict:
        return {
            "state": str(self.state),
            "entries_permitted": self.entries_permitted,
            "exits_permitted": self.exits_permitted,
            "active": [c.as_dict() for c in self.active],
            "blocking_reasons": self.blocking_reasons(),
            "tolerated": {str(k): v for k, v in
                          TOLERATED_FOR_ENTRIES.items()},
            "assessed_at": self.assessed_at,
        }


class HealthStore:
    """Tracks active conditions across invocations.

    Raising a condition is cheap and always allowed. Clearing one is
    restricted: a latching condition can be cleared only with an
    explicit human actor, never by the agent's own cycle.
    """

    def __init__(self):
        self._active: Dict[Condition, ActiveCondition] = {}

    # Subclasses persist; the in-memory form is the reference behaviour.
    def _load(self) -> Dict[Condition, ActiveCondition]:
        return self._active

    def _save(self, active: Dict[Condition, ActiveCondition]) -> None:
        self._active = active

    def raise_condition(self, condition: Condition, detail: str = "") -> None:
        active = self._load()
        existing = active.get(condition)
        if existing:
            existing.count += 1
            if detail:
                existing.detail = detail
        else:
            active[condition] = ActiveCondition(
                condition=condition, since=utcnow(), detail=detail)
        self._save(active)

    def clear_condition(self, condition: Condition,
                        cleared_by: Optional[str] = None) -> bool:
        """Clear a condition. Returns True if it was cleared.

        A latching condition needs `cleared_by` naming a human. The
        agent's own cycle passes None and is refused, which is what
        prevents a system from talking itself back into the situation
        that halted it.
        """
        active = self._load()
        if condition not in active:
            return False
        if condition in LATCHING and not cleared_by:
            return False
        del active[condition]
        self._save(active)
        return True

    def snapshot(self) -> HealthSnapshot:
        return HealthSnapshot(active=list(self._load().values()))

    # Consecutive failed cycles. One failure is noise; three in a row is
    # a pattern worth a human's attention.
    def get_streak(self) -> int:
        return getattr(self, "_streak", 0)

    def set_streak(self, value: int) -> None:
        self._streak = value

    def record_cycle(self, ok: bool) -> int:
        """Record a cycle outcome and return the current failure streak."""
        streak = 0 if ok else self.get_streak() + 1
        self.set_streak(streak)
        return streak


class InMemoryHealthStore(HealthStore):
    pass


class DynamoDBHealthStore(HealthStore):
    """PK = CONTROL#HEALTH, SK = STATE. One record for the agent.

    A read failure raises rather than returning "healthy": an unreadable
    health record is not a clean bill of health.
    """

    def __init__(self, table_name: Optional[str] = None, client=None):
        super().__init__()
        self.table_name = table_name or os.environ.get(
            "AGENT_JOURNAL_TABLE", "stock-agent-dev-journal")
        self._client = client

    @property
    def client(self):
        if self._client is None:
            import boto3
            self._client = boto3.client("dynamodb")
        return self._client

    def _load(self) -> Dict[Condition, ActiveCondition]:
        response = self.client.get_item(
            TableName=self.table_name,
            Key={"PK": {"S": "CONTROL#HEALTH"}, "SK": {"S": "STATE"}},
            ConsistentRead=True)
        item = response.get("Item")
        if not item:
            return {}
        # A record created by set_streak (update_item) has a streak but no
        # conditions attribute yet. That is "no active conditions", not an
        # unreadable record; indexing it raised KeyError and aborted every
        # live cycle after the first.
        rows = json.loads((item.get("conditions") or {"S": "[]"})["S"])
        return {Condition(r["condition"]): ActiveCondition(
            condition=Condition(r["condition"]), since=r["since"],
            detail=r.get("detail", ""), count=r.get("count", 1))
            for r in rows}

    def _save(self, active: Dict[Condition, ActiveCondition]) -> None:
        # update_item, not put_item: a put would overwrite the streak
        # attribute that lives on the same record.
        self.client.update_item(
            TableName=self.table_name,
            Key={"PK": {"S": "CONTROL#HEALTH"}, "SK": {"S": "STATE"}},
            UpdateExpression="SET conditions = :c, updated_at = :u",
            ExpressionAttributeValues={
                ":c": {"S": json.dumps(
                    [c.as_dict() for c in active.values()])},
                ":u": {"S": utcnow()}})

    def get_streak(self) -> int:
        response = self.client.get_item(
            TableName=self.table_name,
            Key={"PK": {"S": "CONTROL#HEALTH"}, "SK": {"S": "STATE"}},
            ConsistentRead=True)
        item = response.get("Item") or {}
        return int((item.get("streak") or {}).get("N", 0))

    def set_streak(self, value: int) -> None:
        self.client.update_item(
            TableName=self.table_name,
            Key={"PK": {"S": "CONTROL#HEALTH"}, "SK": {"S": "STATE"}},
            UpdateExpression="SET streak = :s",
            ExpressionAttributeValues={":s": {"N": str(value)}})

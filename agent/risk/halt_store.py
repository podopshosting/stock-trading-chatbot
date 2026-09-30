"""
Durable global halt state.

This fixes a known Milestone 3 deferral. The emergency stop lived on the
daily session record, so a new session began with a fresh record and the
halt silently evaporated overnight. **A stop that expires by itself is
not a stop.**

The halt therefore lives in its own record with NO session date:

    PK = CONTROL#GLOBAL   SK = HALT

It persists until something explicitly clears it, and clearing requires
naming who did it and why. Writes are guarded by a revision so two
concurrent clears cannot both succeed.
"""
from __future__ import annotations

import json
from abc import ABC, abstractmethod
from typing import Dict, Optional

from ..observability import log_event
from .models import GlobalHaltState, HaltScope, utcnow


class HaltStoreError(Exception):
    pass


class HaltStore(ABC):
    @abstractmethod
    def get(self) -> GlobalHaltState: ...

    @abstractmethod
    def _put(self, state: GlobalHaltState, expected_revision: int) -> None: ...

    def engage(self, reason: str, engaged_by: str = "system") -> GlobalHaltState:
        """Halt. Idempotent: halting an already-halted system is a no-op
        that preserves the ORIGINAL reason, because the first cause is
        the one worth keeping."""
        current = self.get()
        if current.halted:
            return current
        state = GlobalHaltState(
            halted=True, reason=reason, scope=HaltScope.GLOBAL,
            engaged_at=utcnow(), engaged_by=engaged_by,
            revision=current.revision + 1)
        self._put(state, current.revision)
        log_event("emergency_stop_triggered", reason=reason,
                  engaged_by=engaged_by, scope="GLOBAL")
        return state

    def clear(self, cleared_by: str, reason: str = "") -> GlobalHaltState:
        """Release the halt. Requires naming who did it.

        A halt that anything could clear anonymously would be a
        suggestion. `cleared_by` is mandatory.
        """
        if not cleared_by or not cleared_by.strip():
            raise HaltStoreError(
                "clearing a global halt requires naming who cleared it")
        current = self.get()
        if not current.halted:
            return current
        state = GlobalHaltState(
            halted=False, reason=current.reason, scope=current.scope,
            engaged_at=current.engaged_at, engaged_by=current.engaged_by,
            cleared_at=utcnow(), cleared_by=cleared_by,
            revision=current.revision + 1)
        self._put(state, current.revision)
        log_event("emergency_stop_cleared", cleared_by=cleared_by,
                  original_reason=current.reason, note=reason)
        return state


class InMemoryHaltStore(HaltStore):
    def __init__(self, state: Optional[GlobalHaltState] = None):
        self._state = state or GlobalHaltState()

    def get(self) -> GlobalHaltState:
        return GlobalHaltState.from_dict(self._state.as_dict())

    def _put(self, state: GlobalHaltState, expected_revision: int) -> None:
        if self._state.revision != expected_revision:
            raise HaltStoreError(
                f"halt state changed underneath us (expected revision "
                f"{expected_revision}, found {self._state.revision})")
        self._state = state


class DynamoDBHaltStore(HaltStore):
    """Production store.

    The key carries no session date on purpose: there is exactly one
    global halt record and rolling into a new day cannot produce a fresh
    empty one.
    """

    PK = "CONTROL#GLOBAL"
    SK = "HALT"

    def __init__(self, table_name: str, region: str = "us-east-2",
                 client=None):
        self.table_name = table_name
        self.region = region
        self._client = client

    @property
    def client(self):
        if self._client is None:
            import boto3
            self._client = boto3.client("dynamodb", region_name=self.region)
        return self._client

    def get(self) -> GlobalHaltState:
        try:
            row = self.client.get_item(
                TableName=self.table_name,
                Key={"PK": {"S": self.PK}, "SK": {"S": self.SK}},
                ConsistentRead=True).get("Item")
        except Exception as e:
            # Fail closed. If we cannot read the halt state we must
            # assume the system is halted: the alternative is trading
            # through an outage that might be hiding a stop.
            log_event("provider_error", operation="halt_store_get",
                      error=type(e).__name__)
            return GlobalHaltState(
                halted=True,
                reason=f"halt state unreadable ({type(e).__name__}); failing "
                       f"closed",
                engaged_at=utcnow(), engaged_by="halt_store_failsafe")
        if not row or "payload" not in row:
            return GlobalHaltState()
        return GlobalHaltState.from_dict(json.loads(row["payload"]["S"]))

    def _put(self, state: GlobalHaltState, expected_revision: int) -> None:
        item = {
            "PK": {"S": self.PK}, "SK": {"S": self.SK},
            "revision": {"N": str(state.revision)},
            "payload": {"S": json.dumps(state.as_dict())},
        }
        try:
            if expected_revision == 0:
                self.client.put_item(
                    TableName=self.table_name, Item=item,
                    ConditionExpression="attribute_not_exists(PK) OR "
                                        "revision = :r",
                    ExpressionAttributeValues={
                        ":r": {"N": str(expected_revision)}})
            else:
                self.client.put_item(
                    TableName=self.table_name, Item=item,
                    ConditionExpression="revision = :r",
                    ExpressionAttributeValues={
                        ":r": {"N": str(expected_revision)}})
        except Exception as e:
            raise HaltStoreError(
                f"could not write halt state: {type(e).__name__}") from e

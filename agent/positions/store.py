"""
Persistence for managed positions.

The broker knows quantity and cost basis. It does not know the exit
plan, and the exit plan is the part that keeps a position from becoming
an unbounded loss. So if this store loses a position, reconciliation
finds a holding at the broker with no plan attached, halts, and a human
has to intervene - which is the correct outcome but not a good one.

The high-water mark is the other thing only the agent knows. Losing it
resets the trailing stop to the entry, silently giving back every
ratchet the position had earned.
"""
from __future__ import annotations

import json
import os
from typing import Dict, List, Optional

from ..observability import log_event
from .models import (
    ExitPlan, ExitReason, ManagedPosition, PositionState, StopMechanism,
)


# Below this, a difference between the stored stop and its recorded
# history is rounding, not a loosened stop. Six-decimal rounding can
# differ by 5e-7; a tenth of a cent is far larger than that and far
# smaller than any price move that matters.
STOP_COMPARISON_TOLERANCE = 1e-4


class PositionStoreError(Exception):
    pass


class ConcurrentPositionUpdate(PositionStoreError):
    pass


def to_item(position: ManagedPosition) -> Dict:
    """Everything the broker does not know."""
    return {
        "position_id": position.position_id,
        "symbol": position.symbol,
        "quantity": position.quantity,
        "entry_price": position.entry_price,
        "opened_at": position.opened_at,
        "state": str(position.state),
        "high_water_price": position.high_water_price,
        "low_water_price": position.low_water_price,
        "current_price": position.current_price,
        "last_evaluated_at": position.last_evaluated_at,
        "hypothesis_id": position.hypothesis_id,
        "risk_decision_id": position.risk_decision_id,
        "entry_order_id": position.entry_order_id,
        "exit_order_id": position.exit_order_id,
        "config_version": position.config_version,
        "stop_history": position.stop_history,
        "exit_reasons": [str(r) for r in position.exit_reasons],
        "plan": {
            "stop_price": position.plan.stop_price,
            "target_price": position.plan.target_price,
            "trailing_stop_pct": position.plan.trailing_stop_pct,
            "max_hold_minutes": position.plan.max_hold_minutes,
            "flatten_before_close_minutes":
                position.plan.flatten_before_close_minutes,
            "stop_mechanism": str(position.plan.stop_mechanism),
            "evaluation_interval_seconds":
                position.plan.evaluation_interval_seconds,
        },
    }


def from_item(item: Dict) -> ManagedPosition:
    plan_data = item.get("plan") or {}
    if "stop_price" not in plan_data:
        # A position without a stop is an unbounded loss. Refusing to
        # reconstruct it is better than defaulting one, because a
        # defaulted stop would be a number nobody chose applied to real
        # exposure.
        raise PositionStoreError(
            f"{item.get('symbol')} has no stored stop price; refusing to "
            "reconstruct a position without the plan that bounds it")

    plan = ExitPlan(
        stop_price=float(plan_data["stop_price"]),
        target_price=plan_data.get("target_price"),
        trailing_stop_pct=plan_data.get("trailing_stop_pct"),
        max_hold_minutes=plan_data.get("max_hold_minutes"),
        flatten_before_close_minutes=plan_data.get(
            "flatten_before_close_minutes"),
        stop_mechanism=StopMechanism(
            plan_data.get("stop_mechanism", "ENGINE_POLLED")),
        evaluation_interval_seconds=plan_data.get(
            "evaluation_interval_seconds"),
    )
    position = ManagedPosition(
        position_id=item["position_id"],
        symbol=item["symbol"],
        quantity=float(item["quantity"]),
        entry_price=float(item["entry_price"]),
        plan=plan,
        opened_at=item.get("opened_at") or "",
        state=PositionState(item.get("state", "OPEN")),
        high_water_price=item.get("high_water_price"),
        low_water_price=item.get("low_water_price"),
        current_price=item.get("current_price"),
        last_evaluated_at=item.get("last_evaluated_at"),
        hypothesis_id=item.get("hypothesis_id"),
        risk_decision_id=item.get("risk_decision_id"),
        entry_order_id=item.get("entry_order_id"),
        exit_order_id=item.get("exit_order_id"),
        config_version=item.get("config_version") or "",
        stop_history=item.get("stop_history") or [],
    )
    position.exit_reasons = [ExitReason(r)
                             for r in (item.get("exit_reasons") or [])]

    # The stop must never come back WIDER than the history says it
    # reached. If storage were rolled back or edited, restoring the
    # older, wider stop would quietly increase risk on a live position -
    # the one thing tighten_stop exists to make impossible.
    #
    # Compared with a tolerance, not exactly. stop_history rounds `to`
    # to six places while plan.stop_price keeps the raw float, so a
    # trailing stop at 116.39999999999999 reads as "wider" than its own
    # recorded 116.4. An exact comparison refused legitimate positions
    # on the second round trip and would have halted the agent over
    # floating-point dust. The guard is for a MEANINGFULLY wider stop.
    for move in position.stop_history:
        recorded = move.get("to")
        if recorded is None:
            continue
        drift = recorded - position.plan.stop_price
        if drift > STOP_COMPARISON_TOLERANCE:
            raise PositionStoreError(
                f"{position.symbol}: stored stop {position.plan.stop_price} "
                f"is wider than {recorded} recorded in its own history; "
                "refusing to restore a loosened stop")
    return position


class PositionStore:
    def load_open(self, session_date: str) -> List[ManagedPosition]:
        raise NotImplementedError

    def save(self, position: ManagedPosition, session_date: str) -> None:
        raise NotImplementedError

    def delete(self, symbol: str, session_date: str) -> None:
        raise NotImplementedError


class InMemoryPositionStore(PositionStore):
    def __init__(self):
        self._items: Dict[str, Dict[str, Dict]] = {}

    def load_open(self, session_date: str) -> List[ManagedPosition]:
        rows = self._items.get(session_date, {})
        return [from_item(json.loads(json.dumps(r))) for r in rows.values()]

    def save(self, position: ManagedPosition, session_date: str) -> None:
        self._items.setdefault(session_date, {})[position.symbol] = \
            to_item(position)

    def delete(self, symbol: str, session_date: str) -> None:
        self._items.get(session_date, {}).pop(symbol, None)


class DynamoDBPositionStore(PositionStore):
    """PK = POSITIONS#<session_date>, SK = SYMBOL#<symbol>."""

    def __init__(self, table_name: Optional[str] = None, client=None):
        self.table_name = table_name or os.environ.get(
            "AGENT_POSITIONS_TABLE", "stock-agent-dev-positions")
        self._client = client

    @property
    def client(self):
        if self._client is None:
            import boto3
            self._client = boto3.client("dynamodb")
        return self._client

    def load_open(self, session_date: str) -> List[ManagedPosition]:
        """Raises on a read error.

        An unreadable position store is not an empty one. Returning []
        would make the orchestrator believe it holds nothing while the
        broker holds real positions - and reconciliation would then halt,
        which is safe but only because reconciliation exists. This layer
        should not be relying on that.
        """
        try:
            response = self.client.query(
                TableName=self.table_name,
                KeyConditionExpression="PK = :pk",
                ExpressionAttributeValues={
                    ":pk": {"S": f"POSITIONS#{session_date}"}},
                ConsistentRead=True)
        except Exception as exc:                          # noqa: BLE001
            raise PositionStoreError(
                f"could not read positions for {session_date}: {exc}"
            ) from exc

        positions = []
        for item in response.get("Items", []):
            position = from_item(json.loads(item["payload"]["S"]))
            if position.state is not PositionState.CLOSED:
                positions.append(position)
        return positions

    def save(self, position: ManagedPosition, session_date: str) -> None:
        try:
            self.client.put_item(
                TableName=self.table_name,
                Item={
                    "PK": {"S": f"POSITIONS#{session_date}"},
                    "SK": {"S": f"SYMBOL#{position.symbol}"},
                    "position_id": {"S": position.position_id},
                    "state": {"S": str(position.state)},
                    "stop_price": {"N": str(position.plan.stop_price)},
                    "payload": {"S": json.dumps(to_item(position))},
                })
        except Exception as exc:                          # noqa: BLE001
            raise PositionStoreError(
                f"could not save {position.symbol}: {exc}") from exc
        log_event("position_state_saved", symbol=position.symbol,
                  position_id=position.position_id,
                  stop=round(position.plan.stop_price, 4),
                  state=str(position.state))

    def delete(self, symbol: str, session_date: str) -> None:
        try:
            self.client.delete_item(
                TableName=self.table_name,
                Key={"PK": {"S": f"POSITIONS#{session_date}"},
                     "SK": {"S": f"SYMBOL#{symbol}"}})
        except Exception as exc:                          # noqa: BLE001
            raise PositionStoreError(
                f"could not delete {symbol}: {exc}") from exc

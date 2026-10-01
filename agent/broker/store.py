"""
Persistence for the paper broker.

Lambda is stateless, so a paper account that lives in memory resets on
every invocation: cash returns to its starting value, open positions
vanish, and the pilot produces a stream of unrelated one-cycle
experiments rather than a continuous record.

Two properties matter more than the storage details.

**Optimistic concurrency.** Two cycles that both load the account, both
decide, and both save would silently lose one set of fills. The revision
guard makes the second write fail instead. Combined with the cycle lock
this is belt and braces, and it stays that way: the lock prevents the
overlap, and the revision guard catches the case where the lock itself
was wrong.

**Fail closed on a read error.** An account state that cannot be read is
not an empty account. Returning a fresh broker with full starting cash
would be catastrophic - it would look like a clean slate while real
positions sat unmanaged at the broker. A read failure raises.
"""
from __future__ import annotations

import json
import os
from typing import Dict, List, Optional

from ..observability import log_event
from .models import (
    Fill, Order, OrderSide, OrderStatus, OrderType, BrokerPosition,
    Account, RejectReason, TimeInForce,
)


class BrokerStateError(Exception):
    pass


class ConcurrentBrokerUpdate(BrokerStateError):
    """Another writer got there first. The caller must reload, not retry
    blindly: its decisions were made against stale state."""


def serialise(broker) -> Dict:
    """Capture everything needed to resume a paper account."""
    account = broker._account
    return {
        "account": {
            "account_id": account.account_id,
            "cash": account.cash,
            "starting_cash": account.starting_cash,
            "reserved_cash": account.reserved_cash,
            "realized_pnl": account.realized_pnl,
            "is_paper": account.is_paper,
            "currency": account.currency,
        },
        "positions": [
            {
                "symbol": p.symbol,
                "quantity": p.quantity,
                "average_entry_price": p.average_entry_price,
                "opened_at": p.opened_at,
            }
            for p in broker._positions.values()
        ],
        # EVERY order, not just the open ones.
        #
        # The first version of this kept only open orders while keeping
        # the client-id map for all of them, so after a restart a
        # duplicate submission looked up a client id, found an order id,
        # and crashed because that order was no longer there. Idempotency
        # has to return the EXISTING order, which means the order must
        # still exist.
        #
        # Growth is bounded in practice: the risk limits cap new
        # positions at a few per day and a snapshot covers one session.
        "orders": [_order_dict(o) for o in broker._orders.values()],
        # Client ids of every order, so idempotency survives a cold
        # start. Without this a retried submission after a restart would
        # place a second order - the exact failure the client id exists
        # to prevent.
        "client_order_ids": dict(broker._client_ids),
        "config": {
            "starting_cash": broker.config.starting_cash,
            "slippage_bps": broker.config.slippage_bps,
            "partial_fill_probability": broker.config.partial_fill_probability,
        },
    }


def _order_dict(order: Order) -> Dict:
    return {
        "order_id": order.order_id,
        "client_order_id": order.client_order_id,
        "symbol": order.symbol,
        "side": str(order.side),
        "order_type": str(order.order_type),
        "quantity": order.quantity,
        "limit_price": order.limit_price,
        "time_in_force": str(order.time_in_force),
        "status": str(order.status),
        "filled_quantity": order.filled_quantity,
        "average_fill_price": order.average_fill_price,
        "created_at": order.created_at,
        "submitted_at": order.submitted_at,
        "hypothesis_id": order.hypothesis_id,
        "risk_decision_id": order.risk_decision_id,
        "intent": order.intent,
    }


def restore(broker, data: Dict) -> None:
    """Load a snapshot back into a broker instance.

    Mutates the broker rather than constructing one, so the caller keeps
    control of the config and the clock.
    """
    if not data:
        raise BrokerStateError("cannot restore from an empty snapshot")

    acct = data.get("account") or {}
    broker._account = Account(
        account_id=acct.get("account_id", "paper"),
        cash=float(acct.get("cash", 0.0)),
        starting_cash=float(acct.get("starting_cash", 0.0)),
        reserved_cash=float(acct.get("reserved_cash", 0.0)),
        realized_pnl=float(acct.get("realized_pnl", 0.0)),
        is_paper=bool(acct.get("is_paper", True)),
        currency=acct.get("currency", "USD"),
    )

    broker._positions = {}
    for row in data.get("positions") or []:
        broker._positions[row["symbol"]] = BrokerPosition(
            symbol=row["symbol"],
            quantity=float(row["quantity"]),
            average_entry_price=float(row["average_entry_price"]),
            opened_at=row.get("opened_at") or "",
        )

    broker._orders = {}
    # "open_orders" is the older key; read both so a snapshot written
    # before the fix above still restores rather than silently losing
    # every order and with it the idempotency guarantee.
    for row in (data.get("orders") or data.get("open_orders") or []):
        order = Order(
            order_id=row["order_id"],
            client_order_id=row["client_order_id"],
            symbol=row["symbol"],
            side=OrderSide(row["side"]),
            order_type=OrderType(row["order_type"]),
            quantity=float(row["quantity"]),
            limit_price=row.get("limit_price"),
            time_in_force=TimeInForce(row.get("time_in_force", "DAY")),
            status=OrderStatus(row["status"]),
            filled_quantity=float(row.get("filled_quantity") or 0.0),
            average_fill_price=row.get("average_fill_price"),
            created_at=row.get("created_at") or "",
            submitted_at=row.get("submitted_at"),
            hypothesis_id=row.get("hypothesis_id"),
            risk_decision_id=row.get("risk_decision_id"),
            intent=row.get("intent") or "",
        )
        broker._orders[order.order_id] = order

    broker._client_ids = dict(data.get("client_order_ids") or {})


class BrokerStateStore:
    def load(self, account_id: str = "paper"):
        raise NotImplementedError

    def save(self, broker, account_id: str = "paper",
             expected_revision: Optional[int] = None) -> int:
        raise NotImplementedError


class InMemoryBrokerStateStore(BrokerStateStore):
    def __init__(self):
        self._snapshots: Dict[str, Dict] = {}
        self._revisions: Dict[str, int] = {}

    def load(self, account_id: str = "paper"):
        snapshot = self._snapshots.get(account_id)
        if snapshot is None:
            return None, 0
        return json.loads(json.dumps(snapshot)), self._revisions[account_id]

    def save(self, broker, account_id: str = "paper",
             expected_revision: Optional[int] = None) -> int:
        current = self._revisions.get(account_id, 0)
        if expected_revision is not None and expected_revision != current:
            raise ConcurrentBrokerUpdate(
                f"{account_id} is at revision {current}, not "
                f"{expected_revision}; reload before saving")
        self._snapshots[account_id] = serialise(broker)
        self._revisions[account_id] = current + 1
        return current + 1


class DynamoDBBrokerStateStore(BrokerStateStore):
    """PK = BROKER#<account_id>, SK = STATE."""

    def __init__(self, table_name: Optional[str] = None, client=None):
        self.table_name = table_name or os.environ.get(
            "AGENT_BROKER_TABLE", "stock-agent-dev-broker")
        self._client = client

    @property
    def client(self):
        if self._client is None:
            import boto3
            self._client = boto3.client("dynamodb")
        return self._client

    def load(self, account_id: str = "paper"):
        """Returns (snapshot_or_None, revision).

        Raises on a read error. An unreadable account is NOT an empty
        account: handing back a fresh broker with full starting cash
        would look like a clean slate while real positions sat
        unmanaged.
        """
        try:
            response = self.client.get_item(
                TableName=self.table_name,
                Key={"PK": {"S": f"BROKER#{account_id}"},
                     "SK": {"S": "STATE"}},
                ConsistentRead=True)
        except Exception as exc:                          # noqa: BLE001
            raise BrokerStateError(
                f"could not read broker state for {account_id}: {exc}"
            ) from exc

        item = response.get("Item")
        if not item:
            # Genuinely absent, which is different from unreadable. A
            # first run has no state and that is fine.
            return None, 0
        return (json.loads(item["snapshot"]["S"]),
                int(item["revision"]["N"]))

    def save(self, broker, account_id: str = "paper",
             expected_revision: Optional[int] = None) -> int:
        snapshot = serialise(broker)
        revision = (0 if expected_revision is None
                    else expected_revision) + 1
        item = {
            "PK": {"S": f"BROKER#{account_id}"},
            "SK": {"S": "STATE"},
            "revision": {"N": str(revision)},
            "snapshot": {"S": json.dumps(snapshot)},
            "cash": {"N": str(round(broker._account.cash, 4))},
            "open_positions": {"N": str(len(broker._positions))},
        }
        kwargs = {"TableName": self.table_name, "Item": item}
        if expected_revision is None:
            kwargs["ConditionExpression"] = "attribute_not_exists(SK)"
        else:
            kwargs["ConditionExpression"] = "revision = :expected"
            kwargs["ExpressionAttributeValues"] = {
                ":expected": {"N": str(expected_revision)}}

        try:
            self.client.put_item(**kwargs)
        except Exception as exc:                          # noqa: BLE001
            if "ConditionalCheckFailed" in str(exc):
                raise ConcurrentBrokerUpdate(
                    f"{account_id} was modified by another writer; "
                    "reload before saving") from exc
            raise BrokerStateError(
                f"could not save broker state: {exc}") from exc

        log_event("broker_state_saved", account_id=account_id,
                  revision=revision,
                  cash=round(broker._account.cash, 2),
                  open_positions=len(broker._positions))
        return revision

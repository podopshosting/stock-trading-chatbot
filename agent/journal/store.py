"""
Journal persistence.

Two implementations: in-memory for tests and replay, DynamoDB for the
dev environment. Both are append-only. A trade record is written once
when the trade closes and never edited, because a performance history
that can be revised is not a record of anything.
"""
from __future__ import annotations

import json
import os
from typing import Dict, List, Optional, Sequence

from ..observability import log_event
from .models import TradeCosts, TradeRecord


class JournalError(Exception):
    pass


class TradeAlreadyRecorded(JournalError):
    """The journal is append-only; a trade is written exactly once."""


class JournalStore:
    """Interface."""

    def record(self, trade: TradeRecord) -> TradeRecord:
        raise NotImplementedError

    def get(self, trade_id: str) -> Optional[TradeRecord]:
        raise NotImplementedError

    def list_trades(self, session_date: Optional[str] = None,
                    limit: int = 500) -> List[TradeRecord]:
        raise NotImplementedError


class InMemoryJournal(JournalStore):
    def __init__(self):
        self._trades: Dict[str, TradeRecord] = {}

    def record(self, trade: TradeRecord) -> TradeRecord:
        if trade.trade_id in self._trades:
            raise TradeAlreadyRecorded(
                f"{trade.trade_id} is already in the journal")
        self._trades[trade.trade_id] = trade
        log_event("trade_recorded", symbol=trade.symbol,
                  trade_id=trade.trade_id,
                  net_pnl=round(trade.net_pnl, 4),
                  r_multiple=(None if trade.r_multiple is None
                              else round(trade.r_multiple, 4)),
                  outcome=str(trade.outcome),
                  exit_reason=trade.exit_reason,
                  is_paper=trade.is_paper)
        return trade

    def get(self, trade_id: str) -> Optional[TradeRecord]:
        return self._trades.get(trade_id)

    def list_trades(self, session_date: Optional[str] = None,
                    limit: int = 500) -> List[TradeRecord]:
        trades = list(self._trades.values())
        if session_date is not None:
            trades = [t for t in trades if t.session_date == session_date]
        trades.sort(key=lambda t: t.closed_at)
        return trades[:limit]


class DynamoDBJournal(JournalStore):
    """Single-table layout: PK = JOURNAL#<session_date>, SK = TRADE#<id>.

    Writes use a condition expression so a duplicate record is rejected
    by the database rather than by application code that might not be
    running.
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

    def record(self, trade: TradeRecord) -> TradeRecord:
        item = {
            "PK": {"S": f"JOURNAL#{trade.session_date or 'UNDATED'}"},
            "SK": {"S": f"TRADE#{trade.trade_id}"},
            "trade_id": {"S": trade.trade_id},
            "symbol": {"S": trade.symbol},
            "closed_at": {"S": trade.closed_at},
            "is_paper": {"BOOL": trade.is_paper},
            "payload": {"S": json.dumps(trade.as_dict())},
        }
        try:
            self.client.put_item(
                TableName=self.table_name, Item=item,
                ConditionExpression="attribute_not_exists(SK)")
        except Exception as exc:                          # noqa: BLE001
            if "ConditionalCheckFailed" in str(exc):
                raise TradeAlreadyRecorded(
                    f"{trade.trade_id} is already in the journal") from exc
            raise JournalError(f"could not record {trade.trade_id}: "
                               f"{exc}") from exc
        log_event("trade_recorded", symbol=trade.symbol,
                  trade_id=trade.trade_id,
                  net_pnl=round(trade.net_pnl, 4),
                  outcome=str(trade.outcome),
                  exit_reason=trade.exit_reason,
                  is_paper=trade.is_paper)
        return trade

    def get(self, trade_id: str,
            session_date: Optional[str] = None) -> Optional[TradeRecord]:
        response = self.client.get_item(
            TableName=self.table_name,
            Key={"PK": {"S": f"JOURNAL#{session_date or 'UNDATED'}"},
                 "SK": {"S": f"TRADE#{trade_id}"}})
        item = response.get("Item")
        if not item:
            return None
        return from_dict(json.loads(item["payload"]["S"]))

    def list_trades(self, session_date: Optional[str] = None,
                    limit: int = 500) -> List[TradeRecord]:
        if session_date is None:
            raise JournalError(
                "a session_date is required; scanning the whole journal "
                "would be both slow and unbounded")
        response = self.client.query(
            TableName=self.table_name,
            KeyConditionExpression="PK = :pk",
            ExpressionAttributeValues={
                ":pk": {"S": f"JOURNAL#{session_date}"}},
            Limit=limit)
        trades = [from_dict(json.loads(i["payload"]["S"]))
                  for i in response.get("Items", [])]
        trades.sort(key=lambda t: t.closed_at)
        return trades


def from_dict(data: Dict) -> TradeRecord:
    """Rebuild a record from its serialised form.

    Only the stored fields are restored; everything derived is
    recomputed from them, so a stale or tampered derived value in
    storage cannot survive a round trip.
    """
    costs = TradeCosts(**{k: v or 0.0
                          for k, v in (data.get("costs") or {}).items()
                          if k in ("entry_slippage", "exit_slippage",
                                   "spread_paid", "commission")})
    return TradeRecord(
        trade_id=data["trade_id"],
        symbol=data["symbol"],
        quantity=data["quantity"],
        entry_price=data["entry_price"],
        exit_price=data["exit_price"],
        opened_at=data["opened_at"],
        closed_at=data["closed_at"],
        planned_stop=data["planned_stop"],
        planned_target=data.get("planned_target"),
        strategy=data.get("strategy", ""),
        hypothesis_id=data.get("hypothesis_id"),
        hypothesis_strength=data.get("hypothesis_strength"),
        risk_decision_id=data.get("risk_decision_id"),
        exit_reason=data.get("exit_reason", ""),
        all_exit_reasons=data.get("all_exit_reasons") or [],
        entry_order_id=data.get("entry_order_id"),
        exit_order_id=data.get("exit_order_id"),
        position_id=data.get("position_id"),
        costs=costs,
        stop_history=data.get("stop_history") or [],
        max_favourable_price=data.get("max_favourable_price"),
        max_adverse_price=data.get("max_adverse_price"),
        config_versions=data.get("config_versions") or {},
        is_paper=data.get("is_paper", True),
        session_date=data.get("session_date", ""),
        recorded_at=data.get("recorded_at", ""),
    )

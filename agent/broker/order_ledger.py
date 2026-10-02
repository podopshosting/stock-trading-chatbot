"""
A durable record of every order the agent has sent to an EXTERNAL venue.

This exists because of a real incident. On 2026-10-02 the agent
submitted an order to Alpaca's paper venue, read it back unfilled,
recorded "not filled" and moved on. The venue filled it afterwards, and
the agent held a position it had no record of until reconciliation
halted it.

The missing piece was not a status mapping - `normalise_order` already
maps sixteen Alpaca statuses and defaults an unknown one to PENDING. The
missing piece was that nothing durably recorded the order at all. The
simulator's state store serialises the simulator's own `_orders`, and
when an external venue is authoritative that store is not used for its
orders. So after the cycle ended there was no record that an order
existed, which makes four things impossible at once:

  * querying the broker by client order id after a lost response
  * polling a non-terminal order until it reaches a terminal state
  * reserving risk against an accepted-but-unfilled order
  * establishing that a discovered position was the agent's own

This ledger is NOT a second source of truth about exposure. The venue
remains authoritative for what is filled and held. The ledger records
what the agent *asked for*, which the venue cannot tell us, and the last
status the agent *observed*, which is a claim about an observation and
not about the present.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional

# Statuses from which no further transition is possible. Anything else -
# including an unrecognised status - is non-terminal, because an order we
# cannot classify must not be treated as finished.
# This system's own conclusion, not a venue status: the venue has
# CONFIRMED there is no such order. Reachable only from a confirmed
# absence after a grace period, never from a failed lookup, because
# "I could not ask" and "it does not exist" release exposure in
# opposite directions.
NEVER_PLACED = "NEVER_PLACED"

TERMINAL = frozenset({"FILLED", "CANCELLED", "REJECTED", "EXPIRED",
                      NEVER_PLACED})

# What the agent knows about whether this order resulted in exposure.
LIFECYCLE_EXPECTED_NOT_FILLED = "EXPECTED_NOT_FILLED"
LIFECYCLE_PARTIALLY_FILLED = "PARTIALLY_FILLED"
LIFECYCLE_FILLED = "FILLED"
LIFECYCLE_CANCELLED_UNFILLED = "CANCELLED_UNFILLED"
LIFECYCLE_CANCELLED_PARTIAL = "CANCELLED_PARTIAL"
LIFECYCLE_REJECTED = "REJECTED"
LIFECYCLE_UNKNOWN = "UNKNOWN"
LIFECYCLE_NEVER_PLACED = "NEVER_PLACED"


class OrderLedgerError(Exception):
    """A ledger read or write failed. Callers must treat exposure as
    UNKNOWN rather than absent."""


@dataclass
class ExternalOrderRecord:
    """One logical order, from intent to terminal state.

    `client_order_id` is the key, not the broker's id: the broker id is
    unknown at the moment of intent, and the whole point is to have a
    handle that survives a lost response.
    """
    client_order_id: str
    session_date: str
    symbol: str
    side: str
    requested_quantity: Optional[float] = None
    requested_notional: Optional[float] = None
    intent: str = "ENTRY"

    broker_order_id: Optional[str] = None
    broker_environment: Optional[str] = None
    status: Optional[str] = None            # this system's vocabulary
    raw_status: Optional[str] = None        # exactly what the venue said
    filled_quantity: float = 0.0
    remaining_quantity: Optional[float] = None
    average_fill_price: Optional[float] = None

    hypothesis_id: Optional[str] = None
    risk_decision_id: Optional[str] = None
    cohort: Optional[str] = None
    position_id: Optional[str] = None

    intent_at: Optional[str] = None
    submitted_at: Optional[str] = None
    last_observed_at: Optional[str] = None
    observations: int = 0
    submission_outcome_known: bool = False

    @property
    def is_terminal(self) -> bool:
        """An unknown status is NOT terminal. The default has to fail
        toward "still live", because a live order is exposure."""
        return (self.status or "") in TERMINAL

    @property
    def lifecycle(self) -> str:
        """What this order did, in exposure terms."""
        if not self.submission_outcome_known:
            return LIFECYCLE_UNKNOWN
        status = self.status or ""
        if status == NEVER_PLACED:
            return LIFECYCLE_NEVER_PLACED
        if status == "FILLED":
            return LIFECYCLE_FILLED
        if status == "REJECTED":
            return LIFECYCLE_REJECTED
        if status in ("CANCELLED", "EXPIRED"):
            return (LIFECYCLE_CANCELLED_PARTIAL if self.filled_quantity > 0
                    else LIFECYCLE_CANCELLED_UNFILLED)
        if self.filled_quantity > 0:
            return LIFECYCLE_PARTIALLY_FILLED
        if status in TERMINAL or status:
            return LIFECYCLE_EXPECTED_NOT_FILLED
        return LIFECYCLE_UNKNOWN

    @property
    def created_exposure(self) -> bool:
        """Did this order put something in the book?"""
        return self.filled_quantity > 0

    @property
    def potential_exposure(self) -> Optional[float]:
        """The most this order could still cost, conservatively.

        An accepted-but-unfilled BUY reserves its whole notional: the
        venue may fill it a moment later, and a second order sized as
        though the first were not live is how an account ends up twice
        as exposed as its limits allow.

        None means it cannot be established, which callers must treat as
        a reason to refuse new exposure rather than as zero.
        """
        if self.is_terminal and self.submission_outcome_known:
            return 0.0
        if self.requested_notional is not None:
            filled_value = (self.filled_quantity or 0.0) * (
                self.average_fill_price or 0.0)
            return max(0.0, self.requested_notional - filled_value)
        if (self.requested_quantity is not None
                and self.average_fill_price is not None):
            remaining = max(0.0, self.requested_quantity
                            - (self.filled_quantity or 0.0))
            return remaining * self.average_fill_price
        return None

    def as_dict(self) -> Dict:
        d = {k: getattr(self, k) for k in self.__dataclass_fields__}
        d.update({"is_terminal": self.is_terminal,
                  "lifecycle": self.lifecycle,
                  "created_exposure": self.created_exposure,
                  "potential_exposure": self.potential_exposure})
        return d

    @classmethod
    def from_dict(cls, d: Dict) -> "ExternalOrderRecord":
        fields = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in d.items() if k in fields})


def committed_exposure(records: List["ExternalOrderRecord"]) -> Dict:
    """Conservative exposure implied by live orders.

    `known` is False when any record's outcome is unestablished. A caller
    must then refuse new exposure: an unknown order is not a zero one.
    """
    total, unknown = 0.0, []
    for r in records:
        if r.is_terminal and r.submission_outcome_known:
            continue
        value = r.potential_exposure
        if value is None:
            unknown.append(r.client_order_id)
            continue
        total += value
    return {"reserved": round(total, 6),
            "live_orders": [r.client_order_id for r in records
                            if not (r.is_terminal
                                    and r.submission_outcome_known)],
            "unestablished": unknown,
            "known": not unknown,
            "note": ("an accepted-but-unfilled order reserves its notional; "
                     "an order whose outcome is unestablished makes the "
                     "total UNKNOWN, which is not zero")}


class OrderLedger:
    """Interface. Reads raise OrderLedgerError rather than returning []:
    an unreadable ledger must not look like an empty one."""

    def record_intent(self, record: ExternalOrderRecord) -> None:
        raise NotImplementedError

    def record_observation(self, client_order_id: str, order: Dict,
                           observed_at: str,
                           session_date: Optional[str] = None
                           ) -> ExternalOrderRecord:
        raise NotImplementedError

    def get(self, client_order_id: str,
            session_date: Optional[str] = None
            ) -> Optional[ExternalOrderRecord]:
        """The record, or None where absence is ESTABLISHED.

        `session_date` is the partition. A caller that knows it must
        pass it: without it an implementation can only search, and a
        search that finds nothing has not established absence.
        """
        raise NotImplementedError

    def for_session(self, session_date: str) -> List[ExternalOrderRecord]:
        raise NotImplementedError

    def record_never_placed(self, client_order_id: str, observed_at: str,
                            session_date: Optional[str] = None
                            ) -> ExternalOrderRecord:
        raise NotImplementedError

    def non_terminal(self, session_date: str) -> List[ExternalOrderRecord]:
        return [r for r in self.for_session(session_date)
                if not (r.is_terminal and r.submission_outcome_known)]


class InMemoryOrderLedger(OrderLedger):
    def __init__(self):
        self._rows: Dict[str, ExternalOrderRecord] = {}

    def record_intent(self, record: ExternalOrderRecord) -> None:
        existing = self._rows.get(record.client_order_id)
        if existing is not None:
            # Deliberately not overwritten. A repeated intent for the
            # same deterministic id is the duplicate-submission case, and
            # the first record is the one with the observations on it.
            return
        self._rows[record.client_order_id] = record

    def record_observation(self, client_order_id, order, observed_at,
                           session_date=None):
        row = self._rows.get(client_order_id)
        if row is None:
            raise OrderLedgerError(
                f"no intent recorded for {client_order_id}; an order "
                f"observed without an intent cannot be attributed")
        _apply(row, order, observed_at)
        return row

    def record_never_placed(self, client_order_id, observed_at,
                            session_date=None):
        row = self._rows.get(client_order_id)
        if row is None:
            raise OrderLedgerError(
                f"no intent recorded for {client_order_id}")
        _mark_never_placed(row, observed_at)
        return row

    def get(self, client_order_id, session_date=None):
        # Full visibility, so None really is absence.
        return self._rows.get(client_order_id)

    def for_session(self, session_date):
        return [r for r in self._rows.values()
                if r.session_date == session_date]


def _mark_never_placed(row: ExternalOrderRecord, observed_at: str) -> None:
    """Record that the venue confirms this order does not exist.

    Two guards, both of which exist because the alternative is silently
    wrong rather than loudly wrong:

    A row with a fill cannot be "never placed" - something filled it,
    so the record and the conclusion contradict each other and the
    conclusion is the one that must give way.

    A row whose outcome was already observed cannot be "never placed"
    either: we saw it at the venue. Treating a later absence as proof of
    non-existence would release the reservation on an order we have
    watched execute.
    """
    if (row.filled_quantity or 0.0) > 0:
        raise OrderLedgerError(
            f"{row.client_order_id} has a recorded fill of "
            f"{row.filled_quantity}; an order that filled cannot be "
            f"marked as never placed")
    if row.submission_outcome_known:
        raise OrderLedgerError(
            f"{row.client_order_id} was already observed at the venue "
            f"with status {row.status!r}; a later absence is a "
            f"contradiction, not proof it was never placed")
    row.status = NEVER_PLACED
    row.raw_status = None          # the venue never said this
    row.remaining_quantity = 0.0
    row.last_observed_at = observed_at
    row.observations += 1
    row.submission_outcome_known = True


def _apply(row: ExternalOrderRecord, order: Dict, observed_at: str) -> None:
    """Fold a broker observation into the record.

    Out-of-order observations are handled by never letting filled
    quantity go backwards: a stale response that reports less filled
    than we have already seen must not un-fill a position.
    """
    row.broker_order_id = order.get("order_id") or row.broker_order_id
    row.status = order.get("status") or row.status
    row.raw_status = order.get("raw_status") or row.raw_status
    seen = float(order.get("filled_quantity") or 0.0)
    row.filled_quantity = max(row.filled_quantity or 0.0, seen)
    if order.get("remaining_quantity") is not None:
        row.remaining_quantity = order["remaining_quantity"]
    if order.get("average_fill_price") is not None:
        row.average_fill_price = order["average_fill_price"]
    row.submitted_at = order.get("submitted_at") or row.submitted_at
    row.last_observed_at = observed_at
    row.observations += 1
    row.submission_outcome_known = True


class DynamoDBOrderLedger(OrderLedger):
    """PK = EXTORDERS#<session_date>, SK = <client_order_id>.

    Keyed by client order id because that is the handle that exists
    before the broker has answered. A lost response leaves a row that
    can be matched against the venue.

    Reads RAISE rather than returning []. An unreadable order ledger
    means exposure is unknown, and a caller that saw an empty list would
    conclude the opposite.
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

    def record_intent(self, record: ExternalOrderRecord) -> None:
        """Write the intent before the order is sent.

        Conditional on absence, so a retry of the same deterministic id
        cannot overwrite a record that already carries observations.
        """
        try:
            self.client.put_item(
                TableName=self.table_name,
                Item={"PK": {"S": f"EXTORDERS#{record.session_date}"},
                      "SK": {"S": record.client_order_id},
                      "payload": {"S": json.dumps(
                          {k: getattr(record, k)
                           for k in record.__dataclass_fields__},
                          default=str)}},
                ConditionExpression=(
                    "attribute_not_exists(PK) AND attribute_not_exists(SK)"))
        except Exception as exc:                          # noqa: BLE001
            if "ConditionalCheckFailed" in type(exc).__name__:
                return
            raise OrderLedgerError(
                f"could not record order intent for "
                f"{record.client_order_id}: {type(exc).__name__}") from None

    def record_observation(self, client_order_id, order, observed_at,
                           session_date=None):
        row = self.get(client_order_id, session_date=session_date)
        if row is None:
            raise OrderLedgerError(
                f"no intent recorded for {client_order_id}; an order "
                f"observed without an intent cannot be attributed")
        _apply(row, order, observed_at)
        try:
            self.client.put_item(
                TableName=self.table_name,
                Item={"PK": {"S": f"EXTORDERS#{row.session_date}"},
                      "SK": {"S": row.client_order_id},
                      "payload": {"S": json.dumps(
                          {k: getattr(row, k)
                           for k in row.__dataclass_fields__},
                          default=str)}})
        except Exception as exc:                          # noqa: BLE001
            raise OrderLedgerError(
                f"could not record observation for {client_order_id}: "
                f"{type(exc).__name__}") from None
        return row

    def record_never_placed(self, client_order_id, observed_at,
                            session_date=None):
        row = self.get(client_order_id, session_date=session_date)
        if row is None:
            raise OrderLedgerError(
                f"no intent recorded for {client_order_id}")
        _mark_never_placed(row, observed_at)
        try:
            self.client.put_item(
                TableName=self.table_name,
                Item={"PK": {"S": f"EXTORDERS#{row.session_date}"},
                      "SK": {"S": row.client_order_id},
                      "payload": {"S": json.dumps(
                          {k: getattr(row, k)
                           for k in row.__dataclass_fields__},
                          default=str)}})
        except Exception as exc:                          # noqa: BLE001
            raise OrderLedgerError(
                f"could not record absence for {client_order_id}: "
                f"{type(exc).__name__}") from None
        return row

    def get(self, client_order_id, session_date=None):
        """The record, or None where absence is ESTABLISHED.

        With a session_date this reads that one partition, and a miss is
        proof the row is not there.

        Without one it can only guess, because the partition key is the
        session. It searches a short window of recent dates, and if the
        row is not in any of them it RAISES rather than returning None -
        because "not in the last few days" is not "does not exist", and
        the caller that asks this question is deciding whether an order
        was ever placed. That conflation was real: this method used to
        scan the window unconditionally, so an order outside it read as
        absent. Validation against the dev table on 2026-10-02 found it,
        and the comment that used to sit here claimed the method did not
        guess the date while it was guessing the date.
        """
        if session_date:
            try:
                r = self.client.get_item(
                    TableName=self.table_name,
                    Key={"PK": {"S": f"EXTORDERS#{session_date}"},
                         "SK": {"S": client_order_id}},
                    ConsistentRead=True)
            except Exception as exc:                      # noqa: BLE001
                raise OrderLedgerError(
                    f"order ledger unreadable: {type(exc).__name__}"
                ) from None
            item = r.get("Item")
            if not item:
                return None            # absence, within a known partition
            return ExternalOrderRecord.from_dict(
                json.loads(item["payload"]["S"]))

        for date in self._candidate_dates():
            try:
                r = self.client.get_item(
                    TableName=self.table_name,
                    Key={"PK": {"S": f"EXTORDERS#{date}"},
                         "SK": {"S": client_order_id}},
                    ConsistentRead=True)
            except Exception as exc:                      # noqa: BLE001
                raise OrderLedgerError(
                    f"order ledger unreadable: {type(exc).__name__}"
                ) from None
            item = r.get("Item")
            if item:
                return ExternalOrderRecord.from_dict(
                    json.loads(item["payload"]["S"]))
        raise OrderLedgerError(
            f"{client_order_id} was not found in the last "
            f"{len(self._candidate_dates())} session partitions and no "
            f"session_date was supplied, so its absence is NOT "
            f"established; pass the session_date to ask a question that "
            f"can be answered")

    def _candidate_dates(self) -> List[str]:
        from datetime import datetime, timedelta, timezone
        today = datetime.now(timezone.utc).date()
        return [str(today - timedelta(days=d)) for d in range(0, 4)]

    def for_session(self, session_date):
        try:
            r = self.client.query(
                TableName=self.table_name,
                KeyConditionExpression="PK = :pk",
                ExpressionAttributeValues={
                    ":pk": {"S": f"EXTORDERS#{session_date}"}},
                ConsistentRead=True)
        except Exception as exc:                          # noqa: BLE001
            raise OrderLedgerError(
                f"order ledger unreadable for {session_date}: "
                f"{type(exc).__name__}") from None
        out = []
        for item in r.get("Items", []):
            try:
                out.append(ExternalOrderRecord.from_dict(
                    json.loads(item["payload"]["S"])))
            except Exception:                             # noqa: BLE001
                # One unreadable row must not hide the rest, and must not
                # vanish silently either.
                raise OrderLedgerError(
                    f"an order row under {session_date} could not be "
                    f"parsed; exposure cannot be established from a "
                    f"partial ledger")
        return out

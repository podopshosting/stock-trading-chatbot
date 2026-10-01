"""
Broker abstraction and the paper implementation.

The strategy and risk layers never know which broker is underneath,
which is what makes the paper record comparable with a live one.
"""
from .base import BrokerAdapter
from .live_contract import (
    REQUIREMENT_RATIONALE, AdapterAssessment, Requirement,
    assess_adapter,
)
from .store import (
    BrokerStateError, BrokerStateStore, ConcurrentBrokerUpdate,
    DynamoDBBrokerStateStore, InMemoryBrokerStateStore, restore,
    serialise,
)
from .execution import (
    ExecutionRefused, MARKETABLE_LIMIT_BUFFER_PCT, OrderProposal,
    build_proposal, submit_approved,
)
from .models import (
    Account, BrokerError, BrokerPosition, Fill, Order, OrderRejected,
    OrderSide, OrderStatus, OrderType, Quote, RejectReason, TimeInForce,
)
from .paper import PaperBroker, PaperBrokerConfig

__all__ = [
    "REQUIREMENT_RATIONALE", "AdapterAssessment", "Requirement",
    "assess_adapter",
    "BrokerStateError", "BrokerStateStore", "ConcurrentBrokerUpdate",
    "DynamoDBBrokerStateStore", "InMemoryBrokerStateStore",
    "restore", "serialise",
    "Account", "BrokerAdapter", "BrokerError", "BrokerPosition",
    "ExecutionRefused", "Fill", "MARKETABLE_LIMIT_BUFFER_PCT", "Order",
    "OrderProposal", "OrderRejected", "OrderSide", "OrderStatus",
    "OrderType", "PaperBroker", "PaperBrokerConfig", "Quote",
    "RejectReason", "TimeInForce", "build_proposal", "submit_approved",
]

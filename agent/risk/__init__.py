"""
Risk Governor.

The safety boundary. The only component that can approve anything, and
its rejection is absolute: approval is derived from the absence of
rejection codes, so there is no path by which a model, a prompt or a
caller can override a no.
"""
from .governor import evaluate, position_size, should_engage_risk_lock
from .halt_store import (
    DynamoDBHaltStore, HaltStore, HaltStoreError, InMemoryHaltStore,
)
from .models import (
    HALT_CODES, GlobalHaltState, HaltScope, RejectionCode, RiskContext,
    RiskDecision, RiskLimits,
)

__all__ = [
    "HALT_CODES", "DynamoDBHaltStore", "GlobalHaltState", "HaltScope",
    "HaltStore", "HaltStoreError", "InMemoryHaltStore", "RejectionCode",
    "RiskContext", "RiskDecision", "RiskLimits", "evaluate", "position_size",
    "should_engage_risk_lock",
]

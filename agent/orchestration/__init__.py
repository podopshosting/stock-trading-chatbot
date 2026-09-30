"""
Market-day orchestration.

The ordering rule is the design:

    reconcile -> exits -> entries

Reconciliation first, because acting on a position set that disagrees
with the broker is acting on fiction. Exits second, because if the
process dies after that point risk has already been reduced. Entries
last, because they are the only step that adds risk and the only step
that is safe to skip.

Every failure path leads to the same place: exits continue, entries
stop. There is no failure mode in which the agent keeps opening
positions while something is wrong.
"""
from .day import (
    CONFIG_VERSION, OPENING_MINUTES, PRE_CLOSE_MINUTES,
    MarketDayOrchestrator, resolve_phase,
)
from .lock import (
    DEFAULT_TTL_SECONDS, CycleLock, DynamoDBCycleLock, InMemoryCycleLock,
)
from .models import (
    CycleOutcome, CyclePhase, CycleResult, CycleStep, HaltReason,
)

__all__ = [
    "CONFIG_VERSION", "DEFAULT_TTL_SECONDS", "OPENING_MINUTES",
    "PRE_CLOSE_MINUTES", "CycleLock", "CycleOutcome", "CyclePhase",
    "CycleResult", "CycleStep", "DynamoDBCycleLock", "HaltReason",
    "InMemoryCycleLock", "MarketDayOrchestrator", "resolve_phase",
]

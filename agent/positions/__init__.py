"""
Position management and the exit engine.

The asymmetry worth knowing before reading further: for a NEW entry the
safe direction is to do nothing, but for an OPEN position the safe
direction is to get out. Missing data blocks an entry and forces an
exit. The two are not the same kind of caution.
"""
from .exits import (
    CONFIG_VERSION, MAX_QUOTE_AGE_SECONDS, TRAILING_ACTIVATION_PCT,
    ExitContext, apply_trailing_stop, evaluate, stop_gap_disclosure,
    trailing_stop_price, update_high_water, update_low_water,
)
from .manager import PositionManager, PositionManagerError
from .models import (
    EXIT_PRIORITY, PROTECTIVE_REASONS, ExitIntent, ExitPlan, ExitReason,
    ManagedPosition, PositionState, ReconciliationResult, StopMechanism,
    StopWidened,
)

__all__ = [
    "CONFIG_VERSION", "EXIT_PRIORITY", "MAX_QUOTE_AGE_SECONDS",
    "PROTECTIVE_REASONS", "TRAILING_ACTIVATION_PCT", "ExitContext",
    "ExitIntent", "ExitPlan", "ExitReason", "ManagedPosition",
    "PositionManager", "PositionManagerError", "PositionState",
    "ReconciliationResult", "StopMechanism", "StopWidened",
    "apply_trailing_stop", "evaluate", "stop_gap_disclosure",
    "trailing_stop_price", "update_high_water", "update_low_water",
]

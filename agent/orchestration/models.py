"""
Orchestration types.

One idea shapes this whole package: the ORDER of work within a cycle is
a safety property, not an implementation detail.

A cycle does reconciliation, then exits, then entries. If the process
dies partway through - and on Lambda it will, eventually, at the worst
possible moment - then having done exits first means risk was reduced
before the failure. Doing entries first would mean risk was added and
the code that protects it never ran.

Every phase below therefore states explicitly whether it permits new
exposure, and the permission is derived from the phase rather than
passed alongside it.
"""
from __future__ import annotations

import enum
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional


class CyclePhase(str, enum.Enum):
    """What part of the day a cycle is running in."""
    PRE_MARKET = "PRE_MARKET"
    OPENING = "OPENING"          # first minutes; wide spreads, no entries
    INTRADAY = "INTRADAY"        # the only phase that opens positions
    PRE_CLOSE = "PRE_CLOSE"      # exits only; flatten window
    CLOSED = "CLOSED"
    UNKNOWN = "UNKNOWN"          # provider unreachable; never assume open

    def __str__(self) -> str:
        return self.value

    @property
    def permits_new_exposure(self) -> bool:
        """Derived from the phase. There is no flag to override it.

        Only INTRADAY opens positions. The opening minutes are excluded
        because spreads are widest and the first print is often not a
        price anyone can trade; PRE_CLOSE is excluded because a position
        opened there cannot be given time to work before it must be
        flattened.
        """
        return self is CyclePhase.INTRADAY

    @property
    def permits_exits(self) -> bool:
        """Almost always true.

        A system that can open a position but not close one is far more
        dangerous than one that can do neither, so exits are permitted
        in every phase where the market can be reached at all.
        """
        return self in (CyclePhase.OPENING, CyclePhase.INTRADAY,
                        CyclePhase.PRE_CLOSE)

    @property
    def requires_flatten(self) -> bool:
        return self is CyclePhase.PRE_CLOSE


class CycleOutcome(str, enum.Enum):
    COMPLETED = "COMPLETED"
    COMPLETED_WITH_ERRORS = "COMPLETED_WITH_ERRORS"
    HALTED = "HALTED"
    SKIPPED_DUPLICATE = "SKIPPED_DUPLICATE"
    SKIPPED_MARKET_CLOSED = "SKIPPED_MARKET_CLOSED"
    ABORTED = "ABORTED"

    def __str__(self) -> str:
        return self.value


class HaltReason(str, enum.Enum):
    """Why new exposure stopped. Exits continue regardless."""
    BROKER_DIVERGENCE = "BROKER_DIVERGENCE"
    GLOBAL_HALT = "GLOBAL_HALT"
    HALT_STATE_UNREADABLE = "HALT_STATE_UNREADABLE"
    DAILY_LOSS_LIMIT = "DAILY_LOSS_LIMIT"
    TRADING_DISABLED = "TRADING_DISABLED"
    EXECUTION_UNAVAILABLE = "EXECUTION_UNAVAILABLE"
    MARKET_STATUS_UNKNOWN = "MARKET_STATUS_UNKNOWN"
    UNHANDLED_ERROR = "UNHANDLED_ERROR"
    CAPITAL_EXHAUSTED = "CAPITAL_EXHAUSTED"
    CONCURRENT_CYCLE = "CONCURRENT_CYCLE"

    def __str__(self) -> str:
        return self.value


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class CycleStep:
    """One unit of work within a cycle, and whether it succeeded.

    Recorded individually so a partial cycle is legible afterwards. "The
    cycle failed" is not useful; "reconciliation succeeded, exits
    succeeded, the scan failed" tells you the risk was managed before
    the failure.
    """
    name: str
    ok: bool
    detail: str = ""
    duration_ms: Optional[float] = None
    skipped: bool = False
    skip_reason: str = ""

    def as_dict(self) -> Dict:
        return asdict(self)


@dataclass
class CycleResult:
    """What one cycle did."""
    cycle_id: str
    session_date: str
    phase: CyclePhase
    outcome: CycleOutcome
    started_at: str = field(default_factory=utcnow)
    finished_at: Optional[str] = None

    steps: List[CycleStep] = field(default_factory=list)
    halt_reasons: List[HaltReason] = field(default_factory=list)

    positions_reconciled: bool = False
    exits_evaluated: int = 0
    exits_submitted: int = 0
    exits_failed: int = 0
    symbols_scanned: int = 0
    hypotheses_generated: int = 0
    decisions_approved: int = 0
    entries_submitted: int = 0
    entries_failed: int = 0
    trades_journalled: int = 0

    open_positions: int = 0
    capital_deployed: float = 0.0
    errors: List[str] = field(default_factory=list)
    config_version: str = ""

    @staticmethod
    def make_id() -> str:
        return f"cycle_{uuid.uuid4().hex[:16]}"

    @property
    def new_exposure_permitted(self) -> bool:
        """Derived from the phase AND the halts. No setter.

        Both conditions must hold, so a halt cannot be cleared by
        changing the phase and a wrong phase cannot be excused by an
        empty halt list.
        """
        return self.phase.permits_new_exposure and not self.halt_reasons

    @property
    def halted(self) -> bool:
        return bool(self.halt_reasons)

    @property
    def risk_was_managed(self) -> bool:
        """Did the protective half of the cycle complete?

        This is the question that matters after a failure. A cycle that
        reconciled and ran its exits before dying left the account in a
        known state; one that died earlier did not.
        """
        names = {s.name: s for s in self.steps}
        reconcile = names.get("reconcile")
        exits = names.get("manage_exits")
        return (reconcile is not None and reconcile.ok
                and exits is not None and (exits.ok or exits.skipped))

    def add_step(self, name: str, ok: bool, detail: str = "",
                 skipped: bool = False, skip_reason: str = "") -> CycleStep:
        step = CycleStep(name=name, ok=ok, detail=detail, skipped=skipped,
                         skip_reason=skip_reason)
        self.steps.append(step)
        return step

    def halt(self, reason: HaltReason, detail: str = "") -> None:
        if reason not in self.halt_reasons:
            self.halt_reasons.append(reason)
        if detail:
            self.errors.append(f"{reason}: {detail}")

    def as_dict(self) -> Dict:
        return {
            "cycle_id": self.cycle_id,
            "session_date": self.session_date,
            "phase": str(self.phase),
            "outcome": str(self.outcome),
            "new_exposure_permitted": self.new_exposure_permitted,
            "halted": self.halted,
            "halt_reasons": [str(r) for r in self.halt_reasons],
            "risk_was_managed": self.risk_was_managed,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "steps": [s.as_dict() for s in self.steps],
            "positions_reconciled": self.positions_reconciled,
            "exits_evaluated": self.exits_evaluated,
            "exits_submitted": self.exits_submitted,
            "exits_failed": self.exits_failed,
            "symbols_scanned": self.symbols_scanned,
            "hypotheses_generated": self.hypotheses_generated,
            "decisions_approved": self.decisions_approved,
            "entries_submitted": self.entries_submitted,
            "entries_failed": self.entries_failed,
            "trades_journalled": self.trades_journalled,
            "open_positions": self.open_positions,
            "capital_deployed": round(self.capital_deployed, 2),
            "errors": self.errors,
            "config_version": self.config_version,
        }

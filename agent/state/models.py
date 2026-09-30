"""
Agent state model.

Two rules:

1. **A state is an enum member, never a string.** An agent that can be put
   into `"SCANNNIG"` by a typo has no state machine.
2. **Transitions are explicitly allowed or rejected.** A move that is not
   in the table raises, rather than quietly succeeding and leaving the
   agent somewhere no code expects.

The safety states (`EMERGENCY_STOP`, `DAILY_RISK_LOCK`) are reachable from
anywhere, because a stop that depends on already being in a tidy state is
not a stop.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Dict, List, Optional, Set, Tuple


class AgentState(enum.Enum):
    OFFLINE = "OFFLINE"
    PRE_MARKET = "PRE_MARKET"
    SCANNING = "SCANNING"
    WATCHING = "WATCHING"
    TRADE_CANDIDATE = "TRADE_CANDIDATE"
    PAPER_ORDER_PENDING = "PAPER_ORDER_PENDING"
    POSITION_OPEN = "POSITION_OPEN"
    POSITION_EXITING = "POSITION_EXITING"
    DAILY_RISK_LOCK = "DAILY_RISK_LOCK"
    MARKET_CLOSED = "MARKET_CLOSED"
    EMERGENCY_STOP = "EMERGENCY_STOP"

    def __str__(self) -> str:
        return self.value

    @classmethod
    def parse(cls, value) -> "AgentState":
        if isinstance(value, cls):
            return value
        try:
            return cls(str(value).strip().upper())
        except ValueError as e:
            raise ValueError(
                f"unknown agent state {value!r}; valid: "
                f"{[s.value for s in cls]}"
            ) from e


class MarketSession(enum.Enum):
    PRE_MARKET = "PRE_MARKET"
    OPEN = "OPEN"
    AFTER_HOURS = "AFTER_HOURS"
    CLOSED = "CLOSED"
    UNKNOWN = "UNKNOWN"          # provider unreachable; never assume open

    def __str__(self) -> str:
        return self.value

    @classmethod
    def parse(cls, value) -> "MarketSession":
        if isinstance(value, cls):
            return value
        try:
            return cls(str(value).strip().upper())
        except ValueError:
            return cls.UNKNOWN


# Reachable from any state. A halt that requires a tidy starting point is
# not a halt.
_ALWAYS_REACHABLE: Set[AgentState] = {
    AgentState.EMERGENCY_STOP,
    AgentState.DAILY_RISK_LOCK,
}

# States that only later milestones produce. Declared so the model is
# complete, but nothing in Milestone 3 drives the agent into them.
TRADE_STATES: Set[AgentState] = {
    AgentState.WATCHING,
    AgentState.TRADE_CANDIDATE,
    AgentState.PAPER_ORDER_PENDING,
    AgentState.POSITION_OPEN,
    AgentState.POSITION_EXITING,
}

# States that take on or extend exposure. A daily risk lock must block
# every one of these for the rest of the session.
#
# POSITION_EXITING is deliberately NOT here: when the lock fires, open
# positions still have to be wound down, so the lock forbids *opening*
# exposure, not closing it. A lock that also blocked exits would trap the
# agent in its positions, which is the opposite of risk control.
EXPOSURE_INCREASING_STATES: Set[AgentState] = {
    AgentState.SCANNING,
    AgentState.WATCHING,
    AgentState.TRADE_CANDIDATE,
    AgentState.PAPER_ORDER_PENDING,
    AgentState.POSITION_OPEN,
}

_ALLOWED: Dict[AgentState, Set[AgentState]] = {
    AgentState.OFFLINE: {
        AgentState.PRE_MARKET, AgentState.SCANNING, AgentState.MARKET_CLOSED,
    },
    AgentState.PRE_MARKET: {
        AgentState.SCANNING, AgentState.MARKET_CLOSED, AgentState.OFFLINE,
    },
    AgentState.SCANNING: {
        AgentState.WATCHING, AgentState.TRADE_CANDIDATE,
        AgentState.MARKET_CLOSED, AgentState.OFFLINE,
    },
    AgentState.WATCHING: {
        AgentState.SCANNING, AgentState.TRADE_CANDIDATE,
        AgentState.MARKET_CLOSED,
    },
    AgentState.TRADE_CANDIDATE: {
        AgentState.PAPER_ORDER_PENDING, AgentState.SCANNING,
        AgentState.WATCHING, AgentState.MARKET_CLOSED,
    },
    AgentState.PAPER_ORDER_PENDING: {
        AgentState.POSITION_OPEN, AgentState.SCANNING,
        AgentState.MARKET_CLOSED,
    },
    AgentState.POSITION_OPEN: {
        AgentState.POSITION_EXITING, AgentState.MARKET_CLOSED,
    },
    AgentState.POSITION_EXITING: {
        AgentState.SCANNING, AgentState.POSITION_OPEN,
        AgentState.MARKET_CLOSED,
    },
    # A risk lock holds for the session. It may release to MARKET_CLOSED at
    # the bell, or to OFFLINE on a new session, but never back to scanning
    # on the same day.
    AgentState.DAILY_RISK_LOCK: {
        AgentState.MARKET_CLOSED, AgentState.OFFLINE,
        AgentState.POSITION_EXITING,
    },
    AgentState.MARKET_CLOSED: {
        AgentState.OFFLINE, AgentState.PRE_MARKET,
    },
    # EMERGENCY_STOP is deliberately terminal for the process. Clearing it
    # is a human act (clear_emergency_stop), not a transition the agent can
    # make for itself.
    AgentState.EMERGENCY_STOP: set(),
}


class InvalidTransition(Exception):
    def __init__(self, source: AgentState, target: AgentState, reason: str = ""):
        super().__init__(
            f"invalid transition {source} -> {target}"
            + (f": {reason}" if reason else "")
        )
        self.source = source
        self.target = target


def allowed_targets(source: AgentState) -> Set[AgentState]:
    return set(_ALLOWED.get(source, set())) | _ALWAYS_REACHABLE


def can_transition(source: AgentState, target: AgentState) -> bool:
    if source is target:
        return True                      # idempotent re-assertion
    return target in allowed_targets(source)


def assert_transition(source: AgentState, target: AgentState) -> None:
    if can_transition(source, target):
        return
    reason = ""
    if source is AgentState.EMERGENCY_STOP:
        reason = ("emergency stop must be cleared explicitly, it is not "
                  "exited by a transition")
    elif source is AgentState.DAILY_RISK_LOCK and target in TRADE_STATES:
        reason = "the risk lock holds for the remainder of the session"
    raise InvalidTransition(source, target, reason)


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class StateTransition:
    source: str
    target: str
    at: str
    reason: str = ""

    def as_dict(self) -> Dict:
        return asdict(self)


@dataclass
class RegimeTransition:
    previous_regime: str
    new_regime: str
    previous_score: Optional[float]
    new_score: Optional[float]
    at: str
    reason: str = ""

    def as_dict(self) -> Dict:
        return asdict(self)


@dataclass
class AgentSession:
    """One trading day of agent state.

    `trading_enabled` defaults to False and Phase 1 has no execution path
    at all. The P&L and capital fields exist so the shape is stable for
    later milestones; they stay at zero here.
    """

    session_date: str
    agent_state: AgentState = AgentState.OFFLINE
    trading_enabled: bool = False
    emergency_stop: bool = False
    daily_risk_lock: bool = False

    market_status: MarketSession = MarketSession.UNKNOWN

    market_regime: str = "UNKNOWN"
    market_regime_confidence: float = 0.0
    market_regime_score: Optional[float] = None
    risk_posture: str = "NO_NEW_TRADES"
    regime_updated_at: Optional[str] = None
    regime_detail: Dict = field(default_factory=dict)

    last_scan_at: Optional[str] = None

    daily_capital_limit: float = 50.00
    capital_deployed: float = 0.00
    realized_pnl: float = 0.00
    unrealized_pnl: float = 0.00
    open_positions: int = 0
    orders_pending: int = 0

    state_history: List[Dict] = field(default_factory=list)
    regime_history: List[Dict] = field(default_factory=list)

    created_at: str = field(default_factory=utcnow)
    updated_at: str = field(default_factory=utcnow)
    revision: int = 0

    # -- serialisation ----------------------------------------------------

    def as_dict(self) -> Dict:
        d = asdict(self)
        d["agent_state"] = self.agent_state.value
        d["market_status"] = self.market_status.value
        return d

    @classmethod
    def from_dict(cls, d: Dict) -> "AgentSession":
        d = dict(d)
        d["agent_state"] = AgentState.parse(d.get("agent_state", "OFFLINE"))
        d["market_status"] = MarketSession.parse(d.get("market_status", "UNKNOWN"))
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})

    # -- mutation ---------------------------------------------------------

    def _touch(self) -> None:
        self.updated_at = utcnow()
        self.revision += 1

    def transition_to(self, target: AgentState, reason: str = "") -> bool:
        """Move state. Raises InvalidTransition if the move is not allowed.

        Returns True if the state actually changed.
        """
        target = AgentState.parse(target)
        assert_transition(self.agent_state, target)
        if target is self.agent_state:
            return False

        record = StateTransition(
            source=self.agent_state.value, target=target.value,
            at=utcnow(), reason=reason,
        )
        self.agent_state = target
        self.state_history.append(record.as_dict())

        if target is AgentState.EMERGENCY_STOP:
            self.emergency_stop = True
            self.trading_enabled = False
        elif target is AgentState.DAILY_RISK_LOCK:
            self.daily_risk_lock = True

        self._touch()
        return True

    def trigger_emergency_stop(self, reason: str) -> None:
        """Always permitted, and idempotent: flags are set even if the
        state is already EMERGENCY_STOP, so a repeated call cannot leave
        trading enabled."""
        self.emergency_stop = True
        self.trading_enabled = False
        if self.agent_state is not AgentState.EMERGENCY_STOP:
            self.transition_to(AgentState.EMERGENCY_STOP, reason)
        else:
            self._touch()

    def clear_emergency_stop(self, reason: str = "") -> None:
        """Deliberately separate from transition_to: leaving a halt is a
        human decision. Trading stays disabled afterwards."""
        self.emergency_stop = False
        self.agent_state = AgentState.OFFLINE
        self.trading_enabled = False
        self.state_history.append(StateTransition(
            source=AgentState.EMERGENCY_STOP.value,
            target=AgentState.OFFLINE.value,
            at=utcnow(),
            reason=reason or "emergency stop cleared",
        ).as_dict())
        self._touch()

    def set_daily_risk_lock(self, reason: str) -> None:
        self.daily_risk_lock = True
        if self.agent_state is not AgentState.DAILY_RISK_LOCK:
            self.transition_to(AgentState.DAILY_RISK_LOCK, reason)
        else:
            self._touch()

    def record_regime(self, regime: str, score: Optional[float],
                      confidence: float, risk_posture: str,
                      detail: Optional[Dict] = None,
                      min_score_delta: float = 0.10) -> Optional[RegimeTransition]:
        """Store a regime evaluation, recording a transition only when the
        change is meaningful.

        A label change with a tiny score move is usually a boundary
        wobble, not a new environment, so it is suppressed to keep the
        history readable.
        """
        previous_regime = self.market_regime
        previous_score = self.market_regime_score

        transition = None
        label_changed = regime != previous_regime
        if label_changed:
            significant = (
                previous_score is None
                or score is None
                or abs(score - previous_score) >= min_score_delta
                # A move to or from UNKNOWN is always worth recording: it
                # means data availability changed, not just magnitude.
                or "UNKNOWN" in (regime, previous_regime)
            )
            if significant:
                transition = RegimeTransition(
                    previous_regime=previous_regime,
                    new_regime=regime,
                    previous_score=previous_score,
                    new_score=score,
                    at=utcnow(),
                    reason=(detail or {}).get("primary_reason", ""),
                )
                self.regime_history.append(transition.as_dict())

        self.market_regime = regime
        self.market_regime_score = score
        self.market_regime_confidence = confidence
        self.risk_posture = risk_posture
        self.regime_detail = detail or {}
        self.regime_updated_at = utcnow()
        self._touch()
        return transition

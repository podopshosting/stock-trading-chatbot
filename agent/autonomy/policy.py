"""
The autonomy policy.

Autonomy here means: no individual PAPER trade needs a human. It does
not mean the agent decides what it is allowed to do. The boundary
between those is this module, and it is written as code rather than
convention because a convention is something a future change can
quietly stop honouring.

Two separate questions were previously folded into one boolean:

    "is trading on?"      and      "can this reach real money?"

`ExecutionMode` separates them. PAPER is a mode in which the agent
trades freely inside its risk envelope against a simulated or paper
venue. LIVE exists in the enum only so that it can be named and refused
- it is not constructible as an authorised mode, and nothing in this
package will ever select it.

The asymmetry is deliberate. The agent has wide latitude over actions
that REDUCE or SIZE risk inside fixed ceilings, and none over the
ceilings themselves.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Dict, FrozenSet, Optional


class ExecutionMode(str, enum.Enum):
    DISABLED = "DISABLED"
    PAPER = "PAPER"
    LIVE = "LIVE"

    def __str__(self) -> str:
        return self.value

    @classmethod
    def parse(cls, value) -> "ExecutionMode":
        """Parse a configured value, failing CLOSED.

        Anything unrecognised - a typo, an empty string, a missing
        variable - becomes DISABLED rather than raising or defaulting
        to something permissive. A misconfigured deployment should stop
        trading, not guess.
        """
        if isinstance(value, cls):
            return value
        try:
            return cls(str(value or "").strip().upper())
        except ValueError:
            return cls.DISABLED


class LiveExecutionRefused(Exception):
    """Raised on any attempt to operate in LIVE mode.

    This is not a configuration error to be handled and continued past.
    """


class PolicyViolation(Exception):
    """An action the autonomy policy forbids."""


class Action(str, enum.Enum):
    """Things the agent might try to do. Each is either permitted
    autonomously or forbidden outright; none is 'permitted with
    approval', because an approval step is exactly what autonomous
    operation removes and a half-measure would be a gap."""

    # --- permitted autonomously ---------------------------------------
    INITIALISE_SESSION = "INITIALISE_SESSION"
    EVALUATE_REGIME = "EVALUATE_REGIME"
    SCAN_UNIVERSE = "SCAN_UNIVERSE"
    GENERATE_SIGNALS = "GENERATE_SIGNALS"
    COLLECT_EVIDENCE = "COLLECT_EVIDENCE"
    FORM_HYPOTHESIS = "FORM_HYPOTHESIS"
    REJECT_HYPOTHESIS = "REJECT_HYPOTHESIS"
    SUBMIT_PAPER_ORDER = "SUBMIT_PAPER_ORDER"
    CANCEL_PAPER_ORDER = "CANCEL_PAPER_ORDER"
    MANAGE_PAPER_POSITION = "MANAGE_PAPER_POSITION"
    TIGHTEN_STOP = "TIGHTEN_STOP"
    EXIT_PAPER_POSITION = "EXIT_PAPER_POSITION"
    FLATTEN_POSITIONS = "FLATTEN_POSITIONS"
    ENGAGE_RISK_LOCK = "ENGAGE_RISK_LOCK"
    ENGAGE_EMERGENCY_STOP = "ENGAGE_EMERGENCY_STOP"
    TAKE_ZERO_TRADES = "TAKE_ZERO_TRADES"
    JOURNAL_DECISION = "JOURNAL_DECISION"

    # --- forbidden outright -------------------------------------------
    ENABLE_REAL_EXECUTION = "ENABLE_REAL_EXECUTION"
    SELECT_LIVE_MODE = "SELECT_LIVE_MODE"
    RAISE_RISK_CEILING = "RAISE_RISK_CEILING"
    INCREASE_DAILY_CAPITAL = "INCREASE_DAILY_CAPITAL"
    ENABLE_MARGIN = "ENABLE_MARGIN"
    ENABLE_SHORTING = "ENABLE_SHORTING"
    ENABLE_OPTIONS = "ENABLE_OPTIONS"
    ALLOW_OVERNIGHT_POSITIONS = "ALLOW_OVERNIGHT_POSITIONS"
    CHANGE_BROKER = "CHANGE_BROKER"
    DISABLE_RECONCILIATION = "DISABLE_RECONCILIATION"
    CLEAR_CRITICAL_HALT = "CLEAR_CRITICAL_HALT"
    WIDEN_STOP = "WIDEN_STOP"
    CLEAR_EMERGENCY_STOP = "CLEAR_EMERGENCY_STOP"
    EDIT_OWN_POLICY = "EDIT_OWN_POLICY"

    def __str__(self) -> str:
        return self.value


PERMITTED: FrozenSet[Action] = frozenset({
    Action.INITIALISE_SESSION, Action.EVALUATE_REGIME,
    Action.SCAN_UNIVERSE, Action.GENERATE_SIGNALS,
    Action.COLLECT_EVIDENCE, Action.FORM_HYPOTHESIS,
    Action.REJECT_HYPOTHESIS, Action.SUBMIT_PAPER_ORDER,
    Action.CANCEL_PAPER_ORDER, Action.MANAGE_PAPER_POSITION,
    Action.TIGHTEN_STOP, Action.EXIT_PAPER_POSITION,
    Action.FLATTEN_POSITIONS, Action.ENGAGE_RISK_LOCK,
    Action.ENGAGE_EMERGENCY_STOP, Action.TAKE_ZERO_TRADES,
    Action.JOURNAL_DECISION,
})

FORBIDDEN: FrozenSet[Action] = frozenset(
    a for a in Action if a not in PERMITTED)

# Actions that are asymmetric: the agent may INVOKE a protection but may
# never LIFT one. Lifting a halt is how a system talks itself back into
# the situation that caused it.
PROTECTIONS_AGENT_MAY_ENGAGE_BUT_NOT_CLEAR = frozenset({
    Action.ENGAGE_RISK_LOCK, Action.ENGAGE_EMERGENCY_STOP,
})

# A forbidden action must remain forbidden whatever mode is active.
assert not (PERMITTED & FORBIDDEN)
assert set(Action) == PERMITTED | FORBIDDEN


@dataclass(frozen=True)
class AutonomyPolicy:
    """What the agent may do, in a given mode.

    Frozen: the agent cannot edit its own permissions, and EDIT_OWN_POLICY
    is itself a forbidden action so the prohibition is stated twice.
    """
    mode: ExecutionMode = ExecutionMode.DISABLED

    def __post_init__(self):
        if self.mode is ExecutionMode.LIVE:
            # Refused at construction, not at use. A policy object that
            # exists in LIVE mode is already a mistake, and failing here
            # means nothing downstream ever sees one.
            raise LiveExecutionRefused(
                "LIVE execution is not implemented and not authorised. "
                "No component of this system may operate in LIVE mode, "
                "and enabling it is a decision for the account holder.")

    @property
    def paper_trading_enabled(self) -> bool:
        return self.mode is ExecutionMode.PAPER

    @property
    def live_trading_enabled(self) -> bool:
        """Always False. A property, not a field: nothing can set it."""
        return False

    @property
    def may_open_new_exposure(self) -> bool:
        """Necessary, not sufficient: health and the Risk Governor
        must also agree."""
        return self.paper_trading_enabled

    def permits(self, action: Action) -> bool:
        if action in FORBIDDEN:
            return False
        # While DISABLED the agent may still reduce risk and journal,
        # but must not originate anything.
        if self.mode is ExecutionMode.DISABLED:
            return action in {
                Action.INITIALISE_SESSION, Action.EVALUATE_REGIME,
                Action.EXIT_PAPER_POSITION, Action.FLATTEN_POSITIONS,
                Action.CANCEL_PAPER_ORDER, Action.TIGHTEN_STOP,
                Action.ENGAGE_RISK_LOCK, Action.ENGAGE_EMERGENCY_STOP,
                Action.JOURNAL_DECISION, Action.TAKE_ZERO_TRADES,
                Action.MANAGE_PAPER_POSITION, Action.SCAN_UNIVERSE,
                Action.GENERATE_SIGNALS, Action.COLLECT_EVIDENCE,
                Action.FORM_HYPOTHESIS, Action.REJECT_HYPOTHESIS,
            }
        return action in PERMITTED

    def require(self, action: Action) -> None:
        """Raise unless permitted. For call sites that should not
        proceed on a refusal."""
        if not self.permits(action):
            raise PolicyViolation(
                f"{action} is not permitted in {self.mode} mode"
                + (" (forbidden outright)" if action in FORBIDDEN else ""))

    def as_dict(self) -> Dict:
        return {
            "mode": str(self.mode),
            "paper_trading_enabled": self.paper_trading_enabled,
            "live_trading_enabled": self.live_trading_enabled,
            "real_money_disabled": True,
            "may_open_new_exposure": self.may_open_new_exposure,
            "permitted": sorted(str(a) for a in PERMITTED
                                if self.permits(a)),
            "forbidden": sorted(str(a) for a in FORBIDDEN),
        }


def policy_from_environment(value) -> AutonomyPolicy:
    """Build a policy from a configured string, failing closed.

    LIVE in the environment does not raise out of the Lambda in a way
    that could be retried into something; it is converted to DISABLED
    with the reason recorded, because a deployment that was told to go
    live must stop, not trade.
    """
    mode = ExecutionMode.parse(value)
    if mode is ExecutionMode.LIVE:
        return AutonomyPolicy(mode=ExecutionMode.DISABLED)
    return AutonomyPolicy(mode=mode)

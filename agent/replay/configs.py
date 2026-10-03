"""
Named replay configurations, and which ones may be deployed.

Some risk limits are structurally DOMINATED by others. With a $50 daily
capital ceiling and roughly $25 a position, a session affords about two
trades risking about $2 each - so the $5 daily loss limit cannot be
reached by ordinary 1R losses, and the three-per-day position cap cannot
be reached at all. Those guards are not broken; they are shadowed by the
ceiling above them, and they become relevant only when a single trade
loses MORE than planned, which the gap scenarios show can be four times
over.

That leaves a real problem for validation: a guard the harness can never
make fire is a guard nobody has tested. The answer is not to loosen the
live limits so everything fires - that would be changing the strategy to
suit the test. It is to run the guard under a configuration that exists
only to expose it, and to label that configuration so its results can
never be quoted as current-strategy performance.

Hence `deployable`. Exactly one configuration here is deployable, and it
is the one that changes nothing.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from ..risk import RiskLimits

# Stamped on every result from a non-deployable configuration.
TEST_ONLY_LABEL = "TEST_CONFIGURATION_ONLY"

DEFAULT_NAME = "DEFAULT_LIVE_CONFIG"


@dataclass
class ReplayConfigSpec:
    """A named set of deviations from the live paper parameters."""
    name: str
    purpose: str
    # Which guard this exists to make reachable. Empty for the default.
    exposes: str = ""
    # Only the parameters that must change. Anything else is noise that
    # makes the result harder to compare with the default.
    limit_overrides: Dict = field(default_factory=dict)
    engine_overrides: Dict = field(default_factory=dict)
    deployable: bool = False
    note: str = ""
    # The scenario this configuration must be paired with.
    #
    # A configuration cannot expose a guard on its own: raising the
    # capital ceiling does not create losses, and a tighter stop does
    # not create a gap. Verified by running the PAIR - the first
    # version of this matrix ran everything against the benign control
    # and three of the configurations reported their guard as
    # unreachable when the scenario was simply wrong.
    scenario: str = "grind_up"

    @property
    def is_default(self) -> bool:
        return not self.limit_overrides and not self.engine_overrides

    def limits(self) -> RiskLimits:
        """The risk limits this configuration implies.

        Built from a FRESH RiskLimits each time, so a configuration can
        never mutate the live defaults for anything else in the process.
        """
        base = RiskLimits()
        values = {f: getattr(base, f) for f in base.__dataclass_fields__}
        unknown = set(self.limit_overrides) - set(values)
        if unknown:
            raise KeyError(
                f"{self.name} overrides unknown risk limits: "
                f"{sorted(unknown)}")
        values.update(self.limit_overrides)
        return RiskLimits(**values)

    @property
    def config_hash(self) -> str:
        """Addresses the DEVIATIONS, not the whole config.

        Two runs with the same name and the same overrides are the same
        configuration; a renamed one with the same overrides is too, and
        should compare equal.
        """
        payload = json.dumps(
            {"limits": self.limit_overrides,
             "engine": self.engine_overrides},
            sort_keys=True)
        return hashlib.sha256(payload.encode()).hexdigest()[:16]

    def diff_from_default(self) -> List[str]:
        base = RiskLimits()
        out = []
        for key, value in sorted(self.limit_overrides.items()):
            out.append(f"risk.{key}: {getattr(base, key, '?')} -> {value}")
        for key, value in sorted(self.engine_overrides.items()):
            out.append(f"engine.{key}: -> {value}")
        return out

    def as_dict(self) -> Dict:
        return {
            "name": self.name,
            "purpose": self.purpose,
            "exposes": self.exposes,
            "deployable": self.deployable,
            "label": None if self.deployable else TEST_ONLY_LABEL,
            "config_hash": self.config_hash,
            "diff_from_default": self.diff_from_default(),
            "note": self.note,
        }


# --- the one that changes nothing -----------------------------------

DEFAULT_LIVE_CONFIG = ReplayConfigSpec(
    name=DEFAULT_NAME,
    purpose="the exact current paper parameters; the only configuration "
            "whose results describe the strategy as it actually runs",
    deployable=True)


# --- configurations that exist only to expose a guard ---------------
#
# Each changes the MINIMUM needed. Raising the capital ceiling also
# requires raising max_daily_capital, because RiskLimits refuses a
# ceiling above its own maximum - two parameters for one effect, which
# is the floor rather than a convenience.

DAILY_LOSS_LOCK_TEST = ReplayConfigSpec(
    name="DAILY_LOSS_LOCK_TEST",
    purpose="give a session enough capital for ordinary 1R losses to "
            "accumulate past the daily loss limit, which the live $50 "
            "ceiling prevents",
    exposes="DAILY_RISK_LOCK",
    limit_overrides={"daily_capital_limit": 500.0,
                     "max_daily_capital": 1000.0},
    scenario="consecutive_losses",
    note="the loss LIMIT is unchanged at $5; only the capital available "
         "to lose it moves, so the guard is tested at its real value")

MAX_NEW_POSITIONS_TEST = ReplayConfigSpec(
    name="MAX_NEW_POSITIONS_TEST",
    purpose="give a session enough capital for more than three entries, "
            "so the per-day position cap becomes independently "
            "reachable rather than shadowed by the capital ceiling",
    exposes="MAX_NEW_POSITIONS_REACHED",
    limit_overrides={"daily_capital_limit": 500.0,
                     "max_daily_capital": 1000.0},
    note="the cap itself is unchanged at 3 per day")

SPREAD_GATE_TEST = ReplayConfigSpec(
    name="SPREAD_GATE_TEST",
    purpose="quote a spread wider than the entry gate permits",
    exposes="SPREAD_TOO_WIDE",
    engine_overrides={"spread_pct": 5.0},
    scenario="grind_up",
    note="the gate's own threshold is unchanged; the MARKET is what moves")

LIQUIDITY_GATE_TEST = ReplayConfigSpec(
    name="LIQUIDITY_GATE_TEST",
    purpose="lower the dollar-volume floor so ordinary synthetic volume "
            "can fall below it without needing an implausible scenario",
    exposes="INSUFFICIENT_LIQUIDITY",
    limit_overrides={"min_dollar_volume": 1.0e12},
    note="raising the FLOOR is equivalent to lowering the volume and "
         "keeps the bar fixture honest")

TOO_LATE_TEST = ReplayConfigSpec(
    name="TOO_LATE_TEST",
    purpose="place the decision minutes from the close",
    exposes="TOO_LATE_IN_SESSION",
    engine_overrides={"minutes_to_close": 1.0})

# STALE_DATA_TEST was removed rather than kept as a no-op.
#
# Staleness is now the EXCESS over the expected bar interval, so a bar
# arriving on schedule is fresh at any declared interval and no
# interval override can manufacture it - the excess for an on-time bar
# is zero by construction. Only a HOLE produces stale data, and the
# trading_halt scenario produces one under the DEFAULT configuration.
# A named configuration that changes nothing would imply the guard
# needed special treatment to be reachable when it does not.
STALE_DATA_SCENARIO = "trading_halt"

GAP_RISK_TEST = ReplayConfigSpec(
    name="GAP_RISK_TEST",
    purpose="a tighter stop, so a gap of a given size overshoots it by "
            "more and the excess beyond planned risk is easier to read",
    exposes="stop_integrity",
    engine_overrides={"stop_distance_pct": 1.0},
    scenario="gap_through_stop",
    note="this does NOT make the system riskier in reality; it makes the "
         "overshoot arithmetic legible")


ALL: Dict[str, ReplayConfigSpec] = {
    c.name: c for c in (
        DEFAULT_LIVE_CONFIG, DAILY_LOSS_LOCK_TEST, MAX_NEW_POSITIONS_TEST,
        SPREAD_GATE_TEST, LIQUIDITY_GATE_TEST, TOO_LATE_TEST,
        GAP_RISK_TEST)}


def build(name: str) -> ReplayConfigSpec:
    if name not in ALL:
        raise KeyError(f"{name!r} is not a replay configuration; "
                       f"known: {sorted(ALL)}")
    return ALL[name]


def deployable_names() -> List[str]:
    return sorted(n for n, c in ALL.items() if c.deployable)

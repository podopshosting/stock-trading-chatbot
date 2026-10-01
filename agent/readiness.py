"""
The live-readiness gate.

A single question with a derived answer: may this system place a
real-money order?

Everything about this module assumes the answer is no and makes it
expensive to change. That bias is deliberate. The cost of a false
"not ready" is delay. The cost of a false "ready" is money, and the
specific way an automated system loses money is not a single large
mistake but a small one repeated faster than anyone notices.

Three design choices carry that bias:

**The verdict is derived, never assigned.** `ready` is a property
computed from the unmet gates. There is no field to set, no override
flag, and no "force" parameter anywhere in this file.

**Gates are not weighted and do not trade off.** A gate is not a score
contribution; it is a prerequisite. Fifteen of sixteen is not a pass,
because each gate is an independent way to lose money and a strong
showing elsewhere does not compensate for an open one.

**An unknown gate counts as unmet.** The system cannot be ready in a
respect it has not checked. Treating unknown as satisfied would make
the gate weaker the less it knew, which is exactly backwards.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence


class GateStatus(str, enum.Enum):
    MET = "MET"
    UNMET = "UNMET"
    UNKNOWN = "UNKNOWN"          # counts as unmet
    BLOCKED = "BLOCKED"          # cannot be met from here

    def __str__(self) -> str:
        return self.value

    @property
    def is_satisfied(self) -> bool:
        return self is GateStatus.MET


class GateCategory(str, enum.Enum):
    EVIDENCE = "EVIDENCE"              # does the strategy work?
    EXECUTION = "EXECUTION"            # can it reach a venue safely?
    OPERATIONS = "OPERATIONS"          # can it be run and stopped?
    AUTHORISATION = "AUTHORISATION"    # is it permitted?

    def __str__(self) -> str:
        return self.value


@dataclass
class Gate:
    """One prerequisite, its status, and why it matters."""
    name: str
    category: GateCategory
    status: GateStatus
    why: str
    detail: str = ""
    # What would have to be true, stated so the gate is actionable
    # rather than merely closed.
    to_satisfy: str = ""

    @property
    def blocks_live(self) -> bool:
        return not self.status.is_satisfied

    def as_dict(self) -> Dict:
        return {
            "name": self.name,
            "category": str(self.category),
            "status": str(self.status),
            "blocks_live": self.blocks_live,
            "why": self.why,
            "detail": self.detail,
            "to_satisfy": self.to_satisfy,
        }


@dataclass
class ReadinessReport:
    """The answer, and everything behind it."""
    gates: List[Gate] = field(default_factory=list)
    assessed_at: str = ""
    config_version: str = "readiness-v1.0.0"

    @property
    def unmet(self) -> List[Gate]:
        return [g for g in self.gates if g.blocks_live]

    @property
    def ready(self) -> bool:
        """Derived from the gates. No setter, no override.

        Requires EVERY gate. Deliberately not a proportion: a gate is a
        prerequisite, not a score contribution, and compensating for an
        open gate with a strong showing elsewhere is how a checklist
        becomes a formality.
        """
        return bool(self.gates) and not self.unmet

    @property
    def blocked(self) -> List[Gate]:
        """Gates that cannot be met from here at all."""
        return [g for g in self.gates if g.status is GateStatus.BLOCKED]

    def by_category(self) -> Dict[str, List[Dict]]:
        out: Dict[str, List[Dict]] = {}
        for gate in self.gates:
            out.setdefault(str(gate.category), []).append(gate.as_dict())
        return out

    def as_dict(self) -> Dict:
        return {
            "config_version": self.config_version,
            "ready_for_real_money": self.ready,
            "verdict": ("READY" if self.ready
                        else "NOT_READY_BLOCKED" if self.blocked
                        else "NOT_READY"),
            "gates_total": len(self.gates),
            "gates_met": len(self.gates) - len(self.unmet),
            "gates_unmet": len(self.unmet),
            "gates_blocked": len(self.blocked),
            "unmet": [g.as_dict() for g in self.unmet],
            "by_category": self.by_category(),
            "assessed_at": self.assessed_at,
            "summary": self.summary(),
        }

    def summary(self) -> str:
        if self.ready:
            return (
                "Every gate is met. This is a statement about the "
                "checklist, not a recommendation to trade.")
        blocked = self.blocked
        lead = (f"{len(self.unmet)} of {len(self.gates)} gates are not met")
        if blocked:
            lead += (f", and {len(blocked)} cannot be met from here: "
                     + ", ".join(g.name for g in blocked))
        return lead + "."


def _gate(name, category, status, why, detail="", to_satisfy="") -> Gate:
    return Gate(name=name, category=category, status=status, why=why,
                detail=detail, to_satisfy=to_satisfy)


def assess(*, performance: Optional[Dict] = None,
           calibration: Optional[Dict] = None,
           adapter_assessment: Optional[Dict] = None,
           switches: Optional[Dict] = None,
           pilot: Optional[Dict] = None,
           authorisation: Optional[Dict] = None,
           assessed_at: str = "") -> ReadinessReport:
    """Build the report from evidence that is passed in.

    Nothing is inferred from absence being convenient: every argument
    defaults to None and a None argument produces UNKNOWN, which counts
    as unmet. The gate cannot be passed by calling it with no
    arguments.
    """
    performance = performance or {}
    calibration = calibration or {}
    adapter_assessment = adapter_assessment or {}
    switches = switches or {}
    pilot = pilot or {}
    authorisation = authorisation or {}

    gates: List[Gate] = []

    # Whether the evidence behind the performance gates is allowed to
    # support a claim about the strategy at all. Paper fills computed
    # against a 15-minute-delayed feed exercise the machinery but do not
    # measure what the market would have given; a session spanning a
    # redeploy measures no single program. Both are recorded as an
    # evidence class upstream and honoured here.
    #
    # Fail closed: when nothing says the evidence is real-time,
    # single-runtime evidence, it is not treated as such. Silence cannot
    # promote a record.
    strategy_grade = pilot.get("counts_toward_strategy_gates")
    evidence_class = pilot.get("evidence_class") or "UNKNOWN"
    class_reasons = "; ".join(pilot.get("evidence_class_reasons") or []) \
        or "no evidence class recorded"

    def strategy_claim(status: GateStatus, detail: str):
        """Demote a MET performance gate that rests on evidence which is
        not real-time, single-runtime. The measurement still happened;
        it just cannot support this claim."""
        if status is GateStatus.MET and strategy_grade is not True:
            return GateStatus.UNMET, (
                f"{detail}; NOT COUNTED: evidence_class={evidence_class} "
                f"({class_reasons})")
        return status, detail

    # --- EVIDENCE: does the strategy work? ---------------------------
    verdict = performance.get("verdict")
    counted = performance.get("trades_counted")
    if verdict == "POSITIVE_EDGE_DEMONSTRATED":
        status = GateStatus.MET
    elif verdict is None:
        status = GateStatus.UNKNOWN
    else:
        status = GateStatus.UNMET
    status, edge_detail = strategy_claim(
        status, f"verdict={verdict}, trades_counted={counted}")
    gates.append(_gate(
        "demonstrated_edge", GateCategory.EVIDENCE, status,
        why=("Risking real money on a strategy whose edge has not been "
             "demonstrated is gambling with extra steps. The sample "
             "must be large enough that the result is not luck."),
        detail=edge_detail,
        to_satisfy=("a paper or live record whose expectancy interval "
                    "excludes zero at the claim threshold, gathered on "
                    "real-time data under one runtime")))

    stop_integrity = performance.get("stop_integrity") or {}
    breach_rate = stop_integrity.get("breach_rate")
    assessed = stop_integrity.get("trades_assessed") or 0
    if breach_rate is None or assessed == 0:
        status = GateStatus.UNKNOWN
    elif breach_rate <= 0.05:
        status = GateStatus.MET
    else:
        status = GateStatus.UNMET
    status, stops_detail = strategy_claim(
        status, f"breach_rate={breach_rate}, trades_assessed={assessed}")
    gates.append(_gate(
        "stops_hold", GateCategory.EVIDENCE, status,
        why=("If losses routinely exceed the planned risk, every "
             "position size upstream is wrong and the limits promise "
             "something they do not deliver."),
        detail=stops_detail,
        to_satisfy="a breach rate at or below 5% over a real sample"))

    cal_verdict = calibration.get("verdict")
    if cal_verdict == "MONOTONIC_AND_SIGNIFICANT":
        status = GateStatus.MET
    elif cal_verdict is None:
        status = GateStatus.UNKNOWN
    else:
        status = GateStatus.UNMET
    status, cal_detail = strategy_claim(status, f"verdict={cal_verdict}")
    gates.append(_gate(
        "strength_is_predictive", GateCategory.EVIDENCE, status,
        why=("Position size is scaled by hypothesis strength. If "
             "strength does not order outcomes, the sizing is "
             "arbitrary and the risk model rests on a number that "
             "means nothing."),
        detail=cal_detail,
        to_satisfy=("strength bands with enough trades each that the "
                    "extreme bands' intervals separate")))

    # --- EXECUTION: can it reach a venue safely? ---------------------
    adapter_ready = adapter_assessment.get("ready_for_real_money")
    if adapter_ready is True:
        status = GateStatus.MET
    elif adapter_ready is None:
        status = GateStatus.UNKNOWN
    else:
        status = GateStatus.UNMET
    gates.append(_gate(
        "live_adapter_exists", GateCategory.EXECUTION, status,
        why=("BrokerAdapter conformance is not evidence of live "
             "readiness: the paper broker satisfies it completely "
             "while being unable to lose a cent."),
        detail=(f"adapter={adapter_assessment.get('adapter_name')}, "
                f"is_paper={adapter_assessment.get('is_paper')}, "
                f"unmet={adapter_assessment.get('unmet_count')}"),
        to_satisfy=("a non-paper adapter satisfying every requirement "
                    "in agent/broker/live_contract.py")))

    gates.append(_gate(
        "execution_venue_available", GateCategory.EXECUTION,
        GateStatus.BLOCKED,
        why=("Without a venue that offers a supported retail execution "
             "API, there is no lawful automated route to an order."),
        detail=("Fidelity offers no retail execution API; see "
                "docs/FIDELITY_AUTOMATED_EXECUTION_UNAVAILABLE.md. "
                "Browser automation is excluded by instruction and "
                "because it cannot satisfy broker-side duplicate "
                "protection."),
        to_satisfy=("an account at a venue with a supported execution "
                    "API - an account action for the account holder, "
                    "not something this system can arrange")))

    # --- OPERATIONS: can it be run and stopped? ----------------------
    pilot_days = pilot.get("sessions_completed")
    if pilot_days is None:
        status = GateStatus.UNKNOWN
    elif pilot_days >= 20:
        status = GateStatus.MET
    else:
        status = GateStatus.UNMET
    gates.append(_gate(
        "pilot_ran_unattended", GateCategory.OPERATIONS, status,
        why=("A strategy that works and an operation that runs are "
             "different claims. Scheduler gaps, provider outages and "
             "cold starts only appear over many sessions."),
        detail=f"sessions_completed={pilot_days}",
        to_satisfy="at least 20 completed market sessions"))

    live_path = pilot.get("live_data_path_exercised")
    if live_path is True:
        status = GateStatus.MET
    elif live_path is None:
        status = GateStatus.UNKNOWN
    else:
        status = GateStatus.UNMET
    gates.append(_gate(
        "live_data_path_exercised", GateCategory.OPERATIONS, status,
        why=("Synthetic quotes in tests do not exercise real provider "
             "responses, rate limits, or the shapes a live feed "
             "actually returns."),
        detail=f"live_data_path_exercised={live_path}",
        to_satisfy=("a cycle completing against live market data with "
                    "real quotes and a real candidate list")))

    kill = switches.get("kill_switch_cancels_working_orders")
    if kill is True:
        status = GateStatus.MET
    elif kill is None:
        status = GateStatus.UNKNOWN
    else:
        status = GateStatus.UNMET
    gates.append(_gate(
        "kill_switch_reaches_live_risk", GateCategory.OPERATIONS, status,
        why=("The current switch stops NEW orders. A resting order is "
             "live risk it does not reach, so with real money the "
             "switch would be advertising protection it does not "
             "provide."),
        detail=f"cancels_working_orders={kill}",
        to_satisfy="a kill switch that cancels working orders"))

    reconciled = pilot.get("reconciliation_clean_sessions")
    if reconciled is None:
        status = GateStatus.UNKNOWN
    elif pilot_days and reconciled >= pilot_days:
        status = GateStatus.MET
    else:
        status = GateStatus.UNMET
    gates.append(_gate(
        "reconciliation_clean", GateCategory.OPERATIONS, status,
        why=("A reconciliation failure means the agent's risk "
             "arithmetic was wrong in an unknown direction. With real "
             "money that is the state in which losses are discovered "
             "rather than prevented."),
        detail=(f"clean={reconciled} of {pilot_days} sessions"),
        to_satisfy="every pilot session reconciling without divergence"))

    # --- AUTHORISATION: is it permitted? -----------------------------
    explicit = authorisation.get("explicit_user_authorisation")
    if explicit is True:
        status = GateStatus.MET
    elif explicit is None:
        status = GateStatus.UNKNOWN
    else:
        status = GateStatus.UNMET
    gates.append(_gate(
        "explicit_authorisation", GateCategory.AUTHORISATION, status,
        why=("No amount of passing tests authorises spending someone "
             "else's money. This gate cannot be satisfied by evidence, "
             "only by a decision."),
        detail=f"explicit_user_authorisation={explicit}",
        to_satisfy=("an explicit, informed instruction from the "
                    "account holder to begin real-money trading")))

    capital = authorisation.get("capital_at_risk_agreed")
    if capital is True:
        status = GateStatus.MET
    elif capital is None:
        status = GateStatus.UNKNOWN
    else:
        status = GateStatus.UNMET
    gates.append(_gate(
        "capital_at_risk_agreed", GateCategory.AUTHORISATION, status,
        why=("The amount that may be lost has to be a decision taken "
             "in advance, not a number discovered afterwards."),
        detail=f"capital_at_risk_agreed={capital}",
        to_satisfy="an agreed maximum loss, in writing, before any order"))

    return ReadinessReport(gates=gates, assessed_at=assessed_at)

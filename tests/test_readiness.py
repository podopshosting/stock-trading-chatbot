"""
The live-readiness gate.

The gate answers one question — may this place a real-money order — and
every test here is an attempt to get a "yes" it has not earned.

The bias is deliberate and asymmetric. A false "not ready" costs delay.
A false "ready" costs money, and an automated system loses money not
through one large mistake but through a small one repeated faster than
anyone notices.
"""
import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from agent.broker import (                                        # noqa: E402
    PaperBroker, PaperBrokerConfig, assess_adapter,
)
from agent.evaluation import assess as calibrate                  # noqa: E402
from agent.journal import describe                                # noqa: E402
from agent.readiness import (                                     # noqa: E402
    Gate, GateCategory, GateStatus, ReadinessReport, assess,
)


def all_met(n=5):
    return [Gate(name=f"g{i}", category=GateCategory.EVIDENCE,
                 status=GateStatus.MET, why="because") for i in range(n)]


class TestTheGateCannotBeForced(unittest.TestCase):

    def test_readiness_is_derived_and_has_no_setter(self):
        report = ReadinessReport(gates=all_met())
        self.assertTrue(report.ready)
        with self.assertRaises(AttributeError):
            report.ready = False

    def test_one_unmet_gate_is_disqualifying(self):
        """
        A gate is a prerequisite, not a score contribution. Allowing a
        strong showing elsewhere to compensate is how a checklist
        becomes a formality.
        """
        gates = all_met(11)
        gates.append(Gate(name="last", category=GateCategory.EVIDENCE,
                          status=GateStatus.UNMET, why="because"))
        self.assertFalse(ReadinessReport(gates=gates).ready)

    def test_an_unknown_gate_counts_as_unmet(self):
        """
        The system cannot be ready in a respect it has not checked.
        Treating unknown as satisfied would make the gate weaker the
        less it knew.
        """
        gates = all_met(3)
        gates.append(Gate(name="unchecked", category=GateCategory.EVIDENCE,
                          status=GateStatus.UNKNOWN, why="because"))
        self.assertFalse(ReadinessReport(gates=gates).ready)

    def test_a_blocked_gate_counts_as_unmet(self):
        gates = all_met(3)
        gates.append(Gate(name="impossible", category=GateCategory.EXECUTION,
                          status=GateStatus.BLOCKED, why="because"))
        self.assertFalse(ReadinessReport(gates=gates).ready)

    def test_an_empty_report_is_not_ready(self):
        """
        Zero gates would otherwise pass trivially - "no gate blocks me"
        is true of a gate list that does not exist.
        """
        self.assertFalse(ReadinessReport(gates=[]).ready)

    def test_calling_assess_with_no_arguments_is_not_ready(self):
        """
        Nothing is inferred from absence being convenient. The gate
        cannot be passed by being asked nothing.
        """
        report = assess()
        self.assertFalse(report.ready)
        self.assertEqual(len(report.unmet), len(report.gates))

    def test_all_gates_met_does_pass(self):
        """
        The falsifying control for this entire class: the gate must be
        passable, or it is a refusal rather than a standard.
        """
        self.assertTrue(ReadinessReport(gates=all_met()).ready)

    def test_there_is_no_override_or_force_anywhere_in_the_module(self):
        """
        Checked against the source. A flag that skips the gate would
        make every test above decorative.
        """
        source = open(os.path.join(REPO_ROOT, "agent",
                                   "readiness.py")).read().lower()
        for forbidden in ("force=", "override=", "skip_gates",
                          "ignore_gates", "bypass"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, source)

    def test_falsifying_control_the_source_scan_can_match(self):
        source = open(os.path.join(REPO_ROOT, "agent",
                                   "readiness.py")).read().lower()
        self.assertIn("derived", source)
        self.assertIn("no setter", source)


class TestGateContent(unittest.TestCase):

    def _report(self):
        return assess()

    def test_every_gate_explains_why_it_exists(self):
        for gate in self._report().gates:
            with self.subTest(gate=gate.name):
                self.assertGreater(len(gate.why), 50)

    def test_every_gate_says_what_would_satisfy_it(self):
        """
        A closed gate with no stated remedy is an obstacle rather than
        a checklist item.
        """
        for gate in self._report().gates:
            with self.subTest(gate=gate.name):
                self.assertTrue(gate.to_satisfy)

    def test_the_gates_cover_all_four_categories(self):
        categories = {g.category for g in self._report().gates}
        self.assertEqual(categories, set(GateCategory))

    def test_authorisation_cannot_be_satisfied_by_evidence(self):
        """
        No amount of passing tests authorises spending someone else's
        money, and the gate says so.
        """
        gate = next(g for g in self._report().gates
                    if g.name == "explicit_authorisation")
        self.assertIn("cannot be satisfied by evidence", gate.why)

    def test_the_venue_gate_is_blocked_not_merely_unmet(self):
        """
        BLOCKED records that this cannot be fixed from here: it needs
        an account action by the account holder.
        """
        gate = next(g for g in self._report().gates
                    if g.name == "execution_venue_available")
        self.assertIs(gate.status, GateStatus.BLOCKED)
        self.assertIn("account holder", gate.to_satisfy)

    def test_the_venue_gate_cites_the_fidelity_finding(self):
        gate = next(g for g in self._report().gates
                    if g.name == "execution_venue_available")
        self.assertIn("FIDELITY_AUTOMATED_EXECUTION_UNAVAILABLE",
                      gate.detail)

    def test_the_kill_switch_gate_names_the_actual_shortfall(self):
        gate = next(g for g in self._report().gates
                    if g.name == "kill_switch_reaches_live_risk")
        self.assertIn("resting order", gate.why)


class TestGatesReflectRealEvidence(unittest.TestCase):
    """Driven by the real modules, not hand-written dicts."""

    def test_an_empty_journal_does_not_demonstrate_an_edge(self):
        report = assess(performance=describe([]))
        gate = next(g for g in report.gates
                    if g.name == "demonstrated_edge")
        self.assertFalse(gate.status.is_satisfied)

    def test_an_untested_calibration_does_not_satisfy_its_gate(self):
        report = assess(calibration=calibrate([]))
        gate = next(g for g in report.gates
                    if g.name == "strength_is_predictive")
        self.assertFalse(gate.status.is_satisfied)

    def test_the_paper_broker_does_not_satisfy_the_adapter_gate(self):
        report = assess(adapter_assessment=assess_adapter(
            PaperBroker(PaperBrokerConfig())).as_dict())
        gate = next(g for g in report.gates
                    if g.name == "live_adapter_exists")
        self.assertFalse(gate.status.is_satisfied)

    def test_a_high_stop_breach_rate_fails_its_gate(self):
        report = assess(performance={
            "verdict": "POSITIVE_EDGE_DEMONSTRATED",
            "stop_integrity": {"breach_rate": 0.4, "trades_assessed": 100}})
        gate = next(g for g in report.gates if g.name == "stops_hold")
        self.assertIs(gate.status, GateStatus.UNMET)

    def test_a_low_stop_breach_rate_passes_its_gate(self):
        # Strategy-grade evidence is supplied so this isolates the
        # breach-rate threshold itself. Without it the gate is demoted,
        # which TestStrategyGatesRequireRealTimeEvidence covers.
        report = assess(
            performance={"verdict": "POSITIVE_EDGE_DEMONSTRATED",
                         "stop_integrity": {"breach_rate": 0.01,
                                            "trades_assessed": 100}},
            pilot={"counts_toward_strategy_gates": True,
                   "evidence_class": "REAL_TIME_STRATEGY_EVIDENCE"})
        gate = next(g for g in report.gates if g.name == "stops_hold")
        self.assertIs(gate.status, GateStatus.MET)

    def test_zero_assessed_trades_is_unknown_not_a_pass(self):
        """
        A breach rate of 0.0 over zero trades is not evidence that stops
        hold - it is evidence that nothing was measured.
        """
        report = assess(performance={
            "stop_integrity": {"breach_rate": 0.0, "trades_assessed": 0}})
        gate = next(g for g in report.gates if g.name == "stops_hold")
        self.assertIs(gate.status, GateStatus.UNKNOWN)

    def test_a_short_pilot_does_not_satisfy_the_operations_gate(self):
        report = assess(pilot={"sessions_completed": 3})
        gate = next(g for g in report.gates
                    if g.name == "pilot_ran_unattended")
        self.assertIs(gate.status, GateStatus.UNMET)

    def test_a_long_pilot_satisfies_the_operations_gate(self):
        report = assess(pilot={"sessions_completed": 25})
        gate = next(g for g in report.gates
                    if g.name == "pilot_ran_unattended")
        self.assertIs(gate.status, GateStatus.MET)

    def test_a_single_reconciliation_failure_fails_its_gate(self):
        report = assess(pilot={"sessions_completed": 25,
                               "reconciliation_clean_sessions": 24})
        gate = next(g for g in report.gates
                    if g.name == "reconciliation_clean")
        self.assertIs(gate.status, GateStatus.UNMET)


class TestCurrentStateIsNotReady(unittest.TestCase):
    """
    The actual answer today, asserted so it cannot drift silently.
    """

    def _current(self):
        return assess(
            performance=describe([]),
            calibration=calibrate([]),
            adapter_assessment=assess_adapter(
                PaperBroker(PaperBrokerConfig())).as_dict(),
            switches={"kill_switch_cancels_working_orders": False},
            pilot={"sessions_completed": 0,
                   "live_data_path_exercised": False,
                   "reconciliation_clean_sessions": 0},
            authorisation={"explicit_user_authorisation": False,
                           "capital_at_risk_agreed": False})

    def test_the_system_is_not_ready_for_real_money(self):
        self.assertFalse(self._current().ready)

    def test_the_verdict_records_that_something_is_blocked(self):
        self.assertEqual(self._current().as_dict()["verdict"],
                         "NOT_READY_BLOCKED")

    def test_no_gate_is_currently_met(self):
        report = self._current()
        self.assertEqual(len(report.unmet), len(report.gates))

    def test_the_summary_names_the_blocked_gate(self):
        self.assertIn("execution_venue_available",
                      self._current().summary())

    def test_a_ready_report_does_not_read_as_a_recommendation(self):
        """
        Passing a checklist is not advice to trade, and the summary
        says so rather than congratulating.
        """
        summary = ReadinessReport(gates=all_met()).summary()
        self.assertIn("not a recommendation", summary)


class TestSerialisation(unittest.TestCase):

    def test_the_report_serialises_with_its_verdict(self):
        data = assess().as_dict()
        for key in ("ready_for_real_money", "verdict", "gates_total",
                    "gates_unmet", "unmet", "by_category", "summary"):
            with self.subTest(key=key):
                self.assertIn(key, data)

    def test_the_counts_add_up(self):
        data = assess().as_dict()
        self.assertEqual(data["gates_met"] + data["gates_unmet"],
                         data["gates_total"])

    def test_the_report_records_a_configuration_version(self):
        self.assertTrue(assess().as_dict()["config_version"])


if __name__ == "__main__":
    unittest.main()


class TestStrategyGatesRequireRealTimeEvidence(unittest.TestCase):
    """
    A paper fill computed against a 15-minute-delayed quote exercises the
    machinery; it does not measure what the market would have given. A
    session spanning a redeploy measures no single program. Neither may
    satisfy a gate that claims the strategy works.
    """

    PERF = {"verdict": "POSITIVE_EDGE_DEMONSTRATED", "trades_counted": 80,
            "stop_integrity": {"breach_rate": 0.0, "trades_assessed": 80}}
    CAL = {"verdict": "MONOTONIC_AND_SIGNIFICANT"}
    CLAIMS = ("demonstrated_edge", "stops_hold", "strength_is_predictive")

    def statuses(self, pilot):
        report = assess(performance=self.PERF, calibration=self.CAL,
                        pilot=pilot)
        return {g.name: g for g in report.gates}

    def test_real_time_single_runtime_evidence_can_satisfy_them(self):
        """The control: without this the demotion tests below would pass
        even if the gates could never be met at all."""
        gates = self.statuses({
            "counts_toward_strategy_gates": True,
            "evidence_class": "REAL_TIME_STRATEGY_EVIDENCE"})
        for name in self.CLAIMS:
            with self.subTest(gate=name):
                self.assertIs(gates[name].status, GateStatus.MET)

    def test_delayed_data_evidence_cannot_satisfy_them(self):
        gates = self.statuses({
            "counts_toward_strategy_gates": False,
            "evidence_class": "OPERATIONAL_VALIDATION_ONLY",
            "evidence_class_reasons": ["traded on delayed market data"]})
        for name in self.CLAIMS:
            with self.subTest(gate=name):
                self.assertIs(gates[name].status, GateStatus.UNMET)
                self.assertIn("NOT COUNTED", gates[name].detail)
                self.assertIn("delayed market data", gates[name].detail)

    def test_an_unlabelled_body_of_evidence_fails_closed(self):
        """Silence must not promote a record to real-time evidence."""
        gates = self.statuses({})
        for name in self.CLAIMS:
            with self.subTest(gate=name):
                self.assertIs(gates[name].status, GateStatus.UNMET)
                self.assertIn("UNKNOWN", gates[name].detail)

    def test_the_measurement_is_still_reported_not_hidden(self):
        """Demoted is not deleted: the number stays visible."""
        gates = self.statuses({"counts_toward_strategy_gates": False,
                               "evidence_class": "OPERATIONAL_VALIDATION_ONLY"})
        self.assertIn("POSITIVE_EDGE_DEMONSTRATED",
                      gates["demonstrated_edge"].detail)
        self.assertIn("trades_counted=80", gates["demonstrated_edge"].detail)

    def test_demotion_does_not_touch_operational_gates(self):
        """Delayed data still proves the machinery ran."""
        gates = self.statuses({
            "counts_toward_strategy_gates": False,
            "evidence_class": "OPERATIONAL_VALIDATION_ONLY",
            "live_data_path_exercised": True})
        self.assertIs(gates["live_data_path_exercised"].status, GateStatus.MET)

    def test_a_report_on_delayed_evidence_is_never_ready(self):
        report = assess(performance=self.PERF, calibration=self.CAL,
                        pilot={"counts_toward_strategy_gates": False,
                               "evidence_class": "OPERATIONAL_VALIDATION_ONLY"})
        self.assertFalse(report.ready)

"""
The live adapter contract, and the Fidelity finding.

The point of this suite is that `BrokerAdapter` conformance is not
evidence of live readiness. The paper broker satisfies that protocol
completely while being unable to lose a cent, so a live adapter needs a
separate, harder contract — and `ready_for_real_money` must be
impossible to reach by accident.
"""
import os
import re
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from agent.broker import (                                        # noqa: E402
    REQUIREMENT_RATIONALE, AdapterAssessment, PaperBroker,
    PaperBrokerConfig, Requirement, assess_adapter,
)

FINDING = os.path.join(
    REPO_ROOT, "docs", "FIDELITY_AUTOMATED_EXECUTION_UNAVAILABLE.md")


class TestPaperIsNeverLiveReady(unittest.TestCase):

    def test_the_paper_broker_is_not_ready_for_real_money(self):
        assessment = assess_adapter(PaperBroker(PaperBrokerConfig()))
        self.assertFalse(assessment.ready_for_real_money)
        self.assertTrue(assessment.is_paper)

    def test_a_paper_adapter_declaring_everything_is_still_not_ready(self):
        """
        The load-bearing test. A paper adapter can satisfy every stated
        requirement and must still be refused, because being paper is
        itself disqualifying - otherwise the checklist could be passed
        by an adapter that cannot trade.
        """
        declared = {str(r).lower(): True for r in Requirement}
        assessment = assess_adapter(PaperBroker(PaperBrokerConfig()),
                                    declared=declared)
        self.assertEqual(assessment.unmet, [])
        self.assertFalse(assessment.ready_for_real_money)

    def test_readiness_is_derived_and_cannot_be_assigned(self):
        assessment = AdapterAssessment(adapter_name="X", is_paper=False)
        with self.assertRaises(AttributeError):
            assessment.ready_for_real_money = True

    def test_a_single_unmet_requirement_is_disqualifying(self):
        """
        No partial credit. Each requirement is a way to lose money the
        paper record cannot show, so sixteen-of-seventeen is not a pass.
        """
        assessment = AdapterAssessment(
            adapter_name="X", is_paper=False,
            satisfied=[r for r in Requirement
                       if r is not Requirement.CANCEL_CONFIRMATION],
            unmet=[Requirement.CANCEL_CONFIRMATION])
        self.assertFalse(assessment.ready_for_real_money)

    def test_a_non_paper_adapter_meeting_everything_can_qualify(self):
        """
        The falsifying control: the gate must be reachable in principle,
        or it is a refusal rather than a standard.
        """
        assessment = AdapterAssessment(
            adapter_name="Hypothetical", is_paper=False,
            satisfied=list(Requirement), unmet=[])
        self.assertTrue(assessment.ready_for_real_money)

    def test_an_adapter_whose_capabilities_raise_is_treated_as_paper(self):
        """Fail closed: an adapter that cannot describe itself is not
        trusted with real money."""
        class Opaque:
            def capabilities(self):
                raise RuntimeError("not implemented")
        self.assertTrue(assess_adapter(Opaque()).is_paper)

    def test_a_declared_capability_is_recorded_as_declared(self):
        """
        A claim is not verification, and the assessment says so rather
        than letting a declaration read as a test result.
        """
        declared = {str(Requirement.ORDER_STATUS_POLLING).lower(): True}
        assessment = assess_adapter(PaperBroker(PaperBrokerConfig()),
                                    declared=declared)
        self.assertTrue(any("not verified" in n for n in assessment.notes))


class TestRequirementsAreSpecified(unittest.TestCase):

    def test_every_requirement_has_a_written_rationale(self):
        """
        A bare enum name is not a specification. Whoever builds the live
        adapter will read this rather than ask.
        """
        for requirement in Requirement:
            with self.subTest(requirement=requirement):
                self.assertIn(requirement, REQUIREMENT_RATIONALE)
                self.assertGreater(len(REQUIREMENT_RATIONALE[requirement]),
                                   60)

    def test_every_rationale_describes_a_consequence(self):
        """
        Not what the requirement IS - what breaks without it. The first
        is a restatement, the second is a reason.

        Checked per rationale rather than against the joined text: a
        joined check passes as long as ONE entry mentions a consequence,
        which is exactly the kind of assertion that looks like coverage
        of sixteen items while covering one.
        """
        # "causes" and "no concept of" were missing from an earlier
        # list, which failed a rationale that stated its consequence
        # perfectly clearly. The list describes how consequences get
        # phrased; it is not a style rule.
        indicators = ("does not", "do not", "without", "cannot", "can be",
                      "assume", "will", "is how", "leaves", "removes",
                      "depends on", "must", "causes", "no concept")
        for requirement, rationale in REQUIREMENT_RATIONALE.items():
            with self.subTest(requirement=requirement):
                lowered = rationale.lower()
                self.assertTrue(
                    any(i in lowered for i in indicators),
                    f"{requirement} states what it is but not what goes "
                    f"wrong without it: {rationale!r}")

    def test_the_unmet_rationale_is_returned_with_the_assessment(self):
        assessment = assess_adapter(PaperBroker(PaperBrokerConfig()))
        data = assessment.as_dict()
        self.assertEqual(set(data["rationale"]),
                         {str(r) for r in assessment.unmet})

    def test_duplicate_submission_protection_is_broker_side(self):
        """
        Our own client-id map is lost if our state is lost. The broker's
        memory is what actually prevents a double position.
        """
        rationale = REQUIREMENT_RATIONALE[
            Requirement.DUPLICATE_SUBMISSION_PROTECTION]
        self.assertIn("BY THE BROKER", rationale)

    def test_the_kill_switch_requirement_covers_working_orders(self):
        """
        Today's switch stops NEW orders. A resting order is live risk it
        does not currently reach.
        """
        rationale = REQUIREMENT_RATIONALE[
            Requirement.KILL_SWITCH_THAT_CANCELS_WORKING_ORDERS]
        self.assertIn("cancel working", rationale)

    def test_the_contract_covers_the_async_fill_problem(self):
        """
        The most dangerous paper-to-live difference: code written
        against a synchronous fill will record positions that do not
        exist yet, with exit plans attached.
        """
        self.assertIn(Requirement.ASYNC_FILL_RECONCILIATION,
                      REQUIREMENT_RATIONALE)
        self.assertIn("do not yet exist",
                      REQUIREMENT_RATIONALE[
                          Requirement.ASYNC_FILL_RECONCILIATION])

    def test_the_contract_covers_pattern_day_trader_rules(self):
        """
        An intraday strategy under $25,000 makes this likely rather
        than incidental.
        """
        self.assertIn("pattern day trader",
                      REQUIREMENT_RATIONALE[
                          Requirement.PATTERN_DAY_TRADER_RULES].lower())

    def test_the_contract_covers_settlement(self):
        """The capital arithmetic has no concept of it."""
        self.assertIn("settlement",
                      REQUIREMENT_RATIONALE[
                          Requirement.SETTLEMENT_AND_GOOD_FAITH].lower())


class TestFidelityFinding(unittest.TestCase):
    """The finding is a deliverable, so it is checked like one."""

    def _text(self) -> str:
        """Whitespace-normalised, because the document is wrapped.

        An earlier version read the raw file and failed on phrases that
        happened to straddle a line break - reporting a missing claim
        that was present. Markdown emphasis is stripped for the same
        reason.
        """
        raw = open(FINDING).read().replace("**", "")
        return re.sub(r"\s+", " ", raw)

    def test_the_finding_document_exists(self):
        self.assertTrue(os.path.exists(FINDING))

    def test_it_states_the_documented_status_code(self):
        self.assertIn("FIDELITY_AUTOMATED_EXECUTION_UNAVAILABLE",
                      self._text())

    def test_it_records_what_was_checked(self):
        text = self._text().lower()
        for path in ("snaptrade", "read-only", "institutional"):
            with self.subTest(path=path):
                self.assertIn(path, text)

    def test_it_explains_why_browser_automation_is_excluded(self):
        """
        Deference to the instruction is not a reason. The document gives
        the engineering case as well.
        """
        text = self._text().lower()
        self.assertIn("unauthorised", text)
        self.assertIn("double a position", text)

    def test_it_does_not_claim_an_account_was_opened(self):
        """
        Opening an account, accepting agreements or supplying
        credentials is the account holder's action alone.
        """
        text = self._text().lower()
        self.assertIn("none of these has been set up", text)
        self.assertIn("account holder", text)

    def test_it_distinguishes_data_credentials_from_trading_credentials(self):
        """
        The project already holds Alpaca credentials. Those grant data
        access, and data access is not an execution capability.
        """
        text = self._text()
        self.assertIn("data access only", text)
        self.assertIn("not trading credentials", text)

    def test_it_lists_venues_that_do_offer_an_execution_api(self):
        text = self._text()
        for broker in ("Alpaca", "Interactive Brokers", "Tradier",
                       "Schwab"):
            with self.subTest(broker=broker):
                self.assertIn(broker, text)

    def test_it_is_dated(self):
        """A finding about a third party's API decays."""
        self.assertTrue(re.search(r"20\d\d-\d\d-\d\d", self._text()))


class TestNoExecutionRouteExists(unittest.TestCase):
    """
    The conclusion of the milestone, asserted against the code rather
    than the prose.
    """

    def test_no_live_broker_adapter_is_implemented(self):
        broker_dir = os.path.join(REPO_ROOT, "agent", "broker")
        modules = sorted(f for f in os.listdir(broker_dir)
                         if f.endswith(".py"))
        # alpaca_paper.py and shadow.py were added in milestone 19A. Both
        # are paper-only: the first pins the paper host and refuses any
        # other, the second only compares. The next test checks that
        # claim against the SOURCE rather than trusting this list.
        self.assertEqual(
            modules,
            ["__init__.py", "alpaca_paper.py", "base.py", "execution.py",
             "live_contract.py", "models.py", "paper.py", "shadow.py",
             "store.py"],
            "a new module appeared in agent/broker; if it is a live "
            "adapter, the readiness gate must be reconsidered")

    def test_no_broker_module_can_reach_a_non_paper_host(self):
        """
        The module list above only notices a NEW file. This notices a
        live endpoint appearing in ANY broker file, including an
        existing one: every broker host must be the paper host.
        """
        import re
        broker_dir = os.path.join(REPO_ROOT, "agent", "broker")
        hosts = set()
        for name in os.listdir(broker_dir):
            if not name.endswith(".py"):
                continue
            body = open(os.path.join(broker_dir, name)).read()
            hosts.update(re.findall(r"https?://([A-Za-z0-9.-]+)", body))
        self.assertEqual(hosts, {"paper-api.alpaca.markets"},
                         f"broker modules reference hosts: {sorted(hosts)}")

    def test_falsifying_control_the_host_scan_finds_a_live_host(self):
        import re
        found = re.findall(r"https?://([A-Za-z0-9.-]+)",
                           'BASE = "https://api.alpaca.markets"')
        self.assertEqual(found, ["api.alpaca.markets"])

    def test_the_only_broker_that_can_fill_is_the_paper_one(self):
        """
        Checked by capability rather than by name, so a renamed live
        adapter would still be caught.
        """
        from agent.broker.paper import PaperBroker as P
        self.assertTrue(P(PaperBrokerConfig()).capabilities()["is_paper"])

    def test_no_module_references_a_fidelity_endpoint(self):
        """
        Guards against the excluded path reappearing. Scans for the
        hostname and for playwright, not for the word "Fidelity", which
        appears legitimately in the finding document.
        """
        offenders = []
        for root, _dirs, files in os.walk(os.path.join(REPO_ROOT, "agent")):
            for name in files:
                if not name.endswith(".py"):
                    continue
                body = open(os.path.join(root, name)).read().lower()
                if "fidelity.com" in body or "playwright" in body:
                    offenders.append(os.path.join(root, name))
        self.assertEqual(offenders, [])

    def test_falsifying_control_the_scan_matches_what_it_looks_for(self):
        sample = "connecting to fidelity.com via playwright".lower()
        self.assertIn("fidelity.com", sample)
        self.assertIn("playwright", sample)


if __name__ == "__main__":
    unittest.main()

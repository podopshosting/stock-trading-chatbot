"""Non-latching recovery, and the latching refusal that bounds it.

REPEATED_CYCLE_FAILURE is documented as non-latching: it should clear
once its cause passes. It did not. The condition has two raise paths -
the cycle handler's early-abort and the orchestrator - but only one
clear path, inside the orchestrator, which a market-closed skip returns
before reaching.

Observed on a live dev invocation: one SKIPPED_MARKET_CLOSED cycle took
consecutive_failures from 4 to 0 and left the condition raised with its
original timestamp. A non-latching condition whose cause has passed is
a latching condition nobody declared.
"""
from __future__ import annotations

import os
import sys
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from agent.autonomy.health import (                          # noqa: E402
    HALTING, LATCHING, Condition, InMemoryHealthStore,
)


class TestTheConditionIsNonLatching(unittest.TestCase):

    def test_repeated_cycle_failure_is_not_latching(self):
        # The premise of the whole fix. If this ever changes, the clear
        # below must be removed rather than quietly kept working.
        self.assertNotIn(Condition.REPEATED_CYCLE_FAILURE, LATCHING)

    def test_but_it_does_halt(self):
        # So leaving it raised is not cosmetic - it blocks entries.
        self.assertIn(Condition.REPEATED_CYCLE_FAILURE, HALTING)


class TestRecoveryOnASuccessfulCycle(unittest.TestCase):

    def setUp(self):
        self.health = InMemoryHealthStore()

    def test_a_successful_cycle_resets_the_streak(self):
        for _ in range(4):
            self.health.record_cycle(False)
        self.assertEqual(self.health.get_streak(), 4)
        self.assertEqual(self.health.record_cycle(True), 0)

    def test_the_condition_clears_once_the_streak_is_zero(self):
        for _ in range(4):
            self.health.record_cycle(False)
        self.health.raise_condition(Condition.REPEATED_CYCLE_FAILURE,
                                    "4 consecutive failed cycles")
        self.assertIn(Condition.REPEATED_CYCLE_FAILURE,
                      {c.condition for c in self.health.snapshot().active})
        streak = self.health.record_cycle(True)
        self.assertEqual(streak, 0)
        self.assertTrue(
            self.health.clear_condition(Condition.REPEATED_CYCLE_FAILURE,
                                        cleared_by=None))
        self.assertNotIn(Condition.REPEATED_CYCLE_FAILURE,
                         {c.condition for c in self.health.snapshot().active})

    def test_resetting_the_streak_alone_does_NOT_clear_it(self):
        """The defect, pinned.

        If someone removes the clear and keeps record_cycle(True), this
        fails - which is the only way the fix stays in place.
        """
        for _ in range(4):
            self.health.record_cycle(False)
        self.health.raise_condition(Condition.REPEATED_CYCLE_FAILURE, "x")
        self.health.record_cycle(True)          # streak reset, no clear
        self.assertIn(
            Condition.REPEATED_CYCLE_FAILURE,
            {c.condition for c in self.health.snapshot().active},
            "resetting the streak is not supposed to clear the condition "
            "by itself; if it now does, the explicit clear is redundant "
            "and should be removed rather than left as decoration")


class TestTheLatchingRefusalBoundsIt(unittest.TestCase):
    """The clear is called with cleared_by=None, so it must be unable
    to touch anything latching. That property belongs to the store."""

    def setUp(self):
        self.health = InMemoryHealthStore()

    def test_every_latching_condition_refuses_an_unnamed_clear(self):
        for condition in sorted(LATCHING, key=str):
            with self.subTest(condition=str(condition)):
                h = InMemoryHealthStore()
                h.raise_condition(condition, "raised")
                self.assertFalse(
                    h.clear_condition(condition, cleared_by=None),
                    f"{condition} was cleared without a named human")
                self.assertIn(condition,
                              {c.condition for c in h.snapshot().active})

    def test_the_three_live_latches_cannot_be_cleared_this_way(self):
        # The conditions actually halting the dev agent right now.
        for condition in (Condition.RECONCILIATION_MISMATCH,
                          Condition.EMERGENCY_STOP,
                          Condition.UNEXPECTED_BROKER_POSITION):
            with self.subTest(condition=str(condition)):
                h = InMemoryHealthStore()
                h.raise_condition(condition, "raised")
                self.assertFalse(h.clear_condition(condition,
                                                   cleared_by=None))

    def test_a_named_human_CAN_clear_a_latching_condition(self):
        # The falsifying control: if clear_condition refused
        # unconditionally, every test above would pass by being
        # unsatisfiable rather than by the rule working.
        h = InMemoryHealthStore()
        h.raise_condition(Condition.EMERGENCY_STOP, "raised")
        self.assertTrue(h.clear_condition(Condition.EMERGENCY_STOP,
                                          cleared_by="a-named-operator"))
        self.assertNotIn(Condition.EMERGENCY_STOP,
                         {c.condition for c in h.snapshot().active})

    def test_clearing_something_not_raised_reports_false(self):
        # So a caller cannot read True as "the condition existed".
        self.assertFalse(
            self.health.clear_condition(Condition.REPEATED_CYCLE_FAILURE,
                                        cleared_by=None))


class TestTheSkipPathWiresItUp(unittest.TestCase):

    def test_the_handler_clears_the_condition_on_a_successful_skip(self):
        # Source-level, because the skip path builds real AWS stores and
        # the seal (correctly) blocks constructing them here. The
        # behavioural proof is the live invocation recorded in
        # docs/progress/.
        path = os.path.join(REPO, "lambda-micro", "agent-cycle",
                            "handler.py")
        with open(path) as fh:
            src = fh.read()
        self.assertIn("clear_condition(Condition.REPEATED_CYCLE_FAILURE",
                      src)
        # And only with cleared_by=None, never a fabricated name.
        self.assertNotIn('cleared_by="', src,
                         "the cycle must never name a human to clear a "
                         "condition on its own behalf")


if __name__ == "__main__":
    unittest.main()

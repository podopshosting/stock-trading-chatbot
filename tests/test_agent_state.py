"""
Tests for the agent state model, persistence and service (Milestone 3).

The load-bearing properties here are safety ones: trading is off by
default, a halt cannot be undone by an ordinary transition, and a stale
writer cannot erase a concurrent update.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.config import AgentConfig, RegimeWeights  # noqa: E402
from agent.state import (  # noqa: E402
    AgentSession, AgentState, AgentStateService, ConcurrentUpdate,
    EXPOSURE_INCREASING_STATES, InMemoryStateStore, InvalidTransition,
    MarketSession, TRADE_STATES,
    allowed_targets, can_transition,
)


class TestAgentStateEnum(unittest.TestCase):
    def test_all_documented_states_exist(self):
        expected = {
            "OFFLINE", "PRE_MARKET", "SCANNING", "WATCHING",
            "TRADE_CANDIDATE", "PAPER_ORDER_PENDING", "POSITION_OPEN",
            "POSITION_EXITING", "DAILY_RISK_LOCK", "MARKET_CLOSED",
            "EMERGENCY_STOP",
        }
        self.assertEqual({s.value for s in AgentState}, expected)

    def test_arbitrary_strings_are_rejected(self):
        """A typo must not become a state."""
        for bad in ("SCANNNIG", "running", "", "TRADING"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    AgentState.parse(bad)

    def test_parse_is_case_insensitive_and_idempotent(self):
        self.assertIs(AgentState.parse("scanning"), AgentState.SCANNING)
        self.assertIs(AgentState.parse(AgentState.SCANNING), AgentState.SCANNING)


class TestTransitionRules(unittest.TestCase):
    def test_daily_session_path(self):
        self.assertTrue(can_transition(AgentState.OFFLINE, AgentState.PRE_MARKET))
        self.assertTrue(can_transition(AgentState.PRE_MARKET, AgentState.SCANNING))
        self.assertTrue(can_transition(AgentState.SCANNING, AgentState.MARKET_CLOSED))
        self.assertTrue(can_transition(AgentState.MARKET_CLOSED, AgentState.OFFLINE))

    def test_safety_states_are_reachable_from_everywhere(self):
        """A halt that needs a tidy starting state is not a halt.

        The one exception is deliberate: from EMERGENCY_STOP the weaker
        DAILY_RISK_LOCK is NOT reachable. That edge let an emergency stop
        be downgraded and then walked back to SCANNING, so the state
        documented as terminal was not terminal.
        """
        for state in AgentState:
            with self.subTest(state=state):
                self.assertIn(AgentState.EMERGENCY_STOP, allowed_targets(state))
                if state is not AgentState.EMERGENCY_STOP:
                    self.assertIn(AgentState.DAILY_RISK_LOCK,
                                  allowed_targets(state))

    def test_emergency_stop_is_terminal_for_transitions(self):
        """Only re-assertion. Leaving is a human act."""
        self.assertEqual(allowed_targets(AgentState.EMERGENCY_STOP),
                         {AgentState.EMERGENCY_STOP})

    def test_an_emergency_stop_cannot_be_walked_out_of_by_any_path(self):
        """
        The leak was a CHAIN: EMERGENCY_STOP -> DAILY_RISK_LOCK ->
        POSITION_EXITING -> SCANNING. Checks reachability, not just the
        first edge, because that is the shape the bug had.
        """
        reachable, frontier = {AgentState.EMERGENCY_STOP}, [
            AgentState.EMERGENCY_STOP]
        while frontier:
            for nxt in allowed_targets(frontier.pop()):
                if nxt not in reachable:
                    reachable.add(nxt)
                    frontier.append(nxt)
        self.assertEqual(reachable, {AgentState.EMERGENCY_STOP})

    def test_a_daily_risk_lock_can_still_escalate_to_an_emergency_stop(self):
        """The falsifying control: escalation must remain possible."""
        self.assertIn(AgentState.EMERGENCY_STOP,
                      allowed_targets(AgentState.DAILY_RISK_LOCK))

    def test_invalid_transitions_are_rejected(self):
        bad = [
            (AgentState.OFFLINE, AgentState.POSITION_OPEN),
            (AgentState.MARKET_CLOSED, AgentState.SCANNING),
            (AgentState.OFFLINE, AgentState.TRADE_CANDIDATE),
        ]
        for source, target in bad:
            with self.subTest(source=source, target=target):
                self.assertFalse(can_transition(source, target))
                session = AgentSession(session_date="2026-09-30",
                                       agent_state=source)
                with self.assertRaises(InvalidTransition):
                    session.transition_to(target)

    def test_emergency_stop_is_not_exited_by_a_transition(self):
        session = AgentSession(session_date="2026-09-30")
        session.trigger_emergency_stop("test halt")
        for target in (AgentState.SCANNING, AgentState.OFFLINE,
                       AgentState.PRE_MARKET):
            with self.subTest(target=target):
                with self.assertRaises(InvalidTransition):
                    session.transition_to(target)

    def test_risk_lock_blocks_every_exposure_increasing_state(self):
        for target in EXPOSURE_INCREASING_STATES:
            with self.subTest(target=target):
                self.assertFalse(
                    can_transition(AgentState.DAILY_RISK_LOCK, target),
                    f"risk lock must not permit new exposure via {target}",
                )

    def test_risk_lock_still_permits_winding_down_positions(self):
        """A lock that also blocked exits would trap the agent in its
        positions, which is the opposite of risk control."""
        self.assertTrue(
            can_transition(AgentState.DAILY_RISK_LOCK,
                           AgentState.POSITION_EXITING)
        )
        self.assertTrue(
            can_transition(AgentState.DAILY_RISK_LOCK,
                           AgentState.MARKET_CLOSED)
        )

    def test_same_state_is_idempotent_and_records_nothing(self):
        session = AgentSession(session_date="2026-09-30")
        session.transition_to(AgentState.PRE_MARKET)
        history = len(session.state_history)
        changed = session.transition_to(AgentState.PRE_MARKET)
        self.assertFalse(changed)
        self.assertEqual(len(session.state_history), history)

    def test_transitions_are_timestamped_and_logged(self):
        session = AgentSession(session_date="2026-09-30")
        session.transition_to(AgentState.PRE_MARKET, "market opens soon")
        entry = session.state_history[-1]
        self.assertEqual(entry["source"], "OFFLINE")
        self.assertEqual(entry["target"], "PRE_MARKET")
        self.assertEqual(entry["reason"], "market opens soon")
        self.assertTrue(entry["at"].startswith("20"))


class TestSafetyDefaults(unittest.TestCase):
    def test_trading_is_disabled_by_default(self):
        self.assertFalse(AgentSession(session_date="2026-09-30").trading_enabled)
        self.assertFalse(AgentConfig().trading_enabled)

    def test_emergency_stop_disables_trading(self):
        session = AgentSession(session_date="2026-09-30", trading_enabled=True)
        session.trigger_emergency_stop("kill")
        self.assertTrue(session.emergency_stop)
        self.assertFalse(session.trading_enabled)

    def test_repeated_emergency_stop_cannot_re_enable_trading(self):
        """Idempotence matters: a second call must not take the 'already
        stopped' path and leave flags untouched."""
        session = AgentSession(session_date="2026-09-30")
        session.trigger_emergency_stop("first")
        session.trading_enabled = True          # simulate a bad write
        session.trigger_emergency_stop("second")
        self.assertFalse(session.trading_enabled)

    def test_clearing_emergency_stop_leaves_trading_disabled(self):
        session = AgentSession(session_date="2026-09-30")
        session.trigger_emergency_stop("halt")
        session.clear_emergency_stop("operator cleared")
        self.assertFalse(session.emergency_stop)
        self.assertIs(session.agent_state, AgentState.OFFLINE)
        self.assertFalse(session.trading_enabled,
                         "clearing a halt must not re-enable trading")


class TestSerialisation(unittest.TestCase):
    def test_round_trip_preserves_state(self):
        session = AgentSession(session_date="2026-09-30")
        session.transition_to(AgentState.PRE_MARKET, "prep")
        session.market_status = MarketSession.PRE_MARKET
        session.record_regime("BULLISH", 0.4, 0.8, "NORMAL", {"a": 1})

        restored = AgentSession.from_dict(session.as_dict())

        self.assertIs(restored.agent_state, AgentState.PRE_MARKET)
        self.assertIs(restored.market_status, MarketSession.PRE_MARKET)
        self.assertEqual(restored.market_regime, "BULLISH")
        self.assertEqual(restored.market_regime_score, 0.4)
        self.assertEqual(restored.revision, session.revision)

    def test_unknown_fields_are_ignored(self):
        """A record written by a newer version must still load."""
        d = AgentSession(session_date="2026-09-30").as_dict()
        d["some_future_field"] = "value"
        self.assertEqual(
            AgentSession.from_dict(d).session_date, "2026-09-30"
        )


class TestRegimeTransitionRecording(unittest.TestCase):
    def test_meaningful_change_is_recorded(self):
        session = AgentSession(session_date="2026-09-30")
        session.record_regime("NEUTRAL", 0.05, 0.7, "NORMAL")
        t = session.record_regime("BULLISH", 0.35, 0.8, "NORMAL")
        self.assertIsNotNone(t)
        self.assertEqual(t.previous_regime, "NEUTRAL")
        self.assertEqual(t.new_regime, "BULLISH")
        self.assertEqual(len(session.regime_history), 2)

    def test_boundary_wobble_is_not_recorded(self):
        """A label flip driven by a 0.01 score move is noise, not a new
        market environment."""
        session = AgentSession(session_date="2026-09-30")
        session.record_regime("NEUTRAL", 0.199, 0.7, "NORMAL")
        history = len(session.regime_history)
        t = session.record_regime("BULLISH", 0.201, 0.7, "NORMAL",
                                  min_score_delta=0.10)
        self.assertIsNone(t, "tiny score move must not log a transition")
        self.assertEqual(len(session.regime_history), history)
        # The label still updates; only the history entry is suppressed.
        self.assertEqual(session.market_regime, "BULLISH")

    def test_moving_to_unknown_is_always_recorded(self):
        """Availability changing is always worth knowing about, however
        small the score move."""
        session = AgentSession(session_date="2026-09-30")
        session.record_regime("BULLISH", 0.30, 0.8, "NORMAL")
        t = session.record_regime("UNKNOWN", None, 0.0, "NO_NEW_TRADES",
                                  min_score_delta=0.10)
        self.assertIsNotNone(t)

    def test_same_regime_records_no_transition(self):
        session = AgentSession(session_date="2026-09-30")
        session.record_regime("BULLISH", 0.30, 0.8, "NORMAL")
        t = session.record_regime("BULLISH", 0.55, 0.8, "NORMAL")
        self.assertIsNone(t)


class TestInMemoryStore(unittest.TestCase):
    def setUp(self):
        self.store = InMemoryStateStore()

    def test_get_or_create_is_stable(self):
        a = self.store.get_or_create("2026-09-30")
        b = self.store.get_or_create("2026-09-30")
        self.assertEqual(a.session_date, b.session_date)
        self.assertEqual(b.revision, a.revision)

    def test_stale_write_is_rejected(self):
        """The property that stops one invocation erasing another's halt."""
        self.store.get_or_create("2026-09-30")
        first = self.store.get("2026-09-30")
        second = self.store.get("2026-09-30")

        first.transition_to(AgentState.PRE_MARKET)
        self.store.put(first, expected_revision=0)

        second.transition_to(AgentState.SCANNING)
        with self.assertRaises(ConcurrentUpdate):
            self.store.put(second, expected_revision=0)

    def test_creating_twice_is_rejected(self):
        self.store.get_or_create("2026-09-30")
        with self.assertRaises(ConcurrentUpdate):
            self.store.put(AgentSession(session_date="2026-09-30"),
                           expected_revision=None)

    def test_stored_sessions_are_isolated_copies(self):
        """Mutating a returned session must not change the store."""
        self.store.get_or_create("2026-09-30")
        got = self.store.get("2026-09-30")
        got.transition_to(AgentState.PRE_MARKET)
        self.assertIs(self.store.get("2026-09-30").agent_state,
                      AgentState.OFFLINE)


class TestAgentStateService(unittest.TestCase):
    def setUp(self):
        self.store = InMemoryStateStore()
        self.svc = AgentStateService(self.store, clock=lambda: "2026-09-30")

    def test_session_is_created_with_safe_defaults(self):
        s = self.svc.get_session()
        self.assertIs(s.agent_state, AgentState.OFFLINE)
        self.assertFalse(s.trading_enabled)
        self.assertEqual(s.daily_capital_limit, 50.00)

    def test_transition_persists(self):
        self.svc.transition(AgentState.PRE_MARKET, "prep")
        self.assertIs(self.svc.get_session().agent_state, AgentState.PRE_MARKET)

    def test_state_survives_a_new_service_instance(self):
        """Stands in for a new Lambda invocation against the same store."""
        self.svc.transition(AgentState.PRE_MARKET)
        self.svc.transition(AgentState.SCANNING)

        fresh = AgentStateService(self.store, clock=lambda: "2026-09-30")
        self.assertIs(fresh.get_session().agent_state, AgentState.SCANNING)

    def test_concurrent_update_is_retried_not_lost(self):
        """A competing writer between our read and write must not cause the
        change to be dropped."""
        self.svc.get_session()
        original_put = self.store.put
        calls = {"n": 0}

        def flaky_put(session, expected_revision=None):
            calls["n"] += 1
            if calls["n"] == 1:
                # Simulate someone else committing first.
                other = self.store.get("2026-09-30")
                other.market_status = MarketSession.OPEN
                other._touch()
                original_put(other, expected_revision=expected_revision)
                raise ConcurrentUpdate("2026-09-30", expected_revision or 0)
            return original_put(session, expected_revision=expected_revision)

        self.store.put = flaky_put
        self.svc.transition(AgentState.PRE_MARKET, "retry path")
        self.store.put = original_put

        final = self.svc.get_session()
        self.assertIs(final.agent_state, AgentState.PRE_MARKET)
        self.assertIs(final.market_status, MarketSession.OPEN,
                      "the competing write must survive too")

    def test_emergency_stop_through_the_service(self):
        self.svc.transition(AgentState.PRE_MARKET)
        s = self.svc.trigger_emergency_stop("operator")
        self.assertTrue(s.emergency_stop)
        self.assertIs(s.agent_state, AgentState.EMERGENCY_STOP)
        self.assertFalse(s.trading_enabled)

    def test_risk_lock_through_the_service(self):
        s = self.svc.set_daily_risk_lock("loss limit reached")
        self.assertTrue(s.daily_risk_lock)
        self.assertIs(s.agent_state, AgentState.DAILY_RISK_LOCK)


class TestConfigValidation(unittest.TestCase):
    def test_default_weights_sum_to_one(self):
        RegimeWeights().validate()

    def test_weights_that_do_not_sum_to_one_are_rejected(self):
        with self.assertRaises(ValueError):
            RegimeWeights(spy_trend=0.9).validate()

    def test_negative_weight_is_rejected(self):
        with self.assertRaises(ValueError):
            RegimeWeights(spy_trend=-0.1, qqq_trend=0.35).validate()

    def test_default_config_is_valid(self):
        AgentConfig().validate()

    def test_daily_bars_must_cover_the_long_average(self):
        from dataclasses import replace
        cfg = AgentConfig()
        bad = replace(cfg, regime=replace(cfg.regime, daily_bars=10))
        with self.assertRaises(ValueError):
            bad.validate()


if __name__ == "__main__":
    unittest.main(verbosity=2)

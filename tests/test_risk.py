"""
Risk Governor.

The safety boundary, so these tests are adversarial by design: each one
asks "can this be made to approve something it should not?"

Three structural properties are asserted directly, because they are what
make the rest trustworthy:

  * approval is DERIVED from the absence of rejection codes, so nothing
    can set it
  * missing data is a rejection, never a pass
  * a global halt survives session rollover
"""
import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from agent.hypothesis import generate                             # noqa: E402
from agent.risk import (                                          # noqa: E402
    GlobalHaltState, HaltStoreError, InMemoryHaltStore, RejectionCode,
    RiskContext, RiskDecision, RiskLimits, evaluate, position_size,
    should_engage_risk_lock,
)
from tests.test_hypothesis import catalyst, regime, signal        # noqa: E402


def good_hypothesis():
    return generate("XYZ", signal(), catalyst(), regime())


def context(**overrides):
    base = dict(session_date="2026-09-30", market_session="OPEN",
                minutes_to_close=180.0, trading_enabled=True,
                execution_available=True, price=100.0, spread_pct=0.05,
                dollar_volume=5e8, quote_age_seconds=10.0)
    base.update(overrides)
    return RiskContext(**base)


def codes(decision):
    return set(decision.reason_codes)


class TestApprovalCannotBeForced(unittest.TestCase):
    """The structural guarantee the whole component rests on."""

    def test_a_clean_setup_is_approved(self):
        d = evaluate(good_hypothesis(), context())
        self.assertTrue(d.approved, d.reasons)
        self.assertEqual(d.reason_codes, [])

    def test_approved_is_derived_not_stored(self):
        """
        There is no `approved` field to set. It is a property computed
        from the codes, which is what makes an override impossible
        rather than merely discouraged.
        """
        self.assertNotIn("approved", RiskDecision.__dataclass_fields__)
        self.assertIsInstance(
            type(RiskDecision(decision_id="d", symbol="X",
                              hypothesis_id=None)).approved, property)

    def test_a_decision_with_codes_can_never_report_approved(self):
        d = RiskDecision(decision_id="d", symbol="X", hypothesis_id=None,
                         reason_codes=[RejectionCode.SPREAD_TOO_WIDE])
        self.assertFalse(d.approved)
        with self.assertRaises(AttributeError):
            d.approved = True           # noqa: B010

    def test_every_rejection_carries_a_reason(self):
        d = evaluate(good_hypothesis(), context(spread_pct=2.0))
        self.assertFalse(d.approved)
        self.assertEqual(len(d.reasons), len(d.reason_codes))
        for reason in d.reasons:
            self.assertGreater(len(reason), 10,
                               "a code with no explanation is a bug report")

    def test_a_decision_snapshots_the_limits_that_produced_it(self):
        """Without this, "why was this allowed" is unanswerable later."""
        d = evaluate(good_hypothesis(), context())
        self.assertEqual(d.limits_version, RiskLimits().version)
        self.assertIn("daily_capital_limit", d.limits_snapshot)
        self.assertIn("spread_pct", d.context_snapshot)


class TestGlobalHalts(unittest.TestCase):

    def test_trading_disabled_blocks_everything(self):
        d = evaluate(good_hypothesis(), context(trading_enabled=False))
        self.assertIn(RejectionCode.TRADING_DISABLED, codes(d))

    def test_execution_unavailable_blocks_everything(self):
        d = evaluate(good_hypothesis(), context(execution_available=False))
        self.assertIn(RejectionCode.EXECUTION_UNAVAILABLE, codes(d))

    def test_emergency_stop_blocks_everything(self):
        d = evaluate(good_hypothesis(),
                     context(emergency_stop=True,
                             emergency_stop_reason="operator halt"))
        self.assertIn(RejectionCode.EMERGENCY_STOP, codes(d))
        self.assertTrue(d.requires_halt)

    def test_a_durable_global_halt_blocks_even_a_clean_context(self):
        """
        The Milestone 3 deferral. The halt is passed separately from the
        session context precisely so a fresh session cannot clear it.
        """
        halt = GlobalHaltState(halted=True, reason="halted yesterday")
        d = evaluate(good_hypothesis(), context(), halt=halt)
        self.assertIn(RejectionCode.EMERGENCY_STOP, codes(d))

    def test_daily_risk_lock_blocks_new_exposure(self):
        d = evaluate(good_hypothesis(), context(daily_risk_lock=True))
        self.assertIn(RejectionCode.DAILY_RISK_LOCK, codes(d))
        self.assertTrue(d.requires_halt)


class TestFailClosed(unittest.TestCase):
    """Missing data is a rejection. A provider outage must never become
    permission to trade."""

    def test_unknown_spread_is_rejected_not_treated_as_tight(self):
        d = evaluate(good_hypothesis(), context(spread_pct=None))
        self.assertIn(RejectionCode.SPREAD_TOO_WIDE, codes(d))

    def test_unknown_quote_age_is_rejected_not_treated_as_fresh(self):
        d = evaluate(good_hypothesis(), context(quote_age_seconds=None))
        self.assertIn(RejectionCode.STALE_MARKET_DATA, codes(d))

    def test_unknown_liquidity_is_rejected(self):
        d = evaluate(good_hypothesis(), context(dollar_volume=None))
        self.assertIn(RejectionCode.INSUFFICIENT_LIQUIDITY, codes(d))

    def test_missing_price_is_rejected(self):
        d = evaluate(good_hypothesis(), context(price=None))
        self.assertIn(RejectionCode.STALE_MARKET_DATA, codes(d))

    def test_unknown_time_to_close_is_rejected(self):
        d = evaluate(good_hypothesis(), context(minutes_to_close=None))
        self.assertIn(RejectionCode.TOO_LATE_IN_SESSION, codes(d))

    def test_no_hypothesis_is_rejected(self):
        d = evaluate(None, context())
        self.assertIn(RejectionCode.HYPOTHESIS_NOT_ACTIONABLE, codes(d))


class TestMarketQuality(unittest.TestCase):

    def test_wide_spread_is_rejected(self):
        d = evaluate(good_hypothesis(), context(spread_pct=1.5))
        self.assertIn(RejectionCode.SPREAD_TOO_WIDE, codes(d))

    def test_stale_quote_is_rejected(self):
        d = evaluate(good_hypothesis(), context(quote_age_seconds=600))
        self.assertIn(RejectionCode.STALE_MARKET_DATA, codes(d))

    def test_thin_liquidity_is_rejected(self):
        d = evaluate(good_hypothesis(), context(dollar_volume=1_000_000))
        self.assertIn(RejectionCode.INSUFFICIENT_LIQUIDITY, codes(d))

    def test_price_outside_the_band_is_rejected(self):
        for price in (2.0, 5000.0):
            with self.subTest(price=price):
                d = evaluate(good_hypothesis(), context(price=price))
                self.assertIn(RejectionCode.PRICE_OUT_OF_RANGE, codes(d))


class TestExposure(unittest.TestCase):

    def test_max_concurrent_positions(self):
        d = evaluate(good_hypothesis(), context(open_positions=2))
        self.assertIn(RejectionCode.MAX_POSITIONS_REACHED, codes(d))

    def test_max_new_positions_per_day(self):
        d = evaluate(good_hypothesis(), context(positions_opened_today=3))
        self.assertIn(RejectionCode.MAX_NEW_POSITIONS_REACHED, codes(d))

    def test_averaging_down_is_prohibited(self):
        """
        Adding to a losing position is the most reliable way to turn a
        small loss into an account-sized one.
        """
        d = evaluate(good_hypothesis(), context(held_symbols=["XYZ"]))
        self.assertIn(RejectionCode.ALREADY_HOLDING, codes(d))
        self.assertIn(RejectionCode.AVERAGING_DOWN_PROHIBITED, codes(d))

    def test_holding_a_different_symbol_does_not_block(self):
        d = evaluate(good_hypothesis(),
                     context(held_symbols=["AAA"], open_positions=1))
        self.assertNotIn(RejectionCode.ALREADY_HOLDING, codes(d))


class TestCapital(unittest.TestCase):

    def test_capital_is_a_ceiling_not_a_target(self):
        """Nothing here tries to deploy the allocation."""
        d = evaluate(good_hypothesis(), context())
        self.assertLessEqual(d.capital_required,
                             RiskLimits().daily_capital_limit)

    def test_exhausted_daily_capital_is_rejected(self):
        d = evaluate(good_hypothesis(),
                     context(capital_deployed_today=50.0))
        self.assertIn(RejectionCode.INSUFFICIENT_CAPITAL, codes(d))

    def test_single_name_concentration_is_capped(self):
        limits = RiskLimits()
        d = evaluate(good_hypothesis(), context())
        self.assertLessEqual(d.position_value, limits.max_position_value + 1e-9)

    def test_per_trade_loss_never_exceeds_the_limit(self):
        limits = RiskLimits()
        for stop in (1.0, 2.5, 5.0, 8.0):
            with self.subTest(stop=stop):
                h = good_hypothesis()
                h.suggested_stop_distance_pct = stop
                d = evaluate(h, context(), limits)
                self.assertLessEqual(d.max_loss, limits.max_trade_risk + 1e-6)

    def test_a_trade_that_would_breach_the_daily_loss_limit_is_rejected(self):
        d = evaluate(good_hypothesis(), context(realized_pnl_today=-4.50))
        self.assertIn(RejectionCode.DAILY_RISK_LOCK, codes(d))

    def test_sizing_is_by_risk_not_by_conviction(self):
        """
        Sizing on strength would mean the most confident-looking setups
        carry the largest losses - precisely backwards.
        """
        weak = good_hypothesis()
        weak.hypothesis_strength = 0.40
        strong = good_hypothesis()
        strong.hypothesis_strength = 0.99
        self.assertEqual(position_size(weak, context(), RiskLimits())[0],
                         position_size(strong, context(), RiskLimits())[0])

    def test_a_wider_stop_produces_a_smaller_position(self):
        tight, wide = good_hypothesis(), good_hypothesis()
        tight.suggested_stop_distance_pct = 1.0
        wide.suggested_stop_distance_pct = 8.0
        self.assertGreater(position_size(tight, context(), RiskLimits())[0],
                           position_size(wide, context(), RiskLimits())[0])

    def test_the_daily_ceiling_guard_fires_when_reached_directly(self):
        """
        `position_size` caps at the remaining allocation, so the ceiling
        check inside `_check_capital` is unreachable through `evaluate`.
        It is defence in depth against a future change to sizing, and
        an unreachable guard that nothing tests is a guard that will
        rot - so it is exercised directly here.
        """
        from agent.risk.governor import _Rejections, _check_capital
        rej = _Rejections()
        _check_capital(rej, value=80.0, max_loss=1.0, remaining=20.0,
                       context=context(), limits=RiskLimits())
        self.assertIn(RejectionCode.DAILY_CAPITAL_EXCEEDED, set(rej.codes))

    def test_the_single_name_cap_guard_fires_when_reached_directly(self):
        from agent.risk.governor import _Rejections, _check_capital
        limits = RiskLimits()
        rej = _Rejections()
        _check_capital(rej, value=limits.max_position_value + 5.0,
                       max_loss=1.0, remaining=1000.0,
                       context=context(), limits=limits)
        self.assertIn(RejectionCode.POSITION_SIZE_EXCEEDED, set(rej.codes))

    def test_sizing_never_exceeds_the_remaining_allocation(self):
        """The property that makes the guard above unreachable."""
        limits = RiskLimits()
        for deployed in (0.0, 10.0, 25.0, 45.0, 49.0):
            with self.subTest(deployed=deployed):
                value, _loss, remaining = position_size(
                    good_hypothesis(),
                    context(capital_deployed_today=deployed), limits)
                self.assertLessEqual(value, remaining + 1e-9)

    def test_a_position_below_the_practical_minimum_is_rejected(self):
        d = evaluate(good_hypothesis(),
                     context(capital_deployed_today=48.0))
        self.assertIn(RejectionCode.INSUFFICIENT_CAPITAL, codes(d))


class TestInstrumentPolicy(unittest.TestCase):

    def test_leveraged_products_are_rejected(self):
        d = evaluate(good_hypothesis(), context(is_leveraged=True))
        self.assertIn(RejectionCode.INSTRUMENT_NOT_PERMITTED, codes(d))

    def test_otc_is_rejected(self):
        d = evaluate(good_hypothesis(), context(is_otc=True))
        self.assertIn(RejectionCode.INSTRUMENT_NOT_PERMITTED, codes(d))

    def test_non_equity_instruments_are_rejected(self):
        for kind in ("option", "crypto", "future"):
            with self.subTest(kind=kind):
                d = evaluate(good_hypothesis(), context(security_type=kind))
                self.assertIn(RejectionCode.INSTRUMENT_NOT_PERMITTED, codes(d))

    def test_a_non_long_hypothesis_is_rejected(self):
        h = generate("XYZ", signal(direction="SELL", buy_groups=0,
                                   sell_groups=2), None, regime())
        d = evaluate(h, context())
        self.assertIn(RejectionCode.NOT_LONG_ONLY, codes(d))


class TestSession(unittest.TestCase):

    def test_a_closed_market_is_rejected(self):
        for session in ("CLOSED", "PREMARKET", "AFTER_HOURS", "UNKNOWN"):
            with self.subTest(session=session):
                d = evaluate(good_hypothesis(),
                             context(market_session=session))
                self.assertIn(RejectionCode.MARKET_CLOSED, codes(d))

    def test_no_new_entries_close_to_the_bell(self):
        d = evaluate(good_hypothesis(), context(minutes_to_close=15.0))
        self.assertIn(RejectionCode.TOO_LATE_IN_SESSION, codes(d))

    def test_mid_session_is_fine(self):
        d = evaluate(good_hypothesis(), context(minutes_to_close=200.0))
        self.assertNotIn(RejectionCode.TOO_LATE_IN_SESSION, codes(d))


class TestEvents(unittest.TestCase):

    def test_an_imminent_binary_event_is_rejected(self):
        """The outcome is not a function of the setup."""
        d = evaluate(good_hypothesis(),
                     context(imminent_binary_event=True,
                             imminent_event_detail="earnings after close"))
        self.assertIn(RejectionCode.IMMINENT_BINARY_EVENT, codes(d))


class TestHypothesisQuality(unittest.TestCase):

    def test_a_non_actionable_hypothesis_is_rejected(self):
        h = generate("XYZ", signal(direction="NO_SIGNAL", buy_groups=0,
                                   agreement=0, magnitude=0), None, regime())
        d = evaluate(h, context())
        self.assertIn(RejectionCode.HYPOTHESIS_NOT_ACTIONABLE, codes(d))

    def test_a_weak_hypothesis_is_rejected(self):
        h = good_hypothesis()
        h.hypothesis_strength = 0.10
        d = evaluate(h, context())
        self.assertIn(RejectionCode.HYPOTHESIS_TOO_WEAK, codes(d))


class TestRiskLimitsValidation(unittest.TestCase):

    def test_default_limits_are_valid(self):
        RiskLimits().validate()

    def test_daily_capital_cannot_exceed_the_permitted_maximum(self):
        with self.assertRaises(ValueError):
            RiskLimits(daily_capital_limit=500.0).validate()

    def test_one_trade_cannot_risk_more_than_the_whole_day(self):
        with self.assertRaises(ValueError):
            RiskLimits(max_trade_risk=10.0, daily_loss_limit=5.0).validate()

    def test_prohibited_instruments_cannot_be_enabled_by_configuration(self):
        for flag in ("allow_options", "allow_margin", "allow_leverage",
                     "allow_shorting", "allow_crypto", "allow_otc",
                     "allow_averaging_down"):
            with self.subTest(flag=flag):
                with self.assertRaises(ValueError):
                    RiskLimits(**{flag: True}).validate()

    def test_long_only_cannot_be_turned_off(self):
        with self.assertRaises(ValueError):
            RiskLimits(long_only=False).validate()

    def test_limits_are_frozen(self):
        limits = RiskLimits()
        with self.assertRaises(Exception):
            limits.daily_capital_limit = 1000.0   # noqa: B010

    def test_configuration_up_to_one_hundred_is_allowed(self):
        RiskLimits(daily_capital_limit=100.0).validate()


class TestRiskLock(unittest.TestCase):

    def test_unrealised_losses_count_toward_the_daily_limit(self):
        """
        Waiting for a loss to be realised would mean the limit is only
        enforced after the damage is taken.
        """
        locked, reason = should_engage_risk_lock(0.0, -5.5, RiskLimits())
        self.assertTrue(locked)
        self.assertIn("unrealised", reason)

    def test_combined_losses_count(self):
        locked, _r = should_engage_risk_lock(-3.0, -2.5, RiskLimits())
        self.assertTrue(locked)

    def test_a_profitable_day_does_not_lock(self):
        locked, _r = should_engage_risk_lock(4.0, 1.0, RiskLimits())
        self.assertFalse(locked)


class TestGlobalHaltStore(unittest.TestCase):
    """
    The Milestone 3 deferral, fixed. A stop that expires by itself is
    not a stop.
    """

    def setUp(self):
        self.store = InMemoryHaltStore()

    def test_a_fresh_system_is_not_halted(self):
        self.assertFalse(self.store.get().halted)

    def test_engaging_a_halt_persists_it(self):
        self.store.engage("provider outage", engaged_by="monitor")
        state = self.store.get()
        self.assertTrue(state.halted)
        self.assertEqual(state.reason, "provider outage")
        self.assertEqual(state.engaged_by, "monitor")

    def test_the_halt_has_no_session_date(self):
        """
        Structural: a record with no session cannot be replaced by a
        fresh one when the day rolls over.
        """
        self.store.engage("halted")
        self.assertNotIn("session_date", self.store.get().as_dict())

    def test_a_halt_survives_a_session_rollover(self):
        self.store.engage("halted on Monday")
        # A new session begins. The session store would be blank; the
        # halt store is not consulted through it and is unaffected.
        tomorrow = context(session_date="2026-10-01")
        d = evaluate(good_hypothesis(), tomorrow, halt=self.store.get())
        self.assertIn(RejectionCode.EMERGENCY_STOP, codes(d))

    def test_engaging_twice_keeps_the_original_reason(self):
        self.store.engage("first cause")
        self.store.engage("second cause")
        self.assertEqual(self.store.get().reason, "first cause")

    def test_clearing_requires_naming_who_did_it(self):
        self.store.engage("halted")
        with self.assertRaises(HaltStoreError):
            self.store.clear(cleared_by="")
        self.assertTrue(self.store.get().halted)

    def test_clearing_records_who_and_when(self):
        self.store.engage("halted")
        state = self.store.clear(cleared_by="operator", reason="resolved")
        self.assertFalse(state.halted)
        self.assertEqual(state.cleared_by, "operator")
        self.assertIsNotNone(state.cleared_at)

    def test_clearing_lets_trading_resume(self):
        self.store.engage("halted")
        self.store.clear(cleared_by="operator")
        d = evaluate(good_hypothesis(), context(), halt=self.store.get())
        self.assertNotIn(RejectionCode.EMERGENCY_STOP, codes(d))

    def test_concurrent_clears_cannot_both_win(self):
        self.store.engage("halted")
        stale = self.store.get()
        self.store.clear(cleared_by="first")
        with self.assertRaises(HaltStoreError):
            self.store._put(stale, stale.revision - 1)

    def test_reads_are_copies(self):
        self.store.engage("halted")
        state = self.store.get()
        state.halted = False
        self.assertTrue(self.store.get().halted)


class TestPurity(unittest.TestCase):

    def test_the_governor_is_a_pure_function_of_its_inputs(self):
        """
        No I/O, no clock, no provider - so a decision can be replayed
        exactly from its stored snapshots.
        """
        h = good_hypothesis()
        c = context()
        first = evaluate(h, c)
        second = evaluate(h, c)
        self.assertEqual(first.approved, second.approved)
        self.assertEqual(first.reason_codes, second.reason_codes)
        self.assertEqual(first.capital_required, second.capital_required)

    def test_decision_ids_do_not_collide(self):
        ids = {RiskDecision.make_id("X", "2026-09-30T20:00:00")
               for _ in range(200)}
        self.assertEqual(len(ids), 200)


if __name__ == "__main__":
    unittest.main()

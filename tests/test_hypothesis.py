"""
Trade Hypothesis Engine.

The property under test throughout: the quantitative reading and the
published evidence may disagree, and every combination must produce an
honest, inspectable result rather than being forced into a trade or
forced out of one.
"""
import json
import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from agent.hypothesis import (                                   # noqa: E402
    CONFIG_VERSION, ContradictionSeverity, HypothesisDirection,
    HypothesisRun, HypothesisService, HypothesisStatus,
    InMemoryHypothesisStore, Strategy, choose_strategy, compute_strength,
    find_contradictions, generate, suggest_stop_distance,
)
from agent.hypothesis.engine import (                             # noqa: E402
    _evidence_view, _market_view, _quant_view,
)


def signal(direction="BUY", agreement=1.0, magnitude=0.76, freshness="FRESH",
           sell_groups=0, buy_groups=2, volatility=2.5, price=100.0):
    return {
        "direction": direction, "signal_agreement": agreement,
        "signal_magnitude": magnitude,
        "regime_adjusted_magnitude": magnitude * 0.9,
        "strength_band": "STRONG", "buy_groups": buy_groups,
        "sell_groups": sell_groups,
        "opinionated_groups": buy_groups + sell_groups, "price": price,
        "data_quality": {"freshness": freshness},
        "indicators": {"volatility_pct": volatility},
    }


def catalyst(active=True, direction="POSITIVE", materiality=0.89,
             novelty=0.91, conflicting=False, ctype="GUIDANCE", sources=2):
    return {
        "has_active_catalyst": active, "direction": direction,
        "evidence_score": 0.8, "conflicting_evidence": conflicting,
        "independent_source_count": sources, "primary_source_count": 1,
        "primary_catalyst": {"type": ctype, "materiality": materiality,
                             "novelty": novelty, "window": "RECENT"},
    }


NO_CATALYST = {"has_active_catalyst": False, "direction": "NEUTRAL",
               "primary_catalyst": None, "independent_source_count": 0,
               "primary_source_count": 0, "conflicting_evidence": False}


def regime(name="BULLISH", posture="NORMAL"):
    return {"regime": name, "regime_confidence": 0.7,
            "risk_posture": posture, "market_session": "OPEN"}


def codes(hypothesis):
    return {c.code for c in hypothesis.contradictions}


class TestStrongAlignedCase(unittest.TestCase):

    def setUp(self):
        self.h = generate("XYZ", signal(), catalyst(), regime())

    def test_produces_a_momentum_catalyst_hypothesis(self):
        self.assertIs(self.h.strategy, Strategy.MOMENTUM_CATALYST)
        self.assertIs(self.h.direction, HypothesisDirection.LONG)
        self.assertIs(self.h.status, HypothesisStatus.PROPOSED)

    def test_strength_is_high_and_decomposable(self):
        self.assertGreater(self.h.hypothesis_strength, 0.7)
        self.assertIn("signal_agreement", self.h.strength_components)
        self.assertIn("evidence_support", self.h.strength_components)
        self.assertIn("market_regime", self.h.strength_components)

    def test_it_is_actionable_but_not_approved(self):
        """Actionable means "worth evaluating", never "approved"."""
        self.assertTrue(self.h.is_actionable)
        self.assertIsNot(self.h.status, HypothesisStatus.RISK_APPROVED)

    BANNED_FIELDS = frozenset({
        "quantity", "shares", "order_type", "limit_price", "stop_price",
        "take_profit", "position_size", "broker", "buying_power",
        "notional", "order_id", "fill_price",
    })

    @staticmethod
    def _all_keys(obj, out=None):
        out = set() if out is None else out
        if isinstance(obj, dict):
            for key, value in obj.items():
                out.add(key)
                TestStrongAlignedCase._all_keys(value, out)
        elif isinstance(obj, list):
            for value in obj:
                TestStrongAlignedCase._all_keys(value, out)
        return out

    def test_carries_no_order_fields(self):
        """
        Checked on the KEYS, not the serialised text. The disclaimer
        legitimately says "carries no order, quantity or price" - a
        denial - and a substring scan flagged its own disclaimer.
        """
        keys = self._all_keys(self.h.as_dict())
        self.assertEqual(keys & self.BANNED_FIELDS, set())

    def test_falsifying_control_the_field_scan_can_fail(self):
        payload = self.h.as_dict()
        payload["quantity"] = 10
        self.assertTrue(self._all_keys(payload) & self.BANNED_FIELDS)

    def test_the_disclaimer_does_deny_order_semantics(self):
        """Stripping prose from the scan would pass if no denial existed."""
        self.assertIn("no order, quantity or price",
                      self.h.as_dict()["disclaimer"])

    def test_execution_is_unavailable(self):
        self.assertFalse(self.h.execution_available)

    def test_config_version_is_stamped(self):
        """Without it, later analysis cannot reproduce why this existed."""
        self.assertEqual(self.h.config_version, CONFIG_VERSION)


class TestQuantitativeOnly(unittest.TestCase):

    def test_quant_buy_with_no_catalyst_is_still_a_hypothesis(self):
        """
        Price action is real information even when nobody has published
        a reason for it. The absence is recorded, not used to veto.
        """
        h = generate("XYZ", signal(), NO_CATALYST, regime())
        self.assertTrue(h.is_actionable)
        self.assertIn(h.strategy, (Strategy.MOMENTUM, Strategy.BREAKOUT))
        self.assertIn("NO_CATALYST", codes(h))

    def test_the_missing_catalyst_is_stated_in_the_reasons(self):
        h = generate("XYZ", signal(), NO_CATALYST, regime())
        joined = " ".join(h.supporting_reasons)
        self.assertIn("no active catalyst", joined)

    def test_uncollected_evidence_is_distinguished_from_none_found(self):
        """
        "We did not look" and "we looked and found nothing" are
        different claims, and only one of them is an observation.
        """
        looked = generate("XYZ", signal(), NO_CATALYST, regime())
        did_not = generate("XYZ", signal(), None, regime())
        self.assertIn("NO_CATALYST", codes(looked))
        self.assertIn("EVIDENCE_NOT_COLLECTED", codes(did_not))
        self.assertLess(did_not.hypothesis_strength,
                        looked.hypothesis_strength)


class TestCatalystOnly(unittest.TestCase):

    def test_positive_catalyst_without_price_confirmation_is_not_a_trade(self):
        """
        Good news on a stock that is not moving is a story. The market
        has seen it and declined to act; buying here is trading the
        wire rather than the market.
        """
        h = generate("XYZ", signal(direction="NEUTRAL", agreement=0.0,
                                   magnitude=0.0, buy_groups=0),
                     catalyst(), regime())
        self.assertIs(h.strategy, Strategy.NO_VALID_STRATEGY)
        self.assertIs(h.direction, HypothesisDirection.NONE)
        self.assertEqual(h.hypothesis_strength, 0.0)

    def test_the_refusal_is_explained(self):
        h = generate("XYZ", signal(direction="NEUTRAL", agreement=0.0,
                                   magnitude=0.0, buy_groups=0),
                     catalyst(), regime())
        joined = " ".join(h.supporting_reasons)
        self.assertIn("price action does not confirm", joined)


class TestContradiction(unittest.TestCase):

    def test_material_negative_evidence_blocks_a_long(self):
        """
        The most expensive mistake available to this system: buying into
        a published negative because the chart looks good.
        """
        h = generate("XYZ", signal(),
                     catalyst(direction="NEGATIVE", ctype="SHARE_OFFERING"),
                     regime())
        self.assertIs(h.strategy, Strategy.NO_VALID_STRATEGY)
        self.assertIn("MATERIAL_NEGATIVE_EVIDENCE", codes(h))
        self.assertTrue(h.blocking_contradictions)

    def test_immaterial_negative_evidence_does_not_block(self):
        """A trivial negative should not veto a strong setup."""
        h = generate("XYZ", signal(),
                     catalyst(direction="NEGATIVE", materiality=0.1), regime())
        self.assertNotIn("MATERIAL_NEGATIVE_EVIDENCE", codes(h))

    def test_conflicting_evidence_is_a_major_contradiction(self):
        h = generate("XYZ", signal(), catalyst(conflicting=True), regime())
        conflict = [c for c in h.contradictions
                    if c.code == "CONFLICTING_EVIDENCE"]
        self.assertTrue(conflict)
        self.assertIs(conflict[0].severity, ContradictionSeverity.MAJOR)

    def test_an_opposing_signal_group_is_recorded(self):
        h = generate("XYZ", signal(sell_groups=1), catalyst(), regime())
        self.assertIn("OPPOSING_SIGNAL_GROUP", codes(h))

    def test_contradictions_reduce_strength(self):
        clean = generate("XYZ", signal(), catalyst(), regime())
        messy = generate("XYZ", signal(sell_groups=1),
                         catalyst(conflicting=True), regime())
        self.assertLess(messy.hypothesis_strength, clean.hypothesis_strength)

    def test_contradictions_are_itemised_not_netted(self):
        h = generate("XYZ", signal(sell_groups=1),
                     catalyst(conflicting=True), regime("MIXED"))
        self.assertGreaterEqual(len(h.contradictions), 3)
        for c in h.contradictions:
            self.assertTrue(c.detail, "every contradiction must explain itself")


class TestStaleEvidence(unittest.TestCase):

    def test_stale_price_data_blocks_a_hypothesis(self):
        for freshness in ("STALE", "MISSING", "UNKNOWN"):
            with self.subTest(freshness=freshness):
                h = generate("XYZ", signal(freshness=freshness), catalyst(),
                             regime())
                self.assertIs(h.strategy, Strategy.NO_VALID_STRATEGY)
                self.assertIn("STALE_QUANTITATIVE_DATA", codes(h))

    def test_fresh_data_does_not_trigger_the_block(self):
        h = generate("XYZ", signal(freshness="FRESH"), catalyst(), regime())
        self.assertNotIn("STALE_QUANTITATIVE_DATA", codes(h))


class TestWeakSignals(unittest.TestCase):

    def test_weak_agreement_is_flagged(self):
        h = generate("XYZ", signal(agreement=0.4), catalyst(), regime())
        self.assertIn("WEAK_SIGNAL_AGREEMENT", codes(h))

    def test_weak_magnitude_is_flagged(self):
        h = generate("XYZ", signal(magnitude=0.2), catalyst(), regime())
        self.assertIn("WEAK_SIGNAL_MAGNITUDE", codes(h))

    def test_weak_everything_scores_near_zero(self):
        h = generate("XYZ", signal(agreement=0.35, magnitude=0.2,
                                   sell_groups=1),
                     NO_CATALYST, regime("MIXED", "CAUTIOUS"))
        self.assertLess(h.hypothesis_strength, 0.2)


class TestMarketRegime(unittest.TestCase):

    def test_a_strongly_bearish_regime_blocks_new_long_exposure(self):
        """A long-only system in a falling market is choosing the one
        direction the market is punishing."""
        h = generate("XYZ", signal(), catalyst(), regime("STRONG_BEARISH"))
        self.assertIs(h.strategy, Strategy.NO_VALID_STRATEGY)
        self.assertIn("HOSTILE_REGIME", codes(h))

    def test_a_blocking_risk_posture_blocks_regardless_of_the_setup(self):
        for posture in ("NO_NEW_TRADES", "HALT"):
            with self.subTest(posture=posture):
                h = generate("XYZ", signal(), catalyst(),
                             regime("BULLISH", posture))
                self.assertIs(h.strategy, Strategy.NO_VALID_STRATEGY)
                self.assertIn("RISK_POSTURE_BLOCKS", codes(h))

    def test_an_unfavourable_regime_reduces_strength_without_blocking(self):
        good = generate("XYZ", signal(), catalyst(), regime("BULLISH"))
        poor = generate("XYZ", signal(), catalyst(), regime("MIXED"))
        self.assertTrue(poor.is_actionable)
        self.assertLess(poor.hypothesis_strength, good.hypothesis_strength)

    def test_unknown_regime_is_penalised_not_treated_as_calm(self):
        unknown = generate("XYZ", signal(), catalyst(), regime("UNKNOWN"))
        neutral = generate("XYZ", signal(), catalyst(), regime("NEUTRAL"))
        self.assertIn("UNKNOWN_REGIME", codes(unknown))
        self.assertLess(unknown.hypothesis_strength,
                        neutral.hypothesis_strength)


class TestNoValidHypothesis(unittest.TestCase):

    def test_no_quantitative_signal_produces_nothing(self):
        h = generate("XYZ", signal(direction="NO_SIGNAL", agreement=0.0,
                                   magnitude=0.0, buy_groups=0),
                     NO_CATALYST, regime())
        self.assertIs(h.strategy, Strategy.NO_VALID_STRATEGY)
        self.assertIs(h.status, HypothesisStatus.NOT_GENERATED)

    def test_a_sell_reading_never_becomes_a_long(self):
        """The system is long-only. A bearish reading is analysis."""
        h = generate("XYZ", signal(direction="SELL", buy_groups=0,
                                   sell_groups=2),
                     NO_CATALYST, regime())
        self.assertIs(h.direction, HypothesisDirection.NONE)

    def test_a_declined_hypothesis_still_records_why(self):
        """'We looked and declined' is what a decision log is for."""
        h = generate("XYZ", signal(direction="SELL", buy_groups=0,
                                   sell_groups=2),
                     NO_CATALYST, regime())
        self.assertTrue(h.supporting_reasons)
        self.assertTrue(h.contradictions or h.supporting_reasons)


class TestStopDistance(unittest.TestCase):

    def test_stop_scales_with_the_securitys_own_volatility(self):
        calm = suggest_stop_distance(1.0)
        wild = suggest_stop_distance(5.0)
        self.assertLess(calm, wild)

    def test_stop_is_bounded(self):
        self.assertGreaterEqual(suggest_stop_distance(0.01), 1.0)
        self.assertLessEqual(suggest_stop_distance(100.0), 8.0)

    def test_missing_volatility_gets_a_conservative_default(self):
        self.assertGreater(suggest_stop_distance(None), 0)

    def test_stop_is_a_distance_not_an_order(self):
        h = generate("XYZ", signal(), catalyst(), regime())
        self.assertIsNotNone(h.suggested_stop_distance_pct)
        self.assertNotIn("stop_price", h.as_dict())


class TestStrengthTransparency(unittest.TestCase):

    def test_components_sum_to_the_score(self):
        h = generate("XYZ", signal(), catalyst(), regime())
        total = sum(h.strength_components.values())
        self.assertAlmostEqual(total, h.hypothesis_strength, places=6)

    def test_strength_is_bounded(self):
        for s in (signal(), signal(agreement=0.0, magnitude=0.0),
                  signal(agreement=1.0, magnitude=1.0)):
            h = generate("XYZ", s, catalyst(), regime("STRONG_BULLISH"))
            self.assertTrue(0.0 <= h.hypothesis_strength <= 1.0)

    def test_strength_is_disclaimed_as_not_a_probability(self):
        payload = generate("XYZ", signal(), catalyst(), regime()).as_dict()
        self.assertIn("NOT a probability", payload["strength_meaning"])

    def test_evidence_contributes_nothing_when_it_does_not_support(self):
        with_support = generate("XYZ", signal(), catalyst(), regime())
        without = generate("XYZ", signal(), NO_CATALYST, regime())
        self.assertGreater(with_support.strength_components["evidence_support"],
                           0.0)
        self.assertEqual(without.strength_components["evidence_support"], 0.0)


class TestService(unittest.TestCase):

    def setUp(self):
        self.store = InMemoryHypothesisStore()
        self.svc = HypothesisService(store=self.store,
                                     clock=lambda: "2026-09-30")

    def _inputs(self):
        return [
            {"symbol": "AAA", "signal": signal(), "catalyst": catalyst()},
            {"symbol": "BBB", "signal": signal(direction="SELL", buy_groups=0,
                                               sell_groups=2),
             "catalyst": NO_CATALYST},
            {"symbol": "CCC", "signal": signal(agreement=0.6),
             "catalyst": NO_CATALYST},
        ]

    def test_run_generates_and_counts(self):
        run = self.svc.run(self._inputs(), regime())
        self.assertEqual(run.considered_count, 3)
        self.assertEqual(run.generated_count, 2)
        self.assertEqual(run.rejected_count, 1)

    def test_rejected_hypotheses_are_persisted_too(self):
        """A journal that records only trades cannot answer
        'why didn't you take BBB'."""
        run = self.svc.run(self._inputs(), regime())
        stored = self.store.get_run(run.hypothesis_run_id)
        symbols = {h["symbol"] for h in stored["hypotheses"]}
        self.assertEqual(symbols, {"AAA", "BBB", "CCC"})

    def test_actionable_is_sorted_by_strength(self):
        run = self.svc.run(self._inputs(), regime())
        actionable = self.svc.actionable(run)
        strengths = [h.hypothesis_strength for h in actionable]
        self.assertEqual(strengths, sorted(strengths, reverse=True))

    def test_latest_by_symbol(self):
        self.svc.run(self._inputs(), regime())
        self.assertIsNotNone(self.store.latest_for_symbol("AAA"))

    def test_reads_are_copies(self):
        run = self.svc.run(self._inputs(), regime())
        first = self.store.get_run(run.hypothesis_run_id)
        first["generated_count"] = 999
        self.assertNotEqual(
            self.store.get_run(run.hypothesis_run_id)["generated_count"], 999)

    def test_run_ids_do_not_collide(self):
        ids = {HypothesisRun.make_id("2026-09-30", "2026-09-30T20:00:00")
               for _ in range(200)}
        self.assertEqual(len(ids), 200)

    def test_run_carries_no_execution_capability(self):
        run = self.svc.run(self._inputs(), regime())
        self.assertFalse(run.execution_available)


if __name__ == "__main__":
    unittest.main()

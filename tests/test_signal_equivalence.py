"""
Anti-divergence guard between the two implementations of the maths.

`agent/signals/indicators.py` is the canonical home. `ml_agent_lite.py`
carries an equivalent copy because it is what the deployed production
`/chatbot` Lambda runs, and rebuilding that artifact from repo source is
a production change this milestone is not authorised to make.

Two copies of arithmetic drift. That is not a risk, it is a certainty
given enough edits - and a silent divergence between what the chatbot
says and what the agent computes is worse than either being wrong alone,
because the disagreement is invisible until a user notices the two
surfaces contradict each other.

So the copies are pinned to each other here, on randomised inputs rather
than a handful of hand-picked series. When they are deliberately allowed
to differ, the difference is named and asserted, never left implicit.
"""
import importlib.util
import math
import os
import random
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from agent.signals import indicators as canonical   # noqa: E402


def _load_ml_agent_lite():
    """Load by path under a unique name.

    Both Lambda directories ship a module called `handler`, and this
    project has already been bitten by one test poisoning
    `sys.modules` for another. Loading by path keeps that from
    happening again.
    """
    path = os.path.join(REPO_ROOT, "lambda-micro", "chatbot-router",
                        "ml_agent_lite.py")
    spec = importlib.util.spec_from_file_location("ml_agent_lite_under_test",
                                                  path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["ml_agent_lite_under_test"] = module
    spec.loader.exec_module(module)
    return module


ml = _load_ml_agent_lite()
LEGACY = ml.MLTradingAgent("TEST")

TOLERANCE = 1e-9


def series(seed, n=260, start=100.0, drift=0.0, vol=0.02):
    rng = random.Random(seed)
    prices = [start]
    for _ in range(n - 1):
        prices.append(max(0.01, prices[-1] * (1 + drift + rng.gauss(0, vol))))
    return prices


CASES = [
    ("flat_market", series(1, drift=0.0, vol=0.012)),
    ("bull_market", series(2, drift=0.004, vol=0.015)),
    ("bear_market", series(3, start=400.0, drift=-0.004, vol=0.015)),
    ("high_vol", series(4, drift=0.0, vol=0.05)),
    ("low_vol", series(5, drift=0.0005, vol=0.002)),
    ("penny_prices", series(6, start=6.0, vol=0.03)),
    ("expensive", series(7, start=1800.0, vol=0.01)),
    ("short_history", series(8, n=60)),
    ("minimum_history", series(9, n=50)),
]


class TestIndicatorEquivalence(unittest.TestCase):
    """Every shared indicator must agree to floating point."""

    def _both(self, fn_name, *args):
        return (getattr(canonical, fn_name)(*args),
                getattr(LEGACY, f"calculate_{fn_name}")(*args))

    def test_sma_agrees(self):
        for name, prices in CASES:
            for period in (20, 50, 200):
                with self.subTest(case=name, period=period):
                    a, b = self._both("sma", prices, period)
                    if a is None or b is None:
                        self.assertEqual(a, b)
                    else:
                        self.assertAlmostEqual(a, b, delta=TOLERANCE)

    def test_ema_agrees(self):
        for name, prices in CASES:
            for period in (12, 26):
                with self.subTest(case=name, period=period):
                    a = canonical.ema(prices, period)
                    b = LEGACY.calculate_ema(prices, period)
                    if a is None or b is None:
                        self.assertEqual(a, b)
                    else:
                        self.assertAlmostEqual(a, b, delta=TOLERANCE)

    def test_rsi_agrees(self):
        for name, prices in CASES:
            with self.subTest(case=name):
                a, b = self._both("rsi", prices, 14)
                if a is None or b is None:
                    self.assertEqual(a, b)
                else:
                    self.assertAlmostEqual(a, b, delta=TOLERANCE)

    def test_macd_agrees_including_the_signal_line(self):
        """The corrected signal line is the accepted baseline. If these
        two ever disagree, one of them has regressed."""
        for name, prices in CASES:
            with self.subTest(case=name):
                a = canonical.macd(prices)
                b = LEGACY.calculate_macd(prices)
                if a is None or b is None:
                    self.assertEqual(a, b)
                    continue
                for key in ("macd", "signal", "histogram"):
                    self.assertAlmostEqual(a[key], b[key], delta=TOLERANCE,
                                           msg=f"{name}: {key} diverged")

    def test_bollinger_agrees(self):
        for name, prices in CASES:
            with self.subTest(case=name):
                a = canonical.bollinger_bands(prices, 20)
                b = LEGACY.calculate_bollinger_bands(prices, 20)
                if a is None or b is None:
                    self.assertEqual(a, b)
                    continue
                for key in ("upper", "middle", "lower"):
                    self.assertAlmostEqual(a[key], b[key], delta=TOLERANCE,
                                           msg=f"{name}: {key} diverged")

    def test_randomised_sweep(self):
        """Hand-picked cases are where the author was already looking."""
        for seed in range(40, 120):
            prices = series(seed, n=random.Random(seed).randint(50, 300),
                            start=random.Random(seed + 1).uniform(5, 900),
                            drift=random.Random(seed + 2).uniform(-0.006, 0.006),
                            vol=random.Random(seed + 3).uniform(0.004, 0.06))
            with self.subTest(seed=seed):
                a = canonical.macd(prices)
                b = LEGACY.calculate_macd(prices)
                if a is None or b is None:
                    self.assertEqual(a, b)
                else:
                    self.assertAlmostEqual(a["histogram"], b["histogram"],
                                           delta=TOLERANCE)
                self.assertAlmostEqual(canonical.rsi(prices, 14) or 0.0,
                                       LEGACY.calculate_rsi(prices, 14) or 0.0,
                                       delta=TOLERANCE)


class TestKnownDefectFixedInBothCopies(unittest.TestCase):
    """
    The flat-series RSI defect was inherited, so it existed in both. It
    is fixed in both, and pinned here so a later edit to either cannot
    reintroduce it in one and not the other.
    """

    FLAT = [100.0] * 40
    RISING = [100.0 + i for i in range(40)]

    def test_flat_series_is_fifty_in_the_canonical_engine(self):
        self.assertEqual(canonical.rsi(self.FLAT), 50.0)

    def test_flat_series_is_fifty_in_ml_agent_lite(self):
        self.assertEqual(LEGACY.calculate_rsi(self.FLAT), 50)

    def test_a_series_that_only_rose_is_still_one_hundred_in_both(self):
        self.assertEqual(canonical.rsi(self.RISING), 100.0)
        self.assertEqual(LEGACY.calculate_rsi(self.RISING), 100)


class TestFalsifyingControls(unittest.TestCase):
    """
    The equivalence assertions above pass trivially if the two modules
    are the same object, or if every case returns None. These prove the
    comparison has teeth.
    """

    def test_the_two_modules_are_genuinely_distinct(self):
        self.assertIsNot(canonical, ml)
        self.assertNotEqual(canonical.__file__, ml.__file__)

    def test_the_cases_actually_produce_values(self):
        """A sweep over inputs that all return None proves nothing."""
        computed = sum(1 for _name, prices in CASES
                       if canonical.macd(prices) is not None)
        self.assertGreaterEqual(computed, len(CASES) - 1,
                                "most equivalence cases returned None")

    def test_a_deliberate_divergence_would_be_detected(self):
        """
        Mutate one copy in memory and confirm the comparison fails. If
        this passes silently, the equivalence tests above are decorative.
        """
        prices = CASES[1][1]
        original = LEGACY.calculate_macd(prices)
        self.assertIsNotNone(original)

        mutated = dict(original)
        mutated["signal"] = mutated["macd"] * 0.9      # the old defect
        mutated["histogram"] = mutated["macd"] - mutated["signal"]

        truth = canonical.macd(prices)
        self.assertNotAlmostEqual(
            truth["histogram"], mutated["histogram"], delta=TOLERANCE,
            msg="the comparison cannot distinguish the fixed-multiplier "
                "defect from the correct signal line",
        )


if __name__ == "__main__":
    unittest.main()

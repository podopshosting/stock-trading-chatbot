"""
Named replay configurations.

Some risk limits are structurally dominated by others: with a $50 daily
ceiling and roughly $25 a position, ordinary 1R losses cannot reach the
$5 daily loss limit and the three-per-day cap cannot be reached at all.
A guard the harness can never make fire is a guard nobody has tested.

The answer is not to loosen the live limits so everything fires - that
is changing the strategy to suit the test. It is to run each guard under
a configuration whose only purpose is to expose it, labelled so its
results can never be quoted as current-strategy performance.
"""
import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from agent.replay import ReplayConfig, configs, run, scenarios     # noqa: E402
from agent.risk import RiskLimits                                  # noqa: E402


def execute(spec):
    """Run a configuration against the scenario it is paired with."""
    scenario = scenarios.build(spec.scenario)
    limits = spec.limits()
    config = ReplayConfig(
        warmup_bars=scenarios.WARMUP, risk_limits=limits,
        starting_cash=max(limits.daily_capital_limit * 2, 100.0))
    engine = dict(spec.engine_overrides)
    spread = engine.pop("spread_pct", None)
    for key, value in engine.items():
        assert hasattr(config, key), f"unknown engine override {key}"
        setattr(config, key, value)
    for key, value in (scenario.config_overrides or {}).items():
        setattr(config, key, value)
    kwargs = {} if spread is None else {"spread_pct": spread}
    if scenario.spread_pct is not None:
        kwargs["spread_pct"] = scenario.spread_pct
    return run(scenario.bars, config,
               regime_for=scenarios.regime_for(scenario),
               config_name=spec.name,
               config_deployable=spec.deployable, **kwargs)


class TestOnlyTheDefaultIsDeployable(unittest.TestCase):

    def test_exactly_one_configuration_is_deployable(self):
        self.assertEqual(configs.deployable_names(),
                         [configs.DEFAULT_NAME])

    def test_the_deployable_one_changes_nothing(self):
        """If the default deviated from the live parameters, every
        'current strategy' result would be from an altered system."""
        default = configs.build(configs.DEFAULT_NAME)
        self.assertTrue(default.is_default)
        self.assertEqual(default.diff_from_default(), [])
        base = RiskLimits()
        built = default.limits()
        for field_name in base.__dataclass_fields__:
            with self.subTest(field=field_name):
                self.assertEqual(getattr(built, field_name),
                                 getattr(base, field_name))

    def test_every_test_configuration_is_labelled(self):
        for name, spec in configs.ALL.items():
            if spec.deployable:
                continue
            with self.subTest(name=name):
                self.assertEqual(spec.as_dict()["label"],
                                 configs.TEST_ONLY_LABEL)

    def test_the_default_carries_no_test_label(self):
        self.assertIsNone(
            configs.build(configs.DEFAULT_NAME).as_dict()["label"])

    def test_a_test_configuration_result_is_marked_undeployable(self):
        result = execute(configs.build("SPREAD_GATE_TEST"))
        self.assertFalse(result.as_dict()["config_deployable"])
        self.assertEqual(result.as_dict()["config_name"],
                         "SPREAD_GATE_TEST")

    def test_an_unknown_configuration_is_refused(self):
        with self.assertRaises(KeyError):
            configs.build("NOT_A_CONFIG")

    def test_an_unknown_risk_override_is_refused(self):
        """A typo in an override would silently leave the limit at its
        live value and the configuration would prove nothing."""
        spec = configs.ReplayConfigSpec(
            name="BAD", purpose="x",
            limit_overrides={"not_a_real_limit": 1.0})
        with self.assertRaises(KeyError):
            spec.limits()


class TestEachConfigurationExposesItsGuard(unittest.TestCase):
    """The self-verifying property.

    A configuration that claims to expose a guard and does not is worse
    than no configuration: it reports the guard as covered. The first
    version of this matrix ran every configuration against the benign
    control, and three of them reported their guard unreachable because
    the SCENARIO was wrong rather than the configuration.
    """

    def _fired(self, spec, result):
        if spec.exposes == "stop_integrity":
            stops = (result.as_dict().get("performance") or {}).get(
                "stop_integrity") or {}
            return (stops.get("stop_breaches") or 0) > 0
        return result.rejections.get(spec.exposes, 0) > 0

    def test_every_test_configuration_fires_what_it_claims(self):
        for name, spec in sorted(configs.ALL.items()):
            if spec.deployable:
                continue
            with self.subTest(name=name, exposes=spec.exposes):
                self.assertTrue(spec.exposes,
                                f"{name} is not the default and names no "
                                f"guard, so it has no stated purpose")
                self.assertTrue(
                    self._fired(spec, execute(spec)),
                    f"{name} claims to expose {spec.exposes} and did "
                    f"not; either the configuration or its paired "
                    f"scenario {spec.scenario!r} is wrong")

    def test_the_default_fires_none_of_them(self):
        """The control. If the live configuration already tripped these
        guards, the test configurations would be proving nothing."""
        result = execute(configs.build(configs.DEFAULT_NAME))
        for name, spec in configs.ALL.items():
            if spec.deployable or spec.exposes == "stop_integrity":
                continue
            with self.subTest(guard=spec.exposes):
                self.assertEqual(result.rejections.get(spec.exposes, 0), 0)

    def test_the_dominated_guards_need_a_test_configuration(self):
        """Recorded as a finding: these two cannot be reached under the
        live limits, which is why the configurations exist at all."""
        default = execute(configs.build(configs.DEFAULT_NAME))
        for guard in ("DAILY_RISK_LOCK", "MAX_NEW_POSITIONS_REACHED"):
            with self.subTest(guard=guard):
                self.assertEqual(
                    default.rejections.get(guard, 0), 0,
                    f"{guard} is now reachable under the LIVE limits; the "
                    f"capital ceiling used to dominate it, and that "
                    f"change is worth noticing deliberately")


class TestConfigurationIdentity(unittest.TestCase):

    def test_the_hash_addresses_the_deviations(self):
        a = configs.ReplayConfigSpec(name="A", purpose="x",
                                     limit_overrides={"max_trade_risk": 9.0})
        b = configs.ReplayConfigSpec(name="B", purpose="y",
                                     limit_overrides={"max_trade_risk": 9.0})
        self.assertEqual(a.config_hash, b.config_hash,
                         "the same deviations are the same configuration "
                         "whatever it is called")

    def test_different_deviations_hash_differently(self):
        a = configs.ReplayConfigSpec(name="A", purpose="x",
                                     limit_overrides={"max_trade_risk": 9.0})
        b = configs.ReplayConfigSpec(name="A", purpose="x",
                                     limit_overrides={"max_trade_risk": 8.0})
        self.assertNotEqual(a.config_hash, b.config_hash)

    def test_the_diff_names_the_live_value_it_replaces(self):
        spec = configs.build("DAILY_LOSS_LOCK_TEST")
        diff = " ".join(spec.diff_from_default())
        self.assertIn("daily_capital_limit", diff)
        self.assertIn("50.0", diff, "the live value must be visible")
        self.assertIn("500.0", diff)

    def test_a_test_configuration_changes_only_what_it_must(self):
        """Extra deviations make a result harder to compare with the
        default, so each configuration is checked for restraint."""
        for name, spec in configs.ALL.items():
            if spec.deployable:
                continue
            with self.subTest(name=name):
                changed = len(spec.limit_overrides) + len(
                    spec.engine_overrides)
                self.assertLessEqual(
                    changed, 2,
                    f"{name} changes {changed} parameters; a guard "
                    f"exposed by several simultaneous deviations cannot "
                    f"be attributed to any one of them")

    def test_the_loss_limit_itself_is_never_overridden(self):
        """The guard must be tested at its REAL threshold. Moving the
        threshold to make it fire would test nothing."""
        for name, spec in configs.ALL.items():
            with self.subTest(name=name):
                self.assertNotIn("daily_loss_limit", spec.limit_overrides)
                self.assertNotIn("max_trade_risk", spec.limit_overrides)
                self.assertNotIn("max_new_positions_per_day",
                                 spec.limit_overrides)

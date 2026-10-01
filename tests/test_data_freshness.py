"""
Freshness is a property of the DATA, not of our fetch.

The defect this pins: `max_quote_age_seconds = 120` was checked against
the time since we downloaded the quote. On Alpaca's `delayed_sip` feed a
quote downloaded three seconds ago describes the market as it was fifteen
minutes earlier, and it passed. The agent traded all of 2026-10-01 on
prices a quarter of an hour old while a control named "quote age" read
three seconds.

So: the age comes from the provider's own timestamp, an absent timestamp
is UNKNOWN rather than fresh, and only the consolidated tape in real time
may support a claim about the strategy.
"""
import os
import sys
import time
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.autonomy.evidence_class import (                       # noqa: E402
    DataQuality, FeedQuality, OPERATIONAL_FEEDS, STRATEGY_GRADE_FEED,
    classify_feed, classify_session, data_quality_from_feeds,
)
from agent.providers.base import Provenance                       # noqa: E402
from agent.risk import RiskContext, RiskLimits                    # noqa: E402
from agent.risk import evaluate as evaluate_risk                  # noqa: E402
from agent.risk.models import RejectionCode                       # noqa: E402
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_risk import context, good_hypothesis                    # noqa: E402

NOW = 1_790_000_000.0


def stamp(age_seconds, precision="ns"):
    """An RFC-3339 timestamp `age_seconds` before NOW."""
    moment = (datetime.fromtimestamp(NOW, timezone.utc)
              - timedelta(seconds=age_seconds))
    text = moment.isoformat().replace("+00:00", "")
    if precision == "ns":
        return f"{text}123456789Z" if "." in text else f"{text}.123456789Z"
    return text + "Z"


class TestProvenanceSeparatesDataAgeFromFetchAge(unittest.TestCase):

    def test_the_data_age_comes_from_the_providers_timestamp(self):
        p = Provenance("alpaca", retrieved_at=NOW - 3, as_of=stamp(900),
                       feed="delayed_sip")
        self.assertAlmostEqual(p.age_seconds(NOW), 3, delta=1)
        self.assertAlmostEqual(p.source_age_seconds(NOW), 900, delta=2)

    def test_a_fresh_fetch_of_old_data_is_not_fresh(self):
        """The exact shape of the defect."""
        p = Provenance("alpaca", retinue := NOW, as_of=stamp(900))
        del retinue
        self.assertLess(p.age_seconds(NOW), 1)
        self.assertGreater(p.source_age_seconds(NOW), 800)

    def test_no_timestamp_is_unknown_not_a_fallback_to_fetch_age(self):
        p = Provenance("alpaca", retrieved_at=NOW - 3)
        self.assertIsNone(p.source_age_seconds(NOW))
        self.assertIsNone(p.source_is_stale(120, NOW))

    def test_an_unparseable_timestamp_is_unknown(self):
        for bad in ("", "not-a-date", "2026-13-45T99:99:99Z"):
            with self.subTest(value=bad):
                p = Provenance("alpaca", NOW, as_of=bad)
                self.assertIsNone(p.source_age_seconds(NOW))

    def test_nanosecond_precision_is_accepted(self):
        """Alpaca returns nanoseconds; naive parsing rejects them."""
        p = Provenance("alpaca", NOW, as_of="2026-10-01T16:02:03.123456789Z")
        self.assertIsNotNone(p.source_age_seconds(NOW))

    def test_a_naive_timestamp_is_read_as_utc(self):
        p = Provenance("alpaca", NOW, as_of=stamp(60, precision="s")[:-1])
        self.assertAlmostEqual(p.source_age_seconds(NOW), 60, delta=2)

    def test_source_staleness_against_a_limit(self):
        fresh = Provenance("alpaca", NOW, as_of=stamp(5))
        old = Provenance("alpaca", NOW, as_of=stamp(500))
        self.assertFalse(fresh.source_is_stale(120, NOW))
        self.assertTrue(old.source_is_stale(120, NOW))


class TestFeedClassification(unittest.TestCase):

    def test_real_time_consolidated_tape(self):
        self.assertIs(classify_feed("sip", 3.0), FeedQuality.REALTIME_SIP)

    def test_iex_is_real_time_but_not_the_consolidated_tape(self):
        self.assertIs(classify_feed("iex", 3.0), FeedQuality.REALTIME_IEX)
        self.assertIsNot(classify_feed("iex", 3.0), STRATEGY_GRADE_FEED)
        self.assertIn(FeedQuality.REALTIME_IEX, OPERATIONAL_FEEDS)

    def test_a_delayed_feed_is_delayed_even_with_a_fresh_looking_stamp(self):
        """Its own timestamps are delayed too, so a small number there
        does not make the data current."""
        self.assertIs(classify_feed("delayed_sip", 1.0),
                      FeedQuality.DELAYED_SIP)

    def test_a_real_time_feed_serving_old_data_is_stale(self):
        self.assertIs(classify_feed("sip", 900.0), FeedQuality.STALE)

    def test_an_absent_timestamp_is_unknown_whatever_the_feed(self):
        for feed in ("sip", "iex", "delayed_sip", None, ""):
            with self.subTest(feed=feed):
                self.assertIs(classify_feed(feed, None), FeedQuality.UNKNOWN)

    def test_an_unrecognised_feed_is_unknown(self):
        self.assertIs(classify_feed("otc", 3.0), FeedQuality.UNKNOWN)

    def test_the_limit_is_configurable_and_respected(self):
        self.assertIs(classify_feed("sip", 200.0, max_age_seconds=300),
                      FeedQuality.REALTIME_SIP)
        self.assertIs(classify_feed("sip", 400.0, max_age_seconds=300),
                      FeedQuality.STALE)

    def test_only_realtime_sip_is_strategy_grade(self):
        self.assertIs(STRATEGY_GRADE_FEED, FeedQuality.REALTIME_SIP)


class TestSessionLevelAggregation(unittest.TestCase):

    def test_an_all_sip_session_is_real_time(self):
        self.assertIs(data_quality_from_feeds({"REALTIME_SIP": 40}),
                      DataQuality.REAL_TIME)

    def test_an_all_iex_session_is_not_real_time(self):
        self.assertIsNot(data_quality_from_feeds({"REALTIME_IEX": 40}),
                         DataQuality.REAL_TIME)

    def test_a_delayed_session_is_delayed(self):
        self.assertIs(data_quality_from_feeds({"DELAYED_SIP": 40}),
                      DataQuality.DELAYED)

    def test_one_stale_cycle_makes_the_session_mixed(self):
        self.assertIs(data_quality_from_feeds({"REALTIME_SIP": 39,
                                               "STALE": 1}),
                      DataQuality.MIXED)

    def test_nothing_recorded_is_unknown(self):
        self.assertIs(data_quality_from_feeds({}), DataQuality.UNKNOWN)

    def test_only_a_pure_sip_session_counts_toward_strategy_gates(self):
        cases = {
            "REALTIME_SIP": True, "REALTIME_IEX": False,
            "DELAYED_SIP": False, "STALE": False, "UNKNOWN": False,
        }
        for feed, expected in cases.items():
            with self.subTest(feed=feed):
                verdict = classify_session({
                    "code_shas": ["abc1234"],
                    "feed_quality_counts": {feed: 30}})
                self.assertIs(verdict["counts_toward_strategy_gates"],
                              expected)

    def test_the_feeds_used_are_named_in_the_reasons(self):
        verdict = classify_session({"code_shas": ["abc1234"],
                                    "feed_quality_counts": {"DELAYED_SIP": 5}})
        self.assertIn("DELAYED_SIP", " ".join(verdict["reasons"]))

    def test_an_iex_session_says_why_it_does_not_count(self):
        verdict = classify_session({"code_shas": ["abc1234"],
                                    "feed_quality_counts": {"REALTIME_IEX": 5}})
        self.assertIn("2.5%", " ".join(verdict["reasons"]))


class TestTheRiskGovernorJudgesTheDataAge(unittest.TestCase):

    def codes(self, **kw):
        decision = evaluate_risk(good_hypothesis(), context(**kw),
                                 limits=RiskLimits())
        return decision, [c for c in decision.reason_codes]

    def test_a_current_quote_is_approved(self):
        """The control: without it the refusals below would prove
        nothing, because the governor might refuse everything."""
        decision, _codes = self.codes()
        self.assertTrue(decision.approved, decision.reasons)

    def test_an_unknown_data_age_is_refused(self):
        decision, codes = self.codes(source_age_seconds=None)
        self.assertFalse(decision.approved)
        self.assertIn(RejectionCode.STALE_MARKET_DATA, codes)

    def test_old_data_is_refused_however_recently_it_was_fetched(self):
        decision, codes = self.codes(quote_age_seconds=1.0,
                                     source_age_seconds=900.0)
        self.assertFalse(decision.approved)
        self.assertIn(RejectionCode.STALE_MARKET_DATA, codes)
        self.assertTrue(any("900s old" in r for r in decision.reasons),
                        decision.reasons)

    def test_a_recent_fetch_cannot_rescue_old_data(self):
        """The precise substitution that caused the defect: a tiny fetch
        age must not stand in for the data age."""
        decision, _codes = self.codes(quote_age_seconds=0.0,
                                      source_age_seconds=1_000.0)
        self.assertFalse(decision.approved)

    def test_a_slow_fetch_of_current_data_is_still_acceptable(self):
        """The converse: our own latency is not the market's."""
        decision, _codes = self.codes(quote_age_seconds=600.0,
                                      source_age_seconds=5.0)
        self.assertTrue(decision.approved, decision.reasons)

    def test_the_refusal_names_the_feed(self):
        decision, _codes = self.codes(source_age_seconds=900.0,
                                      feed_quality="DELAYED_SIP")
        self.assertTrue(any("DELAYED_SIP" in r for r in decision.reasons),
                        decision.reasons)

    def test_the_delayed_feed_the_agent_actually_ran_would_be_refused(self):
        """2026-10-01's configuration, evaluated against the fixed rule."""
        decision, _codes = self.codes(quote_age_seconds=3.0,
                                      source_age_seconds=900.0,
                                      feed_quality="DELAYED_SIP")
        self.assertFalse(decision.approved)


if __name__ == "__main__":
    unittest.main()

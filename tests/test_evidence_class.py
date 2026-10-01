"""
What a body of paper evidence is allowed to prove.

Two live facts drive these tests:

  * The agent trades on Alpaca's `delayed_sip` feed, 15 minutes behind the
    consolidated tape. Its paper fills exercise the machinery and do not
    measure what the market would have given.
  * On 2026-10-01 a session spanned a redeploy. The stored tally named
    code SHA 8674df7 for all 15 cycles while 14 of them ran 66989c4, so
    the record misattributed the work to the version that could not trade.

Both must be visible to the readiness gate, not just written down.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.autonomy.evidence_class import (                     # noqa: E402
    DataQuality, EvidenceClass, classify_evidence, classify_session,
    data_quality, quality_from_provenance,
)
from agent.autonomy.sessions import (                           # noqa: E402
    InMemorySessionStore, SessionTally, aggregate_evidence, record_cycle,
)

SHA_PRE, SHA_POST = "8674df7", "66989c4"
VERSIONS = {"strategy": "hypothesis-v1.0.0", "risk": "risk-v1.0.0",
            "exits": "exits-v1.0.0", "orchestration": "orchestration-v1.1.0"}


def cycle(outcome="COMPLETED", quality="REAL_TIME", phase="INTRADAY"):
    c = {"phase": phase, "outcome": outcome, "quotes_requested": 4,
         "quotes_missing": 0, "steps": []}
    if quality is not None:
        c["data_quality"] = quality
    return c


def tally(**kw):
    base = dict(session_date="2026-10-02", cohort="c1", finalized=True,
                cycles_live_market=5, code_shas=[SHA_POST])
    base.update(kw)
    return SessionTally(**base)


class TestDataQualityIsDerived(unittest.TestCase):

    def test_all_real_time(self):
        self.assertIs(data_quality(realtime_cycles=5), DataQuality.REAL_TIME)

    def test_all_delayed(self):
        self.assertIs(data_quality(delayed_cycles=5), DataQuality.DELAYED)

    def test_a_mix_is_mixed_not_the_better_half(self):
        self.assertIs(data_quality(realtime_cycles=9, delayed_cycles=1),
                      DataQuality.MIXED)

    def test_nothing_recorded_is_unknown_not_real_time(self):
        self.assertIs(data_quality(), DataQuality.UNKNOWN)

    def test_one_unrecorded_cycle_contaminates_a_real_time_session(self):
        """We cannot say a session ran on real-time data if we do not
        know what part of it ran on."""
        self.assertIs(data_quality(realtime_cycles=9, unknown_cycles=1),
                      DataQuality.MIXED)

    def test_provenance_none_is_unknown_never_real_time(self):
        self.assertIs(quality_from_provenance(None), DataQuality.UNKNOWN)
        self.assertIs(quality_from_provenance(True), DataQuality.DELAYED)
        self.assertIs(quality_from_provenance(False), DataQuality.REAL_TIME)


class TestSessionClassification(unittest.TestCase):

    def test_real_time_single_runtime_is_strategy_grade(self):
        c = classify_session(tally(realtime_data_cycles=5))
        self.assertEqual(c["evidence_class"],
                         str(EvidenceClass.REAL_TIME_STRATEGY_EVIDENCE))
        self.assertTrue(c["counts_toward_strategy_gates"])

    def test_delayed_data_is_operational_validation_only(self):
        c = classify_session(tally(delayed_data_cycles=5))
        self.assertEqual(c["evidence_class"],
                         str(EvidenceClass.OPERATIONAL_VALIDATION_ONLY))
        self.assertFalse(c["counts_toward_strategy_gates"])
        self.assertIn("delayed", " ".join(c["reasons"]))

    def test_a_session_spanning_a_redeploy_is_void(self):
        c = classify_session(tally(code_shas=[SHA_PRE, SHA_POST],
                                   realtime_data_cycles=15))
        self.assertEqual(c["evidence_class"], str(EvidenceClass.VOID))
        self.assertFalse(c["counts_toward_strategy_gates"])
        self.assertIn(SHA_PRE, " ".join(c["reasons"]))
        self.assertIn(SHA_POST, " ".join(c["reasons"]))

    def test_void_outranks_good_data(self):
        """Perfect data cannot rescue a session with no single runtime."""
        c = classify_session(tally(code_shas=["a", "b"],
                                   realtime_data_cycles=20))
        self.assertEqual(c["evidence_class"], str(EvidenceClass.VOID))

    def test_an_unlabelled_session_is_not_strategy_grade(self):
        c = classify_session(tally())
        self.assertFalse(c["counts_toward_strategy_gates"])
        self.assertEqual(c["data_quality"], str(DataQuality.UNKNOWN))

    def test_a_legacy_record_falls_back_and_says_so(self):
        """Records written before per-cycle SHAs were kept must not look
        like confirmed single-runtime sessions."""
        legacy = SessionTally(session_date="2026-09-30", cohort="c1",
                              finalized=True,
                              versions={"code_sha": SHA_PRE})
        c = classify_session(legacy)
        self.assertEqual(c["code_shas"], [SHA_PRE])
        self.assertIn("not recorded", c["sha_source"])
        self.assertFalse(c["counts_toward_strategy_gates"])

    def test_it_accepts_a_plain_dict(self):
        c = classify_session({"code_shas": [SHA_POST],
                              "realtime_data_cycles": 3})
        self.assertTrue(c["counts_toward_strategy_gates"])


class TestBodyOfEvidence(unittest.TestCase):

    def test_all_real_time_sessions_are_strategy_grade(self):
        body = classify_evidence([tally(realtime_data_cycles=5),
                                  tally(session_date="2026-10-03",
                                        realtime_data_cycles=5)])
        self.assertTrue(body["counts_toward_strategy_gates"])
        self.assertEqual(body["strategy_grade_sessions"], 2)

    def test_one_delayed_session_demotes_the_whole_body(self):
        body = classify_evidence([tally(realtime_data_cycles=5),
                                  tally(session_date="2026-10-03",
                                        delayed_data_cycles=5)])
        self.assertFalse(body["counts_toward_strategy_gates"])
        self.assertEqual(body["evidence_class"],
                         str(EvidenceClass.OPERATIONAL_VALIDATION_ONLY))

    def test_no_sessions_proves_nothing(self):
        body = classify_evidence([])
        self.assertFalse(body["counts_toward_strategy_gates"])
        self.assertEqual(body["sessions"], 0)

    def test_void_sessions_are_counted_and_named(self):
        body = classify_evidence([tally(code_shas=["a", "b"])])
        self.assertEqual(body["void_sessions"], 1)
        self.assertIn("VOID", " ".join(body["reasons"]))


class TestTheTallyRecordsItsOwnRuntime(unittest.TestCase):
    """The 2026-10-01 defect, reproduced: a redeploy inside a session."""

    def run_session(self, shas):
        store = InMemorySessionStore()
        for sha in shas:
            record_cycle(store, "2026-10-01",
                         cycle(quality="DELAYED"),
                         dict(VERSIONS, code_sha=sha))
        return store.get("2026-10-01")

    def test_every_sha_is_recorded_not_just_the_first(self):
        t = self.run_session([SHA_PRE, SHA_POST, SHA_POST])
        self.assertEqual(t.code_shas, [SHA_PRE, SHA_POST])
        self.assertEqual(t.versions["code_sha"], SHA_PRE)   # opened with
        self.assertIs(t.single_runtime_version, False)

    def test_a_redeploy_mid_session_is_flagged(self):
        t = self.run_session([SHA_PRE, SHA_POST])
        self.assertEqual(
            t.halt_reasons.get("RUNTIME_VERSION_CHANGED_MID_SESSION"), 1)

    def test_the_cohort_key_alone_does_not_notice(self):
        """Why the SHA list is needed: the cohort key excludes code_sha
        deliberately, so the existing mid-session check stays silent."""
        t = self.run_session([SHA_PRE, SHA_POST])
        self.assertIsNone(t.halt_reasons.get("COHORT_CHANGED_MID_SESSION"))

    def test_one_runtime_stays_single(self):
        t = self.run_session([SHA_POST, SHA_POST])
        self.assertEqual(t.code_shas, [SHA_POST])
        self.assertIs(t.single_runtime_version, True)

    def test_per_cycle_data_quality_is_counted(self):
        store = InMemorySessionStore()
        for quality in ("REAL_TIME", "DELAYED", None):
            record_cycle(store, "2026-10-02", cycle(quality=quality),
                         dict(VERSIONS, code_sha=SHA_POST))
        t = store.get("2026-10-02")
        self.assertEqual((t.realtime_data_cycles, t.delayed_data_cycles,
                          t.unknown_data_quality_cycles), (1, 1, 1))

    def test_a_tally_survives_a_round_trip(self):
        t = self.run_session([SHA_PRE, SHA_POST])
        again = SessionTally.from_dict(t.as_dict())
        self.assertEqual(again.code_shas, t.code_shas)
        self.assertEqual(again.delayed_data_cycles, t.delayed_data_cycles)

    def test_an_old_stored_record_without_the_new_fields_still_loads(self):
        old = {"session_date": "2026-09-29", "cohort": "c1",
               "cycles_total": 3, "finalized": True}
        t = SessionTally.from_dict(old)
        self.assertEqual(t.code_shas, [])
        self.assertIsNone(t.single_runtime_version)


class TestAggregatedEvidenceExcludesWhatItCannotAttribute(unittest.TestCase):

    def body(self, tallies):
        return aggregate_evidence(tallies, "c1")

    def test_a_redeploy_session_is_excluded_and_named(self):
        good = tally(realtime_data_cycles=5)
        spanned = tally(session_date="2026-10-01", cycles_live_market=15,
                        code_shas=[SHA_PRE, SHA_POST],
                        delayed_data_cycles=15)
        e = self.body([good, spanned])
        self.assertEqual(e["sessions_completed"], 1)
        self.assertEqual(e["sessions_voided_by_redeploy"],
                         [{"session_date": "2026-10-01",
                           "code_shas": [SHA_PRE, SHA_POST]}])

    def test_delayed_sessions_count_as_sessions_but_not_as_strategy_evidence(self):
        e = self.body([tally(delayed_data_cycles=5)])
        self.assertEqual(e["sessions_completed"], 1)
        self.assertEqual(e["strategy_grade_sessions"], 0)
        self.assertFalse(e["counts_toward_strategy_gates"])
        self.assertEqual(e["data_quality"], str(DataQuality.DELAYED))

    def test_real_time_sessions_are_strategy_grade(self):
        e = self.body([tally(realtime_data_cycles=5)])
        self.assertEqual(e["strategy_grade_sessions"], 1)
        self.assertTrue(e["counts_toward_strategy_gates"])

    def test_other_cohorts_are_still_not_pooled(self):
        mine = tally(realtime_data_cycles=5)
        theirs = tally(session_date="2026-09-01", cohort="c-other",
                       realtime_data_cycles=5)
        e = self.body([mine, theirs])
        self.assertEqual(e["sessions_completed"], 1)
        self.assertEqual(e["sessions_in_other_cohorts"], 1)

    def test_the_reason_reaches_the_report(self):
        e = self.body([tally(delayed_data_cycles=5)])
        self.assertTrue(e["evidence_class_reasons"])


if __name__ == "__main__":
    unittest.main()

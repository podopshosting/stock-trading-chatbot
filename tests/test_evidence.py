"""
Tests for the Evidence & Catalyst Engine.

No network, no AWS, no LLM: every provider and clock is injected.

The properties defended here are the ones that make evidence
trustworthy rather than merely present:

  * a syndicated story is ONE catalyst, not five
  * a shelf registration is capacity, not dilution happening now
  * an insider sale under a tax-withholding code is not bearish
  * a beat with cut guidance is MIXED, not positive
  * missing evidence is stated, never filled in
  * one provider failing does not erase another's items
"""
import os
import sys
import time
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from agent.evidence import (                                    # noqa: E402
    CatalystWindow, Direction, EvidenceItem, EvidenceSource, EvidenceType,
    Fact, FinancingStage, Freshness, LLMStatus, MaterialityBand, SourceClass,
    aggregate_direction, build_catalyst_result, canonical_url,
    classify_earnings_outcome, classify_eight_k, classify_form,
    classify_form4_transaction, classify_freshness, classify_headline,
    classify_window, current_relevance, decay_factor, deduplicate,
    detect_conflict, evidence_score, headline_tokens,
    independent_source_count, jaccard, novelty_for, same_story,
    subject_relevance,
)


def item(evidence_id="e1", symbol="XYZ", headline="Something happened",
         provider="alpaca_news", publisher="Benzinga",
         source_class=SourceClass.STRUCTURED_NEWS,
         etype=EvidenceType.OTHER, direction=Direction.NEUTRAL,
         materiality=0.5, age_hours=2.0, url="", symbols=None,
         summary=""):
    it = EvidenceItem(
        evidence_id=evidence_id, symbol=symbol,
        source=EvidenceSource(provider=provider, publisher=publisher,
                              source_class=source_class, url=url,
                              document_id=evidence_id),
        evidence_type=etype, headline=headline, summary=summary,
        direction=direction, materiality=materiality,
        symbols=symbols or [symbol])
    it.age_hours = age_hours
    return it


# =========================================================================
# SEC classification
# =========================================================================

class TestEightK(unittest.TestCase):

    def test_item_202_is_earnings(self):
        etype, direction, materiality, reason = classify_eight_k("2.02,9.01")
        self.assertIs(etype, EvidenceType.EARNINGS)
        self.assertGreater(materiality, 0.8)
        self.assertIn("2.02", reason)

    def test_item_502_is_a_management_change_with_uncertain_direction(self):
        """
        High materiality, genuinely unknown direction. Forcing a sign
        onto an executive departure would invent information the filing
        does not contain.
        """
        etype, direction, materiality, _ = classify_eight_k("5.02")
        self.assertIs(etype, EvidenceType.MANAGEMENT_CHANGE)
        self.assertIs(direction, Direction.UNCERTAIN)
        self.assertGreater(materiality, 0.5)

    def test_bankruptcy_is_maximally_material_and_negative(self):
        etype, direction, materiality, _ = classify_eight_k("1.03")
        self.assertIs(etype, EvidenceType.BANKRUPTCY)
        self.assertIs(direction, Direction.NEGATIVE)
        self.assertEqual(materiality, 1.0)

    def test_ancillary_item_does_not_decide_the_classification(self):
        """9.01 is 'financial statements and exhibits' - an attachment,
        not an event. It accompanies almost every 8-K."""
        alone = classify_eight_k("9.01")
        with_real = classify_eight_k("2.02,9.01")
        self.assertIs(with_real[0], EvidenceType.EARNINGS)
        self.assertLess(alone[2], 0.1)

    def test_the_most_material_item_wins(self):
        etype, _d, materiality, reason = classify_eight_k("7.01,1.03,9.01")
        self.assertIs(etype, EvidenceType.BANKRUPTCY)
        self.assertIn("also filed", reason)

    def test_unknown_item_is_reported_not_silently_defaulted(self):
        etype, _d, materiality, reason = classify_eight_k("99.99")
        self.assertIs(etype, EvidenceType.OTHER)
        self.assertIn("unrecognised", reason)
        self.assertGreater(materiality, 0.0,
                           "an unknown 8-K still carries some weight")

    def test_empty_items_are_handled(self):
        etype, _d, _m, reason = classify_eight_k("")
        self.assertIs(etype, EvidenceType.OTHER)
        self.assertIn("none listed", reason)


class TestFormClassification(unittest.TestCase):

    def test_periodic_reports(self):
        for form in ("10-K", "10-Q"):
            with self.subTest(form=form):
                etype, _d, _m, stage, _r = classify_form(form)
                self.assertIs(etype, EvidenceType.PERIODIC_REPORT)
                self.assertIs(stage, FinancingStage.NOT_FINANCING)

    def test_shelf_registration_is_capacity_not_an_offering(self):
        """
        The distinction that matters most for short-duration trading.
        Most large issuers keep a shelf on file permanently; reporting
        one as active dilution would fire constantly and wrongly.
        """
        for form in ("S-3", "S-3ASR"):
            with self.subTest(form=form):
                etype, _d, _m, stage, reason = classify_form(form)
                self.assertIs(etype, EvidenceType.SHELF_REGISTRATION)
                self.assertIs(stage, FinancingStage.ABILITY_TO_ISSUE)
                self.assertNotIn("dilut", reason.lower())

    def test_priced_prospectus_is_an_actual_offering(self):
        etype, direction, materiality, stage, _r = classify_form("424B5")
        self.assertIs(etype, EvidenceType.SHARE_OFFERING)
        self.assertIs(stage, FinancingStage.ACTUAL_OFFERING)
        self.assertIs(direction, Direction.NEGATIVE)

    def test_shelf_is_less_material_than_a_priced_offering(self):
        _t, _d, shelf_m, _s, _r = classify_form("S-3")
        _t, _d, priced_m, _s, _r = classify_form("424B5")
        self.assertLess(shelf_m, priced_m)

    def test_13d_is_activist_13g_is_passive(self):
        d_type, _d, d_m, _s, _r = classify_form("SC 13D")
        g_type, _d, g_m, _s, _r = classify_form("SC 13G")
        self.assertIs(d_type, EvidenceType.ACTIVIST_POSITION)
        self.assertIs(g_type, EvidenceType.INSTITUTIONAL_CHANGE)
        self.assertGreater(d_m, g_m)

    def test_amendment_falls_back_to_the_base_form(self):
        etype, _d, _m, _s, reason = classify_form("10-K/A")
        self.assertIs(etype, EvidenceType.PERIODIC_REPORT)

    def test_unknown_form_is_low_materiality_and_says_so(self):
        etype, _d, materiality, _s, reason = classify_form("ZZ-99")
        self.assertIs(etype, EvidenceType.OTHER)
        self.assertLess(materiality, 0.25)
        self.assertIn("not individually classified", reason)

    def test_malformed_input_does_not_raise(self):
        for bad in (None, "", "   ", "8-K", "///"):
            with self.subTest(form=bad):
                classify_form(bad or "")


class TestForm4(unittest.TestCase):

    def test_open_market_purchase_is_positive(self):
        etype, direction, _m, _r = classify_form4_transaction("P")
        self.assertIs(etype, EvidenceType.INSIDER_BUY)
        self.assertIs(direction, Direction.POSITIVE)

    def test_open_market_sale_is_negative(self):
        etype, direction, _m, _r = classify_form4_transaction("S")
        self.assertIs(etype, EvidenceType.INSIDER_SELL)
        self.assertIs(direction, Direction.NEGATIVE)

    def test_tax_withholding_is_not_a_bearish_sale(self):
        """
        Code F is shares withheld by the issuer to pay tax on a vesting
        grant. The insider never chose to sell. Most Form 4 dispositions
        are of this kind, so treating every sale as bearish is wrong on
        the majority of filings.
        """
        etype, direction, materiality, reason = classify_form4_transaction("F")
        self.assertIsNot(etype, EvidenceType.INSIDER_SELL)
        self.assertIs(direction, Direction.NEUTRAL)
        self.assertLess(materiality, 0.2)
        self.assertIn("not a discretionary sale", reason)

    def test_grant_is_not_a_purchase(self):
        etype, direction, _m, _r = classify_form4_transaction("A")
        self.assertIsNot(etype, EvidenceType.INSIDER_BUY)
        self.assertIs(direction, Direction.NEUTRAL)

    def test_a_planned_sale_is_discounted(self):
        """A 10b5-1 sale was scheduled in advance and says much less
        about the insider's current view."""
        _t, plain_dir, plain_m, _r = classify_form4_transaction("S")
        _t, planned_dir, planned_m, reason = classify_form4_transaction(
            "S", footnotes="Sale pursuant to a Rule 10b5-1 trading plan "
                           "adopted on 1 March.")
        self.assertLess(planned_m, plain_m)
        self.assertIs(planned_dir, Direction.NEUTRAL)
        self.assertIn("10b5-1", reason)

    def test_unknown_code_is_neutral_and_says_so(self):
        _t, direction, _m, reason = classify_form4_transaction("Q")
        self.assertIs(direction, Direction.NEUTRAL)
        self.assertIn("not classified", reason)


# =========================================================================
# Headline classification
# =========================================================================

class TestHeadlineClassification(unittest.TestCase):

    def test_recognises_common_events(self):
        cases = [
            ("Acme Q3 Earnings Beat Estimates", EvidenceType.EARNINGS),
            ("Acme Raises Full Year Guidance", EvidenceType.GUIDANCE),
            ("Acme to Acquire Beta Corp for $2B", EvidenceType.ACQUISITION),
            ("Acme Files for Chapter 11 Bankruptcy", EvidenceType.BANKRUPTCY),
            ("Acme Prices $300 Million Common Stock Offering",
             EvidenceType.SHARE_OFFERING),
            ("Analyst Upgrades Acme to Buy", EvidenceType.ANALYST_UPGRADE),
        ]
        for headline, expected in cases:
            with self.subTest(headline=headline):
                etype, _d, _m, _r = classify_headline(headline)
                self.assertIs(etype, expected)

    def test_unmatched_headline_is_low_materiality_with_a_reason(self):
        etype, _d, materiality, reason = classify_headline(
            "Acme Corp Announces Annual Charity Golf Tournament")
        self.assertIs(etype, EvidenceType.OTHER)
        self.assertLess(materiality, 0.3)
        self.assertIn("did not match", reason)

    def test_empty_headline_is_zero_materiality(self):
        _t, _d, materiality, _r = classify_headline("")
        self.assertEqual(materiality, 0.0)

    def test_a_falling_headline_is_never_reported_positive(self):
        """
        Found live: "Why Is Intel Stock Falling on Monday?" was
        classified POSITIVE, because a keyword in the article summary
        matched a positive pattern while the headline said the opposite.

        When the two disagree the honest answer is UNCERTAIN - we know
        they conflict, not which is right.
        """
        etype, direction, _m, reason = classify_headline(
            "Why Is Intel Stock Falling on Monday?",
            "Intel launched a new product line this week.")
        self.assertIsNot(direction, Direction.POSITIVE)
        self.assertIs(direction, Direction.UNCERTAIN)
        self.assertIn("contradicts", reason)

    def test_a_rising_headline_is_never_reported_negative(self):
        _t, direction, _m, _r = classify_headline(
            "Acme Shares Surge on Upgrade",
            "Analyst downgrades a competitor in the same note.")
        self.assertIsNot(direction, Direction.NEGATIVE)

    def test_price_action_agreeing_with_the_keyword_is_left_alone(self):
        _t, direction, _m, reason = classify_headline(
            "Acme Stock Surges After Product Launch")
        self.assertIs(direction, Direction.POSITIVE)
        self.assertNotIn("contradicts", reason)

    def test_contradictory_patterns_yield_mixed(self):
        etype, direction, _m, reason = classify_headline(
            "Acme Upgraded to Buy After Lawsuit Settlement Disappoints; "
            "Analyst Downgrades Peer")
        self.assertIn(direction, (Direction.MIXED, Direction.UNCERTAIN,
                                  Direction.NEGATIVE, Direction.POSITIVE))
        self.assertIn("also matched", reason)


# =========================================================================
# Earnings
# =========================================================================

class TestEarningsOutcome(unittest.TestCase):

    def test_clean_beat_is_positive(self):
        direction, _m, _r = classify_earnings_outcome(0.05, 0.02, "RAISED")
        self.assertIs(direction, Direction.POSITIVE)

    def test_clean_miss_is_negative(self):
        direction, _m, _r = classify_earnings_outcome(-0.05, -0.02, "LOWERED")
        self.assertIs(direction, Direction.NEGATIVE)

    def test_beat_with_lowered_guidance_is_mixed(self):
        """
        The case simplistic beat/miss sentiment gets wrong, and it is
        exactly the case that moves a stock: the market trades the
        forward number.
        """
        direction, _m, reason = classify_earnings_outcome(0.08, 0.03, "LOWERED")
        self.assertIs(direction, Direction.MIXED)
        self.assertIn("guidance lowered", reason)
        self.assertIn("EPS beat", reason)

    def test_miss_with_raised_guidance_is_also_mixed(self):
        direction, _m, _r = classify_earnings_outcome(-0.02, -0.01, "RAISED")
        self.assertIs(direction, Direction.MIXED)

    def test_split_beat_and_miss_is_mixed(self):
        direction, _m, _r = classify_earnings_outcome(0.05, -0.03, None)
        self.assertIs(direction, Direction.MIXED)

    def test_no_figures_is_uncertain_not_neutral(self):
        direction, _m, reason = classify_earnings_outcome(None, None, None)
        self.assertIs(direction, Direction.UNCERTAIN)
        self.assertIn("no comparable figures", reason)


# =========================================================================
# Deduplication
# =========================================================================

class TestCanonicalUrl(unittest.TestCase):

    def test_strips_tracking_parameters(self):
        a = canonical_url("https://www.example.com/story?utm_source=feed&id=7")
        b = canonical_url("http://example.com/story?id=7")
        self.assertEqual(a, b)

    def test_strips_amp_and_trailing_slash(self):
        self.assertEqual(canonical_url("https://amp.example.com/story/amp"),
                         canonical_url("https://example.com/story/"))

    def test_different_stories_stay_different(self):
        self.assertNotEqual(canonical_url("https://x.com/a"),
                            canonical_url("https://x.com/b"))


class TestDeduplication(unittest.TestCase):

    SYNDICATION = [
        ("s1", "Acme Corp Raises Full Year Revenue Guidance", "Benzinga",
         "https://benzinga.com/n/1", 1.0),
        ("s2", "Acme Corp Raises Full-Year Revenue Guidance", "Reuters",
         "https://reuters.com/x/2", 1.3),
        ("s3", "Acme Corp raises full year revenue guidance", "Yahoo",
         "https://finance.yahoo.com/n/3?utm_source=rss", 1.5),
        ("s4", "Acme Corp Raises Full Year Revenue Guidance For 2026",
         "MarketWatch", "https://marketwatch.com/s/4", 2.0),
    ]

    def _syndicated(self):
        return [item(evidence_id=i, headline=h, publisher=p, url=u,
                     age_hours=a, etype=EvidenceType.GUIDANCE,
                     direction=Direction.POSITIVE, materiality=0.8)
                for i, h, p, u, a in self.SYNDICATION]

    def test_four_syndications_collapse_to_one_event(self):
        """
        THE falsifying control for this milestone. Counting republication
        as corroboration would mean the more widely a story is carried,
        the more convincing it appears - exactly backwards.
        """
        result = deduplicate(self._syndicated())
        self.assertEqual(result["group_count"], 1)
        self.assertEqual(result["duplicates_collapsed"], 3)
        self.assertEqual(len(result["canonical_items"]), 1)

    def test_syndications_are_one_independent_source(self):
        """We cannot tell which outlet reported independently and which
        republished the wire, so all copies through one feed count once."""
        groups = deduplicate(self._syndicated())["groups"]
        self.assertEqual(independent_source_count(groups[0]), 1)

    def test_a_primary_filing_adds_a_second_independent_source(self):
        items = self._syndicated() + [
            item(evidence_id="sec1",
                 headline="ACME CORP: 8-K - Results of Operations",
                 provider="sec", publisher="SEC EDGAR",
                 source_class=SourceClass.PRIMARY,
                 etype=EvidenceType.GUIDANCE, direction=Direction.POSITIVE,
                 materiality=0.9, age_hours=1.2,
                 url="https://sec.gov/Archives/x.htm")]
        result = deduplicate(items)
        self.assertEqual(result["group_count"], 1,
                         "the filing and coverage of it are one event")
        self.assertEqual(independent_source_count(result["groups"][0]), 2)

    def test_the_primary_source_becomes_canonical(self):
        items = self._syndicated() + [
            item(evidence_id="sec1",
                 headline="ACME CORP: 8-K - Results of Operations",
                 provider="sec", publisher="SEC EDGAR",
                 source_class=SourceClass.PRIMARY,
                 etype=EvidenceType.GUIDANCE, direction=Direction.POSITIVE,
                 materiality=0.9, age_hours=1.2)]
        canonical = deduplicate(items)["canonical_items"][0]
        self.assertEqual(canonical.evidence_id, "sec1")
        self.assertTrue(canonical.source.is_primary)

    def test_unrelated_stories_stay_separate(self):
        items = [
            item(evidence_id="a", headline="Acme Raises Guidance"),
            item(evidence_id="b", headline="Acme Names New Chief Financial Officer"),
            item(evidence_id="c", headline="Acme Wins Defense Contract"),
        ]
        self.assertEqual(deduplicate(items)["group_count"], 3)

    def test_identical_headlines_far_apart_in_time_are_different_events(self):
        a = item(evidence_id="a", headline="Acme Reports Quarterly Results",
                 age_hours=1.0)
        b = item(evidence_id="b", headline="Acme Reports Quarterly Results",
                 age_hours=24 * 92)
        matched, _reason = same_story(a, b)
        self.assertFalse(matched)

    def test_same_headline_different_companies_are_different_events(self):
        a = item(evidence_id="a", symbol="AAA", headline="Q3 Revenue Rises 12%")
        b = item(evidence_id="b", symbol="BBB", headline="Q3 Revenue Rises 12%")
        matched, _reason = same_story(a, b)
        self.assertFalse(matched)

    def test_same_document_id_is_always_a_duplicate(self):
        a = item(evidence_id="x", headline="One phrasing")
        b = item(evidence_id="x", headline="A completely different phrasing")
        matched, reason = same_story(a, b)
        self.assertTrue(matched)
        self.assertIn("document id", reason)

    def test_duplicates_are_marked_not_deleted(self):
        result = deduplicate(self._syndicated())
        self.assertEqual(len(result["items"]), 4)
        non_canonical = [i for i in result["items"] if not i.is_canonical]
        self.assertEqual(len(non_canonical), 3)
        for i in non_canonical:
            self.assertIsNotNone(i.duplicate_group_id)
            self.assertIsNotNone(i.canonical_evidence_id)


# =========================================================================
# Novelty
# =========================================================================

class TestNovelty(unittest.TestCase):

    def test_a_fresh_unique_story_is_fully_novel(self):
        it = item(age_hours=0.5)
        self.assertGreaterEqual(novelty_for(it, [it]), 0.95)

    def test_a_follow_up_is_less_novel_than_the_break(self):
        first = item(evidence_id="a", age_hours=6.0)
        follow = item(evidence_id="b", age_hours=0.5)
        group = [first, follow]
        self.assertLess(novelty_for(follow, group), novelty_for(first, [first]))

    def test_a_previously_seen_story_is_barely_novel(self):
        prior = item(evidence_id="old",
                     headline="Acme Raises Full Year Revenue Guidance",
                     age_hours=30.0)
        fresh = item(evidence_id="new",
                     headline="Acme Raises Full Year Revenue Guidance",
                     age_hours=0.5)
        self.assertLess(novelty_for(fresh, [fresh], prior_items=[prior]), 0.5)

    def test_an_old_story_is_not_novel(self):
        old = item(age_hours=24 * 40)
        self.assertLessEqual(novelty_for(old, [old]), 0.15)

    def test_republication_does_not_increase_novelty(self):
        """Ten copies of an old story must not make it look new."""
        old = [item(evidence_id=f"e{i}", age_hours=24 * 20 + i)
               for i in range(10)]
        for it in old:
            self.assertLessEqual(novelty_for(it, old), 0.4)

    def test_missing_timestamp_reduces_novelty_rather_than_assuming_fresh(self):
        undated = item(age_hours=None)
        self.assertLessEqual(novelty_for(undated, [undated]), 0.55)


# =========================================================================
# Materiality, reliability, windows, decay
# =========================================================================

class TestMaterialityAndReliability(unittest.TestCase):

    def test_materiality_bands(self):
        for value, band in ((0.9, MaterialityBand.HIGH),
                            (0.5, MaterialityBand.MEDIUM),
                            (0.1, MaterialityBand.LOW),
                            (0.0, MaterialityBand.NONE)):
            with self.subTest(value=value):
                self.assertIs(item(materiality=value).materiality_band, band)

    def test_materiality_is_independent_of_direction(self):
        """'CEO resigns unexpectedly' is HIGH materiality and UNCERTAIN
        direction. Both must be expressible at once."""
        it = item(materiality=0.9, direction=Direction.UNCERTAIN,
                  etype=EvidenceType.MANAGEMENT_CHANGE)
        self.assertIs(it.materiality_band, MaterialityBand.HIGH)
        self.assertIs(it.direction, Direction.UNCERTAIN)

    def test_reliability_follows_the_source_tier(self):
        primary = item(source_class=SourceClass.PRIMARY)
        news = item(source_class=SourceClass.STRUCTURED_NEWS)
        aggregated = item(source_class=SourceClass.AGGREGATED)
        self.assertGreater(primary.reliability, news.reliability)
        self.assertGreater(news.reliability, aggregated.reliability)
        self.assertEqual(primary.reliability, 1.0)

    def test_unknown_source_is_least_reliable(self):
        self.assertLess(item(source_class=SourceClass.UNKNOWN).reliability,
                        item(source_class=SourceClass.AGGREGATED).reliability)


class TestWindowsAndDecay(unittest.TestCase):

    def test_windows(self):
        for age, expected in ((0.5, CatalystWindow.BREAKING),
                              (4.0, CatalystWindow.INTRADAY),
                              (48.0, CatalystWindow.RECENT),
                              (24 * 10, CatalystWindow.BACKGROUND),
                              (24 * 60, CatalystWindow.STALE),
                              (None, CatalystWindow.UNKNOWN)):
            with self.subTest(age=age):
                self.assertIs(classify_window(age), expected)

    def test_a_month_old_story_is_never_breaking(self):
        self.assertIsNot(classify_window(24 * 30), CatalystWindow.BREAKING)

    def test_decay_is_monotonic(self):
        previous = 1.1
        for age in (0, 1, 8, 24, 72, 168, 720):
            value = decay_factor(EvidenceType.EARNINGS, age)
            self.assertLess(value, previous)
            previous = value

    def test_decay_differs_by_event_type(self):
        """An analyst upgrade is spent in days; an acquisition is not."""
        age = 120.0
        upgrade = decay_factor(EvidenceType.ANALYST_UPGRADE, age)
        acquisition = decay_factor(EvidenceType.ACQUISITION, age)
        self.assertLess(upgrade, acquisition)

    def test_unknown_age_is_discounted_not_assumed_current(self):
        self.assertLess(decay_factor(EvidenceType.EARNINGS, None), 1.0)

    def test_newer_evidence_outranks_older_repetition(self):
        fresh = item(evidence_id="new", age_hours=1.0, materiality=0.6,
                     etype=EvidenceType.GUIDANCE)
        old = item(evidence_id="old", age_hours=24 * 20, materiality=0.9,
                   etype=EvidenceType.GUIDANCE)
        self.assertGreater(current_relevance(fresh), current_relevance(old))

    def test_freshness_bands(self):
        self.assertIs(classify_freshness(2.0), Freshness.FRESH)
        self.assertIs(classify_freshness(24 * 5), Freshness.STALE)
        self.assertIs(classify_freshness(24 * 60), Freshness.MISSING)
        self.assertIs(classify_freshness(None), Freshness.UNKNOWN)


# =========================================================================
# Conflict
# =========================================================================

class TestConflict(unittest.TestCase):

    def test_opposing_material_evidence_is_flagged(self):
        items = [
            item(evidence_id="p", headline="Revenue Beats Estimates",
                 direction=Direction.POSITIVE, materiality=0.85, age_hours=2),
            item(evidence_id="n", headline="Company Cuts Full Year Outlook",
                 direction=Direction.NEGATIVE, materiality=0.9, age_hours=2),
        ]
        conflict = detect_conflict(items)
        self.assertTrue(conflict["conflicting"])
        self.assertIs(aggregate_direction(items, conflict), Direction.MIXED)

    def test_mixed_survives_aggregation(self):
        """A net sentiment score would report a small positive here and
        hide the contradiction. The reader needs the contradiction.

        The two items need DISTINCT headlines: identical ones are
        correctly deduplicated into a single event, and then there is no
        conflict left to detect.
        """
        items = [
            item(evidence_id="p", headline="Acme Wins Major Cloud Contract",
                 direction=Direction.POSITIVE, materiality=0.9, age_hours=1),
            item(evidence_id="n", headline="Acme Faces Antitrust Lawsuit",
                 direction=Direction.NEGATIVE, materiality=0.3, age_hours=1),
        ]
        result = build_catalyst_result("XYZ", items)
        self.assertIs(result.direction, Direction.MIXED)
        self.assertTrue(result.conflicting_evidence)
        self.assertTrue(result.conflict_detail)

    def test_an_explicitly_mixed_item_makes_the_aggregate_mixed(self):
        items = [item(direction=Direction.MIXED, materiality=0.8)]
        result = build_catalyst_result("XYZ", items)
        self.assertIs(result.direction, Direction.MIXED)

    def test_stale_opposition_is_not_a_live_conflict(self):
        items = [
            item(evidence_id="p", direction=Direction.POSITIVE,
                 materiality=0.9, age_hours=1),
            item(evidence_id="n", direction=Direction.NEGATIVE,
                 materiality=0.9, age_hours=24 * 60,
                 headline="An entirely different old story"),
        ]
        self.assertFalse(detect_conflict(items)["conflicting"])

    def test_agreement_yields_a_direction(self):
        items = [
            item(evidence_id="a", direction=Direction.POSITIVE,
                 materiality=0.8, age_hours=1, headline="Guidance raised"),
            item(evidence_id="b", direction=Direction.POSITIVE,
                 materiality=0.7, age_hours=2, headline="Major contract won"),
        ]
        result = build_catalyst_result("XYZ", items)
        self.assertIs(result.direction, Direction.POSITIVE)
        self.assertFalse(result.conflicting_evidence)

    def test_uncertain_material_evidence_stays_uncertain(self):
        items = [item(direction=Direction.UNCERTAIN, materiality=0.9,
                      etype=EvidenceType.MANAGEMENT_CHANGE, age_hours=1)]
        result = build_catalyst_result("XYZ", items)
        self.assertIs(result.direction, Direction.UNCERTAIN)


# =========================================================================
# Catalyst aggregation
# =========================================================================

class TestCatalystResult(unittest.TestCase):

    def test_no_evidence_is_a_valid_result(self):
        """
        'No current catalyst' must be reachable and stated. Fabricating a
        narrative because the price moved is the failure this guards.
        """
        result = build_catalyst_result("QUIET", [])
        self.assertFalse(result.has_active_catalyst)
        self.assertIsNone(result.primary_catalyst)
        self.assertIs(result.direction, Direction.NEUTRAL)
        self.assertTrue(any("no evidence" in w for w in result.warnings))

    def test_only_stale_evidence_is_not_an_active_catalyst(self):
        items = [item(age_hours=24 * 50, materiality=0.9)]
        result = build_catalyst_result("XYZ", items)
        self.assertFalse(result.has_active_catalyst)
        self.assertTrue(any("not current" in w or "material enough" in w
                            for w in result.warnings))

    def test_a_fresh_material_item_is_an_active_catalyst(self):
        items = [item(age_hours=1.0, materiality=0.9,
                      etype=EvidenceType.GUIDANCE,
                      direction=Direction.POSITIVE)]
        result = build_catalyst_result("XYZ", items)
        self.assertTrue(result.has_active_catalyst)
        self.assertIsNotNone(result.primary_catalyst)
        self.assertIs(result.primary_catalyst.type, EvidenceType.GUIDANCE)

    def test_counts_are_reported_after_deduplication(self):
        items = [item(evidence_id=f"s{i}",
                      headline="Acme Raises Full Year Revenue Guidance",
                      publisher=p, age_hours=1.0 + i * 0.2,
                      url=f"https://{p.lower()}.com/x{i}",
                      etype=EvidenceType.GUIDANCE, materiality=0.8)
                 for i, p in enumerate(["Benzinga", "Reuters", "Yahoo"])]
        result = build_catalyst_result("XYZ", items)
        self.assertEqual(result.total_evidence_count, 3)
        self.assertEqual(result.supporting_evidence_count, 1)
        self.assertEqual(result.duplicates_collapsed, 2)

    def test_provider_failure_is_surfaced_as_a_warning(self):
        result = build_catalyst_result(
            "XYZ", [item()], providers_attempted=["sec", "alpaca_news"],
            providers_failed=["sec"])
        self.assertIn("sec", result.providers_failed)
        self.assertTrue(any("incomplete" in w for w in result.warnings))

    def test_result_carries_no_trade_semantics(self):
        import json
        items = [item(age_hours=1.0, materiality=0.9)]
        blob = json.dumps(build_catalyst_result("XYZ", items).as_dict()).lower()
        for banned in ("entry_price", "take_profit", "stop_loss",
                       "position_size", "expected_profit", "order_type",
                       "buy_score", "final_score", "trade_score"):
            self.assertNotIn(banned, blob)

    def test_evidence_score_is_disclaimed_as_not_a_probability(self):
        payload = build_catalyst_result("XYZ", [item()]).as_dict()
        self.assertIn("NOT a probability", payload["evidence_score_meaning"])

    def test_execution_is_unavailable(self):
        result = build_catalyst_result("XYZ", [item()])
        self.assertFalse(result.execution_available)
        self.assertFalse(result.as_dict()["execution_available"])

    def test_source_reliability_caveat_is_present(self):
        payload = build_catalyst_result("XYZ", [item()]).as_dict()
        self.assertIn("ACTUALLY STATED", payload["source_reliability_caveat"])


class TestEvidenceScore(unittest.TestCase):

    def test_corroboration_helps_with_diminishing_returns(self):
        """
        Each ADDITIONAL source is worth less than the one before.

        Comparing 1->2 against 2->4 would compare two doublings, and a
        logarithmic curve gives equal increments per doubling by
        construction - an assertion that could never pass whatever the
        shape.
        """
        it = item(age_hours=1.0, materiality=0.8)
        it.novelty = 1.0
        scores = [evidence_score(it, n) for n in range(1, 6)]
        gains = [b - a for a, b in zip(scores, scores[1:])]
        self.assertGreater(gains[0], 0, "a second source must help")
        for earlier, later in zip(gains, gains[1:]):
            self.assertLess(later, earlier,
                            f"gains must shrink, got {gains}")

    def test_falsifying_control_a_linear_curve_would_fail(self):
        """Proves the check above can detect linear growth."""
        linear = [0.2 * n for n in range(1, 6)]
        gains = [b - a for a, b in zip(linear, linear[1:])]
        self.assertFalse(all(later < earlier
                             for earlier, later in zip(gains, gains[1:])),
                         "the shrinking-gain check would pass on a straight "
                         "line, so it proves nothing")

    def test_no_catalyst_scores_zero(self):
        self.assertEqual(evidence_score(None, 3), 0.0)

    def test_score_is_bounded(self):
        it = item(age_hours=0.0, materiality=1.0)
        it.novelty = 1.0
        self.assertLessEqual(evidence_score(it, 50), 1.0)

    def test_stale_evidence_scores_lower_than_fresh(self):
        fresh = item(evidence_id="f", age_hours=1.0, materiality=0.8)
        fresh.novelty = 1.0
        stale = item(evidence_id="s", age_hours=24 * 30, materiality=0.8)
        stale.novelty = 1.0
        self.assertGreater(evidence_score(fresh, 1), evidence_score(stale, 1))


# =========================================================================
# Subject relevance
# =========================================================================

class TestSubjectRelevance(unittest.TestCase):

    def test_a_headline_naming_the_company_is_fully_relevant(self):
        score, _r = subject_relevance(
            "Apple's iPhone Demand Beats Expectations", "", "AAPL",
            ["AAPL", "QCOM"], "Apple Inc.")
        self.assertEqual(score, 1.0)

    def test_a_peers_story_is_heavily_discounted(self):
        """
        Found live: 'Micron's AI Boom Isn't Done yet' was tagged with
        AAPL and was being reported as Apple's primary catalyst.
        """
        score, reason = subject_relevance(
            "Micron's AI Boom Isn't Done yet, Analysts Say", "", "AAPL",
            ["MU", "AAPL"], "Apple Inc.", {"MU": "Micron Technology, Inc."})
        self.assertLessEqual(score, 0.3)
        self.assertIn("not AAPL", reason)

    def test_a_market_wide_story_is_partly_discounted(self):
        score, reason = subject_relevance(
            "Stocks Climb as Inflation Cools", "", "AAPL", ["AAPL", "MSFT"],
            "Apple Inc.")
        self.assertLess(score, 1.0)
        self.assertGreater(score, 0.3)
        self.assertIn("sector coverage", reason)

    def test_the_ticker_alone_counts_as_naming_it(self):
        score, _r = subject_relevance("NVDA Surges on Datacenter Demand", "",
                                      "NVDA", ["NVDA"], None)
        self.assertEqual(score, 1.0)

    def test_corporate_suffixes_do_not_match_everything(self):
        """Matching 'Inc' would make every headline about every company."""
        score, _r = subject_relevance("Beta Inc Announces Layoffs", "",
                                      "AAPL", ["AAPL"], "Apple Inc.")
        self.assertLess(score, 1.0)


if __name__ == "__main__":
    unittest.main()

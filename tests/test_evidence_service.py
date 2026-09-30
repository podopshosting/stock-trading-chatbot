"""
Evidence orchestration, resilience and persistence.

No network, no AWS, no OpenAI: providers, HTTP and clocks are injected.

The properties under test are about what survives failure. A system that
returns nothing when one source is down is worse than one that returns
what it has and says what is missing - because "no catalyst" and "we
could not look" lead a reader to opposite conclusions.
"""
import json
import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from agent.evidence import (                                    # noqa: E402
    Direction, EvidenceItem, EvidenceRun, EvidenceService, EvidenceSource,
    EvidenceType, Fact, InMemoryEvidenceStore, LLMStatus, SourceClass,
    enrich_item, validate_enrichment,
)
from agent.evidence.providers.base import (                     # noqa: E402
    EvidenceProvider, EvidenceRateLimited, EvidenceUnavailable,
    ProviderResult, SymbolNotCovered,
)


def make_item(evidence_id, symbol="XYZ", headline="Acme Raises Guidance",
              provider="fake", publisher="Fake Wire",
              source_class=SourceClass.STRUCTURED_NEWS,
              etype=EvidenceType.GUIDANCE, direction=Direction.POSITIVE,
              materiality=0.8, age_hours=1.0):
    it = EvidenceItem(
        evidence_id=evidence_id, symbol=symbol,
        source=EvidenceSource(provider=provider, publisher=publisher,
                              source_class=source_class,
                              document_id=evidence_id,
                              url=f"https://example.com/{evidence_id}"),
        evidence_type=etype, headline=headline, direction=direction,
        materiality=materiality)
    it.age_hours = age_hours
    return it


class FakeProvider(EvidenceProvider):
    def __init__(self, name, items=None, raises=None,
                 source_class=SourceClass.STRUCTURED_NEWS):
        self.name = name
        self.source_class = source_class
        self._items = items or []
        self._raises = raises
        self.fetch_calls = 0
        self._calls = 0
        self._cache_hits = 0

    def fetch(self, symbol, since=None, limit=25):
        self.fetch_calls += 1
        self._calls = 1
        if self._raises:
            raise self._raises
        return list(self._items)


class TestProviderResilience(unittest.TestCase):

    def test_one_provider_failing_does_not_erase_another(self):
        """
        The property that makes partial evidence useful. If an SEC
        outage removed the news feed's items too, a reader would see
        "no catalyst" for a symbol that has one.
        """
        good = FakeProvider("news", [make_item("a")])
        bad = FakeProvider("sec", raises=EvidenceUnavailable("SEC is down"),
                           source_class=SourceClass.PRIMARY)
        svc = EvidenceService([bad, good])
        result = svc.collect("XYZ")

        self.assertEqual(result.total_evidence_count, 1)
        self.assertEqual(result.providers_failed, ["sec"])
        self.assertIn("sec", result.providers_attempted)
        self.assertTrue(result.has_active_catalyst)

    def test_a_failed_provider_is_surfaced_not_hidden(self):
        bad = FakeProvider("sec", raises=EvidenceUnavailable("down"))
        result = EvidenceService([bad, FakeProvider("news", [make_item("a")])
                                  ]).collect("XYZ")
        self.assertTrue(any("incomplete" in w for w in result.warnings))

    def test_every_provider_failing_still_returns_a_result(self):
        svc = EvidenceService([
            FakeProvider("sec", raises=EvidenceUnavailable("down")),
            FakeProvider("news", raises=EvidenceRateLimited("throttled")),
        ])
        result = svc.collect("XYZ")
        self.assertEqual(result.total_evidence_count, 0)
        self.assertFalse(result.has_active_catalyst)
        self.assertEqual(sorted(result.providers_failed), ["news", "sec"])

    def test_no_coverage_is_not_reported_as_a_failure(self):
        """A provider with nothing for a symbol is a normal outcome.
        Reporting it as an outage would make silence look like breakage."""
        svc = EvidenceService([
            FakeProvider("sec", raises=SymbolNotCovered("not listed")),
            FakeProvider("news", [make_item("a")]),
        ])
        result = svc.collect("XYZ")
        self.assertEqual(result.providers_failed, [])
        self.assertEqual(result.total_evidence_count, 1)

    def test_an_unexpected_exception_is_contained(self):
        svc = EvidenceService([
            FakeProvider("broken", raises=ZeroDivisionError("oops")),
            FakeProvider("news", [make_item("a")]),
        ])
        result = svc.collect("XYZ")
        self.assertEqual(result.providers_failed, ["broken"])
        self.assertEqual(result.total_evidence_count, 1)

    def test_a_malformed_item_does_not_stop_the_rest(self):
        items = [make_item("a"), make_item("b", headline="")]
        result = EvidenceService([FakeProvider("news", items)]).collect("XYZ")
        self.assertEqual(result.total_evidence_count, 2)


class TestRun(unittest.TestCase):

    def test_run_bounds_work_to_top_n(self):
        provider = FakeProvider("news", [make_item("a")])
        svc = EvidenceService([provider], top_n=2)
        run = svc.run(["A", "B", "C", "D"])
        self.assertEqual(run.requested_count, 2)
        self.assertEqual(provider.fetch_calls, 2)

    def test_run_accumulates_metrics(self):
        svc = EvidenceService([FakeProvider("news", [make_item("a")])])
        run = svc.run(["A", "B"])
        self.assertEqual(run.evaluated_count, 2)
        self.assertEqual(run.items_normalized, 2)
        self.assertGreaterEqual(run.provider_calls, 2)

    def test_provider_failures_are_counted_per_provider(self):
        svc = EvidenceService([
            FakeProvider("sec", raises=EvidenceUnavailable("down")),
            FakeProvider("news", [make_item("a")]),
        ])
        run = svc.run(["A", "B"])
        self.assertEqual(run.provider_failures.get("sec"), 2)

    def test_primary_source_share_is_reported(self):
        svc = EvidenceService([
            FakeProvider("sec", [make_item("s", source_class=SourceClass.PRIMARY)],
                         source_class=SourceClass.PRIMARY),
            FakeProvider("news", [make_item("n")]),
        ])
        run = svc.run(["A"])
        self.assertAlmostEqual(run.primary_source_share, 0.5, places=6)

    def test_run_ids_do_not_collide(self):
        ids = {EvidenceRun.make_id("2026-09-30", "2026-09-30T20:00:00")
               for _ in range(200)}
        self.assertEqual(len(ids), 200)

    def test_run_carries_no_execution_capability(self):
        run = EvidenceService([FakeProvider("news", [make_item("a")])]).run(["A"])
        self.assertFalse(run.execution_available)


class TestPersistence(unittest.TestCase):

    def setUp(self):
        self.store = InMemoryEvidenceStore()
        self.svc = EvidenceService([FakeProvider("news", [make_item("a")])],
                                   store=self.store)

    def test_run_and_catalyst_are_persisted(self):
        run = self.svc.run(["XYZ"])
        self.assertIsNotNone(self.store.get_run(run.evidence_run_id))
        self.assertIsNotNone(self.store.latest_catalyst("XYZ"))

    def test_evidence_is_retrievable_by_id(self):
        self.svc.run(["XYZ"])
        catalyst = self.store.latest_catalyst("XYZ")
        eid = catalyst["items"][0]["evidence_id"]
        self.assertIsNotNone(self.store.get_evidence(eid))

    def test_evidence_history_by_symbol(self):
        self.svc.run(["XYZ"])
        self.svc.run(["XYZ"])
        rows = self.store.evidence_for_symbol("XYZ")
        self.assertGreaterEqual(len(rows), 1)

    def test_symbol_lookup_is_case_insensitive(self):
        self.svc.run(["XYZ"])
        self.assertIsNotNone(self.store.latest_catalyst("xyz"))

    def test_duplicate_group_is_retrievable(self):
        items = [make_item("s1", headline="Acme Raises Full Year Guidance"),
                 make_item("s2", headline="Acme Raises Full-Year Guidance",
                           publisher="Reuters", age_hours=1.4)]
        svc = EvidenceService([FakeProvider("news", items)], store=self.store)
        svc.run(["XYZ"])
        catalyst = self.store.latest_catalyst("XYZ")
        gid = catalyst["items"][0]["duplicate_group_id"]
        self.assertIsNotNone(gid)
        self.assertEqual(len(self.store.duplicate_group(gid)), 2)

    def test_reads_are_copies_not_the_stored_object(self):
        self.svc.run(["XYZ"])
        first = self.store.latest_catalyst("XYZ")
        first["direction"] = "TAMPERED"
        self.assertNotEqual(self.store.latest_catalyst("XYZ")["direction"],
                            "TAMPERED")

    def test_full_article_body_is_never_persisted(self):
        """
        Content policy, enforced on write rather than requested of
        providers. A future feed that started returning article bodies
        must not be able to fill the table with them.
        """
        it = make_item("a")
        it.raw_metadata["content"] = "FULL ARTICLE BODY " * 200
        it.raw_metadata["article_text"] = "MORE BODY"
        svc = EvidenceService([FakeProvider("news", [it])], store=self.store)
        svc.run(["XYZ"])
        blob = json.dumps(self.store.latest_catalyst("XYZ"))
        self.assertNotIn("FULL ARTICLE BODY", blob)
        self.assertNotIn("MORE BODY", blob)

    def test_an_overlong_summary_is_truncated(self):
        it = make_item("a")
        it.summary = "x" * 5000
        svc = EvidenceService([FakeProvider("news", [it])], store=self.store)
        svc.run(["XYZ"])
        stored = self.store.latest_catalyst("XYZ")["items"][0]
        self.assertLess(len(stored["summary"]), 1400)
        self.assertIn("[truncated]", stored["summary"])

    def test_prior_evidence_informs_novelty(self):
        """A story already recorded is less novel the second time."""
        svc = EvidenceService([FakeProvider("news", [make_item("a")])],
                             store=self.store)
        first = svc.run(["XYZ"]).results[0]
        second = svc.run(["XYZ"]).results[0]
        self.assertLessEqual(second.novelty, first.novelty)


# =========================================================================
# LLM
# =========================================================================

class FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload


class FakeHTTP:
    def __init__(self, response=None, raises=None):
        self._response = response
        self._raises = raises
        self.calls = 0

    def post(self, *a, **kw):
        self.calls += 1
        if self._raises:
            raise self._raises
        return self._response


def _completion(content):
    return FakeResponse(200, {"choices": [{"message": {"content": content}}]})


SOURCE_TEXT = ("Acme Corporation today raised its full-year revenue guidance "
               "to a range of $4.2 billion to $4.4 billion, from a prior "
               "range of $3.9 billion to $4.1 billion.")


class TestLLMEnrichment(unittest.TestCase):

    def test_outage_leaves_the_evidence_intact(self):
        """
        The required safe behaviour. Evidence is still stored, the
        deterministic classification stands, and nothing is invented to
        fill the gap.
        """
        it = make_item("a")
        original_type = it.evidence_type
        original_direction = it.direction
        http = FakeHTTP(raises=TimeoutError("connection timed out"))

        enriched = enrich_item(it, SOURCE_TEXT, api_key="k", http=http)

        self.assertIs(enriched.llm_status, LLMStatus.UNAVAILABLE)
        self.assertIs(enriched.evidence_type, original_type)
        self.assertIs(enriched.direction, original_direction)
        self.assertEqual(enriched.facts, [])
        self.assertTrue(any("could not be reached" in w
                            for w in enriched.warnings))

    def test_no_api_key_is_not_attempted(self):
        it = enrich_item(make_item("a"), SOURCE_TEXT, api_key=None)
        self.assertIs(it.llm_status, LLMStatus.NOT_ATTEMPTED)

    def test_no_source_text_means_no_summary_is_requested(self):
        """Asking a model to summarise nothing is how invented summaries
        happen."""
        http = FakeHTTP(_completion('{"summary":"invented"}'))
        it = enrich_item(make_item("a"), "", api_key="k", http=http)
        self.assertIs(it.llm_status, LLMStatus.NOT_ATTEMPTED)
        self.assertEqual(http.calls, 0)

    def test_unparseable_output_is_rejected(self):
        http = FakeHTTP(_completion("I'm afraid I can't do that."))
        it = enrich_item(make_item("a"), SOURCE_TEXT, api_key="k", http=http)
        self.assertIs(it.llm_status, LLMStatus.REJECTED)
        self.assertEqual(it.facts, [])

    def test_a_valid_response_is_accepted(self):
        payload = json.dumps({
            "summary": "Acme raised full-year revenue guidance.",
            "facts": [{"claim": "Raised FY revenue guidance",
                       "value": "$4.2B-$4.4B",
                       "previous_value": "$3.9B-$4.1B",
                       "quote": "raised its full-year revenue guidance"}],
            "risks": ["Guidance assumes no supply disruption"],
            "direction": "POSITIVE",
            "direction_reason": "guidance raised",
            "guidance_change": "RAISED",
        })
        it = make_item("a", direction=Direction.UNCERTAIN)
        enriched = enrich_item(it, SOURCE_TEXT, api_key="k",
                               http=FakeHTTP(_completion(payload)))
        self.assertIs(enriched.llm_status, LLMStatus.SUCCEEDED)
        self.assertEqual(len(enriched.facts), 1)
        self.assertEqual(enriched.facts[0].value, "$4.2B-$4.4B")
        self.assertIs(enriched.direction, Direction.POSITIVE)

    def test_a_fact_whose_quote_is_absent_is_discarded(self):
        """
        The enforcement point. The prompt ASKS the model not to invent;
        this is what makes it true. A claim whose supporting quote does
        not appear in the supplied document is dropped.
        """
        payload = json.dumps({
            "facts": [
                {"claim": "Real claim",
                 "quote": "raised its full-year revenue guidance"},
                {"claim": "Fabricated claim about a $10 billion buyback",
                 "quote": "announced a $10 billion share repurchase"},
            ]})
        it = enrich_item(make_item("a"), SOURCE_TEXT, api_key="k",
                         http=FakeHTTP(_completion(payload)))
        claims = [f.claim for f in it.facts]
        self.assertIn("Real claim", claims)
        self.assertNotIn("Fabricated claim about a $10 billion buyback", claims)
        self.assertTrue(any("quote not found" in w for w in it.warnings))

    def test_a_fact_with_no_quote_at_all_is_discarded(self):
        payload = json.dumps({"facts": [{"claim": "Unsupported assertion"}]})
        it = enrich_item(make_item("a"), SOURCE_TEXT, api_key="k",
                         http=FakeHTTP(_completion(payload)))
        self.assertEqual(it.facts, [])
        self.assertTrue(any("no supporting quote" in w for w in it.warnings))

    def test_the_model_cannot_overturn_a_deterministic_classification(self):
        """
        An 8-K item 2.02 is earnings because the filer said so. A model
        disagreeing with the prose does not change what was filed.
        """
        payload = json.dumps({"direction": "NEGATIVE"})
        it = make_item("a", etype=EvidenceType.EARNINGS,
                       direction=Direction.POSITIVE)
        enriched = enrich_item(it, SOURCE_TEXT, api_key="k",
                               http=FakeHTTP(_completion(payload)))
        self.assertIs(enriched.evidence_type, EvidenceType.EARNINGS)
        self.assertIs(enriched.direction, Direction.POSITIVE,
                      "a settled direction must not be overwritten")

    def test_the_model_may_refine_only_an_open_direction(self):
        payload = json.dumps({"direction": "NEGATIVE",
                              "direction_reason": "offering is dilutive"})
        it = make_item("a", direction=Direction.UNCERTAIN)
        enriched = enrich_item(it, SOURCE_TEXT, api_key="k",
                               http=FakeHTTP(_completion(payload)))
        self.assertIs(enriched.direction, Direction.NEGATIVE)

    def test_an_invalid_direction_is_ignored(self):
        payload = json.dumps({"direction": "STRONG_BUY"})
        it = make_item("a", direction=Direction.UNCERTAIN)
        enriched = enrich_item(it, SOURCE_TEXT, api_key="k",
                               http=FakeHTTP(_completion(payload)))
        self.assertIs(enriched.direction, Direction.UNCERTAIN)
        self.assertTrue(any("unrecognised direction" in w
                            for w in enriched.warnings))

    def test_http_error_is_an_outage_not_a_crash(self):
        it = enrich_item(make_item("a"), SOURCE_TEXT, api_key="k",
                         http=FakeHTTP(FakeResponse(500, {})))
        self.assertIs(it.llm_status, LLMStatus.UNAVAILABLE)

    def test_fenced_json_is_still_parsed(self):
        payload = "```json\n" + json.dumps({"summary": "A summary."}) + "\n```"
        it = enrich_item(make_item("a"), SOURCE_TEXT, api_key="k",
                         http=FakeHTTP(_completion(payload)))
        self.assertIs(it.llm_status, LLMStatus.SUCCEEDED)
        self.assertEqual(it.summary, "A summary.")


class TestFactProvenance(unittest.TestCase):

    def test_a_fact_must_reference_its_evidence(self):
        """An untraceable claim is not evidence, so it cannot be built."""
        with self.assertRaises(ValueError):
            Fact(claim="Something was said", source_evidence_id="")

    def test_a_valid_fact_is_accepted(self):
        fact = Fact(claim="Guidance raised", source_evidence_id="ev_1")
        self.assertEqual(fact.source_evidence_id, "ev_1")


class TestValidateEnrichment(unittest.TestCase):

    def test_summary_is_length_capped(self):
        clean = validate_enrichment({"summary": "x" * 5000},
                                    make_item("a"), SOURCE_TEXT)
        self.assertLessEqual(len(clean["summary"]), 600)

    def test_risks_are_capped_in_number(self):
        clean = validate_enrichment({"risks": [f"risk {i}" for i in range(50)]},
                                    make_item("a"), SOURCE_TEXT)
        self.assertLessEqual(len(clean["risks"]), 8)

    def test_non_dict_facts_are_ignored(self):
        clean = validate_enrichment({"facts": ["not a dict", 42, None]},
                                    make_item("a"), SOURCE_TEXT)
        self.assertEqual(clean["facts"], [])


if __name__ == "__main__":
    unittest.main()

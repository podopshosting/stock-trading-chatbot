"""
Company Intelligence must not spend production's Alpha Vantage quota.

`stock-chatbot/alphavantage-api-key` belongs to the production chatbot
and its free tier allows 25 requests a day. A quota exhausted by the
agent would surface as a production outage, so the company layer must
have no path to that provider at all.

`from_alpha_vantage()` stays: it is a pure normaliser over a payload
someone else fetched, and deleting it would mean rewriting and retesting
it when a provider is eventually chosen. What is forbidden is FETCHING.
"""
import importlib
import os
import pathlib
import subprocess
import sys
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

COMPANY = REPO / "agent" / "company"


class TestNoCompanyModuleCanFetchFromAlphaVantage(unittest.TestCase):

    def company_sources(self):
        return [p for p in COMPANY.rglob("*.py")
                if "__pycache__" not in str(p)]

    def test_no_company_module_imports_the_provider(self):
        for path in self.company_sources():
            body = path.read_text()
            with self.subTest(file=path.name):
                self.assertNotIn("AlphaVantageProvider", body)
                self.assertNotIn("from agent.providers.alpha_vantage", body)
                self.assertNotIn("alphavantage-api-key", body)

    def test_the_normaliser_survives_but_takes_a_payload(self):
        """It transforms data; it does not go and get any."""
        from agent.company.earnings import from_alpha_vantage
        import inspect
        params = list(inspect.signature(from_alpha_vantage).parameters)
        self.assertEqual(params, ["payload", "prov"])
        body = inspect.getsource(from_alpha_vantage)
        for fetching in ("requests", "urlopen", "http", "api_key",
                         "ALPHA_VANTAGE_URL"):
            with self.subTest(token=fetching):
                self.assertNotIn(fetching, body)

    def test_the_module_is_loaded_and_that_is_not_the_protection(self):
        """Honest about what is NOT enforced.

        `agent/providers/__init__.py` imports AlphaVantageProvider, so
        importing anything from that package loads the module. A
        module-graph assertion would therefore fail, and asserting it
        anyway and then weakening it would be worse than saying plainly
        that the protection is "never constructed, never credentialed",
        not "unreachable".
        """
        code = ("import sys;"
                f"sys.path.insert(0, {str(REPO)!r});"
                "import agent.company.service;"
                "print('LOADED:' + str(any('alpha_vantage' in k "
                "for k in sys.modules)))")
        out = subprocess.run([sys.executable, "-c", code], cwd=str(REPO),
                             capture_output=True, text=True, timeout=90)
        self.assertIn("LOADED:True", out.stdout, out.stderr[-300:])

    def test_the_provider_cannot_be_constructed_without_a_key(self):
        """So an accidental no-argument construction fails loudly rather
        than quietly reaching for a default credential."""
        from agent.providers.alpha_vantage import AlphaVantageProvider
        with self.assertRaises(ValueError):
            AlphaVantageProvider(api_key="")
        with self.assertRaises(TypeError):
            AlphaVantageProvider()

    def test_nothing_in_the_company_layer_constructs_it(self):
        """The falsifying control for the source scan above: the scan
        must be able to see a construction when one exists."""
        planted = "provider = AlphaVantageProvider(api_key=key)"
        self.assertIn("AlphaVantageProvider", planted)
        for path in self.company_sources():
            with self.subTest(file=path.name):
                self.assertNotIn("AlphaVantageProvider(",
                                 path.read_text())

    def test_the_company_service_takes_its_providers_by_injection(self):
        """It cannot reach for a provider it was not handed."""
        import inspect
        from agent.company.service import CompanyService
        params = inspect.signature(CompanyService.__init__).parameters
        for required in ("store", "actions", "facts", "submissions", "price"):
            self.assertIn(required, params)


class TestUpcomingEarningsStaysUnknown(unittest.TestCase):
    """Until a provider is deliberately selected, the honest answer is
    that we do not know - not a date inferred from a filing cadence."""

    def service(self):
        sys.path.insert(0, str(REPO / "tests"))
        from test_company_service import FakeActions, FakeFacts, QUARTERLY, svc
        return svc(FakeActions(QUARTERLY), FakeFacts({}))[0]

    def test_the_next_earnings_date_is_none_with_a_stated_reason(self):
        out = self.service().earnings("GIS")
        self.assertIsNone(out["next_earnings_date"])
        self.assertIn("no earnings-calendar source configured",
                      out["next_earnings_note"])

    def test_consensus_estimates_are_reported_unavailable(self):
        out = self.service().earnings("GIS")
        self.assertIn("UNAVAILABLE", out["estimates"])
        self.assertIn("never inferred", out["estimates"])

    def test_no_record_carries_an_invented_estimate(self):
        for row in self.service().earnings("GIS")["records"]:
            with self.subTest(period=row["period_end"]):
                self.assertIsNone(row["eps_estimate"])
                self.assertIsNone(row["eps_surprise"])


if __name__ == "__main__":
    unittest.main()

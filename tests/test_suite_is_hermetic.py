"""The suite must not be able to reach real AWS.

tests/__init__.py severs it. This file proves the severing WORKS, and
would fail if someone removed it — a guard whose effect is never
observed is a comment.

The defect being prevented: tests/test_cycle_handler.py invokes the real
cycle lambda_handler, which builds real DynamoDB clients. Without
credentials those failed and the tests passed anyway, so the suite
looked hermetic. With credentials in the environment — which the deploy
script exports before running this very gate — they succeeded and wrote
order intents, cycle snapshots and HEALTH STREAK records into the
production dev tables.
"""
from __future__ import annotations

import os
import sys
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

import tests as suite_init                                   # noqa: E402


class TestCredentialsAreNeutralised(unittest.TestCase):

    def test_no_named_profile_is_active(self):
        # The profile on this machine carries AdministratorAccess, and a
        # named profile overrides the dummy keys entirely.
        self.assertIsNone(os.environ.get("AWS_PROFILE"))
        self.assertIsNone(os.environ.get("AWS_DEFAULT_PROFILE"))

    def test_the_shared_credentials_file_is_not_readable(self):
        self.assertEqual(os.environ.get("AWS_SHARED_CREDENTIALS_FILE"),
                         os.devnull)
        self.assertEqual(os.environ.get("AWS_CONFIG_FILE"), os.devnull)

    def test_keys_are_present_but_not_credential_shaped(self):
        # Present, because an absent key fails with a different error
        # that hides which guard fired.
        self.assertTrue(os.environ.get("AWS_ACCESS_KEY_ID"))
        # And not matching the scanner's patterns, so this file cannot
        # be the reason a real key ever gets committed.
        import re
        self.assertIsNone(
            re.search(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b",
                      os.environ["AWS_ACCESS_KEY_ID"]))
        self.assertIsNone(
            re.search(r"\b[A-Za-z0-9/+]{40}\b",
                      os.environ["AWS_SECRET_ACCESS_KEY"]))

    def test_the_metadata_service_is_disabled(self):
        # Otherwise an instance role could supply real credentials even
        # with the keys above neutralised.
        self.assertEqual(os.environ.get("AWS_EC2_METADATA_DISABLED"), "true")


class TestTheEndpointIsUnreachable(unittest.TestCase):

    def test_dynamodb_points_at_a_closed_port(self):
        self.assertEqual(os.environ.get("AWS_ENDPOINT_URL_DYNAMODB"),
                         "http://127.0.0.1:1")

    def test_a_real_write_to_a_protected_table_fails(self):
        """The falsifying control, and the only one that proves anything.

        Everything above reads environment variables, which would all
        still pass if botocore ignored them. This actually attempts a
        PutItem against a protected table name and requires it to fail.
        """
        try:
            import boto3
        except ImportError:
            self.skipTest("boto3 not installed")
        table = boto3.resource("dynamodb").Table(
            suite_init.PROTECTED_TABLES[0])
        with self.assertRaises(Exception) as caught:
            table.put_item(Item={"PK": "HERMETIC#PROBE",
                                 "SK": "must-never-be-written"})
        # Any failure is acceptable; a SUCCESS is not. Asserting a
        # specific exception type would make this brittle against a
        # botocore change while adding nothing: the property under test
        # is "this did not reach AWS".
        self.assertIsNotNone(caught.exception)

    def test_the_probe_would_notice_a_working_client(self):
        """Prove the previous test is not passing for the wrong reason.

        If `boto3.resource("dynamodb")` raised on CONSTRUCTION, the test
        above would pass without ever attempting a write, and would keep
        passing if the endpoint guard were removed. So the client must
        build successfully and fail only at the call.
        """
        try:
            import boto3
        except ImportError:
            self.skipTest("boto3 not installed")
        table = boto3.resource("dynamodb").Table("whatever")
        self.assertIsNotNone(table)          # construction succeeds
        self.assertTrue(hasattr(table, "put_item"))


class TestTheGuardIsNotOptional(unittest.TestCase):

    def test_the_guard_reads_no_opt_out_variable(self):
        # A guard that an environment variable can disable gets disabled
        # by the first person in a hurry, and this failure is silent.
        with open(os.path.join(REPO, "tests", "__init__.py")) as fh:
            src = fh.read()
        for escape in ("getenv", "os.environ.get(", "if os.environ"):
            self.assertNotIn(
                escape, src,
                "tests/__init__.py reads an environment variable, so the "
                "isolation can be switched off")

    def test_every_protected_table_is_named(self):
        # The list is what a failure message points at, so an empty or
        # truncated list would make a real failure unreadable.
        self.assertGreaterEqual(len(suite_init.PROTECTED_TABLES), 7)
        self.assertIn("stock-agent-dev-journal", suite_init.PROTECTED_TABLES)


if __name__ == "__main__":
    unittest.main()

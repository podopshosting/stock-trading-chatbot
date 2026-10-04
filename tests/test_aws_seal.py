"""The seal that stops ordinary tests calling AWS.

Layer three of four. Layers one and two (neutralised credentials, a
closed-port endpoint) rely on botocore honouring environment variables.
This one blocks the call itself, and these tests prove it does - with
controls that fail if the seal is removed.
"""
from __future__ import annotations

import os
import sys
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from tests.support import aws_seal                           # noqa: E402


class TestTheSealIsInstalled(unittest.TestCase):

    def test_it_is_installed_for_this_suite(self):
        self.assertTrue(aws_seal.installed())
        self.assertFalse(aws_seal.integration_mode())


class TestEveryCallIsBlocked(unittest.TestCase):

    def _client(self, service="dynamodb"):
        try:
            import boto3
        except ImportError:
            self.skipTest("boto3 not installed")
        return boto3.client(service, region_name="us-east-2")

    def test_a_write_is_blocked(self):
        c = self._client()
        with self.assertRaises(aws_seal.UnitTestAwsCallBlocked):
            c.put_item(TableName="stock-agent-dev-journal",
                       Item={"PK": {"S": "x"}, "SK": {"S": "y"}})

    def test_a_READ_is_blocked_too(self):
        # A read proves the boundary is open just as well as a write
        # does, and the next person to add a write finds the door
        # already unlocked. So reads are blocked, not merely counted.
        c = self._client()
        with self.assertRaises(aws_seal.UnitTestAwsCallBlocked):
            c.get_item(TableName="stock-agent-dev-journal",
                       Key={"PK": {"S": "x"}, "SK": {"S": "y"}})

    def test_a_secret_read_is_blocked(self):
        # One test was reading a real secret. Named explicitly because
        # a credential read is not a harmless read.
        c = self._client("secretsmanager")
        with self.assertRaises(aws_seal.UnitTestAwsCallBlocked):
            c.get_secret_value(SecretId="stock-agent/alpaca-paper")

    def test_a_lambda_invoke_is_blocked(self):
        c = self._client("lambda")
        with self.assertRaises(aws_seal.UnitTestAwsCallBlocked):
            c.invoke(FunctionName="stock-agent-dev-cycle")

    def test_the_error_names_the_operation_and_the_target(self):
        # A block that does not say what was attempted sends the reader
        # hunting. The message is the whole remediation.
        c = self._client()
        with self.assertRaises(aws_seal.UnitTestAwsCallBlocked) as caught:
            c.put_item(TableName="stock-agent-dev-journal",
                       Item={"PK": {"S": "x"}, "SK": {"S": "y"}})
        msg = str(caught.exception)
        self.assertIn("PutItem", msg)
        self.assertIn("stock-agent-dev-journal", msg)
        self.assertIn("MUTATES", msg)
        self.assertIn("DEPLOYED resource", msg)
        self.assertIn(aws_seal.INTEGRATION_FLAG, msg)

    def test_the_block_is_not_a_botocore_exception_type(self):
        # A botocore-shaped error would be swallowed by the broad
        # `except Exception` and `except ClientError` handlers that the
        # production code uses everywhere, making the seal invisible in
        # exactly the tests most likely to need it.
        try:
            import botocore.exceptions as be
        except ImportError:
            self.skipTest("botocore not installed")
        self.assertFalse(
            issubclass(aws_seal.UnitTestAwsCallBlocked, be.BotoCoreError))
        self.assertFalse(
            issubclass(aws_seal.UnitTestAwsCallBlocked, be.ClientError))


class TestClassification(unittest.TestCase):

    def test_mutating_operations_are_recognised(self):
        for op in ("PutItem", "UpdateItem", "DeleteItem", "BatchWriteItem",
                   "TransactWriteItems", "CreateTable", "DeleteTable",
                   "Invoke", "PutObject", "Publish"):
            self.assertIn(op, aws_seal.MUTATING_OPERATIONS, op)

    def test_a_read_is_not_classified_as_mutating(self):
        # Otherwise the mutating count would be meaningless.
        for op in ("GetItem", "Query", "Scan", "DescribeTable",
                   "GetSecretValue"):
            self.assertNotIn(op, aws_seal.MUTATING_OPERATIONS, op)

    def test_deployed_resource_names_are_recognised(self):
        for name in ("TableName=stock-agent-dev-journal",
                     "SecretId=stock-agent/alpaca-paper",
                     "FunctionName=stock-chatbot-router"):
            self.assertTrue(aws_seal.names_deployed_resource(name), name)

    def test_a_fixture_name_is_not_a_deployed_resource(self):
        # The guard must discriminate, or it would flag every fixture
        # and get switched off.
        for name in ("TableName=test-table", "TableName=fake",
                     "TableName=unit-test-journal", ""):
            self.assertFalse(aws_seal.names_deployed_resource(name), name)


class TestIntegrationModeIsExplicit(unittest.TestCase):

    def test_credentials_existing_is_not_permission(self):
        # The inference that turned the unit suite into a writer.
        saved = {k: os.environ.get(k) for k in
                 ("AWS_PROFILE", "AWS_ACCESS_KEY_ID")}
        try:
            os.environ["AWS_PROFILE"] = "mypodops"
            os.environ["AWS_ACCESS_KEY_ID"] = "something-real-looking"
            self.assertFalse(
                aws_seal.integration_mode(),
                "having credentials must never imply integration mode")
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    def test_only_an_explicit_affirmative_enables_it(self):
        saved = os.environ.get(aws_seal.INTEGRATION_FLAG)
        try:
            for value, expected in (("1", True), ("true", True),
                                    ("YES", True), ("0", False),
                                    ("", False), ("maybe", False),
                                    ("false", False), (" 1 ", True)):
                os.environ[aws_seal.INTEGRATION_FLAG] = value
                self.assertEqual(aws_seal.integration_mode(), expected,
                                 f"flag={value!r}")
        finally:
            if saved is None:
                os.environ.pop(aws_seal.INTEGRATION_FLAG, None)
            else:
                os.environ[aws_seal.INTEGRATION_FLAG] = saved

    def test_a_typo_fails_closed(self):
        saved = os.environ.get(aws_seal.INTEGRATION_FLAG)
        try:
            os.environ[aws_seal.INTEGRATION_FLAG] = "ture"
            self.assertFalse(aws_seal.integration_mode())
        finally:
            if saved is None:
                os.environ.pop(aws_seal.INTEGRATION_FLAG, None)
            else:
                os.environ[aws_seal.INTEGRATION_FLAG] = saved


class TestFalsifyingControls(unittest.TestCase):
    """Every check above must be capable of failing."""

    def test_without_the_seal_the_call_reaches_the_layer_below(self):
        """The control that gives the rest of this file its meaning.

        Uninstall the seal and prove the call reaches what sits beneath
        it. With the seal on, the same call never gets there. If both
        cases behaved identically, nothing above would be evidence
        about the seal.

        A SENTINEL stands in for the layer below, rather than the real
        botocore call. Letting the call through to botocore would send
        a request at the closed-port endpoint, and the hermeticity gate
        - which counts anything escaping the seal - correctly recorded
        that as a call reaching AWS and failed. The right answer was to
        stop the control needing a network, not to teach the gate an
        exemption: a gate with a carve-out for one test is a gate with
        a carve-out.
        """
        try:
            import botocore.client
        except ImportError:
            self.skipTest("botocore not installed")
        try:
            import boto3
        except ImportError:
            self.skipTest("boto3 not installed")

        reached = []

        def sentinel(self_, operation_name, api_params, *a, **k):
            reached.append(operation_name)
            raise AssertionError("sentinel: the layer below was reached")

        aws_seal.uninstall()
        saved = botocore.client.BaseClient._make_api_call
        try:
            botocore.client.BaseClient._make_api_call = sentinel
            c = boto3.client("dynamodb", region_name="us-east-2")
            with self.assertRaises(AssertionError):
                c.get_item(TableName="stock-agent-dev-journal",
                           Key={"PK": {"S": "x"}, "SK": {"S": "y"}})
            self.assertEqual(reached, ["GetItem"])
        finally:
            botocore.client.BaseClient._make_api_call = saved
            aws_seal.install()
            self.assertTrue(aws_seal.installed(),
                            "the seal must be restored for the rest of "
                            "the suite")

        # And with the seal back on, the identical call does NOT reach
        # the layer below. This is the differential half: same call,
        # different outcome, no network either way.
        reached.clear()
        saved = botocore.client.BaseClient._make_api_call
        try:
            aws_seal.uninstall()
            botocore.client.BaseClient._make_api_call = sentinel
            aws_seal.install()
            c = boto3.client("dynamodb", region_name="us-east-2")
            with self.assertRaises(aws_seal.UnitTestAwsCallBlocked):
                c.get_item(TableName="stock-agent-dev-journal",
                           Key={"PK": {"S": "x"}, "SK": {"S": "y"}})
            self.assertEqual(
                reached, [],
                "the sealed call still reached the layer below")
        finally:
            aws_seal.uninstall()
            botocore.client.BaseClient._make_api_call = saved
            aws_seal.install()
            self.assertTrue(aws_seal.installed())

    def test_installing_twice_does_not_chain_the_patch(self):
        # A chained patch would double-count every attempt and make the
        # hermeticity report wrong in the safe-looking direction.
        before = len(aws_seal.ATTEMPTS)
        self.assertFalse(aws_seal.install())
        try:
            import boto3
        except ImportError:
            self.skipTest("boto3 not installed")
        c = boto3.client("dynamodb", region_name="us-east-2")
        with self.assertRaises(aws_seal.UnitTestAwsCallBlocked):
            c.get_item(TableName="t", Key={})
        self.assertEqual(len(aws_seal.ATTEMPTS), before + 1)

    def test_attempts_are_recorded_for_the_gate(self):
        before = len(aws_seal.ATTEMPTS)
        try:
            import boto3
        except ImportError:
            self.skipTest("boto3 not installed")
        c = boto3.client("dynamodb", region_name="us-east-2")
        with self.assertRaises(aws_seal.UnitTestAwsCallBlocked):
            c.put_item(TableName="stock-agent-dev-state", Item={})
        self.assertEqual(len(aws_seal.ATTEMPTS), before + 1)
        last = aws_seal.ATTEMPTS[-1]
        self.assertEqual(last["operation"], "PutItem")
        self.assertTrue(last["mutating"])
        self.assertTrue(last["deployed_resource"])


if __name__ == "__main__":
    unittest.main()

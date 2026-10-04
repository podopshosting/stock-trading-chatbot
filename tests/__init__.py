"""Tests for Stock Trading Chatbot.

HERMETIC BY CONSTRUCTION, NOT BY ACCIDENT
=========================================

This module runs before any test module is imported, and it severs the
suite from real AWS.

Why it exists: tests/test_cycle_handler.py invokes the REAL cycle
`lambda_handler`, which builds real DynamoDB clients. With no
credentials those constructions fail and the tests pass anyway, so the
suite LOOKED hermetic. With credentials present they succeeded, and the
suite wrote to the production dev tables:

  - order intents into stock-agent-dev-journal, two per run, reserving
    the entire $50 daily capital envelope as phantom committed exposure
  - terminal cycle snapshots via agent/autonomy/snapshot.py
  - HEALTH STREAK records via agent/autonomy/health.py set_streak

Those last two are live operational state. A test run was mutating the
agent's own health accounting.

The symptom was two XYZ orders in the real ledger that looked like an
unexplained mystery and were investigated as one. They were a test run
with credentials in the environment. The deploy script exports
AWS_PROFILE and then runs the test gate, so EVERY DEPLOY added two.

Module-by-module bisection could not find it, because running one
module at a time produced no writes - the write needed the credentials
that happened to be exported in that shell. "It passes locally" and
"it is hermetic" are different claims, and only the second one matters.

LAYERS
------

Four, because the incident was a single-layer failure and repeating
that shape would be learning the wrong lesson:

1. Credentials neutralised (below).
2. Endpoints pointed at a closed port (below).
3. The CALL blocked before any socket, by tests/support/aws_seal.py.
   This holds even if layers 1 and 2 are removed.
4. Integration tests must opt in with RUN_AWS_INTEGRATION_TESTS=1.
   Credentials existing is not permission - that inference is what
   turned the unit suite into a writer.

HOW
---

Credentials are replaced with syntactically valid but unusable values
and the DynamoDB endpoint is pointed at a closed port, so a client that
is built anyway fails fast and loudly instead of writing somewhere real.
A test that genuinely needs AWS must inject its own client, which is
what the rest of the suite already does.

This is deliberately not conditional. A guard that can be switched off
by an environment variable would be switched off by the first person in
a hurry, and the failure it prevents is silent.
"""
import os

# Non-empty so botocore attempts a request and hits the endpoint guard
# below, rather than failing with a confusing "unable to locate
# credentials" that hides which protection actually fired.
#
# Deliberately NOT credential-shaped. The first version used AWS's own
# documented example key, and tests/test_no_secrets_committed.py
# correctly flagged this file as carrying credential-shaped content.
# Adding an exemption for it would have weakened the one scanner that
# stops a real key reaching the repo, to accommodate a fake one. These
# values match no pattern, and the moto convention is the same.
os.environ["AWS_ACCESS_KEY_ID"] = "testing"
os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
os.environ["AWS_SESSION_TOKEN"] = "testing"

# A named profile would override the keys above, and the profile on this
# machine has AdministratorAccess.
os.environ.pop("AWS_PROFILE", None)
os.environ.pop("AWS_DEFAULT_PROFILE", None)
os.environ["AWS_CONFIG_FILE"] = os.devnull
os.environ["AWS_SHARED_CREDENTIALS_FILE"] = os.devnull

# Port 1 on loopback refuses immediately, so a real client fails in
# milliseconds rather than hanging a test for a connect timeout.
os.environ["AWS_ENDPOINT_URL"] = "http://127.0.0.1:1"
os.environ["AWS_ENDPOINT_URL_DYNAMODB"] = "http://127.0.0.1:1"
os.environ["AWS_ENDPOINT_URL_SECRETSMANAGER"] = "http://127.0.0.1:1"
os.environ["AWS_ENDPOINT_URL_SNS"] = "http://127.0.0.1:1"
os.environ["AWS_EC2_METADATA_DISABLED"] = "true"
os.environ.setdefault("AWS_REGION", "us-east-2")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-2")

# Keep retries at one attempt. The default retry policy turns an
# unreachable endpoint into seconds of backoff per call, which would
# make the suite slow enough that someone would want to remove this.
os.environ["AWS_MAX_ATTEMPTS"] = "1"
os.environ["AWS_RETRY_MODE"] = "standard"

# What the guard is protecting, named so a reader of a failure knows.
PROTECTED_TABLES = (
    "stock-agent-dev-journal",
    "stock-agent-dev-state",
    "stock-agent-dev-positions",
    "stock-agent-dev-broker",
    "stock-agent-dev-scanner",
    "stock-agent-dev-signals",
    "stock-agent-dev-evidence",
)


# Layer 3: block the call itself.
#
# Installed AFTER the environment is set, so an integration run - which
# skips the seal - still gets a sane region. install() is a no-op when
# RUN_AWS_INTEGRATION_TESTS=1, which is how real-AWS tests opt out.
from tests.support import aws_seal                            # noqa: E402

if not aws_seal.integration_mode():
    aws_seal.install()

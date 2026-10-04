"""Seal ordinary tests off from AWS, in depth.

WHY DEPTH AND NOT ONE GUARD

The first remediation pointed every AWS endpoint at a closed port. That
works, and it is one layer: it relies on botocore honouring an
environment variable. The incident it fixed was itself a single-layer
failure - the suite was hermetic only because credentials happened to
be absent - so repeating that shape would be learning the wrong lesson.

So this module blocks the CALL ITSELF, before any socket, and records
every attempt. A test cannot reach AWS even if the endpoint variables
are removed, the credentials are real, and the profile is an
administrator.

WHAT COUNTS AS A FAILURE

Any API call at all. Not just mutating ones. A read against a deployed
table still proves the boundary is open, and the next person to add a
write will find the door already unlocked. Mutating operations are
additionally named in the error, because those are the ones that caused
real damage.

INTEGRATION TESTS

Real-AWS tests must opt IN with RUN_AWS_INTEGRATION_TESTS=1. The
presence of credentials is not permission: that inference is precisely
what turned a unit suite into a writer.
"""
from __future__ import annotations

import os
from typing import Dict, List, Tuple

INTEGRATION_FLAG = "RUN_AWS_INTEGRATION_TESTS"

# Operations that CHANGE something. Listed so the error can say which
# kind of boundary was crossed; all operations are blocked regardless.
MUTATING_OPERATIONS = frozenset({
    "PutItem", "UpdateItem", "DeleteItem", "BatchWriteItem",
    "TransactWriteItems", "CreateTable", "UpdateTable", "DeleteTable",
    "PutSecretValue", "CreateSecret", "UpdateSecret", "DeleteSecret",
    "PutObject", "DeleteObject", "DeleteObjects", "CopyObject",
    "Invoke", "InvokeFunction", "CreateFunction", "UpdateFunctionCode",
    "UpdateFunctionConfiguration", "DeleteFunction",
    "Publish", "SendMessage", "PutEvents", "PutRule", "PutTargets",
    "PutLogEvents", "CreateLogGroup",
})

# Substrings that identify a DEPLOYED resource. A unit test naming one
# of these has reached past its own fixtures.
DEPLOYED_RESOURCE_MARKERS = (
    "stock-agent-dev-",
    "stock-agent/",
    "stock-chatbot-",
    "stock-chatbot/",
    "podops-",
    "crm-agent-",
)

# Every blocked attempt, for the hermeticity gate to report. A blocked
# attempt is not a failure of this module - it is the module working -
# but it IS a fact worth surfacing, because an attempt means some code
# path tried.
ATTEMPTS: List[Dict] = []

_installed = False
_original = None


class UnitTestAwsCallBlocked(RuntimeError):
    """An ordinary test tried to call AWS.

    Deliberately a plain RuntimeError subclass rather than a botocore
    error type. A test that broadly catches botocore exceptions would
    swallow a botocore-shaped block and keep passing, which would make
    this guard invisible in exactly the tests most likely to need it.
    """


def integration_mode() -> bool:
    """True only when the flag is explicitly set to 1/true/yes.

    Not `is set`: an empty or accidental value must not count, and a
    typo must fail closed.
    """
    return str(os.environ.get(INTEGRATION_FLAG, "")).strip().lower() in (
        "1", "true", "yes")


def _describe_target(api_params) -> str:
    """Whatever in the request names a resource, for the message."""
    if not isinstance(api_params, dict):
        return ""
    parts = []
    for key in ("TableName", "SecretId", "FunctionName", "Bucket",
                "TopicArn", "QueueUrl", "Name", "LogGroupName"):
        value = api_params.get(key)
        if isinstance(value, str):
            parts.append(f"{key}={value}")
    items = api_params.get("RequestItems")
    if isinstance(items, dict):
        parts.append("RequestItems=" + ",".join(sorted(items)))
    return " ".join(parts)


def names_deployed_resource(target: str) -> bool:
    return any(marker in target for marker in DEPLOYED_RESOURCE_MARKERS)


def install() -> bool:
    """Patch botocore so no ordinary test can call AWS.

    Returns True if the seal was installed. Idempotent: installing twice
    would chain the patch and double-count attempts.
    """
    global _installed, _original
    if _installed or integration_mode():
        return False
    try:
        import botocore.client
    except ImportError:
        # No botocore means nothing to seal, which is fine. Returning
        # False rather than raising keeps a minimal environment usable.
        return False

    _original = botocore.client.BaseClient._make_api_call

    def sealed(self, operation_name, api_params, *args, **kwargs):
        service = getattr(
            getattr(self, "meta", None), "service_model", None)
        service_name = getattr(service, "service_name", "unknown")
        target = _describe_target(api_params)
        record = {
            "service": service_name,
            "operation": operation_name,
            "target": target,
            "mutating": operation_name in MUTATING_OPERATIONS,
            "deployed_resource": names_deployed_resource(target),
        }
        ATTEMPTS.append(record)

        detail = [
            f"an ordinary test attempted {service_name}.{operation_name}"]
        if target:
            detail.append(f"on {target}")
        if record["mutating"]:
            detail.append(
                "- this operation MUTATES state and is the class of call "
                "that wrote phantom order intents, cycle snapshots and "
                "health streak records into the deployed dev tables")
        if record["deployed_resource"]:
            detail.append(
                "- the target is a DEPLOYED resource, not a fixture")
        detail.append(
            f"\n\nInject a fake client or store instead. If this really is "
            f"an integration test, set {INTEGRATION_FLAG}=1 and keep it to "
            f"an isolated partition in dev resources. Credentials existing "
            f"is not permission.")
        raise UnitTestAwsCallBlocked(" ".join(detail))

    botocore.client.BaseClient._make_api_call = sealed
    _installed = True
    return True


def uninstall() -> None:
    """Restore botocore. For the seal's OWN tests, nothing else."""
    global _installed, _original
    if _installed and _original is not None:
        import botocore.client
        botocore.client.BaseClient._make_api_call = _original
        _installed = False


def installed() -> bool:
    return _installed


def summary() -> Dict:
    """What the hermeticity gate reports."""
    return {
        "integration_mode": integration_mode(),
        "seal_installed": _installed,
        "attempts": len(ATTEMPTS),
        "mutating_attempts": sum(1 for a in ATTEMPTS if a["mutating"]),
        "deployed_resource_attempts": sum(
            1 for a in ATTEMPTS if a["deployed_resource"]),
        "by_operation": _tally(a["operation"] for a in ATTEMPTS),
        "by_target": _tally(a["target"] for a in ATTEMPTS if a["target"]),
    }


def _tally(values) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for v in values:
        out[v] = out.get(v, 0) + 1
    return out

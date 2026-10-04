#!/usr/bin/env python3
"""Prove the ordinary test suite makes zero AWS calls.

    python3 scripts/verify_test_hermeticity.py [--verbose]

Exit 0 = PASS, no call reached AWS.
Exit 1 = FAIL, a call reached AWS, or the seal was not installed.
Exit 3 = NOT RUN, the check could not be performed.

NOT RUN is not a pass. A hermeticity check that silently degrades to
"could not tell" is worse than none, because it occupies the slot where
the real check would be.

WHY THIS IS A SEPARATE GATE

The suite passing proves the tests agree with the code. It does not
prove the tests stayed inside the process. For weeks it did not: with
credentials in the environment the suite wrote order intents, cycle
snapshots and health streak records into deployed dev tables, and every
deploy added more because the deploy script exported AWS_PROFILE before
running the gate.

WHAT A BLOCKED ATTEMPT MEANS

The seal raises before any socket, so an attempt is NOT a leak - it is
the seal working. It is still reported, because an attempt means some
code path tried, and the number should go down over time rather than
quietly up. Attempts are informational; a call that REACHES AWS is the
failure.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

PASS, FAIL, NOT_RUN = 0, 1, 3


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    if str(os.environ.get("RUN_AWS_INTEGRATION_TESTS", "")).strip().lower() \
            in ("1", "true", "yes"):
        print("NOT RUN: RUN_AWS_INTEGRATION_TESTS is set, so the seal is "
              "deliberately off and this check cannot mean anything.")
        return NOT_RUN

    # A real-network probe installed BEFORE the suite imports anything,
    # counting calls that get past the seal. Without this the check
    # would only be asking the seal about itself.
    try:
        import botocore.client
    except ImportError:
        print("NOT RUN: botocore is not installed, so no AWS call is "
              "possible and this check proves nothing.")
        return NOT_RUN

    reached = []
    original = botocore.client.BaseClient._make_api_call

    def counting(self, operation_name, api_params, *a, **k):
        # If control arrives here, the seal did NOT stop the call. This
        # wrapper sits closest to the wire, so it is the last thing
        # before a real request.
        svc = getattr(getattr(self, "meta", None), "service_model", None)
        reached.append({
            "service": getattr(svc, "service_name", "unknown"),
            "operation": operation_name,
            "target": {k2: v for k2, v in (api_params or {}).items()
                       if k2 in ("TableName", "SecretId", "FunctionName",
                                 "Bucket", "TopicArn")},
        })
        return original(self, operation_name, api_params, *a, **k)

    botocore.client.BaseClient._make_api_call = counting

    # Importing the package installs the seal ON TOP of the probe, so
    # the seal is reached first and the probe only sees what escapes it.
    try:
        from tests.support import aws_seal
    except Exception as exc:                                  # noqa: BLE001
        print(f"NOT RUN: could not import the seal: "
              f"{type(exc).__name__}: {exc}")
        return NOT_RUN

    if not aws_seal.installed():
        print("FAIL: the seal is not installed, so the suite is only "
              "hermetic by accident - which is the original defect.")
        return FAIL

    suite = unittest.TestLoader().discover(str(REPO / "tests"))
    # The suite writes structured events to stdout, which would bury
    # this report in thousands of lines. Captured and discarded unless
    # --verbose: a gate whose verdict cannot be found is not a gate.
    import contextlib, io
    buf = io.StringIO()
    runner = unittest.TextTestRunner(
        verbosity=2 if args.verbose else 0,
        stream=sys.stderr if args.verbose else buf)
    if args.verbose:
        result = runner.run(suite)
    else:
        with contextlib.redirect_stdout(buf):
            result = runner.run(suite)

    summary = aws_seal.summary()
    summary["calls_that_reached_aws"] = len(reached)
    summary["reached_detail"] = reached[:20]
    summary["tests_run"] = result.testsRun
    summary["suite_ok"] = result.wasSuccessful()

    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True))

    width = 46
    print("=" * (width + 18))
    print("TEST HERMETICITY".ljust(width), "")
    print("=" * (width + 18))
    print(f"{'tests run':{width}} {result.testsRun}")
    print(f"{'suite passed':{width}} {result.wasSuccessful()}")
    print(f"{'seal installed':{width}} {summary['seal_installed']}")
    print(f"{'AWS calls that REACHED AWS':{width}} "
          f"{summary['calls_that_reached_aws']}")
    print(f"{'blocked attempts (seal working)':{width}} "
          f"{summary['attempts']}")
    print(f"{'  of which mutating':{width}} {summary['mutating_attempts']}")
    print(f"{'  of which named a deployed resource':{width}} "
          f"{summary['deployed_resource_attempts']}")
    if summary["by_operation"]:
        print("\nblocked attempts by operation:")
        for op, n in sorted(summary["by_operation"].items(),
                            key=lambda kv: -kv[1]):
            print(f"    {n:4}  {op}")
    if summary["by_target"] and args.verbose:
        print("\nblocked attempts by target:")
        for tgt, n in sorted(summary["by_target"].items(),
                             key=lambda kv: -kv[1])[:20]:
            print(f"    {n:4}  {tgt}")
    print("=" * (width + 18))

    # The suite failing is a separate problem, but it invalidates this
    # measurement: a suite that aborted early never exercised the paths
    # that would have called AWS.
    if not result.wasSuccessful():
        print("FAIL: the suite did not pass, so this measurement is "
              "incomplete - untested paths cannot be certified hermetic.")
        return FAIL
    if reached:
        print(f"FAIL: {len(reached)} call(s) reached AWS.")
        for r in reached[:10]:
            print(f"    {r['service']}.{r['operation']} {r['target']}")
        return FAIL
    print("PASS: no AWS call escaped the seal.")
    return PASS


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""
End-of-day close-out for a paper session.

Retrieves the session report the cycle writes at the close, checks it
against the things that must be true, and writes the record to
docs/progress/. Every check is reported individually and the overall
verdict is derived from them, so a session cannot read as clean while one
of its checks failed.

A check that cannot be performed is a FAILURE, not a skip: an
unverifiable close-out is not a clean one.

    AWS_PROFILE=mypodops python3 scripts/close_out_session.py [--write]

Exit 0 = every check passed. 1 = something failed or is unknown.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

REPO = pathlib.Path(__file__).resolve().parent.parent
REGION = "us-east-2"
FUNCTION = "stock-agent-dev-api"

checks: list[tuple[str, bool, str]] = []


def check(name, ok, detail=""):
    checks.append((name, bool(ok), str(detail)))
    return bool(ok)


def api_base() -> str:
    out = subprocess.run(
        ["aws", "lambda", "get-function-url-config",
         "--function-name", FUNCTION, "--region", REGION,
         "--query", "FunctionUrl", "--output", "text"],
        capture_output=True, text=True, check=True)
    return out.stdout.strip()


def get(url):
    try:
        with urllib.request.urlopen(url, timeout=90) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:300]
    except Exception as e:                                # noqa: BLE001
        return 0, f"{type(e).__name__}: {e}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true",
                    help="write the record to docs/progress/")
    args = ap.parse_args()

    base = api_base()
    status, report = get(f"{base}agent/session-report")
    if status != 200 or not isinstance(report, dict):
        check("session report retrieved", False, f"status={status}")
        return report_out(args, None, None, None)
    check("session report retrieved", True)

    # `written` is the cycle's own flag for "the close-out report exists".
    # Checking only that a body came back passed mid-session, when the
    # payload is {"report": null, "written": false} - a vacuous check.
    written = report.get("written")
    body = report.get("report")
    if not written or not isinstance(body, dict):
        check("session finalised at the close", False,
              f"written={written}; the cycle writes this at MARKET_CLOSED")
        return report_out(args, None, None, None)
    check("session finalised at the close", True,
          str(report.get("session_date", "")))

    # --- the things that must be true -------------------------------
    for name, key in (("all positions flattened", "positions_flat"),
                      ("broker reports no positions", "broker_flat"),
                      ("cash reconciles with the journal", "cash_ok"),
                      ("journal complete", "journal_complete")):
        value = body.get(key)
        check(name, value is True,
              "unknown" if value is None else str(value))

    realized = body.get("realized_pnl")
    check("realized P&L recorded", realized is not None,
          "unknown" if realized is None else f"{realized}")

    check("no residual open positions",
          body.get("positions_open") in (0, None) and
          body.get("positions_flat") is True,
          f"open={body.get('positions_open')}")

    check("session_ok derived clean", body.get("session_ok") is True,
          str(body.get("session_ok")))

    # --- live state after the close ----------------------------------
    status, auto = get(f"{base}agent/autonomy")
    if status == 200 and isinstance(auto, dict):
        health = (auto.get("health") or {}).get("state")
        check("final health state readable", health is not None, str(health))
        check("no open position remains in the store",
              len(auto.get("positions") or []) == 0,
              f"{len(auto.get('positions') or [])} open")
        last = auto.get("last_cycle") or {}
        check("emergency stop clear",
              last.get("emergency_stop_engaged") is not True,
              str(last.get("emergency_stop_engaged")))
    else:
        check("autonomy state readable", False, f"status={status}")

    status, cohorts = get(f"{base}agent/cohorts")
    cohort = None
    if status == 200 and isinstance(cohorts, dict):
        rows = cohorts.get("cohorts") or []
        cohort = rows[-1] if rows else None
        check("cohort classified", bool(cohort),
              cohort.get("evidence_class") if cohort else "none")
        if cohort:
            check("reconciliation clean across the cohort",
                  cohort["reconciliation"].get("clean") is True,
                  f"{cohort['reconciliation'].get('failures')} failures")
            check("EOD flatten clean across the cohort",
                  cohort["eod_flatten"].get("clean") is True,
                  f"{cohort['eod_flatten'].get('failures')} failures")
    else:
        check("cohorts readable", False, f"status={status}")

    return report_out(args, body, auto, cohort)


def report_out(args, body, auto, cohort) -> int:
    width = max((len(n) for n, _, _ in checks), default=10)
    failed = [n for n, ok, _ in checks if not ok]
    print("=" * (width + 24))
    for name, ok, detail in checks:
        print(f"{name:{width}}  {'PASS' if ok else 'FAIL'}  {detail}")
    print("=" * (width + 24))
    print(f"{len(checks) - len(failed)}/{len(checks)} checks passed")
    if failed:
        print(f"NOT CLEAN - {failed}")

    if args.write and body:
        path = (REPO / "docs" / "progress" /
                f"SESSION-{body.get('session_date') or 'unknown'}.md")
        path.write_text(render(body, auto, cohort, failed))
        print(f"written: {path.relative_to(REPO)}")
    return 1 if failed else 0


def render(body, auto, cohort, failed) -> str:
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    lines = [
        f"# Paper session {body.get('session_date', 'unknown')}",
        "",
        f"Close-out recorded {stamp} by `scripts/close_out_session.py`.",
        "",
        f"**Verdict: {'ALL CHECKS PASSED' if not failed else 'NOT CLEAN'}**",
    ]
    if failed:
        lines += ["", "Failed or unknown: " + ", ".join(failed)]
    if cohort:
        lines += [
            "", "## Cohort",
            "",
            f"- Cohort: `{cohort['cohort']}`",
            f"- Evidence class: **{cohort['evidence_class']}**",
            f"- Code SHAs: {cohort['code_shas']}",
            f"- Feed quality: {cohort['feed_quality_counts']}",
            f"- Versions: {cohort['versions']}",
        ]
    lines += ["", "## Close-out checks", "",
              "| Check | Result | Detail |", "|---|---|---|"]
    for name, ok, detail in checks:
        lines.append(f"| {name} | {'PASS' if ok else 'FAIL'} | {detail} |")
    lines += ["", "## Session report (as written by the cycle)", "",
              "```json", json.dumps(body, indent=2, default=str)[:6000],
              "```"]
    if cohort:
        lines += ["", "## Tracked separately", "",
                  "```json",
                  json.dumps({k: cohort[k] for k in
                              ("operational", "reconciliation", "eod_flatten",
                               "data_rejections", "strategy")
                              if k in cohort}, indent=2, default=str)[:6000],
                  "```"]
    lines += [
        "", "## What this session may be used to claim", "",
        "Operational reliability only, unless the cohort above is",
        "`REAL_TIME_STRATEGY_EVIDENCE`. Profitability is not meaningful",
        "until the sample-adequacy gates pass, and a cohort is never",
        "combined with another.", ""]
    return "\n".join(lines)


if __name__ == "__main__":
    sys.exit(main())

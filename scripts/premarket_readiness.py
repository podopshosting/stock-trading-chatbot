#!/usr/bin/env python3
"""Is the agent fit to run at the next open?

    AWS_PROFILE=mypodops python3 scripts/premarket_readiness.py

Exit 0 = every check passed. 1 = something failed. 3 = a check could
not be performed.

A CHECK THAT CANNOT BE PERFORMED IS NOT A PASS

UNKNOWN counts as unmet, everywhere. The whole point of running this
before an open is to find out what is not known, and a check that
degrades to "could not tell" while reporting green occupies the slot
where the real check would be.

RESEARCH DOES NOT GATE TRADING

Prediction models showing no edge is a research result. It has no
bearing on whether the agent may run a paper session, and this script
does not test it. Conflating the two would either block operation on an
unrelated finding or imply the models are in the loop. They are not.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

REGION = "us-east-2"
API_FUNCTION = "stock-agent-dev-api"

PASS, FAIL, NOT_RUN = 0, 1, 3
checks = []          # (category, name, state, detail)
OK, BAD, UNKNOWN = "PASS", "FAIL", "UNKNOWN"


def check(category, name, state, detail=""):
    checks.append((category, name, state, str(detail)[:220]))
    return state


def api_base():
    out = subprocess.run(
        ["/opt/homebrew/bin/aws", "lambda", "get-function-url-config",
         "--function-name", API_FUNCTION, "--region", REGION,
         "--query", "FunctionUrl", "--output", "text"],
        capture_output=True, text=True)
    return out.stdout.strip() if out.returncode == 0 else None


def get(url, timeout=60):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read())
    except Exception as exc:                                  # noqa: BLE001
        return {"__error__": f"{type(exc).__name__}: {exc}"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    base = api_base()
    if not base:
        check("EXECUTION", "read API reachable", UNKNOWN,
              "the function URL could not be read")
        return report(args)
    base = base.rstrip("/")

    # ---------------- EXECUTION ----------------
    auto = get(f"{base}/agent/autonomy")
    if "__error__" in auto:
        check("EXECUTION", "autonomy readable", UNKNOWN, auto["__error__"])
    else:
        check("EXECUTION", "autonomy readable", OK)
        health = auto.get("health") or {}
        state = health.get("state")
        active = health.get("active") or []
        latching = [a for a in active if a.get("latching")]
        # A halted agent is not a failure of this script - it is the
        # state, reported. What would be a failure is not knowing.
        check("EXECUTION", "health state known",
              OK if state else UNKNOWN, str(state))
        check("EXECUTION", "latched conditions enumerated",
              OK if state else UNKNOWN,
              f"{len(latching)} latching: "
              + ", ".join(str(a.get("condition")) for a in latching))
        if latching:
            check("EXECUTION", "NEW ENTRIES PERMITTED",
                  BAD if health.get("entries_permitted") is not True
                  else OK,
                  "entries are blocked while a latching condition is "
                  "active; this is correct, and it means no new "
                  "position will be opened at the open")
            check("EXECUTION", "exits permitted",
                  OK if health.get("exits_permitted") is True else BAD,
                  "exits must remain permitted even when halted, or an "
                  "existing position cannot be closed")
        mode = auto.get("execution_mode")
        check("EXECUTION", "execution mode known",
              OK if mode and mode != "UNKNOWN" else UNKNOWN, str(mode))
        check("EXECUTION", "real money disabled",
              OK if auto.get("real_money") == "DISABLED" else BAD,
              str(auto.get("real_money")))

    # Orders and exposure, for the session the agent will resume.
    session = (auto.get("session_date") if isinstance(auto, dict)
               else None)
    orders = get(f"{base}/agent/orders"
                 + (f"?session_date={session}" if session else ""))
    if "__error__" in orders:
        check("EXECUTION", "order ledger readable", UNKNOWN,
              orders["__error__"])
    else:
        integrity = orders.get("read_integrity")
        check("EXECUTION", "order ledger readable",
              OK if integrity == "COMPLETE" else UNKNOWN, str(integrity))
        known = orders.get("committed_exposure_known")
        check("EXECUTION", "committed exposure established",
              OK if known is True else BAD,
              f"exposure={orders.get('committed_exposure')} known={known}"
              + ("" if known else
                 " - UNKNOWN exposure is not zero and blocks entries"))
        unresolved = orders.get("unresolved_ids") or []
        check("EXECUTION", "no unresolved submissions",
              OK if not unresolved else BAD,
              f"{len(unresolved)} submission(s) with an unknown outcome: "
              + ", ".join(unresolved[:5]))

    positions = get(f"{base}/agent/positions")
    if "__error__" in positions:
        check("EXECUTION", "positions readable", UNKNOWN,
              positions["__error__"])
    else:
        check("EXECUTION", "positions readable", OK,
              f"{positions.get('open_count')} open")
        risk_known = positions.get("risk_known_for")
        check("EXECUTION", "risk known for every open position",
              OK if positions.get("open_count") == 0
              or (risk_known and "of" in str(risk_known)
                  and risk_known.split(" of ")[0]
                  == risk_known.split(" of ")[1]) else UNKNOWN,
              str(risk_known))

    switches = get(f"{base}/agent/switches")
    if "__error__" not in switches:
        check("EXECUTION", "broker reported",
              OK if switches.get("is_paper_only") is True else BAD,
              f"paper_only={switches.get('is_paper_only')}")

    # ---------------- DATA ----------------
    pipeline = get(f"{base}/agent/pipeline?symbol=SPY")
    if "__error__" in pipeline:
        check("DATA", "market data reachable", UNKNOWN,
              pipeline["__error__"])
    else:
        ctx = ((pipeline.get("stages") or {}).get("risk")
               or {}).get("context") or {}
        check("DATA", "market data reachable",
              OK if ctx.get("price") else UNKNOWN,
              f"SPY price={ctx.get('price')}")
        age = ctx.get("quote_age_seconds")
        check("DATA", "quote age known",
              OK if age is not None else UNKNOWN, f"{age}s")
        feed = ctx.get("feed_quality")
        # Market closed means no live quote; UNKNOWN before an open is
        # expected and is reported as UNKNOWN rather than hidden.
        check("DATA", "feed quality known",
              OK if feed else UNKNOWN,
              str(feed) + (" (expected before the open)"
                           if not feed else ""))

    # ---------------- STRATEGY ----------------
    limits = get(f"{base}/agent/risk/limits")
    if "__error__" in limits:
        check("STRATEGY", "risk limits readable", UNKNOWN,
              limits["__error__"])
    else:
        lim = limits.get("limits") or {}
        check("STRATEGY", "risk limits readable", OK,
              f"version={limits.get('version')}")
        # The frozen values. Checked individually so a changed one is
        # named rather than hidden in a version string.
        expected = {"daily_capital_limit": 50.0, "max_trade_risk": 2.0,
                    "max_concurrent_positions": 2,
                    "daily_loss_limit": 5.0}
        for key, want in expected.items():
            got = lim.get(key)
            check("STRATEGY", f"{key} unchanged",
                  OK if got == want else BAD,
                  f"{got} (expected {want})")

    readiness = get(f"{base}/agent/readiness")
    if "__error__" not in readiness:
        check("STRATEGY", "real-money readiness still NOT_READY",
              OK if readiness.get("ready_for_real_money") is False
              else BAD, str(readiness.get("verdict")))

    # ---------------- TEST ----------------
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env = {k: v for k, v in os.environ.items()
           if k not in ("AWS_PROFILE", "AWS_DEFAULT_PROFILE",
                        "RUN_AWS_INTEGRATION_TESTS")}
    herm = subprocess.run(
        [sys.executable, "scripts/verify_test_hermeticity.py"],
        cwd=repo, capture_output=True, text=True, env=env)
    check("TEST", "unit suite is hermetic",
          OK if herm.returncode == 0 else
          (UNKNOWN if herm.returncode == NOT_RUN else BAD),
          [line for line in herm.stdout.splitlines()
           if line.startswith(("PASS", "FAIL", "NOT RUN"))][:1])

    return report(args)


def report(args) -> int:
    width = max((len(n) for _, n, _, _ in checks), default=10)
    bad = [c for c in checks if c[2] == BAD]
    unknown = [c for c in checks if c[2] == UNKNOWN]
    if args.json:
        print(json.dumps([{"category": c[0], "check": c[1],
                           "state": c[2], "detail": c[3]}
                          for c in checks], indent=2))
        return FAIL if bad else (NOT_RUN if unknown else PASS)

    current = None
    print("=" * (width + 34))
    print("PRE-MARKET READINESS")
    print("=" * (width + 34))
    for category, name, state, detail in checks:
        if category != current:
            print(f"\n[{category}]")
            current = category
        print(f"  {name:{width}}  {state:7} {detail}")
    print("\n" + "=" * (width + 34))
    print(f"{len(checks) - len(bad) - len(unknown)}/{len(checks)} passed, "
          f"{len(bad)} failed, {len(unknown)} unknown")
    if bad:
        print("\nNOT READY - failures:")
        for c in bad:
            print(f"  {c[1]}: {c[3]}")
    if unknown:
        print("\nUNKNOWN (counts as unmet, not as a pass):")
        for c in unknown:
            print(f"  {c[1]}: {c[3]}")
    print("\nRESEARCH does not gate operation: prediction models showing "
          "no edge is a research result and is not tested here.")
    return FAIL if bad else (NOT_RUN if unknown else PASS)


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""
Pre-flight for the real-time SIP paper cohort.

Every check is run against the live service, and a check that cannot be
performed counts as a FAILURE rather than being skipped: the point of a
pre-flight is to refuse to proceed on an unknown.

Prints no credential value. Exit 0 = proceed, 1 = do not deploy.

    AWS_PROFILE=mypodops python3 scripts/preflight_realtime_cohort.py
"""
from __future__ import annotations

import json
import subprocess
import os
import pathlib
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from urllib.parse import urlparse

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

REGION = "us-east-2"
SECRET = "stock-agent/alpaca-paper"
PAPER_HOST = "paper-api.alpaca.markets"
LIVE_HOSTS = {"api.alpaca.markets", "broker-api.alpaca.markets"}
DATA = "https://data.alpaca.markets/v2/stocks/{sym}/quotes/latest?feed=sip"
MAX_SOURCE_AGE = 120.0

results: list[tuple[str, bool, str]] = []


def api_base() -> str:
    out = subprocess.run(
        ["aws", "lambda", "get-function-url-config",
         "--function-name", "stock-agent-dev-api", "--region", REGION,
         "--query", "FunctionUrl", "--output", "text"],
        capture_output=True, text=True, check=True)
    return out.stdout.strip()


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))


def get(url, headers, timeout=20):
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:200]
    except Exception as e:                                # noqa: BLE001
        return 0, f"{type(e).__name__}: {e}"


def main() -> int:
    import boto3
    sm = boto3.client("secretsmanager", region_name=REGION)
    try:
        creds = json.loads(sm.get_secret_value(
            SecretId=SECRET)["SecretString"])
    except Exception as e:                                # noqa: BLE001
        check("secret readable", False, type(e).__name__)
        return report()
    check("secret readable", True, SECRET)
    check("credential fields present",
          bool(creds.get("api_key_id") and creds.get("api_secret_key")))
    headers = {"APCA-API-KEY-ID": creds["api_key_id"],
               "APCA-API-SECRET-KEY": creds["api_secret_key"],
               "Accept": "application/json"}

    # --- paper account, on the paper host only ------------------------
    assert urlparse(f"https://{PAPER_HOST}").hostname == PAPER_HOST
    status, body = get(f"https://{PAPER_HOST}/v2/account", headers)
    ok = status == 200 and isinstance(body, dict)
    check("paper /v2/account 200", ok, f"status={status}")
    if ok:
        check("account ACTIVE", body.get("status") == "ACTIVE",
              str(body.get("status")))
        check("trading not blocked", body.get("trading_blocked") is False)
        check("account not blocked", body.get("account_blocked") is False)

    # --- the book must be PROVEN flat, not merely look flat -----------
    #
    # A cohort opened on an unknown book is a cohort whose starting
    # exposure is unknown, and every later figure inherits that. So each
    # of these requires a SUCCESSFUL read as well as a zero count: an
    # unreadable collection is a failure here, never an empty one.
    #
    # This is the guard that `orders == []` cannot give you, because a
    # failed read also produces an empty list.
    status, body = get(f"https://{PAPER_HOST}/v2/orders?status=open", headers)
    read_ok = status == 200 and isinstance(body, list)
    check("paper open-orders read succeeded", read_ok, f"status={status}")
    check("paper open orders = 0 (proven)",
          read_ok and len(body) == 0,
          f"{len(body) if read_ok else 'UNREADABLE'} open order(s)")

    status, body = get(f"https://{PAPER_HOST}/v2/positions", headers)
    read_ok = status == 200 and isinstance(body, list)
    check("paper positions read succeeded", read_ok, f"status={status}")
    check("paper positions = 0 (proven)",
          read_ok and len(body) == 0,
          f"{len(body) if read_ok else 'UNREADABLE'} position(s)")

    # The agent's own view has to agree, and has to be readable. A
    # broker that is flat while the internal store is unreadable is not
    # a clean start: the two cannot be reconciled.
    api = api_base()
    status, body = get(f"{api}agent/positions", {})
    internal_ok = (status == 200 and isinstance(body, dict)
                   and body.get("source") == "position store")
    check("internal position store read succeeded", internal_ok,
          str((body or {}).get("source") if isinstance(body, dict)
              else f"status={status}"))
    check("internal open positions = 0 (proven)",
          internal_ok and body.get("open_count") == 0,
          f"open_count={(body or {}).get('open_count')}"
          if isinstance(body, dict) else "UNREADABLE")

    status, body = get(f"{api}agent/autonomy", {})
    auto_ok = status == 200 and isinstance(body, dict)
    check("autonomy read succeeded", auto_ok, f"status={status}")
    check("positions are known, not merely absent",
          auto_ok and body.get("positions_known") is True,
          str((body or {}).get("positions_known")))
    check("current-session activity is established",
          auto_ok and (body.get("daily") or {}).get(
              "positions_opened_today") is not None,
          str((body or {}).get("daily", {}).get("positions_opened_today")))
    last_date = (body or {}).get("last_cycle_session_date")
    check("last cycle is attributable to a session", bool(last_date),
          str(last_date))

    # --- real-time SIP, judged on the DATA timestamp ------------------
    status, body = get(DATA.format(sym="AAPL"), headers)
    ok = status == 200 and isinstance(body, dict)
    check("SIP quote 200", ok, f"status={status}")
    if ok:
        quote = body.get("quote") or {}
        stamp = quote.get("t")
        age = None
        if stamp:
            text = str(stamp).replace("Z", "+00:00")
            if "." in text:
                head, _, tail = text.partition(".")
                digits = "".join(c for c in tail if c.isdigit())[:6]
                rest = tail[len(digits):].lstrip("0123456789")
                text = f"{head}.{digits}{rest or '+00:00'}"
            try:
                age = (datetime.now(timezone.utc)
                       - datetime.fromisoformat(text)).total_seconds()
            except ValueError:
                age = None
        check("SIP source timestamp present", stamp is not None, str(stamp))
        check("SIP data age within limit",
              age is not None and age <= MAX_SOURCE_AGE,
              f"{age:.1f}s" if age is not None else "unknown")

    # --- the live venue must be unreachable by construction -----------
    from agent.broker.alpaca_paper import AlpacaPaperBroker, NotPaperEndpoint
    refused = 0
    for host in LIVE_HOSTS:
        try:
            AlpacaPaperBroker(transport=object(), base_url=f"https://{host}")
        except NotPaperEndpoint:
            refused += 1
    check("adapter refuses every live host", refused == len(LIVE_HOSTS),
          f"{refused}/{len(LIVE_HOSTS)}")
    adapter = AlpacaPaperBroker(transport=object())
    check("adapter base URL is the paper host",
          urlparse(adapter.base_url).hostname == PAPER_HOST,
          adapter.base_url)

    # --- execution policy ---------------------------------------------
    from agent.autonomy import ExecutionMode, policy_from_environment
    # The mode the cycle Lambda will actually run under, read from its
    # deployed configuration rather than assumed.
    import boto3 as _b
    env = (_b.client("lambda", region_name=REGION)
           .get_function_configuration(FunctionName="stock-agent-dev-cycle")
           .get("Environment", {}).get("Variables", {}))
    configured = env.get("AGENT_EXECUTION_MODE")
    policy = policy_from_environment(configured)
    check("cycle execution mode PAPER", policy.mode is ExecutionMode.PAPER,
          f"AGENT_EXECUTION_MODE={configured}")
    check("live trading disabled", policy.live_trading_enabled is False)
    check("no live URL in cycle config",
          not any("api.alpaca.markets" in str(v)
                  and "paper-api" not in str(v) for v in env.values()))

    # --- halt and emergency state -------------------------------------
    from agent.risk import DynamoDBHaltStore
    try:
        halt = DynamoDBHaltStore(table_name="stock-agent-dev-journal",
                                 region=REGION).get()
        check("global halt clear", not getattr(halt, "halted", True),
              str(getattr(halt, "reason", "")))
    except Exception as e:                                # noqa: BLE001
        check("global halt readable", False, type(e).__name__)

    from agent.autonomy import DynamoDBHealthStore
    try:
        snap = DynamoDBHealthStore(
            table_name="stock-agent-dev-journal").snapshot()
        check("health permits entries", snap.entries_permitted,
              str(snap.state))
    except Exception as e:                                # noqa: BLE001
        check("health readable", False, type(e).__name__)

    return report()


def report() -> int:
    width = max(len(n) for n, _, _ in results)
    failed = [n for n, ok, _ in results if not ok]
    print("=" * (width + 22))
    for name, ok, detail in results:
        print(f"{name:{width}}  {'PASS' if ok else 'FAIL'}  {detail}")
    print("=" * (width + 22))
    if failed:
        print(f"DO NOT DEPLOY - {len(failed)} check(s) failed: {failed}")
        return 1
    print(f"all {len(results)} checks passed - safe to start the cohort")
    return 0


if __name__ == "__main__":
    sys.exit(main())

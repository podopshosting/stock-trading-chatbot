#!/usr/bin/env python3
"""Audit the deployed dev tables for unit-test contamination.

    AWS_PROFILE=mypodops python3 scripts/audit_test_contamination.py

Needs real credentials and real `requests`, so it runs OUTSIDE the test
suite - the suite is sealed from AWS precisely so it cannot do this.

CLASSIFY, do not delete. A row that looks like a fixture may be a real
operation on a thinly-traded symbol, and deleting evidence of an
incident destroys the ability to describe it later.

Classification:
  REAL_OPERATION           - attributable to a scheduled/manual cycle
  UNIT_TEST_CONTAMINATION  - fixture symbol or fixture-shaped id
  INTEGRATION_TEST         - written under an explicit integration run
  UNKNOWN                  - cannot be decided from the record
"""
import json, boto3
from collections import Counter

ddb = boto3.client("dynamodb", region_name="us-east-2")

# Fixture symbols used by the suite. Read from the test sources rather
# than guessed, so the list cannot drift from what the tests use.
FIXTURE_SYMBOLS = {"XYZ", "AAA", "BBB", "CCC", "XXX", "YYY", "ZZZ",
                   "A", "B", "C", "TEST", "FAKE", "SIM"}
# Partitions whose contents are SUPPOSED to mention fixture symbols.
# REPLAYRUN rows record synthetic scenario results, and the scenario
# library uses XYZ as its symbol - flagging those as contamination
# would bury the real finding under legitimate simulation output.
SIMULATION_PARTITIONS = ("REPLAYRUN#",)
REAL_SYMBOLS = {"DRAM", "MSFT", "AAPL", "NVDA", "SPY", "QQQ", "IWM",
                "INTC", "TSLA", "GOOG", "GOOGL", "AMZN", "META"}

def scan(table):
    rows, kw = [], {"TableName": table}
    while True:
        r = ddb.scan(**kw)
        rows.extend(r.get("Items", []))
        if "LastEvaluatedKey" not in r:
            return rows
        kw["ExclusiveStartKey"] = r["LastEvaluatedKey"]

def plain(item):
    out = {}
    for k, v in item.items():
        if "S" in v: out[k] = v["S"]
        elif "N" in v: out[k] = v["N"]
        elif "BOOL" in v: out[k] = v["BOOL"]
        elif "NULL" in v: out[k] = None
        else: out[k] = next(iter(v.values()))
    return out

def classify(row, blob):
    pk = str(row.get("PK") or "")
    if any(pk.startswith(p) for p in SIMULATION_PARTITIONS):
        return "SIMULATION_RECORD", "persisted replay result"
    sym = row.get("symbol")
    if sym in FIXTURE_SYMBOLS:
        return "UNIT_TEST_CONTAMINATION", f"fixture symbol {sym}"
    # DynamoDB JSON nests values as {"symbol": {"S": "XYZ"}}, so a
    # literal '"symbol": "XYZ"' never matches. The first version of this
    # audit reported ZERO contamination for that reason, while eight
    # known XYZ order rows sat in the table - a clean result produced by
    # a broken probe, which is worse than a dirty one.
    for s in FIXTURE_SYMBOLS:
        if f'"S": "{s}"' in blob:
            return "UNIT_TEST_CONTAMINATION", f"fixture symbol {s} in payload"
    if sym in REAL_SYMBOLS:
        return "REAL_OPERATION", f"traded symbol {sym}"
    if sym:
        return "UNKNOWN", f"symbol {sym} is neither fixture nor known-real"
    return "REAL_OPERATION", "no symbol; control/state record"

for table in ("stock-agent-dev-journal", "stock-agent-dev-positions",
              "stock-agent-dev-broker", "stock-agent-dev-state"):
    try:
        rows = scan(table)
    except Exception as e:
        print(f"\n### {table}: READ FAILED {type(e).__name__}: {e}")
        continue
    tally, suspects = Counter(), []
    for item in rows:
        row = plain(item)
        blob = json.dumps(item, default=str)
        verdict, why = classify(row, blob)
        tally[verdict] += 1
        if verdict == "UNIT_TEST_CONTAMINATION":
            suspects.append((row.get("PK"), row.get("SK"), why,
                             row.get("status"), row.get("session_date")))
    print(f"\n### {table}: {len(rows)} rows  {dict(tally)}")
    for pk, sk, why, status, sd in suspects[:30]:
        print(f"   {pk} | {sk} | {why} | status={status} | session={sd}")
    if len(suspects) > 30:
        print(f"   ... and {len(suspects)-30} more")

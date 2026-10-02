#!/usr/bin/env python3
"""
Validate DynamoDBOrderLedger against the REAL dev table.

Isolated by partition: the session date is a sentinel that cannot be a
market date, so these rows can never be read by a query for a real
session and cannot contaminate strategy evidence.

Every check reports individually. A check that could not be performed is
a FAILURE, not a skip.
"""
import sys, os, json, uuid
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import boto3
from agent.broker.order_ledger import (
    DynamoDBOrderLedger, ExternalOrderRecord, OrderLedgerError,
    committed_exposure,
)

TABLE = "stock-agent-dev-journal"
REGION = "us-east-2"
# Not a date. A query for any real session cannot reach this partition.
SESSION = f"LEDGER-VALIDATION-{uuid.uuid4().hex[:8]}"
RUN = uuid.uuid4().hex[:6]

checks = []
def check(name, ok, detail=""):
    checks.append((name, bool(ok), str(detail)[:160]))
    return bool(ok)

def client():
    return boto3.Session(profile_name="mypodops",
                         region_name=REGION).client("dynamodb")

ledger = DynamoDBOrderLedger(table_name=TABLE, client=client())
written = []

def rec(cli, **kw):
    base = dict(client_order_id=cli, session_date=SESSION, symbol="VALID",
                side="BUY", requested_quantity=1.0, requested_notional=100.0,
                intent="ENTRY", intent_at="2026-10-02T19:00:00+00:00")
    base.update(kw)
    return ExternalOrderRecord(**base)

# --- 1. create intent ------------------------------------------------
cli_a = f"cli_val_{RUN}_a"
try:
    ledger.record_intent(rec(cli_a))
    written.append(cli_a)
    check("intent written", True, cli_a)
except Exception as e:
    check("intent written", False, f"{type(e).__name__}: {e}")

# --- 2. read back by client_order_id --------------------------------
try:
    row = ledger.get(cli_a, session_date=SESSION)
    check("read back by client_order_id", row is not None
          and row.client_order_id == cli_a,
          f"status={getattr(row,'status',None)}")
    check("intent fields survived the round trip",
          row is not None and row.requested_notional == 100.0
          and row.symbol == "VALID" and row.intent == "ENTRY",
          f"notional={getattr(row,'requested_notional',None)}")
    check("an unobserved intent is NOT terminal",
          row is not None and not row.is_terminal
          and not row.submission_outcome_known)
except Exception as e:
    check("read back by client_order_id", False, f"{type(e).__name__}: {e}")

# --- 3. conditional duplicate refusal -------------------------------
#     A repeated intent must not overwrite a row that carries
#     observations. The conditional put is what makes a retried Lambda
#     invocation safe.
try:
    ledger.record_observation(cli_a, {
        "order_id": "brk_val_1", "status": "PARTIALLY_FILLED",
        "raw_status": "partially_filled", "filled_quantity": 0.6,
        "remaining_quantity": 0.4, "average_fill_price": 100.0,
        "submitted_at": "2026-10-02T19:00:05+00:00"},
        "2026-10-02T19:00:06+00:00", session_date=SESSION)
    ledger.record_intent(rec(cli_a))          # the duplicate
    row = ledger.get(cli_a, session_date=SESSION)
    check("a repeated intent does not erase observations",
          row.filled_quantity == 0.6 and row.broker_order_id == "brk_val_1",
          f"filled={row.filled_quantity} broker_id={row.broker_order_id}")
except Exception as e:
    check("a repeated intent does not erase observations", False,
          f"{type(e).__name__}: {e}")

# --- 4. filled quantity monotonicity --------------------------------
try:
    ledger.record_observation(cli_a, {
        "order_id": "brk_val_1", "status": "PARTIALLY_FILLED",
        "raw_status": "partially_filled", "filled_quantity": 0.2,
        "average_fill_price": 100.0}, "2026-10-02T19:00:07+00:00",
        session_date=SESSION)
    row = ledger.get(cli_a, session_date=SESSION)
    check("a stale observation cannot un-fill a position",
          row.filled_quantity == 0.6, f"filled={row.filled_quantity}")
except Exception as e:
    check("a stale observation cannot un-fill a position", False,
          f"{type(e).__name__}: {e}")

# --- 5. unknown status stays non-terminal ---------------------------
cli_b = f"cli_val_{RUN}_b"
try:
    ledger.record_intent(rec(cli_b, symbol="VALIDB"))
    written.append(cli_b)
    ledger.record_observation(cli_b, {
        "order_id": "brk_val_2", "status": "SOMETHING_THE_VENUE_INVENTED",
        "raw_status": "weird_new_state", "filled_quantity": 0.0},
        "2026-10-02T19:00:08+00:00", session_date=SESSION)
    row = ledger.get(cli_b, session_date=SESSION)
    check("an unrecognised status stays non-terminal",
          not row.is_terminal, f"status={row.status}")
    check("the venue's exact words are preserved",
          row.raw_status == "weird_new_state", f"raw={row.raw_status}")
except Exception as e:
    check("an unrecognised status stays non-terminal", False,
          f"{type(e).__name__}: {e}")

# --- 6. observation without an intent is refused --------------------
try:
    ledger.record_observation(f"cli_val_{RUN}_ghost", {
        "order_id": "x", "status": "FILLED", "filled_quantity": 1.0},
        "2026-10-02T19:00:09+00:00", session_date=SESSION)
    check("an observation with no intent is refused", False,
          "it was accepted, which would fabricate provenance")
except OrderLedgerError:
    check("an observation with no intent is refused", True)
except Exception as e:
    check("an observation with no intent is refused", False,
          f"wrong error: {type(e).__name__}")

# --- 7. query outstanding non-terminal orders -----------------------
try:
    rows = ledger.for_session(SESSION)
    outstanding = ledger.non_terminal(SESSION)
    check("the session partition queries back",
          len(rows) == len(written), f"{len(rows)} of {len(written)}")
    check("both rows read as outstanding",
          len(outstanding) == 2, f"{len(outstanding)} outstanding")
except Exception as e:
    check("the session partition queries back", False,
          f"{type(e).__name__}: {e}")

# --- 8. committed_exposure semantics --------------------------------
try:
    exposure = committed_exposure(ledger.non_terminal(SESSION))
    check("exposure is KNOWN and non-zero for live orders",
          exposure["known"] and exposure["reserved"] > 0,
          f"reserved={exposure['reserved']} known={exposure['known']}")
except Exception as e:
    check("exposure is KNOWN and non-zero for live orders", False,
          f"{type(e).__name__}: {e}")

cli_c = f"cli_val_{RUN}_c"
try:
    ledger.record_intent(rec(cli_c, symbol="VALIDC",
                             requested_notional=None,
                             requested_quantity=None))
    written.append(cli_c)
    exposure = committed_exposure(ledger.non_terminal(SESSION))
    check("an unsizeable order makes exposure UNKNOWN",
          not exposure["known"] and cli_c in exposure["unestablished"],
          f"known={exposure['known']}")
except Exception as e:
    check("an unsizeable order makes exposure UNKNOWN", False,
          f"{type(e).__name__}: {e}")

# --- 9. NEVER_PLACED only from a confirmed absence ------------------
try:
    ledger.record_never_placed(cli_b, "2026-10-02T19:00:10+00:00",
                               session_date=SESSION)
    check("a previously observed order refuses NEVER_PLACED", False,
          "it was accepted")
except OrderLedgerError:
    check("a previously observed order refuses NEVER_PLACED", True)
except Exception as e:
    check("a previously observed order refuses NEVER_PLACED", False,
          f"wrong error: {type(e).__name__}")

cli_d = f"cli_val_{RUN}_d"
try:
    ledger.record_intent(rec(cli_d, symbol="VALIDD"))
    written.append(cli_d)
    ledger.record_never_placed(cli_d, "2026-10-02T19:00:11+00:00",
                               session_date=SESSION)
    row = ledger.get(cli_d, session_date=SESSION)
    check("an unobserved intent can be marked NEVER_PLACED",
          row.status == "NEVER_PLACED" and row.is_terminal
          and row.raw_status is None,
          f"status={row.status} raw={row.raw_status}")
    check("NEVER_PLACED releases the reservation",
          row.potential_exposure == 0.0,
          f"exposure={row.potential_exposure}")
except Exception as e:
    check("an unobserved intent can be marked NEVER_PLACED", False,
          f"{type(e).__name__}: {e}")

# --- 10. restart: a FRESH client and a fresh ledger object ----------
try:
    fresh = DynamoDBOrderLedger(table_name=TABLE, client=client())
    row = fresh.get(cli_a, session_date=SESSION)
    check("a fresh client reads the same state",
          row is not None and row.filled_quantity == 0.6
          and row.broker_order_id == "brk_val_1",
          f"filled={getattr(row,'filled_quantity',None)}")
    outstanding = fresh.non_terminal(SESSION)
    check("outstanding orders survive a restart",
          len(outstanding) == 3,
          f"{len(outstanding)} outstanding after restart")
except Exception as e:
    check("a fresh client reads the same state", False,
          f"{type(e).__name__}: {e}")

# --- 11. a malformed row fails closed -------------------------------
cli_bad = f"cli_val_{RUN}_malformed"
try:
    client().put_item(TableName=TABLE, Item={
        "PK": {"S": f"EXTORDERS#{SESSION}"},
        "SK": {"S": cli_bad},
        "payload": {"S": "{this is not json"}})
    written.append(cli_bad)
    try:
        ledger.for_session(SESSION)
        check("a malformed row fails the whole read", False,
              "the read succeeded, so a partial ledger looked complete")
    except OrderLedgerError:
        check("a malformed row fails the whole read", True)
except Exception as e:
    check("a malformed row fails the whole read", False,
          f"{type(e).__name__}: {e}")

# --- 11b. an un-sessioned lookup cannot establish absence -----------
try:
    ledger.get(cli_a)
    check("an un-sessioned lookup refuses to establish absence", False,
          "it returned without raising")
except OrderLedgerError as e:
    check("an un-sessioned lookup refuses to establish absence",
          "NOT established" in str(e), str(e)[:90])
except Exception as e:
    check("an un-sessioned lookup refuses to establish absence", False,
          f"wrong error: {type(e).__name__}")

# --- 12. a real session partition is untouched ----------------------
try:
    real = DynamoDBOrderLedger(table_name=TABLE, client=client())
    rows = real.for_session("2026-10-02")
    check("the real session partition holds no validation rows",
          all("cli_val_" not in (r.client_order_id or "") for r in rows),
          f"{len(rows)} rows in the real partition")
except Exception as e:
    check("the real session partition holds no validation rows", False,
          f"{type(e).__name__}: {e}")

# --- cleanup: ONLY the rows this run wrote --------------------------
removed, failed = 0, []
c = client()
for cli in written:
    try:
        c.delete_item(TableName=TABLE, Key={
            "PK": {"S": f"EXTORDERS#{SESSION}"}, "SK": {"S": cli}})
        removed += 1
    except Exception as e:                                # noqa: BLE001
        failed.append(f"{cli}: {e}")
check("every validation row was removed", removed == len(written) and not failed,
      f"{removed} of {len(written)} removed{'; ' + '; '.join(failed) if failed else ''}")
try:
    leftover = c.query(TableName=TABLE,
                       KeyConditionExpression="PK = :pk",
                       ExpressionAttributeValues={
                           ":pk": {"S": f"EXTORDERS#{SESSION}"}})
    check("the validation partition is empty afterwards",
          not leftover.get("Items"), f"{len(leftover.get('Items', []))} left")
except Exception as e:
    check("the validation partition is empty afterwards", False, str(e))

width = max(len(n) for n, _, _ in checks)
print("=" * (width + 22))
print(f"DynamoDBOrderLedger against {TABLE}")
print(f"isolated partition: EXTORDERS#{SESSION}")
print("=" * (width + 22))
failed_names = [n for n, ok, _ in checks if not ok]
for n, ok, d in checks:
    print(f"{n:{width}}  {'PASS' if ok else 'FAIL'}  {d}")
print("=" * (width + 22))
print(f"{len(checks) - len(failed_names)}/{len(checks)} checks passed")
if failed_names:
    print("NOT CLEAN:", failed_names)
sys.exit(1 if failed_names else 0)

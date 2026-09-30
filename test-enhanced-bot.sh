#!/usr/bin/env bash
#
# End-to-end checks against the production chatbot API.
#
# Asserts on response *structure and behaviour*, never on specific prices or
# dates, so it stays valid as the market moves. Exits non-zero if any check
# fails.
#
# QUOTA WARNING: each stock query spends 2 Alpha Vantage requests, and the
# free tier allows only 25 per day. A full run costs ~4 requests.
#
# Usage:
#   ./test-enhanced-bot.sh              # all checks
#   ./test-enhanced-bot.sh --no-stocks  # general-knowledge checks only (0 quota)

set -uo pipefail

API_URL="${API_URL:-https://lmi4hshs7h.execute-api.us-east-2.amazonaws.com/prod/chatbot}"
RUN_STOCK_TESTS=1
[[ "${1:-}" == "--no-stocks" ]] && RUN_STOCK_TESTS=0

PASS=0
FAIL=0

pass() { echo "  PASS: $1"; PASS=$((PASS + 1)); }
fail() { echo "  FAIL: $1" >&2; FAIL=$((FAIL + 1)); }

# ask <query> -> response body on stdout
ask() {
  curl -sS -m 60 -X POST "$API_URL" \
    -H 'Content-Type: application/json' \
    -d "{\"query\":$(python3 -c 'import json,sys; print(json.dumps(sys.argv[1]))' "$1")}"
}

# check_general <label> <query>
check_general() {
  local label="$1" query="$2" body
  echo "-- $label"
  body="$(ask "$query")" || { fail "$label: request failed"; return; }

  python3 - "$body" <<'PY'
import json, sys
try:
    d = json.loads(sys.argv[1])
except Exception as e:
    print(f"unparseable JSON: {e}"); sys.exit(1)
if d.get("symbol"):
    print(f"unexpected symbol on general query: {d['symbol']}"); sys.exit(1)
r = d.get("response") or ""
if len(r) < 80:
    print(f"response too short ({len(r)} chars)"); sys.exit(1)
sys.exit(0)
PY
  if [[ $? -eq 0 ]]; then pass "$label"; else fail "$label"; fi
}

# check_stock <label> <query> <expected-symbol>
check_stock() {
  local label="$1" query="$2" symbol="$3" body
  echo "-- $label"
  body="$(ask "$query")" || { fail "$label: request failed"; return; }

  python3 - "$body" "$symbol" <<'PY'
import json, sys
body, expected = sys.argv[1], sys.argv[2]
try:
    d = json.loads(body)
except Exception as e:
    print(f"unparseable JSON: {e}"); sys.exit(1)

if d.get("data_unavailable"):
    print("provider rate limit hit - inconclusive, not a failure"); sys.exit(2)

if d.get("symbol") != expected:
    print(f"expected symbol {expected}, got {d.get('symbol')}"); sys.exit(1)

data = d.get("data") or {}
for field in ("price", "change", "volume"):
    if field not in data:
        print(f"missing quote field: {field}"); sys.exit(1)
if not isinstance(data["price"], (int, float)) or data["price"] <= 0:
    print(f"implausible price: {data['price']!r}"); sys.exit(1)

# ML layer: present whenever the provider returned enough history.
for field in ("recommendation", "ml_confidence", "risk_level", "rsi"):
    if field not in data:
        print(f"missing ML field: {field} (ML analysis did not run)"); sys.exit(1)
if data["recommendation"].lower() not in ("buy", "sell", "hold"):
    print(f"bad recommendation: {data['recommendation']!r}"); sys.exit(1)
if not 0 <= data["ml_confidence"] <= 100:
    print(f"confidence out of range: {data['ml_confidence']!r}"); sys.exit(1)

print(f"{expected}: ${data['price']} | {data['recommendation'].upper()} "
      f"| confidence {data['ml_confidence']}% | risk {data['risk_level']}")
sys.exit(0)
PY
  local rc=$?
  case $rc in
    0) pass "$label" ;;
    2) echo "  SKIP: $label (provider rate limited)" ;;
    *) fail "$label" ;;
  esac
}

check_invalid_symbol() {
  local body
  echo "-- Invalid symbol is rejected, not fabricated"
  body="$(ask 'Tell me about ZZZQ')" || { fail "invalid symbol: request failed"; return; }

  python3 - "$body" <<'PY'
import json, sys
d = json.loads(sys.argv[1])
r = (d.get("response") or "").lower()
if d.get("symbol"):
    print(f"bogus ticker resolved to a symbol: {d['symbol']}"); sys.exit(1)
if "invalid" not in r and "could not" not in r and "cannot" not in r:
    print(f"no clear error for a bogus ticker: {r[:120]}"); sys.exit(1)
sys.exit(0)
PY
  if [[ $? -eq 0 ]]; then pass "invalid symbol handled"; else fail "invalid symbol handled"; fi
}

check_cors() {
  echo "-- CORS preflight"
  local hdrs
  hdrs="$(curl -sS -m 30 -i -X OPTIONS "$API_URL" \
    -H 'Origin: http://stock-chatbot-web.s3-website.us-east-2.amazonaws.com' \
    -H 'Access-Control-Request-Method: POST' \
    -H 'Access-Control-Request-Headers: content-type')"
  if printf '%s' "$hdrs" | /usr/bin/grep -qi 'access-control-allow-origin'; then
    pass "CORS preflight returns allow-origin"
  else
    fail "CORS preflight missing allow-origin"
  fi
}

echo "=========================================="
echo "Production chatbot API checks"
echo "  $API_URL"
echo "=========================================="

check_cors
check_general "General knowledge: Buffett strategy" "explain Warren Buffett investment strategy"
check_general "General knowledge: diversification" "tell me about diversification"
check_general "General knowledge: exchanges" "what are the major US stock exchanges?"

if [[ "$RUN_STOCK_TESTS" -eq 1 ]]; then
  check_stock "Stock query: AAPL" "What do you think about AAPL?" "AAPL"
  check_stock "Stock query: MSFT" "Should I buy MSFT?" "MSFT"
  check_invalid_symbol
else
  echo "-- Skipping stock queries (--no-stocks): preserves Alpha Vantage quota"
fi

echo "=========================================="
echo "Passed: $PASS   Failed: $FAIL"
echo "=========================================="
[[ "$FAIL" -eq 0 ]]

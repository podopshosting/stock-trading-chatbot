#!/usr/bin/env bash
#
# Deploy the DEVELOPMENT agent API and dashboard.
#
# Touches only dev resources:
#   Lambda  stock-agent-dev-api
#   Bucket  stock-agent-dev-ui
#
# It does not touch stock-chatbot-router or stock-chatbot-web, which
# serve production /chatbot. Those names do not appear below, so a typo
# in an argument cannot reach them.
#
set -euo pipefail

REGION="${AWS_REGION:-us-east-2}"
PROFILE="${AWS_PROFILE:-mypodops}"
FUNCTION="stock-agent-dev-api"
CYCLE_FUNCTION="stock-agent-dev-cycle"
BUCKET="stock-agent-dev-ui"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD="$(mktemp -d)"
trap 'rm -rf "$BUILD"' EXIT

target="${1:-all}"

# A deploy over a red suite is how a broken invariant reaches a running
# system. On 2026-10-01 the shadow store was added under agent/broker,
# three tests said the read API must not import a broker, and it was
# deployed anyway because the test output had been filtered through grep
# rather than gated on. The suite now gates the deploy.
#
# AGENT_DEPLOY_SKIP_TESTS=1 exists for a genuine emergency and prints a
# warning, because a silent escape hatch is the same bug again.
run_test_gate() {
  if [ "${AGENT_DEPLOY_SKIP_TESTS:-0}" = "1" ]; then
    echo "WARNING: test gate SKIPPED by AGENT_DEPLOY_SKIP_TESTS=1" >&2
    return 0
  fi
  echo "==> test gate"
  # EXPLICIT CREDENTIAL BOUNDARY.
  #
  # This script exports AWS_PROFILE so it can deploy. Running the test
  # gate in that same environment is what turned the unit suite into an
  # integration suite: tests/test_cycle_handler.py invokes the real
  # cycle handler, which built real DynamoDB clients, which then
  # succeeded. Every deploy wrote two phantom order intents, cycle
  # snapshots and health streak records into the deployed dev tables.
  #
  # The test phase now runs with credentials REMOVED from its
  # environment. tests/__init__.py seals the suite anyway - that is
  # layer three - but a deploy script that hands credentials to a test
  # run is a boundary error regardless of whether something downstream
  # catches it, and the two phases genuinely have different authority.
  #
  # env -u is used rather than setting empty values: an empty
  # AWS_PROFILE is still a profile name to botocore.
  if ! ( cd "$REPO" && env -u AWS_PROFILE -u AWS_DEFAULT_PROFILE \
            -u AWS_ACCESS_KEY_ID -u AWS_SECRET_ACCESS_KEY \
            -u AWS_SESSION_TOKEN -u AWS_SECURITY_TOKEN \
            -u RUN_AWS_INTEGRATION_TESTS \
            python3 -m unittest discover -s tests -t . \
            > /tmp/agent-deploy-tests.log 2>&1 ); then
    echo "FATAL: the test suite is RED; refusing to deploy." >&2
    grep -E '^(FAIL|ERROR):' /tmp/agent-deploy-tests.log | head -20 >&2
    tail -3 /tmp/agent-deploy-tests.log >&2
    exit 1
  fi
  tail -1 /tmp/agent-deploy-tests.log | sed 's/^/    /'
  grep -E '^Ran ' /tmp/agent-deploy-tests.log | sed 's/^/    /'

  # And prove the suite stayed inside the process. The suite passing
  # says the tests agree with the code; it says nothing about whether
  # they reached AWS. For weeks they did.
  echo "==> hermeticity gate"
  if ! ( cd "$REPO" && env -u AWS_PROFILE -u AWS_DEFAULT_PROFILE \
            -u RUN_AWS_INTEGRATION_TESTS \
            python3 scripts/verify_test_hermeticity.py \
            > /tmp/agent-deploy-hermetic.log 2>&1 ); then
    echo "FATAL: the test suite is not hermetic; refusing to deploy." >&2
    grep -aE "^(FAIL|NOT RUN|AWS calls)" /tmp/agent-deploy-hermetic.log \
      | head -10 >&2
    exit 1
  fi
  grep -aE "^(AWS calls|blocked attempts|PASS)" \
    /tmp/agent-deploy-hermetic.log | sed 's/^/    /'
}



deploy_lambda() {
  echo "==> packaging $FUNCTION"
  cp "$REPO/lambda-micro/agent-api/handler.py" "$BUILD/"
  # The agent package is the source of truth for every decision; the
  # Lambda must run the same code the tests exercise, not a copy.
  rsync -a --exclude '__pycache__' --exclude '*.pyc' \
    "$REPO/agent" "$BUILD/"

  # Vendored dependencies. Omitting these once silently broke every
  # provider-backed endpoint with "No module named 'requests'" while
  # the Lambda still returned 200s from the endpoints that did not need
  # them - a partial failure that looked like a working deployment.
  echo "==> installing dependencies"
  python3 -m pip install --quiet --target "$BUILD" \
    --platform manylinux2014_x86_64 --implementation cp \
    --python-version 3.12 --only-binary=:all: --upgrade \
    -r "$REPO/lambda-micro/agent-api/requirements.txt"

  # Fail loudly rather than shipping a package missing its imports.
  for module in requests; do
    if [ ! -d "$BUILD/$module" ]; then
      echo "FATAL: $module missing from the package" >&2
      exit 1
    fi
  done

  ( cd "$BUILD" && zip -qr function.zip . )
  local bytes
  bytes=$(wc -c < "$BUILD/function.zip")
  echo "    package: $bytes bytes"

  echo "==> updating $FUNCTION"
  aws lambda update-function-code \
    --function-name "$FUNCTION" \
    --zip-file "fileb://$BUILD/function.zip" \
    --region "$REGION" --profile "$PROFILE" \
    --query '{LastModified:LastModified,CodeSize:CodeSize}' --output json

  aws lambda wait function-updated \
    --function-name "$FUNCTION" --region "$REGION" --profile "$PROFILE"

  # A deploy is not finished until the thing responds. This checks an
  # endpoint that NEEDS the vendored dependencies, because the ones
  # that do not will answer happily from a broken package.
  echo "==> verifying"
  local url
  url=$(aws lambda get-function-url-config \
    --function-name "$FUNCTION" --region "$REGION" --profile "$PROFILE" \
    --query FunctionUrl --output text)
  local body
  body=$(curl -s --max-time 30 "${url}agent/pipeline?symbol=AAPL" || true)
  case "$body" in
    *"No module named"*)
      echo "FATAL: deployed package is missing a dependency" >&2
      echo "$body" >&2; exit 1 ;;
    *'"stages"'*)
      echo "    pipeline responded with stages" ;;
    *)
      echo "    WARNING: unexpected response:" >&2
      echo "    ${body:0:240}" >&2 ;;
  esac
  echo "    done"
}

deploy_cycle() {
  echo "==> packaging $CYCLE_FUNCTION"
  local build
  build="$(mktemp -d)"
  cp "$REPO/lambda-micro/agent-cycle/handler.py" "$build/"
  rsync -a --exclude '__pycache__' --exclude '*.pyc' "$REPO/agent" "$build/"
  python3 -m pip install --quiet --target "$build" \
    --platform manylinux2014_x86_64 --implementation cp \
    --python-version 3.12 --only-binary=:all: --upgrade \
    -r "$REPO/lambda-micro/agent-cycle/requirements.txt"
  if [ ! -d "$build/requests" ]; then
    echo "FATAL: requests missing from the cycle package" >&2
    rm -rf "$build"; exit 1
  fi
  ( cd "$build" && zip -qr function.zip . )

  aws lambda update-function-code \
    --function-name "$CYCLE_FUNCTION" \
    --zip-file "fileb://$build/function.zip" \
    --region "$REGION" --profile "$PROFILE" \
    --query '{LastModified:LastModified,CodeSize:CodeSize}' --output json
  rm -rf "$build"

  aws lambda wait function-updated \
    --function-name "$CYCLE_FUNCTION" --region "$REGION" --profile "$PROFILE"

  # Pin the code SHA and the execution mode. The environment is MERGED,
  # not replaced: update-function-configuration overwrites every
  # variable, and a replace would silently drop the table names.
  #
  # AGENT_TRADING_ENABLED and AGENT_EXECUTION_AVAILABLE are REMOVED. They
  # were two booleans that between them meant both "trading is on" and
  # "this can reach real money"; AGENT_EXECUTION_MODE replaces them and
  # nothing reads the old ones any more.
  local sha dirty=""
  sha="$(git -C "$REPO" rev-parse --short HEAD)"
  if [ -n "$(git -C "$REPO" status --porcelain -- agent lambda-micro)" ]; then
    dirty="-dirty"
    echo "    WARNING: uncommitted changes under agent/ or lambda-micro/;" >&2
    echo "    the pinned SHA will read $sha$dirty so results cannot be" >&2
    echo "    mistaken for a clean commit." >&2
  fi
  aws lambda get-function-configuration \
    --function-name "$CYCLE_FUNCTION" --region "$REGION" --profile "$PROFILE" \
    --query 'Environment.Variables' --output json > /tmp/cycle-env-current.json
  SHA="$sha$dirty" python3 - <<'PY'
import json, os
env = json.load(open("/tmp/cycle-env-current.json")) or {}
env.pop("AGENT_TRADING_ENABLED", None)
env.pop("AGENT_EXECUTION_AVAILABLE", None)
env["AGENT_EXECUTION_MODE"] = os.environ.get("AGENT_EXECUTION_MODE_OVERRIDE", "PAPER")
env["AGENT_CODE_SHA"] = os.environ["SHA"]
json.dump({"Variables": env}, open("/tmp/cycle-env-new.json", "w"))
PY
  aws lambda update-function-configuration \
    --function-name "$CYCLE_FUNCTION" --region "$REGION" --profile "$PROFILE" \
    --environment file:///tmp/cycle-env-new.json \
    --query 'Environment.Variables.{mode:AGENT_EXECUTION_MODE,sha:AGENT_CODE_SHA}' \
    --output json
  aws lambda wait function-updated \
    --function-name "$CYCLE_FUNCTION" --region "$REGION" --profile "$PROFILE"

  # A cycle that cannot even report its phase is not deployed.
  echo "==> verifying"
  aws lambda invoke --function-name "$CYCLE_FUNCTION" \
    --region "$REGION" --profile "$PROFILE" /tmp/cycle-verify.json \
    --query StatusCode --output text >/dev/null
  python3 - <<'PY'
import json, sys
d = json.load(open("/tmp/cycle-verify.json"))
body = json.loads(d["body"]) if "body" in d else d
if body.get("error"):
    print(f"    FATAL: {body['error']}: {body.get('detail','')[:200]}",
          file=sys.stderr)
    sys.exit(1)
print(f"    ran={body.get('ran')} phase={body.get('phase')} "
      f"outcome={body.get('outcome','n/a')}")
PY
  echo "    done"
}

deploy_ui() {
  echo "==> uploading dashboard to s3://$BUCKET/agent/"
  # no-cache so a dev dashboard never serves a stale build while
  # someone is trying to read live state from it.
  aws s3 cp "$REPO/web/agent/index.html" "s3://$BUCKET/agent/index.html" \
    --content-type "text/html; charset=utf-8" \
    --cache-control "no-cache, no-store, must-revalidate" \
    --region "$REGION" --profile "$PROFILE"
  aws s3 cp "$REPO/web/agent/config.js" "s3://$BUCKET/agent/config.js" \
    --content-type "application/javascript; charset=utf-8" \
    --cache-control "no-cache, no-store, must-revalidate" \
    --region "$REGION" --profile "$PROFILE"
  echo "    http://$BUCKET.s3-website.$REGION.amazonaws.com/agent/"
}

run_test_gate

case "$target" in
  lambda) deploy_lambda ;;
  cycle)  deploy_cycle ;;
  ui)     deploy_ui ;;
  all)    deploy_lambda; deploy_cycle; deploy_ui ;;
  *) echo "usage: $0 [lambda|cycle|ui|all]" >&2; exit 2 ;;
esac

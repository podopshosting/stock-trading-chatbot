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
BUCKET="stock-agent-dev-ui"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD="$(mktemp -d)"
trap 'rm -rf "$BUILD"' EXIT

target="${1:-all}"

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

case "$target" in
  lambda) deploy_lambda ;;
  ui)     deploy_ui ;;
  all)    deploy_lambda; deploy_ui ;;
  *) echo "usage: $0 [lambda|ui|all]" >&2; exit 2 ;;
esac

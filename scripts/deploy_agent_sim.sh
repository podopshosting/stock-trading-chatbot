#!/bin/bash
# Deploy the simulation/research Lambda.
#
# Separate from stock-agent-dev-api on purpose: agent.replay imports
# ..broker.paper, so putting replay in the read API would place broker
# code in the Lambda whose safety property is that it has none. The
# source scan that guards that property reads the handler's own text and
# would have kept passing. See lambda-micro/agent-sim/handler.py.
#
# Creates the function on first run. Creating a function changes nothing
# that is already running.
set -euo pipefail
REGION=us-east-2
PROFILE=mypodops
FUNCTION=stock-agent-dev-sim
ROLE=arn:aws:iam::899383035514:role/stock-chatbot-lambda-role
REPO="$(cd "$(dirname "$0")/.." && pwd)"
BUILD="$(mktemp -d)"
trap 'rm -rf "$BUILD"' EXIT

cp "$REPO/lambda-micro/agent-sim/handler.py" "$BUILD/"
rsync -a --exclude '__pycache__' --exclude '*.pyc' "$REPO/agent" "$BUILD/"

# Vendored dependencies.
#
# The agent-api deploy carries this same step and a comment saying that
# omitting it once broke every provider-backed endpoint with "No module
# named 'requests'" while the Lambda kept returning 200s from the
# endpoints that did not need them. This script was written without that
# step and reproduced the failure exactly: historical replay returned a
# DataUnavailable naming the missing module.
echo "==> installing dependencies"
python3 -m pip install --quiet --target "$BUILD" \
  --platform manylinux2014_x86_64 --implementation cp \
  --python-version 3.12 --only-binary=:all: --upgrade \
  -r "$REPO/lambda-micro/agent-sim/requirements.txt"

# Fail loudly rather than shipping a package missing its imports. A
# partial failure that still serves some routes looks like a working
# deployment, which is why this is a hard exit and not a warning.
for module in requests; do
  if [ ! -d "$BUILD/$module" ]; then
    echo "FATAL: $module missing from the package" >&2
    exit 1
  fi
done

( cd "$BUILD" && zip -qr function.zip . )
bytes=$(wc -c < "$BUILD/function.zip")
echo "package: $bytes bytes"

if aws lambda get-function --function-name "$FUNCTION" --region "$REGION" \
     --profile "$PROFILE" >/dev/null 2>&1; then
  aws lambda update-function-code --function-name "$FUNCTION" \
    --zip-file "fileb://$BUILD/function.zip" --region "$REGION" \
    --profile "$PROFILE" --query 'LastModified' --output text
else
  aws lambda create-function --function-name "$FUNCTION" \
    --runtime python3.12 --role "$ROLE" --handler handler.handler \
    --zip-file "fileb://$BUILD/function.zip" --timeout 120 \
    --memory-size 1024 --region "$REGION" --profile "$PROFILE" \
    --description "Simulation and research workbench. No broker adapter for a live venue; nothing here submits an order." \
    --query 'LastModified' --output text
  aws lambda wait function-active --function-name "$FUNCTION" \
    --region "$REGION" --profile "$PROFILE"
fi

# CORS is answered in the handler, so the Function URL config carries
# none: two sources would emit duplicate headers, which browsers reject.
if ! aws lambda get-function-url-config --function-name "$FUNCTION" \
       --region "$REGION" --profile "$PROFILE" >/dev/null 2>&1; then
  aws lambda create-function-url-config --function-name "$FUNCTION" \
    --auth-type NONE --region "$REGION" --profile "$PROFILE" \
    --query 'FunctionUrl' --output text
  aws lambda add-permission --function-name "$FUNCTION" \
    --statement-id FunctionURLAllowPublicAccess \
    --action lambda:InvokeFunctionUrl --principal '*' \
    --function-url-auth-type NONE --region "$REGION" --profile "$PROFILE" \
    --output text >/dev/null
fi
# CODE_SHA stamps each persisted run with the deployed commit. Without
# it a stored result says UNKNOWN, which is correct but useless; with a
# GUESSED value it would be provenance-shaped and wrong.
SHA="$(cd "$REPO" && git rev-parse --short HEAD)"
aws lambda update-function-configuration --function-name "$FUNCTION" \
  --environment "Variables={CODE_SHA=$SHA,JOURNAL_TABLE=stock-agent-dev-journal}" \
  --region "$REGION" --profile "$PROFILE" --query 'LastModified' --output text
echo "stamped CODE_SHA=$SHA"
aws lambda get-function-url-config --function-name "$FUNCTION" \
  --region "$REGION" --profile "$PROFILE" --query 'FunctionUrl' --output text

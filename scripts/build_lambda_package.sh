#!/usr/bin/env bash
#
# Deterministic Lambda package builder.
#
# Single source of truth for how a deployment artifact is assembled, so the
# GitHub Actions workflow and local deploys cannot drift apart.
#
# Guarantees:
#   * builds in a fresh temp dir (stale files can never leak in)
#   * ships every required source module, not just the entry point
#   * excludes backup/superseded handlers and __pycache__
#   * verifies the artifact imports before it is allowed to ship
#
# Usage: build_lambda_package.sh <service-dir> <output-zip>
#   e.g. build_lambda_package.sh lambda-micro/chatbot-router dist/chatbot.zip

set -euo pipefail

SERVICE_DIR="${1:?usage: build_lambda_package.sh <service-dir> <output-zip>}"
OUTPUT_ZIP="${2:?usage: build_lambda_package.sh <service-dir> <output-zip>}"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

if [[ ! -d "$SERVICE_DIR" ]]; then
  echo "ERROR: service dir not found: $SERVICE_DIR" >&2
  exit 1
fi

# Entry module every service shares.
ENTRY_MODULE="handler.py"

# Extra modules imported at runtime, per service. Keep in sync with imports;
# the import check below fails the build if anything is missed.
#
# REQUIRED_PACKAGES are shared package directories copied from the repo
# root. The flat *.py glob cannot reach a package, so a service importing
# one must declare it here or the artifact will not import.
REQUIRED_PACKAGES=()
EXTRA_FILES=()
case "$SERVICE_DIR" in
  *chatbot-router) REQUIRED_MODULES=("$ENTRY_MODULE" "ml_agent_lite.py") ;;
  *agent-api)      REQUIRED_MODULES=("$ENTRY_MODULE")
                   # agent-api now runs the canonical engine in
                   # agent/signals and no longer needs ml_agent_lite.
                   # Shipping it anyway would put a second, unreachable
                   # copy of the maths in the artifact - the kind of
                   # thing that later gets edited and appears to work.
                   REQUIRED_PACKAGES=("agent") ;;
  *agent-scanner)  REQUIRED_MODULES=("$ENTRY_MODULE")
                   REQUIRED_PACKAGES=("agent") ;;
  *)               REQUIRED_MODULES=("$ENTRY_MODULE") ;;
esac

BUILD_DIR="$(mktemp -d "${TMPDIR:-/tmp}/lambda-build.XXXXXX")"
VERIFY_DIR="$(mktemp -d "${TMPDIR:-/tmp}/lambda-verify.XXXXXX")"
trap 'rm -rf "$BUILD_DIR" "$VERIFY_DIR"' EXIT

echo "==> Building $SERVICE_DIR -> $OUTPUT_ZIP"
echo "    build dir: $BUILD_DIR (fresh)"

# --- 1. dependencies -----------------------------------------------------
if [[ -f "$SERVICE_DIR/requirements.txt" ]]; then
  echo "==> Installing dependencies"
  python3 -m pip install -r "$SERVICE_DIR/requirements.txt" -t "$BUILD_DIR" --quiet
else
  echo "==> No requirements.txt (runtime-provided deps only)"
fi

# --- 2. source modules ---------------------------------------------------
echo "==> Copying source modules"
copied=0
for src in "$SERVICE_DIR"/*.py; do
  [[ -e "$src" ]] || continue
  base="$(basename "$src")"
  # Skip superseded/backup handlers - they must never ship.
  case "$base" in
    handler-*.py|*-backup.py|*-old.py) echo "    skip (backup): $base"; continue ;;
  esac
  cp "$src" "$BUILD_DIR/$base"
  echo "    add: $base"
  copied=$((copied + 1))
done

if [[ "$copied" -eq 0 ]]; then
  echo "ERROR: no source modules copied from $SERVICE_DIR" >&2
  exit 1
fi

# --- 2a. extra single files from elsewhere in the repo -------------------
for extra in "${EXTRA_FILES[@]+"${EXTRA_FILES[@]}"}"; do
  if [[ ! -f "$extra" ]]; then
    echo "ERROR: declared extra file not found: $extra" >&2
    exit 1
  fi
  echo "    add extra: $extra"
  cp "$extra" "$BUILD_DIR/$(basename "$extra")"
done

# --- 2b. shared packages ------------------------------------------------
for pkg in "${REQUIRED_PACKAGES[@]+"${REQUIRED_PACKAGES[@]}"}"; do
  if [[ ! -d "$pkg" ]]; then
    echo "ERROR: declared package not found at repo root: $pkg" >&2
    exit 1
  fi
  if [[ ! -f "$pkg/__init__.py" ]]; then
    echo "ERROR: $pkg is not an importable package (no __init__.py)" >&2
    exit 1
  fi
  echo "    add package: $pkg/"
  # -L so a symlinked package is materialised rather than shipped as a
  # dangling link that only resolves on this machine.
  cp -RL "$pkg" "$BUILD_DIR/$pkg"
  find "$BUILD_DIR/$pkg" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
  find "$BUILD_DIR/$pkg" -name '*.pyc' -delete 2>/dev/null || true
  # macOS sprinkles these through any directory Finder has touched.
  find "$BUILD_DIR/$pkg" -name '.DS_Store' -delete 2>/dev/null || true
done

find "$BUILD_DIR" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
find "$BUILD_DIR" -name '*.pyc' -delete 2>/dev/null || true

# --- 3. zip --------------------------------------------------------------
mkdir -p "$(dirname "$OUTPUT_ZIP")"
ABS_ZIP="$(cd "$(dirname "$OUTPUT_ZIP")" && pwd)/$(basename "$OUTPUT_ZIP")"
rm -f "$ABS_ZIP"          # never append to a previous artifact
( cd "$BUILD_DIR" && zip -qr "$ABS_ZIP" . )

# --- 4. verify contents --------------------------------------------------
echo "==> Verifying artifact contents"
# List once into a variable: piping into `grep -q` would SIGPIPE `unzip` and,
# with pipefail, make a present file look missing.
ZIP_ENTRIES="$(unzip -Z1 "$ABS_ZIP")"

missing=0
for pkg in "${REQUIRED_PACKAGES[@]+"${REQUIRED_PACKAGES[@]}"}"; do
  if printf '%s\n' "$ZIP_ENTRIES" | /usr/bin/grep -q "^$pkg/__init__.py$"; then
    count=$(printf '%s\n' "$ZIP_ENTRIES" | /usr/bin/grep -c "^$pkg/.*\.py$" || true)
    echo "    OK      $pkg/ ($count modules)"
  else
    echo "    MISSING $pkg/__init__.py" >&2
    missing=$((missing + 1))
  fi
done

for module in "${REQUIRED_MODULES[@]}"; do
  if printf '%s\n' "$ZIP_ENTRIES" | /usr/bin/grep -Fxq -- "$module"; then
    echo "    OK      $module"
  else
    echo "    MISSING $module" >&2
    missing=$((missing + 1))
  fi
done

if [[ "$missing" -gt 0 ]]; then
  echo "ERROR: $missing required module(s) absent from $OUTPUT_ZIP - refusing to deploy" >&2
  exit 1
fi

# --- 5. verify it actually imports --------------------------------------
# Catches *any* missing module, not only the ones listed above.
#
# This MUST run from inside the extracted artifact. Python puts the
# working directory on sys.path, so running it from the repo root let
# `import agent` resolve out of the repo instead of out of the zip - the
# check passed on an artifact with the package missing entirely, which is
# a guard that cannot fail. Running with cwd = the extraction directory
# means the only importable source is the artifact itself.
echo "==> Verifying artifact imports"
unzip -q "$ABS_ZIP" -d "$VERIFY_DIR"
if ( cd "$VERIFY_DIR" && python3 - <<'PY'
import importlib, os, sys

# Drop anything that is not the extracted artifact or the stdlib, so a
# module present on this machine cannot stand in for one the artifact
# should have shipped.
here = os.getcwd()
sys.path = [p for p in sys.path
            if p in ("", here) or "site-packages" in p or "lib/python" in p]

try:
    m = importlib.import_module("handler")
except Exception as e:
    print(f"    IMPORT FAILED: {type(e).__name__}: {e}", file=sys.stderr)
    sys.exit(1)
if not hasattr(m, "lambda_handler"):
    print("    handler.lambda_handler not found", file=sys.stderr)
    sys.exit(1)

# Confirm each first-party module really came from the artifact.
for name, mod in list(sys.modules.items()):
    origin = getattr(getattr(mod, "__spec__", None), "origin", None) or ""
    # Skip the interpreter's own modules and anything installed: only
    # first-party source is expected to come from the artifact.
    if origin in ("", "built-in", "frozen"):
        continue
    if "site-packages" in origin or "lib/python" in origin \
            or "lib-dynload" in origin:
        continue
    if origin.startswith(here):
        continue
    print(f"    IMPORT LEAKED: {name} loaded from {origin}, not the artifact",
          file=sys.stderr)
    sys.exit(1)

print("    OK      handler imports and exposes lambda_handler")
PY
) then
  :
else
  echo "ERROR: artifact does not import cleanly - refusing to deploy" >&2
  exit 1
fi

echo "==> Built $OUTPUT_ZIP ($(du -h "$ABS_ZIP" | cut -f1))"

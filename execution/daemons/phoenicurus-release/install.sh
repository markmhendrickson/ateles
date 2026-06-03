#!/usr/bin/env bash
# Phoenicurus-Release publish.py — environment validator.
#
# publish.py is invoked ON DEMAND by Onychomys after operator approval, so it
# does not register a scheduled launchd agent. This script verifies the host has
# everything publish.py needs and prints the invocation to wire into Onychomys.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$HOME/.config/neotoma/.env"

echo "Phoenicurus-Release publish.py — preflight"
echo "=========================================="

fail=0

check_cmd() {
  if command -v "$1" >/dev/null 2>&1; then
    echo "  ✓ $1 ($(command -v "$1"))"
  else
    echo "  ✗ $1 NOT FOUND"
    fail=1
  fi
}

echo "Required CLIs:"
check_cmd node
check_cmd npm
check_cmd gh
check_cmd flyctl
check_cmd git

echo "Env file: $ENV_FILE"
if [ -f "$ENV_FILE" ]; then
  echo "  ✓ exists"
  for var in NPM_TOKEN NEOTOMA_BASE_URL TELEGRAM_BOT_TOKEN TELEGRAM_CHAT_ID; do
    if grep -q "^${var}=" "$ENV_FILE"; then
      echo "  ✓ $var present"
    else
      echo "  ✗ $var MISSING (add to $ENV_FILE)"
      [ "$var" = "NPM_TOKEN" ] && fail=1
    fi
  done
else
  echo "  ✗ $ENV_FILE missing"
  fail=1
fi

echo "Neotoma repo:"
NEOTOMA_REPO_ROOT="${NEOTOMA_REPO_ROOT:-$HOME/repos/neotoma}"
if [ -f "$NEOTOMA_REPO_ROOT/package.json" ]; then
  echo "  ✓ $NEOTOMA_REPO_ROOT"
else
  echo "  ✗ no package.json at $NEOTOMA_REPO_ROOT (set NEOTOMA_REPO_ROOT)"
  fail=1
fi

echo "gh auth:"
if gh auth status >/dev/null 2>&1; then echo "  ✓ authenticated"; else echo "  ✗ run 'gh auth login'"; fail=1; fi

echo "flyctl auth:"
if flyctl auth whoami >/dev/null 2>&1; then echo "  ✓ authenticated"; else echo "  ✗ run 'flyctl auth login'"; fail=1; fi

echo "------------------------------------------"
if [ "$fail" -eq 0 ]; then
  echo "✓ Environment OK. publish.py is ready."
else
  echo "✗ Fix the items above before running a real publish."
fi
echo
echo "Onychomys invokes on approval:"
echo "  python3 $SCRIPT_DIR/publish.py --version <vX.Y.Z>"
echo "Dry-run anytime:"
echo "  python3 $SCRIPT_DIR/publish.py --version <vX.Y.Z> --dry-run"

exit "$fail"

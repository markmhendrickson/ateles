#!/usr/bin/env bash
# Phoenicurus-Release publish.py — environment validator.
#
# publish.py is invoked ON DEMAND by Ateles after operator approval, so it
# does not register a scheduled launchd agent. This script verifies the host has
# everything publish.py needs and prints the invocation to wire into Ateles.
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

echo "claude CLI (for prepare.py agent spawn):"
check_cmd claude

echo "------------------------------------------"
if [ "$fail" -eq 0 ]; then
  echo "✓ Environment OK. publish.py and prepare.py are ready."
else
  echo "✗ Fix the items above before running a real publish."
fi

# ---------------------------------------------------------------------------
# Install the scheduled prepare launchd agent (Mon-Thu). publish.py stays
# on-demand and is NOT scheduled.
# ---------------------------------------------------------------------------
if [ "${1:-}" = "--load-prepare" ]; then
  PLIST="com.ateles.phoenicurus-prepare.plist"
  DEST="$HOME/Library/LaunchAgents/$PLIST"
  # The live .plist is gitignored (repo convention); render it from the tracked
  # .tmpl if it isn't already present locally.
  if [ ! -f "$SCRIPT_DIR/$PLIST" ] && [ -f "$SCRIPT_DIR/$PLIST.tmpl" ]; then
    cp "$SCRIPT_DIR/$PLIST.tmpl" "$SCRIPT_DIR/$PLIST"
    echo "Rendered $PLIST from template."
  fi
  mkdir -p "$HOME/Library/LaunchAgents"
  if launchctl list 2>/dev/null | grep -q "com.ateles.phoenicurus-prepare"; then
    echo "Unloading existing phoenicurus-prepare agent..."
    launchctl unload "$DEST" 2>/dev/null || true
  fi
  cp "$SCRIPT_DIR/$PLIST" "$DEST"
  launchctl load "$DEST"
  echo "✓ phoenicurus-prepare scheduled (Mon-Thu 07:00 local)."
else
  echo
  echo "To schedule the Mon-Thu prepare run:  bash install.sh --load-prepare"
fi

echo
echo "prepare.py (scheduled, or run manually):"
echo "  python3 $SCRIPT_DIR/prepare.py            # normal run"
echo "  python3 $SCRIPT_DIR/prepare.py --dry-run  # preflight only, no agent spawn"
echo "Ateles invokes publish.py on approval:"
echo "  python3 $SCRIPT_DIR/publish.py --version <vX.Y.Z>"
echo "Dry-run a publish anytime:"
echo "  python3 $SCRIPT_DIR/publish.py --version <vX.Y.Z> --dry-run"

exit "$fail"

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
# Install the scheduled prepare launchd agents. publish.py stays on-demand and
# is NOT scheduled.
#
# THREE agents, not one — spawning the prepare agent is only the first third of
# the job:
#   com.ateles.phoenicurus-prepare        Mon-Thu 07:00   spawn the prepare agent
#   com.ateles.phoenicurus-prepare-check  every 15 min    --check-agent-outcome
#   com.ateles.phoenicurus-prepare-retry  every 15 min    --retry-if-due
# The check agent is what turns a silently-dead prepare agent into a notification
# (and writes the stamp-on-success lock); the retry agent re-runs a prepare that
# a Claude usage limit deferred. Both are cheap no-ops when idle.
# ---------------------------------------------------------------------------
install_agent() {
  local plist="$1" label="$2" desc="$3"
  local dest="$HOME/Library/LaunchAgents/$plist"
  # The live .plist is gitignored (repo convention); render it from the tracked
  # .tmpl if it isn't already present locally.
  if [ ! -f "$SCRIPT_DIR/$plist" ] && [ -f "$SCRIPT_DIR/$plist.tmpl" ]; then
    cp "$SCRIPT_DIR/$plist.tmpl" "$SCRIPT_DIR/$plist"
    echo "Rendered $plist from template."
  fi
  mkdir -p "$HOME/Library/LaunchAgents"
  if launchctl list 2>/dev/null | grep -q "$label"; then
    echo "Unloading existing $label agent..."
    launchctl unload "$dest" 2>/dev/null || true
  fi
  cp "$SCRIPT_DIR/$plist" "$dest"
  launchctl load "$dest"
  echo "✓ $label $desc"
}

if [ "${1:-}" = "--load-prepare" ]; then
  install_agent "com.ateles.phoenicurus-prepare.plist" \
    "com.ateles.phoenicurus-prepare" "scheduled (Mon-Thu 07:00 local)."
  install_agent "com.ateles.phoenicurus-prepare-check.plist" \
    "com.ateles.phoenicurus-prepare-check" \
    "scheduled (--check-agent-outcome every 15 min)."
  install_agent "com.ateles.phoenicurus-prepare-retry.plist" \
    "com.ateles.phoenicurus-prepare-retry" \
    "scheduled (--retry-if-due every 15 min)."
else
  echo
  echo "To schedule the prepare run + its outcome-check and retry companions:"
  echo "  bash install.sh --load-prepare"
fi

echo
echo "prepare.py (scheduled, or run manually):"
echo "  python3 $SCRIPT_DIR/prepare.py            # normal run"
echo "  python3 $SCRIPT_DIR/prepare.py --dry-run  # preflight only, no agent spawn"
echo "  python3 $SCRIPT_DIR/prepare.py --check-agent-outcome  # reconcile last spawn"
echo "  python3 $SCRIPT_DIR/prepare.py --retry-if-due         # usage-limit retry"
echo "Ateles invokes publish.py on approval:"
echo "  python3 $SCRIPT_DIR/publish.py --version <vX.Y.Z>"
echo "Dry-run a publish anytime:"
echo "  python3 $SCRIPT_DIR/publish.py --version <vX.Y.Z> --dry-run"

exit "$fail"

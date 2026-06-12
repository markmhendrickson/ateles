#!/usr/bin/env bash
#
# redeploy_daemons_from_main.sh — "rolling main = RC" auto-deploy for the
# Ateles T3 daemons (Apis, Formica, neotoma-agent).
#
# Brings the running daemons up to the latest origin/main by:
#   1. fast-forwarding the deploy checkout (this repo, ~/ateles-rc-src) to
#      origin/main — refuses to deploy on divergence (non-fast-forward),
#   2. refreshing the deploy venv from execution/scripts/daemon-requirements.txt
#      only when that pin file changed (cheap no-op otherwise),
#   3. HARD-restarting each daemon LaunchAgent (kickstart -k) so the long-lived
#      Python process re-imports fresh modules from the updated source.
#
# Why a dedicated deploy checkout: daemons must NOT load code from the dev
# checkout (~/repos/ateles), whose branch/working-tree churns during
# development. This mirrors Neotoma's ~/neotoma-rc-src + rc-autodeploy pattern:
# the running swarm always reflects a clean, fast-forwarded origin/main.
#
# Idempotent: if the deploy checkout is already at origin/main, it exits early
# without touching the daemons. Intended to be invoked by the
# com.ateles.rc-autodeploy LaunchAgent on a StartInterval (poll), and safe to
# run by hand.
#
# Exit codes: 0 = up-to-date or successfully deployed; non-zero = error (the
# daemons are left running on their prior build; nothing destructive on failure).

# Note: -e is deliberately omitted. Each fallible step is checked explicitly so
# the script can fail SOFT (leave the daemons on their prior build) rather than
# aborting mid-deploy; -u and pipefail still catch unset vars and pipe failures.
set -uo pipefail

RC_DIR="${RC_DIR:-$HOME/ateles-rc-src}"
DAEMON_LABELS=(
  "com.ateles.apis"
  "com.ateles.formica"
  "com.ateles.neotoma-agent"
)
BRANCH="${BRANCH:-main}"
LOCK_DIR="${LOCK_DIR:-/tmp/ateles-rc-autodeploy.lock.d}"
REQ_FILE="execution/scripts/daemon-requirements.txt"

ts() { date '+%Y-%m-%d %H:%M:%S'; }
log() { echo "[$(ts)] $*"; }

# Single-flight via atomic mkdir (portable; flock is unavailable on macOS).
# A stale lock older than 30 min is reclaimed so a crashed run cannot wedge the
# loop forever.
if [ -d "$LOCK_DIR" ]; then
  if [ -n "$(find "$LOCK_DIR" -prune -mmin +30 2>/dev/null)" ]; then
    log "reclaiming stale lock (>30m old)"
    rmdir "$LOCK_DIR" 2>/dev/null || true
  fi
fi
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  log "another redeploy is in progress; skipping."
  exit 0
fi
trap 'rmdir "$LOCK_DIR" 2>/dev/null || true' EXIT

cd "$RC_DIR" || { log "ERROR: RC_DIR $RC_DIR missing"; exit 1; }

git fetch origin "$BRANCH" --quiet || { log "ERROR: git fetch failed"; exit 1; }

LOCAL="$(git rev-parse HEAD)"
REMOTE="$(git rev-parse "origin/$BRANCH")"

if [ "$LOCAL" = "$REMOTE" ]; then
  log "deploy checkout already at origin/$BRANCH ($(git rev-parse --short HEAD)); nothing to do."
  exit 0
fi

# Refuse to deploy if HEAD has diverged from origin/main (not a fast-forward).
# The deploy checkout must only ever fast-forward; a divergence means someone
# committed here and a human must reconcile.
if ! git merge-base --is-ancestor "$LOCAL" "$REMOTE"; then
  log "ERROR: deploy HEAD has diverged from origin/$BRANCH (not a fast-forward). Aborting; needs manual reconcile."
  exit 1
fi

# Detect whether the dependency pin changed across this update (so we only
# reinstall when needed).
REQ_CHANGED=0
if [ -f "$REQ_FILE" ]; then
  if ! git diff --quiet "$LOCAL" "$REMOTE" -- "$REQ_FILE"; then
    REQ_CHANGED=1
  fi
fi

log "Updating deploy $(git rev-parse --short HEAD) -> $(git rev-parse --short "origin/$BRANCH")"

if ! git merge --ff-only "origin/$BRANCH" --quiet; then
  log "ERROR: fast-forward merge failed"
  exit 1
fi

log "deploy now at $(git rev-parse --short HEAD)"

# Refresh the venv only when the pin file changed (or no venv yet).
if [ ! -x ".venv/bin/python3" ]; then
  log "no venv found — creating .venv"
  python3 -m venv .venv || { log "ERROR: venv create failed"; exit 1; }
  REQ_CHANGED=1
fi
if [ "$REQ_CHANGED" -eq 1 ] && [ -f "$REQ_FILE" ]; then
  log "daemon requirements changed — refreshing venv"
  .venv/bin/pip install --quiet --upgrade pip >/dev/null 2>&1 || true
  if ! .venv/bin/pip install --quiet -r "$REQ_FILE"; then
    log "ERROR: pip install failed; daemons left on prior build."
    exit 1
  fi
fi

# HARD restart each daemon: kickstart -k kills the running instance and
# relaunches it, forcing a fresh module import from the updated source.
rc=0
for label in "${DAEMON_LABELS[@]}"; do
  log "hard-restarting $label…"
  if ! launchctl kickstart -k "gui/$(id -u)/$label" 2>/dev/null; then
    if ! launchctl kickstart -k "user/$(id -u)/$label" 2>/dev/null; then
      log "WARNING: failed to kickstart $label (not loaded?); continuing"
      rc=1
    fi
  fi
done

if [ "$rc" -eq 0 ]; then
  log "DEPLOYED: all daemons restarted at $(git rev-parse --short HEAD)."
else
  log "DEPLOYED with warnings: some daemons could not be restarted (see above)."
fi
exit 0

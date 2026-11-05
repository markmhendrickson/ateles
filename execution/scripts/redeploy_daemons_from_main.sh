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
# Resolved before the cd below, so a relative $0 still finds the helper.
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

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
# The Fly admission holder (see below), if one is running. Released on every
# exit path, so a deferred or failed run never leaves admission blocked.
HOLDER_PID=""
HOLDER_DIR=""
release_admission() {
  if [ -n "$HOLDER_PID" ]; then
    kill "$HOLDER_PID" 2>/dev/null || true
    wait "$HOLDER_PID" 2>/dev/null || true
    HOLDER_PID=""
  fi
  if [ -n "$HOLDER_DIR" ]; then
    rm -rf "$HOLDER_DIR" 2>/dev/null || true
    HOLDER_DIR=""
  fi
}
trap 'release_admission; rmdir "$LOCK_DIR" 2>/dev/null || true' EXIT

cd "$RC_DIR" || { log "ERROR: RC_DIR $RC_DIR missing"; exit 1; }

git fetch origin "$BRANCH" --quiet || { log "ERROR: git fetch failed"; exit 1; }

LOCAL="$(git rev-parse HEAD)"
REMOTE="$(git rev-parse "origin/$BRANCH")"

if [ "$LOCAL" = "$REMOTE" ]; then
  log "deploy checkout already at origin/$BRANCH ($(git rev-parse --short HEAD)); nothing to do."
  exit 0
fi

# Defer while a Fly job is in flight. Apis awaits each Fly job it starts, so a
# restart below would orphan the job (nothing left to read its result; the task
# stays `executing`). The helper takes the Fly runner's own admission lock
# (the cross-process flock every reservation is made under), counts live
# reservations while holding it, and keeps holding it until the restarts below
# finish, so no new job can be reserved between this check and the restart;
# launches that try meanwhile simply wait. The lock dies with the holder, which
# also exits if this script dies, and after 15 minutes at most.
#   busy  -> the lock is held (admission or the reaper at work): defer.
#   ok N  -> N live reservations: defer if N > 0, else deploy holding the lock.
#   anything else (the holder could not start or answer): fail OPEN and deploy,
#   so a broken check still cannot stop fixes from deploying.
# Checked before the checkout is fast-forwarded, so a deferred run leaves the
# checkout behind and the next scheduled run (StartInterval) retries.
HOLDER_REPLY=""
if HOLDER_DIR="$(mktemp -d "${TMPDIR:-/tmp}/ateles-fly-hold.XXXXXX" 2>/dev/null)" \
   && mkfifo "$HOLDER_DIR/reply" 2>/dev/null; then
  python3 "$SCRIPT_DIR/fly_jobs_in_flight.py" --hold "$$" \
    >"$HOLDER_DIR/reply" 2>/dev/null </dev/null &
  HOLDER_PID=$!
  # The background child opens the fifo for writing before it execs python, so
  # this open always pairs; read -t bounds the wait for the answer.
  read -r -t 90 HOLDER_REPLY <"$HOLDER_DIR/reply" || HOLDER_REPLY=""
fi
case "$HOLDER_REPLY" in
  busy)
    log "DEFERRED: Fly admission lock busy; leaving deploy checkout and daemons unchanged until the next run."
    exit 0 ;;
  "ok 0")
    : ;;  # nothing in flight; keep the lock through the restarts
  ok\ [1-9]*)
    log "DEFERRED: ${HOLDER_REPLY#ok } Fly job(s) in flight; leaving deploy checkout and daemons unchanged until the next run."
    exit 0 ;;
  *)
    log "WARNING: Fly in-flight check unavailable; deploying without it."
    release_admission ;;
esac

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
  log "hard-restarting ${label}…"
  if ! launchctl kickstart -k "gui/$(id -u)/$label" 2>/dev/null; then
    if ! launchctl kickstart -k "user/$(id -u)/$label" 2>/dev/null; then
      log "WARNING: failed to kickstart $label (not loaded?); continuing"
      rc=1
    fi
  fi
done

# Restarts done: let Fly admission resume.
release_admission

if [ "$rc" -eq 0 ]; then
  log "DEPLOYED: all daemons restarted at $(git rev-parse --short HEAD)."
else
  log "DEPLOYED with warnings: some daemons could not be restarted (see above)."
fi
exit 0

#!/usr/bin/env bash
# isolate_daemons_to_rc_src.sh — repoint Ateles daemons off the shared interactive
# checkout (~/repos/ateles) onto the stable RC checkout (~/ateles-rc-src).
#
# This is the operator fix path referenced by checkout-identity FATAL output
# (ateles#515 / docs/daemon_rc_autodeploy.md). HOST-SIDE, operator-run.
#
# For the five deploy-bound daemons (cotinga, cyphorhinus, piculet, sylvia,
# phoenicurus-prepare) --apply also:
#   1. inventories co-located daemon-local state in BOTH trees
#   2. snapshots hashed rollback copies
#   3. reconciles each supported format losslessly (refuse on unresolved)
#   4. verifies reconciliation before any plist rewrite
#   5. reloads ONLY those five labels
#   6. proves new PID + executable/script under ~/ateles-rc-src
#   7. fails closed and rolls state back if reconciliation or read-back fails
#
# The shared interactive checkout is left untouched (no restore/cleanup) until
# all five pass read-back. XDG relocation is out of scope.
#
# Safe by construction: backs up each plist, rewrites the path in a copy,
# validates with plutil, and reloads one daemon at a time. Dry-run by default.
#
# Usage:
#   bash execution/scripts/isolate_daemons_to_rc_src.sh            # dry run
#   bash execution/scripts/isolate_daemons_to_rc_src.sh --apply    # reconcile + rewrite + reload
set -euo pipefail

SHARED="${ATELES_SHARED_CHECKOUT:-$HOME/repos/ateles}"
RC="${ATELES_REPO_PATH:-$HOME/ateles-rc-src}"
LA="$HOME/Library/LaunchAgents"
APPLY="${1:-}"
STAMP="$(date +%Y%m%d-%H%M%S 2>/dev/null || echo manual)"
BACKUP="$HOME/.config/ateles/plist-backups/$STAMP"
STATE_BACKUP="$HOME/.config/ateles/daemon-state-backups/$STAMP"

# Repo-held script resolution: prefer RC copy (documented fix path), else this
# checkout's copy when run from a worktree before cutover.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_FOR_HELPER="$(cd "$SCRIPT_DIR/../.." && pwd)"
if [ -f "$RC/lib/daemon_runtime/cutover_state.py" ]; then
  HELPER_ROOT="$RC"
else
  HELPER_ROOT="$REPO_FOR_HELPER"
fi

CUTOVER_LABELS=(
  com.ateles.cotinga
  com.ateles.cyphorhinus
  com.ateles.piculet
  com.ateles.sylvia
  com.ateles.phoenicurus-prepare
)

if [ ! -d "$RC" ]; then echo "FATAL: $RC missing — create the RC checkout first."; exit 1; fi

is_cutover_label() {
  local label="$1"
  local x
  for x in "${CUTOVER_LABELS[@]}"; do
    [ "$x" = "$label" ] && return 0
  done
  return 1
}

snapshot_plists() {
  mkdir -p "$BACKUP"
  local label plist
  for label in "${CUTOVER_LABELS[@]}"; do
    plist="$LA/$label.plist"
    if [ -f "$plist" ] && [ -r "$plist" ]; then
      cp "$plist" "$BACKUP/$label.plist"
    else
      : > "$BACKUP/$label.absent"
    fi
  done
}

load_plist() {
  local plist="$1"
  if launchctl bootstrap "gui/$(id -u)" "$plist" 2>/dev/null; then
    return 0
  fi
  launchctl load "$plist" 2>/dev/null
}

unload_plist() {
  local label="$1"
  local plist="$2"
  launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || \
    launchctl unload "$plist" 2>/dev/null || true
}

rollback_cutover() {
  # Roll back state and every member of the five-daemon plist fleet. Continue
  # through individual failures so a broken member cannot prevent recovery of
  # the remaining four. Retain both backup directories for audit/retry.
  local failed=0 label plist saved
  echo "── restoring daemon-local state from $STATE_BACKUP" >&2
  if ! run_cutover_python restore "$SHARED" "$RC" "$STATE_BACKUP"; then
    echo "FATAL: daemon-local state rollback failed" >&2
    failed=1
  fi

  echo "── restoring and reloading the prior five-daemon plist fleet from $BACKUP" >&2
  for label in "${CUTOVER_LABELS[@]}"; do
    plist="$LA/$label.plist"
    saved="$BACKUP/$label.plist"
    unload_plist "$label" "$plist"
    if [ -f "$saved" ]; then
      if ! cp "$saved" "$plist"; then
        echo "FATAL: could not restore $label plist" >&2
        failed=1
        continue
      fi
      if ! plutil -lint "$plist" >/dev/null 2>&1; then
        echo "FATAL: restored $label plist is invalid" >&2
        failed=1
        continue
      fi
      if ! load_plist "$plist"; then
        echo "FATAL: could not reload prior $label configuration" >&2
        failed=1
      fi
    elif [ -f "$BACKUP/$label.absent" ]; then
      if ! rm -f "$plist"; then
        echo "FATAL: could not restore prior absence for $label plist" >&2
        failed=1
      fi
    else
      echo "FATAL: no pre-run plist snapshot for $label" >&2
      failed=1
    fi
  done
  return "$failed"
}

fail_and_rollback() {
  local message="$1"
  local code="$2"
  echo "FATAL: $message" >&2
  if ! rollback_cutover; then
    echo "FATAL: rollback was incomplete; retained backups require operator inspection" >&2
  fi
  exit "$code"
}

run_cutover_python() {
  # Inline driver so the bash script stays the operator-facing entrypoint.
  PYTHONPATH="$HELPER_ROOT/lib/daemon_runtime${PYTHONPATH:+:$PYTHONPATH}" \
    python3 - "$@" <<'PY'
import json, sys, time
from pathlib import Path

from cutover_state import (
    CUTOVER_DAEMONS,
    PLIST_LABELS,
    assert_all_readbacks,
    assert_reconciled,
    inventory_state_files,
    prove_daemon_cutover,
    reconcile_all,
    restore_from_backup,
    snapshot_inventory,
    launchctl_pid,
)

cmd = sys.argv[1]
shared = Path(sys.argv[2])
rc = Path(sys.argv[3])

if cmd == "inventory-dry":
    items = inventory_state_files(shared, rc)
    for i in items:
        flag = "identical" if i.identical else (
            "shared-only" if i.shared_exists and not i.rc_exists else (
            "rc-only" if i.rc_exists and not i.shared_exists else "divergent"
        ))
        if i.shared_exists or i.rc_exists:
            print(f"  state {i.ref.daemon}/{i.ref.rel_path}: {flag}")
    sys.exit(0)

if cmd == "snapshot-and-reconcile":
    backup = Path(sys.argv[4])
    backup.mkdir(parents=True, exist_ok=True)
    items = inventory_state_files(shared, rc)
    snapshot_inventory(items, backup)
    merges = reconcile_all(items)
    try:
        assert_reconciled(merges)
    except RuntimeError as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        restore_from_backup(backup, shared, rc)
        sys.exit(2)
    for m in merges:
        print(f"  merge {m.ref.daemon}/{m.ref.rel_path}: {m.status} ({m.detail})")
    # Write pre-PIDs for later read-back.
    pre = {d: launchctl_pid(PLIST_LABELS[d]) for d in CUTOVER_DAEMONS}
    (backup / "pre_pids.json").write_text(json.dumps(pre, indent=2) + "\n")
    sys.exit(0)

if cmd == "restore":
    backup = Path(sys.argv[4])
    restore_from_backup(backup, shared, rc)
    sys.exit(0)

if cmd == "readback":
    backup = Path(sys.argv[4])
    pre = json.loads((backup / "pre_pids.json").read_text())
    # Brief settle for KeepAlive relaunch.
    time.sleep(2)
    results = []
    for d in CUTOVER_DAEMONS:
        results.append(
            prove_daemon_cutover(
                d,
                pre_pid=pre.get(d),
                rc_root=rc,
                shared_root=shared,
            )
        )
        r = results[-1]
        status = "OK" if r.ok else "FAIL"
        print(f"  readback {d}: {status} pre={r.pre_pid} post={r.post_pid} {r.detail}")
    try:
        assert_all_readbacks(results)
    except RuntimeError as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        sys.exit(3)
    sys.exit(0)

print(f"unknown cutover cmd: {cmd}", file=sys.stderr)
sys.exit(99)
PY
}

# --- dry-run / apply prelude: always show state inventory for the five ---
echo "── daemon-local state inventory (shared=$SHARED  rc=$RC)"
run_cutover_python inventory-dry "$SHARED" "$RC" || true

if [ "$APPLY" = "--apply" ]; then
  echo "── snapshot prior five-daemon plist fleet (backup: $BACKUP)"
  snapshot_plists
  echo "── snapshot + lossless reconcile into RC (backup: $STATE_BACKUP)"
  run_cutover_python snapshot-and-reconcile "$SHARED" "$RC" "$STATE_BACKUP"
fi

shopt -s nullglob
changed=0
reloaded_cutover=0
for plist in "$LA"/com.ateles.*.plist; do
  nm=$(basename "$plist")
  label="${nm%.plist}"
  [ -f "$plist" ] && [ -r "$plist" ] || continue
  if ! grep -q "$SHARED/" "$plist" 2>/dev/null; then continue; fi

  # Under --apply, only touch the five cutover labels here; other shared-path
  # daemons are still reported in dry-run but must not be reloaded as part of
  # the #515 cutover gate (fleet gate: shared untouched until five pass).
  if [ "$APPLY" = "--apply" ] && ! is_cutover_label "$label"; then
    echo "── $nm references shared checkout (skipped this cutover — not one of the five)"
    continue
  fi

  changed=$((changed+1))
  echo "── $nm references shared checkout"
  if [ "$APPLY" = "--apply" ]; then
    if ! tmp=$(mktemp); then
      fail_and_rollback "could not create temporary plist for $label" 1
    fi
    if ! sed "s#$SHARED/#$RC/#g" "$plist" > "$tmp"; then
      rm -f "$tmp"
      fail_and_rollback "could not rewrite $label plist" 1
    fi
    if ! plutil -lint "$tmp" >/dev/null 2>&1; then
      echo "  ABORT: rewritten plist invalid, leaving $nm untouched"
      rm -f "$tmp"
      fail_and_rollback "plist rewrite failed for $label" 1
    fi
    unload_plist "$label" "$plist"
    if ! mv "$tmp" "$plist"; then
      rm -f "$tmp"
      fail_and_rollback "could not install rewritten $label plist" 1
    fi
    if ! load_plist "$plist"; then
      fail_and_rollback "could not reload $label from the release checkout" 1
    fi
    echo "  reloaded $label from RC (backup: $BACKUP/$nm)"
    reloaded_cutover=$((reloaded_cutover+1))
    sleep 1
  fi
done

if [ "$APPLY" != "--apply" ]; then
  echo
  echo "DRY RUN — $changed daemon plist(s) still on the shared checkout."
  echo "Re-run with --apply to: snapshot+reconcile state, repoint the five plists, reload, and prove PID/path read-back."
  exit 0
fi

echo "── post-reload PID + path read-back (must all be under $RC)"
if ! run_cutover_python readback "$SHARED" "$RC" "$STATE_BACKUP"; then
  fail_and_rollback "cutover read-back failed" 3
fi

echo
echo "Done — $reloaded_cutover cutover daemon(s) repointed to $RC."
echo "State backup: $STATE_BACKUP  Plist backup: $BACKUP"
echo "Shared checkout left untouched (no cleanup) — all five passed read-back."
echo "Verify: launchctl list | grep com.ateles; confirm no FATAL: wrong checkout in logs."

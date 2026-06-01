#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/../../.." && pwd)"
LOG_DIR="${ROOT_DIR}/tmp/agent_logs"
ENV_FILE="${SCRIPT_DIR}/.env.agent"

LABEL_CORE="com.openclaw.agent.core"
LABEL_WORKERS="com.openclaw.agent.workers"
LABEL_WATCHDOG="com.openclaw.agent.watchdog"

PLIST_CORE="${SCRIPT_DIR}/com_openclaw_agent_core.plist"
PLIST_WORKERS="${SCRIPT_DIR}/com_openclaw_agent_workers.plist"
PLIST_WATCHDOG="${SCRIPT_DIR}/com_openclaw_agent_watchdog.plist"
LAUNCH_AGENTS_DIR="${HOME}/Library/LaunchAgents"

mkdir -p "${LOG_DIR}"

if [[ -f "${ENV_FILE}" ]]; then
  # shellcheck disable=SC1090
  source "${ENV_FILE}"
fi

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "Missing required command: $1" >&2
    exit 1
  }
}

check_env_var() {
  local key="$1"
  if [[ -z "${!key:-}" ]]; then
    echo "Missing required env var: ${key}" >&2
    exit 1
  fi
}

launchctl_print_status() {
  local label="$1"
  if launchctl print "gui/${UID}/${label}" >/dev/null 2>&1; then
    echo "${label}: running"
  else
    echo "${label}: not loaded"
  fi
}

preflight() {
  need_cmd launchctl
  need_cmd curl
  need_cmd python3

  echo "== Host checks =="
  python3 - <<'PY'
import os
import shutil
import subprocess

disk = shutil.disk_usage("/")
avail_pct = 100 * disk.free / disk.total
print(f"Disk free: {avail_pct:.1f}%")
if avail_pct < 20:
    raise SystemExit("Disk free below 20% threshold.")
PY

  if [[ -n "${TRUTH_LAYER_HEALTHCHECK_URL:-}" ]]; then
    echo "Checking truth layer: ${TRUTH_LAYER_HEALTHCHECK_URL}"
    curl -fsS "${TRUTH_LAYER_HEALTHCHECK_URL}" >/dev/null
  fi

  if [[ -n "${OPENCLAW_HEALTHCHECK_URL:-}" ]]; then
    echo "Checking OpenClaw endpoint: ${OPENCLAW_HEALTHCHECK_URL}"
    curl -fsS "${OPENCLAW_HEALTHCHECK_URL}" >/dev/null
  fi

  echo "Preflight passed."
}

start_stack() {
  need_cmd launchctl
  launchctl bootstrap "gui/${UID}" "${PLIST_CORE}" 2>/dev/null || true
  launchctl kickstart -k "gui/${UID}/${LABEL_CORE}"
  if [[ -n "${OPENCLAW_AGENT_WORKERS_CMD:-}" ]]; then
    launchctl bootstrap "gui/${UID}" "${PLIST_WORKERS}" 2>/dev/null || true
    launchctl kickstart -k "gui/${UID}/${LABEL_WORKERS}"
  else
    echo "OPENCLAW_AGENT_WORKERS_CMD not set; skipping worker service startup."
  fi
  echo "Stack started."
}

stop_stack() {
  need_cmd launchctl
  if [[ -n "${OPENCLAW_AGENT_WORKERS_CMD:-}" ]]; then
    launchctl bootout "gui/${UID}/${LABEL_WORKERS}" 2>/dev/null || true
  fi
  launchctl bootout "gui/${UID}/${LABEL_CORE}" 2>/dev/null || true
  echo "Stack stopped."
}

restart_stack() {
  stop_stack
  start_stack
}

status_stack() {
  launchctl_print_status "${LABEL_CORE}"
  if [[ -n "${OPENCLAW_AGENT_WORKERS_CMD:-}" ]]; then
    launchctl_print_status "${LABEL_WORKERS}"
  else
    echo "${LABEL_WORKERS}: skipped (OPENCLAW_AGENT_WORKERS_CMD not set)"
  fi
  if [[ -f "${LAUNCH_AGENTS_DIR}/${LABEL_WATCHDOG}.plist" ]]; then
    launchctl_print_status "${LABEL_WATCHDOG}"
  else
    echo "${LABEL_WATCHDOG}: not installed (run install_watchdog)"
  fi
}

logs_stack() {
  local lines="${1:-100}"
  echo "== core =="
  if [[ -f "${LOG_DIR}/agent_core.log" ]]; then
    tail -n "${lines}" "${LOG_DIR}/agent_core.log"
  else
    echo "No core log file yet."
  fi
  echo
  echo "== workers =="
  if [[ -f "${LOG_DIR}/agent_workers.log" ]]; then
    tail -n "${lines}" "${LOG_DIR}/agent_workers.log"
  else
    echo "No workers log file yet."
  fi
  echo
  echo "== watchdog =="
  if [[ -f "${LOG_DIR}/watchdog.log" ]]; then
    tail -n "${lines}" "${LOG_DIR}/watchdog.log"
  else
    echo "No watchdog success log yet."
  fi
}

smoke() {
  check_env_var "OPENCLAW_HEALTHCHECK_URL"
  check_env_var "TRUTH_LAYER_HEALTHCHECK_URL"
  curl -fsS "${OPENCLAW_HEALTHCHECK_URL}" >/dev/null
  curl -fsS "${TRUTH_LAYER_HEALTHCHECK_URL}" >/dev/null
  echo "Smoke checks passed."
}

failover_to_cloud() {
  check_env_var "OPENCLAW_CLOUD_FAILOVER_HOOK"
  curl -fsS -X POST "${OPENCLAW_CLOUD_FAILOVER_HOOK}" >/dev/null
  echo "Cloud failover hook triggered."
}

failback_to_local() {
  check_env_var "OPENCLAW_LOCAL_FAILBACK_HOOK"
  curl -fsS -X POST "${OPENCLAW_LOCAL_FAILBACK_HOOK}" >/dev/null
  echo "Local failback hook triggered."
}

watchdog_run() {
  need_cmd bash
  exec "${SCRIPT_DIR}/watchdog_health.sh"
}

install_watchdog() {
  need_cmd launchctl
  mkdir -p "${LAUNCH_AGENTS_DIR}"
  cp "${PLIST_WATCHDOG}" "${LAUNCH_AGENTS_DIR}/${LABEL_WATCHDOG}.plist"
  launchctl bootout "gui/${UID}/${LABEL_WATCHDOG}" 2>/dev/null || true
  launchctl bootstrap "gui/${UID}" "${LAUNCH_AGENTS_DIR}/${LABEL_WATCHDOG}.plist"
  echo "Watchdog LaunchAgent installed and loaded (every 300s + RunAtLoad)."
}

uninstall_watchdog() {
  need_cmd launchctl
  launchctl bootout "gui/${UID}/${LABEL_WATCHDOG}" 2>/dev/null || true
  rm -f "${LAUNCH_AGENTS_DIR}/${LABEL_WATCHDOG}.plist"
  echo "Watchdog LaunchAgent removed."
}

usage() {
  cat <<'EOF'
Usage: ./agent_stack.sh <command>

Commands:
  preflight            Run host and endpoint checks
  start                Start launchd-managed agent stack
  stop                 Stop launchd-managed agent stack
  restart              Restart launchd-managed agent stack
  status               Show launchd status for stack services
  logs [lines]         Tail stack logs (default: 100)
  smoke                Run health endpoint smoke checks
  failover_to_cloud    Trigger cloud failover hook
  failback_to_local    Trigger local failback hook
  watchdog_run         Run health watchdog once (foreground)
  install_watchdog       Install periodic watchdog LaunchAgent (5 min)
  uninstall_watchdog   Remove watchdog LaunchAgent
EOF
}

main() {
  local cmd="${1:-}"
  case "${cmd}" in
    preflight) preflight ;;
    start) start_stack ;;
    stop) stop_stack ;;
    restart) restart_stack ;;
    status) status_stack ;;
    logs) logs_stack "${2:-100}" ;;
    smoke) smoke ;;
    failover_to_cloud) failover_to_cloud ;;
    failback_to_local) failback_to_local ;;
    watchdog_run) watchdog_run ;;
    install_watchdog) install_watchdog ;;
    uninstall_watchdog) uninstall_watchdog ;;
    *) usage; exit 1 ;;
  esac
}

main "$@"

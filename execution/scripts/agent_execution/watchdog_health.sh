#!/usr/bin/env bash
# Periodic health check for OpenClaw gateway; optional JSON webhook on failure.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/../../.." && pwd)"
LOG_DIR="${ROOT_DIR}/tmp/agent_logs"
ENV_FILE="${SCRIPT_DIR}/.env.agent"

mkdir -p "${LOG_DIR}"

if [[ -f "${ENV_FILE}" ]]; then
  # shellcheck disable=SC1090
  source "${ENV_FILE}"
fi

ts() {
  date -u +"%Y-%m-%dT%H:%M:%SZ"
}

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "$(ts) Missing required command: $1" >&2
    exit 2
  }
}

need_cmd curl
need_cmd python3

failures=()
check_one() {
  local name="$1"
  local url="$2"
  if [[ -z "${url}" ]]; then
    return 0
  fi
  if ! curl -fsS --max-time 20 "${url}" >/dev/null 2>&1; then
    failures+=("${name} ${url}")
  fi
}

check_one "openclaw" "${OPENCLAW_HEALTHCHECK_URL:-}"
check_one "truth_layer" "${TRUTH_LAYER_HEALTHCHECK_URL:-}"

if [[ ${#failures[@]} -eq 0 ]]; then
  echo "$(ts) watchdog ok" >>"${LOG_DIR}/watchdog.log"
  exit 0
fi

msg="$(ts) OpenClaw watchdog FAILED: ${failures[*]}"
echo "${msg}" | tee -a "${LOG_DIR}/watchdog.error.log" >&2

if [[ -n "${OPENCLAW_WATCHDOG_WEBHOOK_URL:-}" ]]; then
  export WATCHDOG_MSG="${msg}"
  payload="$(python3 -c "import json,os; print(json.dumps({'text': os.environ.get('WATCHDOG_MSG',''), 'source': 'openclaw-watchdog'}))")"
  curl -fsS --max-time 15 -X POST \
    -H "Content-Type: application/json" \
    -d "${payload}" \
    "${OPENCLAW_WATCHDOG_WEBHOOK_URL}" >>"${LOG_DIR}/watchdog.error.log" 2>&1 || true
  unset WATCHDOG_MSG
fi

exit 1

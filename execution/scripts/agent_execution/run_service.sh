#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${SCRIPT_DIR}/.env.agent"

if [[ -f "${ENV_FILE}" ]]; then
  # shellcheck disable=SC1090
  source "${ENV_FILE}"
fi

service_name="${1:-}"

if [[ -z "${service_name}" ]]; then
  echo "Usage: ./run_service.sh <agent_core|agent_workers>" >&2
  exit 1
fi

case "${service_name}" in
  agent_core)
    if [[ -z "${OPENCLAW_AGENT_CORE_CMD:-}" ]]; then
      echo "OPENCLAW_AGENT_CORE_CMD is not set in ${ENV_FILE}" >&2
      exit 1
    fi
    exec /bin/zsh -lc "${OPENCLAW_AGENT_CORE_CMD}"
    ;;
  agent_workers)
    if [[ -z "${OPENCLAW_AGENT_WORKERS_CMD:-}" ]]; then
      echo "OPENCLAW_AGENT_WORKERS_CMD is not set in ${ENV_FILE}" >&2
      exit 1
    fi
    exec /bin/zsh -lc "${OPENCLAW_AGENT_WORKERS_CMD}"
    ;;
  *)
    echo "Unknown service: ${service_name}" >&2
    exit 1
    ;;
esac

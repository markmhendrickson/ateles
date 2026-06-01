#!/usr/bin/env bash
# Run Formica (issue-processing daemon) under launchd with Neotoma HTTP API.
#
# Environment: **all variables** come from the ateles repository **`.env`**
# (`$ATELES_ROOT/.env`), loaded via **`execution/daemons/formica/load_ateles_repo_env.sh`**.
#
# Optional overrides only when **`FORMICA_ENV_FILE`** is set to an existing file
# (sourced after `.env`; use for machine-local secrets without editing the repo).
#
# Required after load (typically from `.env`):
#   NEOTOMA_BEARER_TOKEN
#
# Defaults applied after `.env`:
#   NEOTOMA_BASE_URL       If still unset, https://neotoma.markmhendrickson.com
#
# Cursor SDK: **`CURSOR_CLOUD_API_KEY`** in `.env` is mirrored to **`CURSOR_API_KEY`**
# when unset (see load_ateles_repo_env.sh).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEFAULT_ATELES="$(cd "$SCRIPT_DIR/../.." && pwd)"
ATELES_ROOT="${ATELES_ROOT:-$DEFAULT_ATELES}"
export FORMICA_DIR="$ATELES_ROOT/execution/daemons/formica"

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH:-}"

if [[ ! -d "$FORMICA_DIR" ]]; then
  echo "run_formica_launchd: Formica package missing: $FORMICA_DIR" >&2
  exit 1
fi

# shellcheck disable=SC1091
source "$FORMICA_DIR/load_ateles_repo_env.sh"

if [[ -n "${FORMICA_ENV_FILE:-}" && -f "${FORMICA_ENV_FILE}" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$FORMICA_ENV_FILE"
  set +a
fi

export NEOTOMA_BASE_URL="${NEOTOMA_BASE_URL:-https://neotoma.markmhendrickson.com}"
export NEOTOMA_BASE_URL="${NEOTOMA_BASE_URL%/}"

if [[ -z "${NEOTOMA_BEARER_TOKEN:-}" ]]; then
  echo "run_formica_launchd: NEOTOMA_BEARER_TOKEN unset after loading $ATELES_ROOT/.env" >&2
  exit 1
fi

exec bash "$FORMICA_DIR/start.sh"

#!/usr/bin/env bash
# Source the ateles repository .env into the current shell (all variables Formica needs).
#
# Prerequisite: export FORMICA_DIR to this package directory (…/execution/daemons/formica)
# before sourcing:
#   export FORMICA_DIR="$(cd "$(dirname "$0")" && pwd)"
#   source "$FORMICA_DIR/load_ateles_repo_env.sh"
#
# Also mirrors CURSOR_CLOUD_API_KEY → CURSOR_API_KEY when the latter is unset (ateles .env layout).

if [[ -z "${FORMICA_DIR:-}" || ! -d "$FORMICA_DIR" ]]; then
  echo "load_ateles_repo_env.sh: set FORMICA_DIR to the formica package root, then source this file." >&2
  exit 1
fi

export ATELES_ROOT="$(cd "$FORMICA_DIR/../../.." && pwd)"
_REPO_ENV="$ATELES_ROOT/.env"

if [[ ! -f "$_REPO_ENV" ]]; then
  echo "load_ateles_repo_env.sh: missing $_REPO_ENV" >&2
  exit 1
fi

set -a
# shellcheck disable=SC1091
source "$_REPO_ENV"
set +a

if [[ -z "${CURSOR_API_KEY:-}" && -n "${CURSOR_CLOUD_API_KEY:-}" ]]; then
  export CURSOR_API_KEY="$CURSOR_CLOUD_API_KEY"
fi

#!/usr/bin/env bash
# Verify ateles repo .env has Neotoma bearer, then install LaunchAgent for Formica.
#
# Formica loads **all** environment variables from **`$ATELES_ROOT/.env`** at runtime
# (`load_ateles_repo_env.sh` + `start.sh`). This script does **not** write
# `~/.config/ateles/formica.env` unless you add optional overrides via **`FORMICA_ENV_FILE`**.
#
# Run from anywhere:
#   bash /path/to/ateles/execution/scripts/install_formica_launchd_from_ateles_env.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ATELES_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
ENV_FILE="$ATELES_ROOT/.env"
FORMICA_DIR="$ATELES_ROOT/execution/daemons/formica"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "Missing $ENV_FILE" >&2
  exit 1
fi

set -a
# shellcheck disable=SC1091
source "$ENV_FILE"
set +a

if [[ -z "${NEOTOMA_BEARER_TOKEN:-}" ]]; then
  echo "NEOTOMA_BEARER_TOKEN is unset in $ENV_FILE" >&2
  exit 1
fi

if [[ ! -f "$FORMICA_DIR/load_ateles_repo_env.sh" ]]; then
  echo "Missing $FORMICA_DIR/load_ateles_repo_env.sh" >&2
  exit 1
fi

echo "Using Neotoma + all other keys from $ENV_FILE (Formica loads this file at start)."

exec bash "$SCRIPT_DIR/setup_formica_launchagent.sh"

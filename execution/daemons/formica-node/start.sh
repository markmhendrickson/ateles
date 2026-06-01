#!/usr/bin/env bash
set -euo pipefail
FORMICA_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$FORMICA_DIR"
export FORMICA_DIR
# shellcheck disable=SC1091
source "$FORMICA_DIR/load_ateles_repo_env.sh"
if [[ ! -d node_modules ]]; then
  npm install
fi
exec node src/daemon.mjs

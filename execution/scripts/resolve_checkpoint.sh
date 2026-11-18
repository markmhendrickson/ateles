#!/bin/bash
# Dependency- and environment-aware launcher for resolve_checkpoint.py.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MCP_DIR="$(cd "$SCRIPT_DIR/../mcp/ateles" && pwd)"
# Resolved from this script's absolute directory.
# shellcheck disable=SC1091
source "$MCP_DIR/ateles_mcp_bootstrap.sh"

help_only=false
for argument in "$@"; do
    case "$argument" in
        -h|--help) help_only=true ;;
    esac
done

# Help must work from a clean shell and must not touch credential files.
if [ "$help_only" = false ]; then
    ateles_load_neotoma_environment
    ateles_promote_neotoma_token
fi

# Same replay-denial store as the MCP launcher; resolve_checkpoint.py validates
# it before anything that can sign or submit a resolution.
ateles_export_checkpoint_denial_dir

PYTHON="$(ateles_select_mcp_python)"
exec "$PYTHON" "$SCRIPT_DIR/resolve_checkpoint.py" "$@"

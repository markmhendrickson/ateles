#!/bin/bash
# Shared environment and interpreter bootstrap for Ateles MCP consumers.
# Source this file; it does not execute a server or command itself.

ATELES_MCP_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ATELES_REPO_ROOT="$(cd "$ATELES_MCP_SCRIPT_DIR/../../.." && pwd)"

ateles_mcp_log() { echo "[ateles-mcp] $*" >&2; }

ateles_load_neotoma_environment() {
    local env_file line key value
    env_file="${NEOTOMA_ENV_FILE:-$HOME/.config/neotoma/.env}"
    if [ -f "$env_file" ]; then
        while IFS= read -r line || [ -n "$line" ]; do
            line="${line#"${line%%[![:space:]]*}"}"
            case "$line" in ''|'#'*) continue ;; *'='*) ;; *) continue ;; esac
            key="${line%%=*}"
            value="${line#*=}"
            key="${key%"${key##*[![:space:]]}"}"
            key="${key#"${key%%[![:space:]]*}"}"
            case "$key" in *[!A-Za-z0-9_]*|'') continue ;; esac
            value="${value#"${value%%[![:space:]]*}"}"
            value="${value%"${value##*[![:space:]]}"}"
            case "$value" in
                '"'*|"'"*) ;;
                *" #"*)
                    value="${value%% #*}"
                    value="${value%"${value##*[![:space:]]}"}"
                    ;;
            esac
            case "$value" in
                '"'*'"') value="${value:1:${#value}-2}" ;;
                "'"*"'") value="${value:1:${#value}-2}" ;;
            esac
            if [ -z "${!key-}" ]; then
                export "$key=$value"
            fi
        done < "$env_file"
    fi
    # Callers run under set -e: a false test above must not become this
    # function's status and silently stop the launch.
    return 0
}

ateles_neotoma_host_is_local() {
    local host second
    host="$(printf '%s' "${1:-}" | sed -E 's#^[a-zA-Z][a-zA-Z0-9+.-]*://##; s#^[^@/]*@##; s#[/?].*$##; s#:[0-9]+$##' | tr '[:upper:]' '[:lower:]')"
    [ -z "$host" ] && return 0
    case "$host" in
        localhost|127.0.0.1|0.0.0.0|::1|*.local|*.localhost) return 0 ;;
        10.*|192.168.*|169.254.*) return 0 ;;
        172.*)
            second="${host#172.}"
            second="${second%%.*}"
            case "$second" in ''|*[!0-9]*) return 1 ;; esac
            [ "$second" -ge 16 ] && [ "$second" -le 31 ] && return 0
            return 1
            ;;
    esac
    return 1
}

ateles_promote_neotoma_token() {
    local base_url promoted
    base_url="${NEOTOMA_BASE_URL:-https://neotoma.markmhendrickson.com}"
    if ! ateles_neotoma_host_is_local "$base_url"; then
        if [ -n "${NEOTOMA_BEARER_TOKEN_PROD:-}" ]; then
            promoted=${NEOTOMA_BEARER_TOKEN_PROD}
            export NEOTOMA_BEARER_TOKEN=${promoted}
            unset promoted
        else
            ateles_mcp_log "WARNING: NEOTOMA_BASE_URL=$base_url is remote but NEOTOMA_BEARER_TOKEN_PROD is unset — using local-scoped token, which will likely 401 against prod entity reads"
        fi
    fi
    if [ -z "${NEOTOMA_BEARER_TOKEN:-}" ]; then
        ateles_mcp_log "WARNING: NEOTOMA_BEARER_TOKEN is unset — tool calls will fail to authenticate"
    fi
    return 0
}

# Checkpoint approval is consumed inline by every process that can resolve a
# checkpoint (the MCP server and the operator CLI). Bind their replay-denial
# evidence to the same restart-persistent host directory as the launchd Apis
# daemon. Both Python entry points validate it (absolute, real directory, not a
# symlink, writable) before they can resolve anything; this only supplies the
# shared default so the two launchers cannot drift apart.
ATELES_DEFAULT_CHECKPOINT_DENIAL_DIR="/var/tmp/ateles/checkpoint-denials"

ateles_export_checkpoint_denial_dir() {
    APIS_CHECKPOINT_DENIAL_DIR="${APIS_CHECKPOINT_DENIAL_DIR:-$ATELES_DEFAULT_CHECKPOINT_DENIAL_DIR}"
    export APIS_CHECKPOINT_DENIAL_DIR
}

ateles_python_has_mcp_deps() {
    [ -x "$1" ] && "$1" -c 'import mcp, httpx, cryptography, jwt' >/dev/null 2>&1
}

ateles_select_mcp_python() {
    local mcp_venv candidate python
    mcp_venv="${ATELES_MCP_VENV:-$ATELES_REPO_ROOT/.mcp-venv}"
    python=""

    # The explicit Homebrew patterns keep a clean-shell --help useful on macOS
    # when the unversioned python lacks the MCP dependencies.
    for candidate in \
        "$mcp_venv/bin/python3" \
        "$(command -v python3 || true)" \
        /opt/homebrew/bin/python3.[0-9] \
        /opt/homebrew/bin/python3.[0-9][0-9] \
        /opt/homebrew/bin/python3 \
        /usr/local/bin/python3
    do
        if ateles_python_has_mcp_deps "$candidate"; then
            python="$candidate"
            break
        fi
    done

    if [ -z "$python" ]; then
        ateles_mcp_log "no interpreter with MCP and approval-signing dependencies found; bootstrapping $mcp_venv"
        if command -v uv >/dev/null 2>&1; then
            uv venv "$mcp_venv" >&2 \
                && VIRTUAL_ENV="$mcp_venv" uv pip install -r "$ATELES_MCP_SCRIPT_DIR/requirements.txt" >&2
        elif command -v python3 >/dev/null 2>&1; then
            python3 -m venv "$mcp_venv" >&2 \
                && "$mcp_venv/bin/python3" -m pip install --quiet --upgrade pip >&2 \
                && "$mcp_venv/bin/python3" -m pip install --quiet -r "$ATELES_MCP_SCRIPT_DIR/requirements.txt" >&2
        fi
        if ateles_python_has_mcp_deps "$mcp_venv/bin/python3"; then
            python="$mcp_venv/bin/python3"
        else
            ateles_mcp_log "ERROR: could not provision an interpreter with MCP and approval-signing dependencies."
            ateles_mcp_log "Fix: uv venv $mcp_venv && VIRTUAL_ENV=$mcp_venv uv pip install -r $ATELES_MCP_SCRIPT_DIR/requirements.txt"
            return 1
        fi
    fi

    printf '%s\n' "$python"
}

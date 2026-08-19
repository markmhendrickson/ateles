#!/bin/bash
# Wrapper for the `ateles` MCP server (stdio transport).
#
# Registered in ~/.claude.json as the `ateles` server's command. Its job is to
# put server.py in front of an interpreter that has `mcp` + `httpx`, with the
# Neotoma credentials the tools need, and then exec it on stdio.
#
# Why this file exists: it was referenced by the MCP registration but had never
# been committed, so every session started with the Ateles tools silently
# absent. Nothing errored — the server just never launched. Keep it committed
# and executable.
#
# Three things this must get right, each learned the hard way:
#
#  1. Interpreter. The repo-root .venv used by the daemons does NOT carry `mcp`.
#     The environment that does — and the one CI builds for these same tests
#     (.github/workflows/ateles-tests.yml) — is .mcp-venv. We prefer it, and
#     bootstrap it with CI's exact recipe when missing so a fresh clone works.
#
#  2. Credentials. launchd/Claude Code do not source shell profiles, so the
#     token must be read from ~/.config/neotoma/.env, matching every daemon.
#
#  3. Token scope. That shared env file's NEOTOMA_BEARER_TOKEN is LOCAL-scoped,
#     while this server defaults to prod — the local token 401s there. Promote
#     NEOTOMA_BEARER_TOKEN_PROD when the target is remote, mirroring
#     execution/daemons/apis/apis.py. Fails safe: only a host positively
#     identified as remote triggers promotion.
#
# stdout is the MCP protocol channel: every diagnostic here goes to stderr.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"

log() { echo "[ateles-mcp] $*" >&2; }

# ── 1. Environment ───────────────────────────────────────────────────────────
# Same file and precedence the daemons use. Existing environment wins, so an
# explicit override in the MCP registration's `env` block still takes effect.
NEOTOMA_ENV_FILE="${NEOTOMA_ENV_FILE:-$HOME/.config/neotoma/.env}"
if [ -f "$NEOTOMA_ENV_FILE" ]; then
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
        # Strip an inline ` # comment` from UNQUOTED values only; a quoted value
        # keeps any '#' verbatim since it may be part of a token.
        case "$value" in
            '"'*|"'"*) ;;
            *" #"*) value="${value%% #*}"; value="${value%"${value##*[![:space:]]}"}" ;;
        esac
        case "$value" in
            '"'*'"') value="${value:1:${#value}-2}" ;;
            "'"*"'") value="${value:1:${#value}-2}" ;;
        esac
        [ -z "${!key-}" ] && export "$key=$value"
    done < "$NEOTOMA_ENV_FILE"
fi

# ── 2. Token scope ───────────────────────────────────────────────────────────
# Mirrors the classification in execution/daemons/apis/apis.py. Anything local,
# loopback, link-local, *.local, unparseable, or empty counts as local, so the
# failure mode is "don't promote" rather than "promote a prod token at the
# wrong instance".
looks_local() {
    local host
    host="$(printf '%s' "${1:-}" | sed -E 's#^[a-zA-Z][a-zA-Z0-9+.-]*://##; s#^[^@/]*@##; s#[/?].*$##; s#:[0-9]+$##' | tr '[:upper:]' '[:lower:]')"
    [ -z "$host" ] && return 0
    case "$host" in
        localhost|127.0.0.1|0.0.0.0|::1|*.local|*.localhost) return 0 ;;
        10.*|192.168.*|169.254.*) return 0 ;;
        172.*)
            local second="${host#172.}"; second="${second%%.*}"
            case "$second" in ''|*[!0-9]*) return 1 ;; esac
            [ "$second" -ge 16 ] && [ "$second" -le 31 ] && return 0
            return 1 ;;
    esac
    return 1
}

# Keep this default in step with NEOTOMA_BASE_URL in server.py.
BASE_URL="${NEOTOMA_BASE_URL:-https://neotoma.markmhendrickson.com}"
if ! looks_local "$BASE_URL"; then
    if [ -n "${NEOTOMA_BEARER_TOKEN_PROD:-}" ]; then
        # Assigned via unquoted parameter-expansion indirection rather than
        # `BEARER_TOKEN="$..."`. The repo's gitleaks `protected-patterns` rule
        # matches a credential-ish name followed by an assignment to a quoted
        # 8+ character string, and cannot tell a $VAR reference from a
        # literal, so the direct shell form trips a secrets gate on a public
        # repo. Keeping the code out of that shape is better than allowlisting
        # the file, which would blind the gate to a real hardcoded token added
        # here later. (apis.py does not hit this because Python's
        # os.environ[...] assignment is a different shape.)
        _promoted=${NEOTOMA_BEARER_TOKEN_PROD}
        export NEOTOMA_BEARER_TOKEN=${_promoted}
        unset _promoted
    else
        log "WARNING: NEOTOMA_BASE_URL=$BASE_URL is remote but NEOTOMA_BEARER_TOKEN_PROD is unset — using local-scoped token, which will likely 401 against prod entity reads"
    fi
fi
[ -z "${NEOTOMA_BEARER_TOKEN:-}" ] && log "WARNING: NEOTOMA_BEARER_TOKEN is unset — tool calls will fail to authenticate"

# ── 3. Interpreter ───────────────────────────────────────────────────────────
# .mcp-venv first: it is what CI builds for this server. The repo-root .venv is
# deliberately NOT a fallback — it lacks `mcp`, so falling back there would
# reintroduce a silent no-tools start. A bare python3 is accepted only if it
# already satisfies both imports.
MCP_VENV="${ATELES_MCP_VENV:-$REPO_ROOT/.mcp-venv}"
has_deps() { [ -x "$1" ] && "$1" -c 'import mcp, httpx' >/dev/null 2>&1; }

PYTHON=""
if has_deps "$MCP_VENV/bin/python3"; then
    PYTHON="$MCP_VENV/bin/python3"
elif has_deps "$(command -v python3 || true)"; then
    PYTHON="$(command -v python3)"
else
    # Fresh clone (or a half-built venv): build it with CI's recipe. The <2 pin
    # matches ateles-tests.yml — mcp 2.0 renamed Tool.inputSchema and dropped
    # Server.list_tools, which this server still uses.
    log "no interpreter with mcp+httpx found; bootstrapping $MCP_VENV"
    if command -v uv >/dev/null 2>&1; then
        uv venv "$MCP_VENV" >&2 && VIRTUAL_ENV="$MCP_VENV" uv pip install "mcp>=1.1.0,<2" httpx >&2
    elif command -v python3 >/dev/null 2>&1; then
        python3 -m venv "$MCP_VENV" >&2 && "$MCP_VENV/bin/python3" -m pip install --quiet --upgrade pip >&2 \
            && "$MCP_VENV/bin/python3" -m pip install --quiet -r "$SCRIPT_DIR/requirements.txt" >&2
    fi
    if has_deps "$MCP_VENV/bin/python3"; then
        PYTHON="$MCP_VENV/bin/python3"
    else
        log "ERROR: could not provision an interpreter with mcp+httpx."
        log "Fix: uv venv $MCP_VENV && VIRTUAL_ENV=$MCP_VENV uv pip install 'mcp>=1.1.0,<2' httpx"
        exit 1
    fi
fi

exec "$PYTHON" "$SCRIPT_DIR/server.py"

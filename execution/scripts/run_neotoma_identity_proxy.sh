#!/bin/bash
# Launch the Cursor-facing MCP identity proxy pointed at Neotoma's HTTP /mcp
# endpoint. Invoked from the Cursor `mcp.json` command slot so Cursor talks
# stdio to this script and writes reach Neotoma with `clientInfo` (and
# eventually AAuth) attribution applied.
#
# See:
#   - ateles plan: /Users/markmhendrickson/.cursor/plans/cursor_mcp_proxy_4a68cb45.plan.md
#   - neotoma plan: /Users/markmhendrickson/.cursor/plans/proxy_identity_enhancements_4c5ecc8e.plan.md
#   - docs: mcp/IDENTITY_PROXY.md
#
# Env knobs (overridable by the caller or by ateles .env):
#   NEOTOMA_HTTP_URL               Full downstream URL (default http://localhost:3080/mcp)
#   MCP_PROXY_CLIENT_NAME          clientInfo.name (default cursor-neotoma-proxy)
#   MCP_PROXY_CLIENT_VERSION       clientInfo.version (default matches proxy default)
#   MCP_PROXY_AGENT_LABEL          Optional repo/env label appended to clientInfo.name
#   MCP_PROXY_BEARER_TOKEN         Forwarded as Authorization: Bearer
#   MCP_PROXY_CONNECTION_ID        Forwarded as X-Connection-Id
#   MCP_PROXY_SESSION_PREFLIGHT    "1" to hit /session on startup
#   MCP_PROXY_FAIL_CLOSED          "1" to abort on anonymous or unreachable preflight
#   MCP_PROXY_LOG_FILE             Diagnostics file (defaults to /tmp/mcp_identity_proxy.log)
#   MCP_PROXY_AUTOSTART_NEOTOMA    "1" to launch Neotoma HTTP if health check fails
#   MCP_PROXY_NEOTOMA_REPO         Local neotoma repo path (default $HOME/repos/neotoma)
#   MCP_PROXY_NEOTOMA_START_CMD    Optional override command used to start Neotoma HTTP
#   MCP_PROXY_NEOTOMA_HEALTH_URL   Optional override health URL (default derived <base>/session)
#   MCP_PROXY_NEOTOMA_START_TIMEOUT Seconds to wait for Neotoma health (default 30)
#   MCP_PROXY_NEOTOMA_LOG_FILE     Log file for launcher-started Neotoma
#   MCP_PROXY_AAUTH                "1" to AAuth-sign every downstream request
#   NEOTOMA_AAUTH_SUB              AAuth subject claim, e.g. cursor@markmhendrickson.com
#   NEOTOMA_AAUTH_ISS              AAuth issuer claim, e.g. https://markmhendrickson.com
#   NEOTOMA_AAUTH_KID              Optional kid override (default: JWK's kid)
#   NEOTOMA_AAUTH_PRIVATE_JWK_PATH Override path to the private JWK
#   NEOTOMA_AAUTH_TOKEN_TTL_SEC    aa-agent+jwt lifetime (default 300s)
#   NEOTOMA_AAUTH_AUTHORITY_OVERRIDE Force @authority canonicalization
#
# Compact MCP instructions (set on the Neotoma SERVER, not only this proxy):
#   NEOTOMA_MCP_COMPACT_INSTRUCTIONS=1  When the Cursor workspace loads
#   `.cursor/rules/neotoma_harness.mdc` and Neotoma MCP is enabled, set this
#   in ../neotoma/.env (or the process that starts Neotoma HTTP/MCP) so clients
#   receive a short instruction checklist instead of duplicating the full fenced
#   block. See execution/docs/neotoma_cursor_context.md.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

if [ -f "$PROJECT_ROOT/.env" ]; then
    set -a
    # shellcheck disable=SC1091
    source "$PROJECT_ROOT/.env"
    set +a
fi

# Align with common Neotoma env names so user-level Cursor MCP does not need
# duplicate bearer / URL variables in ~/.cursor/mcp.json.
if [ -z "${MCP_PROXY_BEARER_TOKEN:-}" ] && [ -n "${NEOTOMA_BEARER_TOKEN:-}" ]; then
    export MCP_PROXY_BEARER_TOKEN="$NEOTOMA_BEARER_TOKEN"
fi
if [ -z "${NEOTOMA_HTTP_URL:-}" ] && [ -z "${MCP_PROXY_DOWNSTREAM_URL:-}" ] && [ -n "${NEOTOMA_HOST_URL:-}" ]; then
    _nh_base="${NEOTOMA_HOST_URL%/}"
    export NEOTOMA_HTTP_URL="${_nh_base}/mcp"
fi

DOWNSTREAM_URL="${NEOTOMA_HTTP_URL:-${MCP_PROXY_DOWNSTREAM_URL:-http://localhost:3080/mcp}}"
CLIENT_NAME="${MCP_PROXY_CLIENT_NAME:-cursor-neotoma-proxy}"
CLIENT_VERSION="${MCP_PROXY_CLIENT_VERSION:-0.1.0}"
AGENT_LABEL="${MCP_PROXY_AGENT_LABEL:-}"
BEARER_TOKEN="${MCP_PROXY_BEARER_TOKEN:-}"
CONNECTION_ID="${MCP_PROXY_CONNECTION_ID:-}"
SESSION_PREFLIGHT="${MCP_PROXY_SESSION_PREFLIGHT:-}"
FAIL_CLOSED="${MCP_PROXY_FAIL_CLOSED:-}"
LOG_FILE="${MCP_PROXY_LOG_FILE:-/tmp/mcp_identity_proxy.log}"
AUTOSTART_NEOTOMA="${MCP_PROXY_AUTOSTART_NEOTOMA:-}"
NEOTOMA_REPO="${MCP_PROXY_NEOTOMA_REPO:-${HOME:-}/repos/neotoma}"
NEOTOMA_START_CMD="${MCP_PROXY_NEOTOMA_START_CMD:-}"
NEOTOMA_START_TIMEOUT="${MCP_PROXY_NEOTOMA_START_TIMEOUT:-30}"
NEOTOMA_LOG_FILE="${MCP_PROXY_NEOTOMA_LOG_FILE:-/tmp/neotoma_identity_proxy_autostart.log}"
AAUTH="${MCP_PROXY_AAUTH:-}"

DOWNSTREAM_BASE="${DOWNSTREAM_URL%/mcp}"
HEALTH_URL="${MCP_PROXY_NEOTOMA_HEALTH_URL:-$DOWNSTREAM_BASE/session}"
DOWNSTREAM_PORT="$(printf '%s' "$DOWNSTREAM_BASE" | sed -E 's#^[a-z]+://[^:/]+:([0-9]+)$#\1#')"
if [ "$DOWNSTREAM_PORT" = "$DOWNSTREAM_BASE" ]; then
    DOWNSTREAM_PORT="3080"
fi
NEOTOMA_HTTP_PORT="${MCP_PROXY_NEOTOMA_HTTP_PORT:-$DOWNSTREAM_PORT}"

STARTED_NEOTOMA_PID=""

_lc_aauth() {
    case "$(printf '%s' "$AAUTH" | tr '[:upper:]' '[:lower:]')" in
        1|true|yes) echo "1" ;;
        *)          echo "0" ;;
    esac
}
AAUTH_ON="$(_lc_aauth)"

# Required imports per mode. AAuth pulls in the signing stack.
PYTHON_DEPS_BASE="aiohttp"
PYTHON_DEPS_AAUTH="aiohttp,http_message_signatures,http_sfv,jwt,cryptography,requests"

deps_check() {
    local cmd="$1"
    local needed="$PYTHON_DEPS_BASE"
    [ "$AAUTH_ON" = "1" ] && needed="$PYTHON_DEPS_AAUTH"
    "$cmd" -c "import sys, importlib.util as u
mods='$needed'.split(',')
missing=[m for m in mods if u.find_spec(m) is None]
sys.exit(1 if missing else 0)" 2>/dev/null
}

PYTHON3_CMD=""
# Prefer the project venv so we get a known-good signing stack without relying
# on system-wide pip installs.
VENV_PY="$PROJECT_ROOT/.venv/bin/python3"
if [ -x "$VENV_PY" ] && deps_check "$VENV_PY"; then
    PYTHON3_CMD="$VENV_PY"
fi
if [ -z "$PYTHON3_CMD" ]; then
    for cmd in python3 /opt/homebrew/bin/python3 /usr/local/bin/python3 /usr/bin/python3; do
        if command -v "$cmd" >/dev/null 2>&1 && deps_check "$cmd"; then
            PYTHON3_CMD="$cmd"
            break
        fi
    done
fi

if [ -z "$PYTHON3_CMD" ]; then
    if [ "$AAUTH_ON" = "1" ]; then
        echo "[run_neotoma_identity_proxy] AAuth signing requires aiohttp + http-message-signatures + http-sfv + pyjwt + cryptography + requests." >&2
        echo "Install in the project venv: $PROJECT_ROOT/.venv/bin/pip install aiohttp http-message-signatures http-sfv pyjwt cryptography requests" >&2
    else
        echo "[run_neotoma_identity_proxy] aiohttp not found on any python3." >&2
        echo "Install: $PROJECT_ROOT/.venv/bin/pip install aiohttp" >&2
    fi
    exit 1
fi

PROXY_ARGS=(
    "$SCRIPT_DIR/mcp_identity_proxy.py"
    --downstream-url "$DOWNSTREAM_URL"
    --client-name "$CLIENT_NAME"
    --client-version "$CLIENT_VERSION"
    --log-file "$LOG_FILE"
)

[ -n "$AGENT_LABEL" ] && PROXY_ARGS+=(--agent-label "$AGENT_LABEL")
[ -n "$BEARER_TOKEN" ] && PROXY_ARGS+=(--bearer-token "$BEARER_TOKEN")
[ -n "$CONNECTION_ID" ] && PROXY_ARGS+=(--connection-id "$CONNECTION_ID")

_lc() {
    printf '%s' "$1" | tr '[:upper:]' '[:lower:]'
}

http_status() {
    local url="$1"
    if command -v curl >/dev/null 2>&1; then
        curl -sS -o /dev/null -w "%{http_code}" "$url" 2>/dev/null || true
        return
    fi
    "$PYTHON3_CMD" - "$url" <<'PY'
import sys
import urllib.request
import urllib.error

url = sys.argv[1]
try:
    with urllib.request.urlopen(url, timeout=2) as response:
        print(response.getcode())
except urllib.error.HTTPError as exc:
    print(exc.code)
except Exception:
    print("")
PY
}

health_ready() {
    local code
    code="$(http_status "$HEALTH_URL")"
    case "$code" in
        200|401|403)
            return 0
            ;;
        *)
            return 1
            ;;
    esac
}

start_neotoma_if_needed() {
    case "$(_lc "$AUTOSTART_NEOTOMA")" in
        1|true|yes)
            ;;
        *)
            return 0
            ;;
    esac

    if health_ready; then
        return 0
    fi

    if [ -z "$NEOTOMA_START_CMD" ]; then
        if [ ! -d "$NEOTOMA_REPO" ]; then
            echo "[run_neotoma_identity_proxy] MCP_PROXY_NEOTOMA_REPO not found: $NEOTOMA_REPO" >&2
            exit 1
        fi
        if [ -f "$NEOTOMA_REPO/dist/actions.js" ]; then
            NEOTOMA_START_CMD="cd \"$NEOTOMA_REPO\" && NEOTOMA_HTTP_PORT=\"$NEOTOMA_HTTP_PORT\" HTTP_PORT=\"$NEOTOMA_HTTP_PORT\" node dist/actions.js"
        else
            NEOTOMA_START_CMD="cd \"$NEOTOMA_REPO\" && NEOTOMA_HTTP_PORT=\"$NEOTOMA_HTTP_PORT\" HTTP_PORT=\"$NEOTOMA_HTTP_PORT\" npx tsx src/actions.ts"
        fi
    fi

    echo "[run_neotoma_identity_proxy] starting Neotoma HTTP for proxy: $HEALTH_URL" >&2
    sh -lc "$NEOTOMA_START_CMD" >>"$NEOTOMA_LOG_FILE" 2>&1 &
    STARTED_NEOTOMA_PID="$!"

    local waited=0
    while [ "$waited" -lt "$NEOTOMA_START_TIMEOUT" ]; do
        if health_ready; then
            echo "[run_neotoma_identity_proxy] Neotoma HTTP is ready" >&2
            return 0
        fi
        if ! kill -0 "$STARTED_NEOTOMA_PID" 2>/dev/null; then
            echo "[run_neotoma_identity_proxy] Neotoma exited during startup; see $NEOTOMA_LOG_FILE" >&2
            exit 1
        fi
        sleep 1
        waited=$((waited + 1))
    done

    echo "[run_neotoma_identity_proxy] Timed out waiting for Neotoma health at $HEALTH_URL; see $NEOTOMA_LOG_FILE" >&2
    exit 1
}

cleanup() {
    if [ -n "$STARTED_NEOTOMA_PID" ] && kill -0 "$STARTED_NEOTOMA_PID" 2>/dev/null; then
        kill "$STARTED_NEOTOMA_PID" 2>/dev/null || true
        wait "$STARTED_NEOTOMA_PID" 2>/dev/null || true
    fi
}

case "$(_lc "$SESSION_PREFLIGHT")" in
    1|true|yes)
        PROXY_ARGS+=(--session-preflight)
        ;;
esac

case "$(_lc "$FAIL_CLOSED")" in
    1|true|yes)
        PROXY_ARGS+=(--fail-closed)
        ;;
esac

if [ "$AAUTH_ON" = "1" ]; then
    PROXY_ARGS+=(--aauth)
fi

start_neotoma_if_needed

trap cleanup EXIT INT TERM
"$PYTHON3_CMD" "${PROXY_ARGS[@]}"

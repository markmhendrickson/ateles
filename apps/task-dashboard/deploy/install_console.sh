#!/usr/bin/env bash
#
# install_console.sh: serve the Ateles console persistently on this Mac,
# local-only, at http://127.0.0.1:5273/ (a launchd LaunchAgent).
#
#   install_console.sh               build, render the plist, (re)load the agent
#   install_console.sh --dry-run     print every step and the rendered plist;
#                                    writes nothing, builds nothing, loads nothing
#   install_console.sh --uninstall   unload the agent and remove its plist
#                                    (combine with --dry-run to preview)
#
# Follows execution/scripts/install_rc_autodeploy.sh: a template rendered with
# sed into ~/Library/LaunchAgents, then unload-if-loaded and load.
# Idempotent: re-running rebuilds, rewrites the same plist and reloads it.
#
# It serves the checkout this script lives in. Run it from the checkout you
# want served (for a stable console, the deploy checkout ~/ateles-rc-src rather
# than a session's working clone, whose branch changes under it).
#
# The plist carries NO secret: the Node process reads the Neotoma credential
# itself, exactly as server/neotomaProxy.ts does.
#
# Overridable: CONSOLE_PORT (default 5273), CONSOLE_LOG_DIR
# (default ~/Library/Logs/ateles), LAUNCH_AGENTS_DIR (default
# ~/Library/LaunchAgents).

set -euo pipefail

DRY_RUN=0
UNINSTALL=0
for a in "$@"; do
  case "$a" in
    --dry-run) DRY_RUN=1 ;;
    --uninstall) UNINSTALL=1 ;;
    -h|--help) sed -n '2,25p' "$0"; exit 0 ;;
    *) echo "unknown argument: $a" >&2; exit 2 ;;
  esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO_DIR="$(cd "$APP_DIR/../.." && pwd)"
LABEL="com.ateles.console"
PORT="${CONSOLE_PORT:-5273}"
LOG_DIR="${CONSOLE_LOG_DIR:-$HOME/Library/Logs/ateles}"
LAUNCH_AGENTS="${LAUNCH_AGENTS_DIR:-$HOME/Library/LaunchAgents}"
PLIST_DEST="$LAUNCH_AGENTS/$LABEL.plist"
TEMPLATE="$SCRIPT_DIR/$LABEL.plist.template"

# Every side effect goes through run(): in a dry run it is printed, not done.
run() {
  if [ "$DRY_RUN" = 1 ]; then
    echo "[dry-run] would run: $*"
  else
    "$@"
  fi
}

case "$PORT" in
  ''|*[!0-9]*) echo "CONSOLE_PORT must be a number" >&2; exit 2 ;;
esac

if [ "$UNINSTALL" = 1 ]; then
  echo "==> Uninstalling $LABEL"
  if [ -f "$PLIST_DEST" ] || [ "$DRY_RUN" = 1 ]; then
    run launchctl unload "$PLIST_DEST" || true
    run rm -f "$PLIST_DEST"
  else
    echo "    not installed ($PLIST_DEST absent); nothing to do"
  fi
  echo "==> Done. Logs (kept): $LOG_DIR"
  exit 0
fi

if [ "$DRY_RUN" = 0 ] && ! command -v launchctl >/dev/null 2>&1; then
  echo "launchctl not found: this installer is for macOS LaunchAgents." >&2
  exit 1
fi

NODE_BIN="$(command -v node || true)"
if [ -z "$NODE_BIN" ]; then
  echo "node not found on PATH; install Node 22 first." >&2
  exit 1
fi
command -v npm >/dev/null 2>&1 || { echo "npm not found on PATH." >&2; exit 1; }

# launchd starts with a bare PATH. The planning tab runs the repo's own Python
# module, which imports `mcp`. The repo venv does not always carry it, so probe
# candidates in order and use the first interpreter that can import it.
AGENT_PATH="$(dirname "$NODE_BIN"):/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
PYTHON_BIN=""
for candidate in "$REPO_DIR/.venv/bin/python3" /opt/homebrew/bin/python3 /usr/local/bin/python3 "$(command -v python3 || true)"; do
  if [ -n "$candidate" ] && [ -x "$candidate" ] && "$candidate" -c 'import mcp' >/dev/null 2>&1; then
    PYTHON_BIN="$candidate"
    break
  fi
done
if [ -z "$PYTHON_BIN" ]; then
  echo "No Python with the 'mcp' module found (tried the repo venv, Homebrew and PATH)." >&2
  echo "Install it first, e.g. /opt/homebrew/bin/python3 -m pip install mcp, then rerun." >&2
  exit 1
fi
GIT_SHA="$(git -C "$REPO_DIR" rev-parse HEAD 2>/dev/null || echo "")"

RENDERED="$(sed \
  -e "s|{{NODE}}|$NODE_BIN|g" \
  -e "s|{{APP_DIR}}|$APP_DIR|g" \
  -e "s|{{LOG_DIR}}|$LOG_DIR|g" \
  -e "s|{{HOME}}|$HOME|g" \
  -e "s|{{PATH}}|$AGENT_PATH|g" \
  -e "s|{{PYTHON}}|$PYTHON_BIN|g" \
  -e "s|{{PORT}}|$PORT|g" \
  -e "s|{{GIT_SHA}}|$GIT_SHA|g" \
  "$TEMPLATE")"

# The plist must never carry a credential key or an unfilled placeholder.
if printf '%s' "$RENDERED" | grep -q -E '<key>[^<]*(NEOTOMA|TOKEN|SECRET|KEY)[^<]*</key>|\{\{'; then
  echo "REFUSING: the rendered plist names a credential key or has an unfilled placeholder." >&2
  exit 1
fi

echo "==> Building the console in $APP_DIR (npm ci, npm run build:deploy)"
run npm --prefix "$APP_DIR" ci --no-audit --no-fund
run npm --prefix "$APP_DIR" run build:deploy

echo "==> Writing $PLIST_DEST (logs under $LOG_DIR)"
run mkdir -p "$LOG_DIR" "$LAUNCH_AGENTS"
if [ "$DRY_RUN" = 1 ]; then
  echo "[dry-run] would write $PLIST_DEST:"
  printf '%s\n' "$RENDERED"
else
  printf '%s\n' "$RENDERED" > "$PLIST_DEST"
fi

echo "==> (Re)loading $LABEL"
if [ "$DRY_RUN" = 1 ]; then
  echo "[dry-run] would run: launchctl unload $PLIST_DEST (if loaded)"
# `launchctl list <label>` exits non-zero when the job is not loaded. (Not
# `launchctl list | grep -q`: under pipefail an early-exiting grep can SIGPIPE
# launchctl and read a loaded job as absent, skipping the reload.)
elif launchctl list "$LABEL" >/dev/null 2>&1; then
  launchctl unload "$PLIST_DEST" 2>/dev/null || true
fi
run launchctl load "$PLIST_DEST"

echo "==> Done. Open http://127.0.0.1:$PORT/"
echo "    Verify: node $SCRIPT_DIR/verify_console.mjs --port $PORT"
echo "    Logs:   $LOG_DIR/$LABEL.{log,err}"

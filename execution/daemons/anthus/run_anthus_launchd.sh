#!/usr/bin/env bash
# Launcher for the Anthus daemon under launchd.
#
# Historically this script parsed the operator's materialized secret env
# (SOPS -> ~/.config/neotoma/.env) and `export`ed every key into its own
# process environment before `exec`ing python, so that daemon-spawned
# `claude --print` agents would inherit CLAUDE_CODE_OAUTH_TOKEN. That export
# loop is what let `ps eww <anthus-pid>` read every materialized secret
# (including a GitHub PAT and a Telegram bot token unrelated to OAuth) from
# any local process running as the operator (ateles#657).
#
# It was also unnecessary: `lib/daemon_runtime/__init__.py` already loads
# the same materialized dotenv IN-PROCESS at import time, for every daemon
# that imports lib.daemon_runtime — anthus.py does. That in-process load
# populates anthus's own os.environ (visible only via that process's own
# memory, not via `ps eww`), and `_spawn_agent`'s create_subprocess_exec
# call passes no explicit `env=`, so the child `claude --print` process
# inherits CLAUDE_CODE_OAUTH_TOKEN from the parent the same way every other
# environment variable does. No bash-level export was ever required for
# that to work — this now matches every sibling daemon (apis, aquila,
# cotinga, ...), which invoke the venv interpreter directly with no dotenv
# wrapper.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
PY="${ANTHUS_PYTHON:-$REPO_ROOT/.venv/bin/python3}"

exec "$PY" "$REPO_ROOT/execution/daemons/anthus/anthus.py"

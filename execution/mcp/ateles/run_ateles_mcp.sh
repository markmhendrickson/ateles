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
# Resolved from this script's absolute directory.
# shellcheck disable=SC1091
source "$SCRIPT_DIR/ateles_mcp_bootstrap.sh"

# ── 1. Environment ───────────────────────────────────────────────────────────
# Same file and precedence the daemons use. Existing environment wins, so an
# explicit override in the MCP registration's `env` block still takes effect.
ateles_load_neotoma_environment

# ── 2. Token scope ───────────────────────────────────────────────────────────
# Mirrors the classification in execution/daemons/apis/apis.py. Anything local,
# loopback, link-local, *.local, unparseable, or empty counts as local, so the
# failure mode is "don't promote" rather than "promote a prod token at the
# wrong instance". The implementation is shared with the checkpoint launcher.
ateles_promote_neotoma_token

# ── 3. Interpreter ───────────────────────────────────────────────────────────
# .mcp-venv first: it is what CI builds for this server. The repo-root .venv is
# deliberately NOT a fallback — it lacks `mcp`, so falling back there would
# reintroduce a silent no-tools start. A bare python3 is accepted only if it
# already satisfies both imports.
# Checkpoint approval is also consumed inline by this MCP process. Bind its
# replay-denial evidence to the same restart-persistent host directory as the
# launchd Apis daemon; server.py validates that this is absolute, real (not a
# symlink), and writable before it exposes any tools.
APIS_CHECKPOINT_DENIAL_DIR="${APIS_CHECKPOINT_DENIAL_DIR:-/var/tmp/ateles/checkpoint-denials}"
export APIS_CHECKPOINT_DENIAL_DIR

PYTHON="$(ateles_select_mcp_python)"

exec "$PYTHON" "$SCRIPT_DIR/server.py"

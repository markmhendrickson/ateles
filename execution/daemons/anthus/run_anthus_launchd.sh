#!/usr/bin/env bash
# Launcher for the Anthus daemon under launchd.
#
# Sources the operator's materialized secret env (SOPS -> ~/.config/neotoma/.env,
# produced by execution/scripts/secrets_materialize.py) so daemon-spawned
# `claude --print` agents inherit CLAUDE_CODE_OAUTH_TOKEN and authenticate on
# the operator's Claude subscription instead of failing with 401 / "credit
# balance too low". No secret is committed: the plist points here, and this
# script reads the gitignored materialized env at launch.
#
# Safe when the env file is absent (fresh checkout / CI): the daemon still runs,
# just without the OAuth token (spawned agents fall back to ambient creds).
set -euo pipefail

ENV_FILE="${NEOTOMA_MATERIALIZED_ENV:-$HOME/.config/neotoma/.env}"
if [ -f "$ENV_FILE" ]; then
  # Parse line-by-line rather than `source`: the materialized env legitimately
  # contains values with spaces (e.g. ATELES_GMAIL_SEND_CMD="gws gmail ...")
  # that a bare `. env` would try to execute. Export only well-formed
  # KEY=VALUE lines, taking the value verbatim (everything after the first =),
  # and skip comments / blanks / malformed keys.
  while IFS= read -r _line || [ -n "$_line" ]; do
    case "$_line" in
      ''|'#'*) continue ;;
    esac
    _key="${_line%%=*}"
    # A valid env key is letters/digits/underscore and contains no space.
    case "$_key" in
      *[!A-Za-z0-9_]*|'') continue ;;
    esac
    _val="${_line#*=}"
    # Strip one layer of surrounding quotes if present.
    case "$_val" in
      \"*\") _val="${_val#\"}"; _val="${_val%\"}" ;;
      \'*\') _val="${_val#\'}"; _val="${_val%\'}" ;;
    esac
    export "$_key=$_val"
  done < "$ENV_FILE"
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
PY="${ANTHUS_PYTHON:-$REPO_ROOT/.venv/bin/python3}"

exec "$PY" "$REPO_ROOT/execution/daemons/anthus/anthus.py"

#!/usr/bin/env bash
# Ateles cloud daemon entrypoint (task #5, plan ent_aff87747b49e338790568af6).
#
# Materializes secrets OFFLINE (SOPS+age) into the dotenv the daemons read, then
# exec's the per-daemon command. Plaintext lives only in the container's process
# env + ~/.config/neotoma/.env (ephemeral) — never in the image or the registry.
# Mirrors the host model: daemons load ~/.config/neotoma/.env at startup.
set -euo pipefail

AGE_KEY="${SOPS_AGE_KEY_FILE:-/secrets/age/keys.txt}"
SECRETS_DIR="${ATELES_SECRETS_DIR:-/secrets/ateles-private}"

if [[ ! -f "$AGE_KEY" ]]; then
  echo "[entrypoint] FATAL: age private key not found at $AGE_KEY" >&2
  echo "[entrypoint]   mount it read-only (see deploy/cloud/README.md)." >&2
  exit 1
fi
if [[ ! -d "$SECRETS_DIR/secrets" ]]; then
  echo "[entrypoint] FATAL: ateles-private not mounted at $SECRETS_DIR" >&2
  echo "[entrypoint]   mount the private clone read-only (see README)." >&2
  exit 1
fi

echo "[entrypoint] materializing secrets (sops+age) → ~/.config/neotoma/.env"
if ! python /app/execution/scripts/secrets_materialize.py; then
  echo "[entrypoint] FATAL: secret materialization failed" >&2
  exit 1
fi

# Sanity: the daemons need at least a Neotoma token. Warn (don't hard-fail —
# individual daemons fail open) if the prod token is absent for a remote base URL.
if ! grep -q "NEOTOMA_BEARER_TOKEN" "${HOME}/.config/neotoma/.env" 2>/dev/null; then
  echo "[entrypoint] WARNING: no NEOTOMA_BEARER_TOKEN materialized — daemons may 401" >&2
fi

echo "[entrypoint] exec: $*"
exec "$@"

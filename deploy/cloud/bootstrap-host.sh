#!/usr/bin/env bash
# Bootstrap a cloud host for the Ateles Bucket-A swarm (task #5, plan
# ent_aff87747b49e338790568af6). Target: a small EU VM (recommended: Hetzner
# CAX11, Ubuntu/Debian arm64). Idempotent; safe to re-run.
#
# Accepted decisions baked in as defaults: Tailscale networking, per-host age
# key, ANTHROPIC_API_KEY via SOPS, B-thin Google (calendar daemons stay device-
# side for now). Steps that need an operator secret are GUARDED and will pause
# with instructions rather than guess.
#
# Usage (on the VM, as a sudo-capable user):
#   ATELES_PRIVATE_REMOTE=https://github.com/markmhendrickson/ateles-private.git \
#   TS_AUTHKEY=tskey-... \
#   bash bootstrap-host.sh
set -euo pipefail

ATELES_DIR="${ATELES_DIR:-/opt/ateles/ateles}"
PRIVATE_DIR="${ATELES_PRIVATE_DIR:-/opt/ateles/ateles-private}"
AGE_KEY="${ATELES_AGE_KEY:-/opt/ateles/age/keys.txt}"
ATELES_REMOTE="${ATELES_REMOTE:-https://github.com/markmhendrickson/ateles.git}"
ATELES_PRIVATE_REMOTE="${ATELES_PRIVATE_REMOTE:-}"   # operator-provided (deploy key / PAT URL)

log() { echo -e "\n=== $* ==="; }

# ── 1. System packages: Docker + compose plugin ─────────────────────────────
log "1. Docker"
if ! command -v docker >/dev/null 2>&1; then
  curl -fsSL https://get.docker.com | sh
  sudo usermod -aG docker "$USER" || true
  echo "Docker installed. You may need to re-login for the docker group to apply."
else
  echo "docker present: $(docker --version)"
fi

# ── 2. Tailscale (accepted: private mesh; no public SSH/ports) ──────────────
log "2. Tailscale"
if ! command -v tailscale >/dev/null 2>&1; then
  curl -fsSL https://tailscale.com/install.sh | sh
fi
if tailscale status >/dev/null 2>&1; then
  echo "tailscale already up: $(tailscale ip -4 2>/dev/null | head -1)"
elif [[ -n "${TS_AUTHKEY:-}" ]]; then
  sudo tailscale up --authkey "${TS_AUTHKEY}" --hostname ateles-cloud --ssh
  echo "joined tailnet: $(tailscale ip -4 2>/dev/null | head -1)"
else
  echo "GUARDED: set TS_AUTHKEY=tskey-... (Tailscale admin console) and re-run,"
  echo "         or run: sudo tailscale up --ssh --hostname ateles-cloud"
fi

# ── 3. Repos ────────────────────────────────────────────────────────────────
log "3. Repos"
sudo mkdir -p "$(dirname "$ATELES_DIR")" && sudo chown -R "$USER" "$(dirname "$ATELES_DIR")"
if [[ -d "$ATELES_DIR/.git" ]]; then
  git -C "$ATELES_DIR" fetch origin -q && git -C "$ATELES_DIR" checkout main -q && git -C "$ATELES_DIR" pull --ff-only -q
  echo "ateles updated: $(git -C "$ATELES_DIR" rev-parse --short HEAD)"
else
  git clone --depth 1 "$ATELES_REMOTE" "$ATELES_DIR"
fi
if [[ -d "$PRIVATE_DIR/.git" ]]; then
  git -C "$PRIVATE_DIR" pull --ff-only -q || true
  echo "ateles-private updated"
elif [[ -n "$ATELES_PRIVATE_REMOTE" ]]; then
  git clone --depth 1 "$ATELES_PRIVATE_REMOTE" "$PRIVATE_DIR"
else
  echo "GUARDED: set ATELES_PRIVATE_REMOTE=<deploy-key or PAT clone URL> and re-run"
  echo "         (read-only access to the private secrets repo)."
fi

# ── 4. Age private key (accepted: per-host key) ─────────────────────────────
log "4. age key"
if [[ -f "$AGE_KEY" ]]; then
  echo "age key present at $AGE_KEY"
else
  echo "GUARDED — provision a PER-HOST age key (lowest-trust option):"
  echo "  1. on the host:  age-keygen -o $AGE_KEY   (mkdir -p \$(dirname $AGE_KEY); chmod 600)"
  echo "  2. copy its PUBLIC key (age1...) into ateles-private/.sops.yaml recipients,"
  echo "     re-encrypt the snapshot (secrets_publish.py), commit + pull here."
  echo "  Then this host can decrypt without holding the operator's primary key."
fi

# ── 5. Build + launch (dry-run by default; cut over per README M3) ──────────
log "5. Build + launch (APIS_DRY_RUN=${APIS_DRY_RUN:-1})"
COMPOSE_DIR="$ATELES_DIR/deploy/cloud"
ENV_FILE="$COMPOSE_DIR/.env"
{
  echo "ATELES_AGE_KEY=$AGE_KEY"
  echo "ATELES_PRIVATE_DIR=$PRIVATE_DIR"
  echo "APIS_DRY_RUN=${APIS_DRY_RUN:-1}"
} > "$ENV_FILE"
echo "wrote $ENV_FILE"

if [[ -f "$AGE_KEY" && -d "$PRIVATE_DIR/secrets" ]]; then
  ( cd "$COMPOSE_DIR" && docker compose build && docker compose up -d )
  echo "daemons up (dry-run). Verify: docker compose -f $COMPOSE_DIR/docker-compose.yml logs -f apis"
else
  echo "SKIPPED launch — age key and/or ateles-private not yet provisioned (steps 3/4)."
fi

log "done"
echo "Next: confirm SSE connect + '[watchdog] starting' in logs, then cut over"
echo "one daemon at a time per deploy/cloud/README.md (stop the Studio copy first)."

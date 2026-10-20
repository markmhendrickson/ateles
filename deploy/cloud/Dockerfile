# Superseded VM scaffold for Bucket-A (device-agnostic) daemons.
# Current Bucket-C jobs use deploy/fly-runner/Dockerfile and run_job.py instead;
# this full-snapshot daemon image is not their credential boundary.
# Task #5 of plan ent_aff87747b49e338790568af6. See deploy/cloud/README.md.
#
# Historical multi-arch VM image: arm64 and amd64. Secrets are
# NEVER baked in — the age key + the ateles-private clone are mounted at runtime
# and decrypted by the entrypoint (SOPS+age), so the image is safe to store in a
# registry.
FROM python:3.12-slim-bookworm

ARG SOPS_VERSION=3.9.4
# Set automatically by buildkit: "arm64" or "amd64".
ARG TARGETARCH=amd64

# Base system deps: git (repo ops at runtime), curl/ca-certs (downloads + HTTPS).
RUN apt-get update && apt-get install -y --no-install-recommends \
        git curl ca-certificates gnupg \
    && rm -rf /var/lib/apt/lists/*

# sops binary — secrets_lib shells out to `sops --decrypt` for offline secret
# materialization (the age key file is mounted; sops handles age natively).
RUN curl -fsSL -o /usr/local/bin/sops \
        "https://github.com/getsops/sops/releases/download/v${SOPS_VERSION}/sops-v${SOPS_VERSION}.linux.${TARGETARCH}" \
    && chmod +x /usr/local/bin/sops \
    && sops --version

# Historical daemon-host CLI setup. This snapshot materialization is not the
# current subscription-login runner path; see docs/cloud_hosting.md before use.
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && npm install -g @anthropic-ai/claude-code \
    && npm cache clean --force \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first (better layer caching) then the repo.
COPY execution/scripts/daemon-requirements.txt /app/execution/scripts/daemon-requirements.txt
RUN pip install --no-cache-dir -r execution/scripts/daemon-requirements.txt

# The repo at the build revision. Rebuild + redeploy the image to ship a new
# revision (the container-model analog of rc-autodeploy's git fast-forward).
COPY . /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    HOME=/root \
    ATELES_SECRETS_DIR=/secrets/ateles-private \
    SOPS_AGE_KEY_FILE=/secrets/age/keys.txt \
    NEOTOMA_BASE_URL=https://neotoma.markmhendrickson.com

# entrypoint materializes secrets, then exec's the per-daemon command (compose).
ENTRYPOINT ["/app/deploy/cloud/entrypoint.sh"]
CMD ["python", "-c", "import sys; sys.exit('set a daemon command in docker-compose.yml, e.g. python execution/daemons/apis/apis.py')"]

# Ateles Fly runner image (ateles#1456): runs ONE swarm dispatch per machine.
#
# What this image deliberately does NOT contain, and a test
# (execution/daemons/apis/test_fly_runner_image.py) fails the build context if
# it ever does: no sops, no age key or key path, no checkout of the private
# secrets repo, and no copy of the cloud daemon entrypoint, which decrypts the
# full secret snapshot. A job's credentials arrive only as machine-level files
# under /run/job/, written per machine by Apis, and run_job.py deletes them
# before the CLI starts.
#
# Build from the repository root so only the files below are sent (see
# Dockerfile.dockerignore beside this file):
#   fly deploy --config deploy/fly-runner/fly.toml --dockerfile deploy/fly-runner/Dockerfile \
#     --build-only --push --image-label "$(git rev-parse --short HEAD)" .
FROM python:3.12-slim-bookworm

# Pinned to the versions the operator's dispatcher runs today. Bump together.
ARG NODE_VERSION=22.20.0
ARG GH_VERSION=2.98.0
ARG CLAUDE_CODE_VERSION=2.1.285
ARG CODEX_VERSION=0.157.1
ARG CURSOR_AGENT_VERSION=2026.09.23-86fc751
# Cursor publishes no checksum file for the agent CLI. Pass one observed from a
# trusted download to have the build verify it; empty skips the check.
ARG CURSOR_AGENT_SHA256=""
ARG TARGETARCH=amd64

RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates curl xz-utils \
    && rm -rf /var/lib/apt/lists/*

# Node, verified against the release's published SHA256 list.
RUN set -eux; \
    case "${TARGETARCH}" in amd64) NODE_ARCH=x64 ;; arm64) NODE_ARCH=arm64 ;; *) exit 1 ;; esac; \
    NODE_TGZ="node-v${NODE_VERSION}-linux-${NODE_ARCH}.tar.xz"; \
    cd /tmp; \
    curl -fsSLO "https://nodejs.org/dist/v${NODE_VERSION}/${NODE_TGZ}"; \
    curl -fsSLO "https://nodejs.org/dist/v${NODE_VERSION}/SHASUMS256.txt"; \
    grep " ${NODE_TGZ}\$" SHASUMS256.txt | sha256sum -c -; \
    tar -xJf "${NODE_TGZ}" -C /usr/local --strip-components=1 --no-same-owner; \
    rm -f "${NODE_TGZ}" SHASUMS256.txt; \
    node --version

# gh, verified against the release's published checksums file.
RUN set -eux; \
    GH_TGZ="gh_${GH_VERSION}_linux_${TARGETARCH}.tar.gz"; \
    cd /tmp; \
    curl -fsSLO "https://github.com/cli/cli/releases/download/v${GH_VERSION}/${GH_TGZ}"; \
    curl -fsSLO "https://github.com/cli/cli/releases/download/v${GH_VERSION}/gh_${GH_VERSION}_checksums.txt"; \
    grep " ${GH_TGZ}\$" "gh_${GH_VERSION}_checksums.txt" | sha256sum -c -; \
    tar -xzf "${GH_TGZ}"; \
    install -m 0755 "gh_${GH_VERSION}_linux_${TARGETARCH}/bin/gh" /usr/local/bin/gh; \
    rm -rf /tmp/gh_*; \
    gh --version

# Claude and Codex CLIs at pinned versions (npm verifies registry integrity).
RUN npm install -g "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" "@openai/codex@${CODEX_VERSION}" \
    && npm cache clean --force \
    && claude --version && codex --version

# Cursor agent CLI at a pinned version.
RUN set -eux; \
    case "${TARGETARCH}" in amd64) C_ARCH=x64 ;; arm64) C_ARCH=arm64 ;; *) exit 1 ;; esac; \
    cd /tmp; \
    curl -fsSL -o agent.tgz "https://downloads.cursor.com/lab/${CURSOR_AGENT_VERSION}/linux/${C_ARCH}/agent-cli-package.tar.gz"; \
    if [ -n "${CURSOR_AGENT_SHA256}" ]; then echo "${CURSOR_AGENT_SHA256}  agent.tgz" | sha256sum -c -; fi; \
    mkdir -p "/opt/cursor-agent/${CURSOR_AGENT_VERSION}"; \
    tar -xzf agent.tgz -C "/opt/cursor-agent/${CURSOR_AGENT_VERSION}" --strip-components=1; \
    ln -sf "/opt/cursor-agent/${CURSOR_AGENT_VERSION}/cursor-agent" /usr/local/bin/cursor-agent; \
    rm -f agent.tgz; \
    cursor-agent --version

# The signer's two third-party dependencies, pinned.
RUN pip install --no-cache-dir "cryptography==43.0.3" "PyJWT==2.9.0"

# The CLI runs as this unprivileged user; run_job.py (root) keeps the role's
# signing key and Neotoma credential out of its reach. /data is the login
# volume on the long-lived Codex and Cursor machines only.
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin agent \
    && mkdir -p /data && chown agent:agent /data

# Only the runner and the three stdlib-plus-crypto modules it imports.
WORKDIR /opt/ateles
COPY lib/environment_allowlist.py lib/credential_env_names.py /opt/ateles/lib/
COPY lib/daemon_runtime/aauth_httpsig.py /opt/ateles/lib/daemon_runtime/aauth_httpsig.py
COPY deploy/fly-runner/run_job.py /opt/ateles/run_job.py

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

CMD ["python3", "/opt/ateles/run_job.py"]

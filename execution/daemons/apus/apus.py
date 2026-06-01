#!/usr/bin/env python3
"""
Apus — Neotoma → git mirror webhook daemon.

Apus genus: swifts. T3 daemon in the Ateles swarm.

Receives HTTPS webhook payloads from Neotoma when mirror profiles fire,
then commits the mirrored content to the appropriate git repository via
the `ateles-agent` GitHub identity.

Lives at: apus.markmhendrickson.com (Cloudflare Tunnel → localhost:8741)

Mirror profiles handled:
  ateles-public-agents       → markmhendrickson/ateles   .claude/agents/
  ateles-public-skills       → markmhendrickson/ateles   .claude/skills/
  ateles-architecture-docs   → markmhendrickson/ateles   docs/
  ateles-private-agents      → markmhendrickson/ateles-private  agents/

AAuth sub: apus@ateles-swarm
Phase 2: full webhook receiver + git commit logic.

Startup sequence (T3 daemon pattern):
  1. Load env from ~/.config/neotoma/.env
  2. Load agent_definition from Neotoma via lib/daemon_runtime
  3. Load priority_rubric from Neotoma via lib/notify
  4. Start HTTPS webhook receiver on APUS_PORT (default: 8741)

Environment variables:
  NEOTOMA_BEARER_TOKEN      Neotoma API auth token
  NEOTOMA_BASE_URL          Neotoma API base URL (default: https://neotoma.markmhendrickson.com)
  TELEGRAM_BOT_TOKEN        Telegram bot token
  TELEGRAM_CHAT_ID          Telegram chat ID
  TELEGRAM_TOPIC_APUS       Telegram topic ID for Apus notifications (optional)
  APUS_WEBHOOK_SECRET       HMAC-SHA256 secret for Neotoma webhook signature verification
  APUS_PORT                 Port to listen on (default: 8741)
  APUS_AGENT_DEFINITION_ID  Neotoma entity ID for Apus's agent_definition (optional)
  ATELES_REPO_PATH          Local path to markmhendrickson/ateles clone
  ATELES_PRIVATE_REPO_PATH  Local path to markmhendrickson/ateles-private clone
  ATELES_AGENT_GIT_NAME     Git author name for commits (default: ateles-agent)
  ATELES_AGENT_GIT_EMAIL    Git author email for commits
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aiohttp import web

# ── Path bootstrap ────────────────────────────────────────────────────────────
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from lib.daemon_runtime import (  # noqa: E402
    AAuthSigner,
    AgentLoader,
)
from lib.notify import Notifier, Priority  # noqa: E402

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("apus")

# ── Config ────────────────────────────────────────────────────────────────────
DAEMON_NAME = "apus"
APUS_PORT = int(os.environ.get("APUS_PORT", "8741"))
WEBHOOK_SECRET = os.environ.get("APUS_WEBHOOK_SECRET", "")

# Path to the mirror-rebuild script (used by /rebuild endpoint)
MIRROR_REBUILD_SCRIPT = Path(
    os.environ.get(
        "ATELES_MIRROR_REBUILD_SCRIPT",
        str(Path.home() / "repos" / "ateles" / "scripts" / "mirror-rebuild-skills.sh"),
    )
)

# Local repo paths
ATELES_REPO = Path(
    os.environ.get("ATELES_REPO_PATH", str(Path.home() / "repos" / "ateles"))
)
ATELES_PRIVATE_REPO = Path(
    os.environ.get(
        "ATELES_PRIVATE_REPO_PATH", str(Path.home() / "repos" / "ateles-private")
    )
)

# Git commit identity
GIT_AUTHOR_NAME = os.environ.get("ATELES_AGENT_GIT_NAME", "ateles-agent")
GIT_AUTHOR_EMAIL = os.environ.get("ATELES_AGENT_GIT_EMAIL", "")


# ── Mirror profile registry ───────────────────────────────────────────────────


@dataclass
class MirrorProfile:
    """Maps a Neotoma mirror profile name → target repo path + subdirectory."""

    profile_name: str
    repo_path: Path
    target_dir: str  # relative to repo root
    commit_prefix: str  # for commit message prefix


MIRROR_PROFILES: dict[str, MirrorProfile] = {
    "ateles-public-agents": MirrorProfile(
        profile_name="ateles-public-agents",
        repo_path=ATELES_REPO,
        target_dir=".claude/agents",
        commit_prefix="agents",
    ),
    "ateles-public-skills": MirrorProfile(
        profile_name="ateles-public-skills",
        repo_path=ATELES_REPO,
        target_dir=".claude/skills",
        commit_prefix="skills",
    ),
    "ateles-architecture-docs": MirrorProfile(
        profile_name="ateles-architecture-docs",
        repo_path=ATELES_REPO,
        target_dir="docs",
        commit_prefix="docs",
    ),
    "ateles-private-agents": MirrorProfile(
        profile_name="ateles-private-agents",
        repo_path=ATELES_PRIVATE_REPO,
        target_dir="agents",
        commit_prefix="agents",
    ),
}


# ── Webhook handler ────────────────────────────────────────────────────────────


async def handle_webhook(request: web.Request) -> web.Response:
    """
    Receive a Neotoma mirror webhook POST.

    Expected payload:
    {
        "profile": "ateles-public-agents",
        "files": [
            {"path": "relative/file.md", "content": "...", "deleted": false}
        ],
        "entity_id": "ent_...",
        "delivery_id": "..."
    }
    """
    notifier: Notifier = request.app["notifier"]
    delivery_id = request.headers.get("X-Delivery-Id", "unknown")

    # 1. Verify HMAC signature
    if WEBHOOK_SECRET:
        sig_header = request.headers.get("X-Neotoma-Signature", "")
        body = await request.read()
        if not _verify_signature(body, sig_header):
            log.warning(
                f"[apus] Signature verification failed for delivery {delivery_id}"
            )
            return web.Response(status=401, text="Signature mismatch")
    else:
        body = await request.read()

    # 2. Parse payload
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        log.warning(f"[apus] Bad JSON in delivery {delivery_id}: {exc}")
        return web.Response(status=400, text="Invalid JSON")

    profile_name = payload.get("profile", "")
    files: list[dict[str, Any]] = payload.get("files", [])
    entity_id = payload.get("entity_id", "")

    log.info(
        f"[apus] Delivery {delivery_id}: profile={profile_name} "
        f"files={len(files)} entity={entity_id}"
    )

    # 3. Route to mirror profile
    profile = MIRROR_PROFILES.get(profile_name)
    if not profile:
        log.warning(f"[apus] Unknown profile: {profile_name!r}")
        return web.Response(status=422, text=f"Unknown profile: {profile_name}")

    # 4. Apply files and commit
    try:
        commit_sha = await asyncio.to_thread(
            _apply_and_commit, profile, files, entity_id, delivery_id
        )
        log.info(f"[apus] Committed {commit_sha[:8]} for profile={profile_name}")
        notifier.send(
            f"Mirror commit {commit_sha[:8]}: {profile_name} ({len(files)} file(s))",
            priority=Priority.INFO,
            handler=DAEMON_NAME,
        )
        _log_delivery_to_neotoma(delivery_id, profile_name, commit_sha, "success")
        return web.json_response({"status": "ok", "commit": commit_sha})

    except subprocess.CalledProcessError as exc:
        err = exc.stderr.decode(errors="replace") if exc.stderr else str(exc)
        log.error(f"[apus] Git error for {profile_name}: {err}")
        notifier.send(
            f"Mirror FAILED: {profile_name}\n{err[:200]}",
            priority=Priority.BLOCKER,
            handler=DAEMON_NAME,
        )
        _log_delivery_to_neotoma(delivery_id, profile_name, "", "git_error", err)
        return web.Response(status=500, text="Git commit failed")

    except Exception as exc:
        log.error(f"[apus] Unexpected error for {profile_name}: {exc}", exc_info=True)
        _log_delivery_to_neotoma(delivery_id, profile_name, "", "error", str(exc))
        return web.Response(status=500, text="Internal error")


async def handle_health(request: web.Request) -> web.Response:
    """Simple health check endpoint."""
    return web.json_response({"status": "ok", "daemon": DAEMON_NAME})


async def handle_rebuild(request: web.Request) -> web.Response:
    """
    Trigger a mirror rebuild for a specific profile (or all profiles).

    POST /rebuild
    Body (JSON, optional): {"profile": "ateles-public-skills"}
    If profile is omitted, runs the default mirror-rebuild-skills.sh for all.

    This endpoint is for local/operator use only — the Cloudflare Tunnel
    restricts access. No HMAC auth is required (webhook secret is for
    Neotoma-push path only).
    """
    try:
        body_bytes = await request.read()
        payload = json.loads(body_bytes) if body_bytes.strip() else {}
    except json.JSONDecodeError:
        payload = {}

    profile = payload.get("profile", "")
    log.info(f"[apus] Rebuild triggered: profile={profile or 'all'}")

    if not MIRROR_REBUILD_SCRIPT.exists():
        return web.json_response(
            {
                "status": "error",
                "message": f"Rebuild script not found: {MIRROR_REBUILD_SCRIPT}",
            },
            status=500,
        )

    cmd = [str(MIRROR_REBUILD_SCRIPT)]
    if profile:
        cmd += ["--profile", profile]

    try:
        result = await asyncio.to_thread(
            subprocess.run,
            cmd,
            check=True,
            capture_output=True,
            text=True,
        )
        log.info(f"[apus] Rebuild OK: {result.stdout.strip()}")
        return web.json_response(
            {
                "status": "ok",
                "profile": profile or "all",
                "output": result.stdout.strip(),
            }
        )
    except subprocess.CalledProcessError as exc:
        err = exc.stderr.strip() if exc.stderr else str(exc)
        log.error(f"[apus] Rebuild failed: {err}")
        return web.json_response(
            {"status": "error", "profile": profile or "all", "output": err},
            status=500,
        )


# ── Git operations ─────────────────────────────────────────────────────────────


def _apply_and_commit(
    profile: MirrorProfile,
    files: list[dict[str, Any]],
    entity_id: str,
    delivery_id: str,
) -> str:
    """
    Write/delete files in the target repo and create a signed git commit.

    Runs synchronously; call via asyncio.to_thread().
    Returns the new commit SHA.
    """
    repo = profile.repo_path
    target_dir = repo / profile.target_dir
    target_dir.mkdir(parents=True, exist_ok=True)

    git_env = {
        **os.environ,
        "GIT_AUTHOR_NAME": GIT_AUTHOR_NAME,
        "GIT_AUTHOR_EMAIL": GIT_AUTHOR_EMAIL,
        "GIT_COMMITTER_NAME": GIT_AUTHOR_NAME,
        "GIT_COMMITTER_EMAIL": GIT_AUTHOR_EMAIL,
    }

    def git(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", "-C", str(repo), *args],
            check=True,
            capture_output=True,
            env=git_env,
        )

    changed: list[str] = []
    deleted: list[str] = []

    for f in files:
        rel_path = f.get("path", "")
        if not rel_path:
            continue
        is_deleted = f.get("deleted", False)
        abs_path = target_dir / rel_path

        if is_deleted:
            if abs_path.exists():
                abs_path.unlink()
                deleted.append(rel_path)
                git(
                    "rm",
                    "--cached",
                    "--ignore-unmatch",
                    str(abs_path.relative_to(repo)),
                )
        else:
            content = f.get("content", "")
            abs_path.parent.mkdir(parents=True, exist_ok=True)
            abs_path.write_text(content, encoding="utf-8")
            changed.append(rel_path)
            git("add", str(abs_path.relative_to(repo)))

    if not changed and not deleted:
        log.info(
            f"[apus] No file changes for {profile.profile_name} — nothing to commit"
        )
        # Return current HEAD
        result = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            env=git_env,
        )
        return result.stdout.decode().strip()

    # Build commit message
    summary_parts = []
    if changed:
        summary_parts.append(f"{len(changed)} file(s) updated")
    if deleted:
        summary_parts.append(f"{len(deleted)} file(s) removed")
    summary = "; ".join(summary_parts)

    commit_msg = (
        f"mirror({profile.commit_prefix}): {summary}\n\n"
        f"Profile: {profile.profile_name}\n"
        f"Entity: {entity_id}\n"
        f"Delivery: {delivery_id}\n"
        f"Agent: {DAEMON_NAME}@ateles-swarm"
    )

    result = git("commit", "--message", commit_msg)
    sha_result = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        env=git_env,
    )
    sha = sha_result.stdout.decode().strip()

    # Push to remote
    git("push", "origin", "HEAD")
    return sha


def _verify_signature(body: bytes, sig_header: str) -> bool:
    """Verify HMAC-SHA256 webhook signature."""
    if not WEBHOOK_SECRET or not sig_header:
        return not bool(WEBHOOK_SECRET)  # if no secret configured, allow all
    expected = (
        "sha256=" + hmac.new(WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
    )
    return hmac.compare_digest(expected, sig_header)


def _log_delivery_to_neotoma(
    delivery_id: str,
    profile: str,
    commit_sha: str,
    status: str,
    error: str = "",
) -> None:
    """
    Log delivery status back to Neotoma as an observation.
    Best-effort — failures are logged but not re-raised.
    """
    import httpx

    neotoma_url = os.environ.get("NEOTOMA_BASE_URL", "")
    bearer = os.environ.get("NEOTOMA_BEARER_TOKEN", "")
    if not bearer:
        return

    payload = {
        "delivery_id": delivery_id,
        "profile": profile,
        "commit_sha": commit_sha,
        "status": status,
        "daemon": DAEMON_NAME,
        **({"error": error[:500]} if error else {}),
    }
    try:
        httpx.post(
            f"{neotoma_url}/store",
            json={"entity_type": "mirror_delivery", **payload},
            headers={"Authorization": f"Bearer {bearer}"},
            timeout=5,
        )
    except Exception as exc:
        log.debug(f"[apus] Could not log delivery to Neotoma: {exc}")


# ── Main ──────────────────────────────────────────────────────────────────────


async def main() -> None:
    log.info(f"[{DAEMON_NAME}] Starting up...")

    # 1. Load agent_definition
    agent_def = AgentLoader(DAEMON_NAME).load()
    log.info(
        f"[{DAEMON_NAME}] agent_definition: status={agent_def.status} "
        f"grant={agent_def.agent_grant} sub={agent_def.aauth_sub}"
    )

    # 2. Load AAuth signer
    signer = AAuthSigner.from_key_file(DAEMON_NAME)
    if signer.is_stub:
        log.warning(
            f"[{DAEMON_NAME}] AAuth keypair not minted yet — "
            "observations attributed to operator token"
        )

    # 3. Load notification rubric
    notifier = Notifier.from_neotoma()

    if not WEBHOOK_SECRET:
        log.warning(
            f"[{DAEMON_NAME}] APUS_WEBHOOK_SECRET not set — "
            "webhook signature verification disabled"
        )

    # 4. Start webhook server
    app = web.Application()
    app["notifier"] = notifier
    app["signer"] = signer
    app.router.add_post("/webhook", handle_webhook)
    app.router.add_get("/health", handle_health)
    app.router.add_post("/rebuild", handle_rebuild)

    notifier.send(
        f"{DAEMON_NAME} started on port {APUS_PORT}",
        priority=Priority.INFO,
        handler=DAEMON_NAME,
    )

    log.info(
        f"[{DAEMON_NAME}] Webhook receiver listening on "
        f"http://localhost:{APUS_PORT}/webhook"
    )
    log.info(f"[{DAEMON_NAME}] Profiles: {list(MIRROR_PROFILES.keys())}")

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", APUS_PORT)
    await site.start()

    # Keep running
    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info(f"[{DAEMON_NAME}] Stopped by operator.")

"""
execution/daemons/apis/skill_runner.py — spawn a T4 agent via `claude --print`.

Single implementation of the spawn pattern previously inlined in apis.py (and
mirrored in Formica): `claude --print --append-system-prompt <SKILL.md>` with
the work prompt piped on stdin. Extracted so the GitHub trigger pipelines
(swarm_dispatch.py) can reuse it and capture agent output for the review
learning loop.

Failures never raise — callers get a SkillResult and decide how to degrade;
one bad dispatch must not take down the daemon.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("apis.skill_runner")

CLAUDE_BIN = os.environ.get("APIS_CLAUDE_BIN") or shutil.which("claude")
DISPATCH_TIMEOUT_SECONDS = int(os.environ.get("APIS_DISPATCH_TIMEOUT", "1800"))
ATELES_REPO = Path(
    os.environ.get("ATELES_REPO_PATH", str(Path.home() / "repos" / "ateles"))
)


@dataclass
class SkillResult:
    skill: str
    ok: bool
    returncode: int | None
    stdout: str
    stderr: str
    error: str = ""  # non-process failure: missing binary / SKILL.md / timeout


async def run_skill(
    skill: str,
    prompt: str,
    *,
    timeout: int | None = None,
    env_extra: dict[str, str] | None = None,
) -> SkillResult:
    """
    Run one T4 agent to completion and return its output.

    `claude --print` has no --skill flag; the working pattern (Tier 1 smoke
    test, 2026-05-25) is --append-system-prompt with the SKILL.md content.
    """
    timeout = timeout or DISPATCH_TIMEOUT_SECONDS

    if CLAUDE_BIN is None:
        msg = "claude binary unavailable (APIS_CLAUDE_BIN unset, not on PATH)"
        log.warning(f"[apis] {skill} dispatch skipped — {msg}")
        return SkillResult(skill, False, None, "", "", error=msg)

    skill_path = ATELES_REPO / ".claude" / "skills" / skill / "SKILL.md"
    if not skill_path.exists():
        msg = f"SKILL.md not found at {skill_path}"
        log.error(f"[apis] {skill} dispatch skipped — {msg}")
        return SkillResult(skill, False, None, "", "", error=msg)

    try:
        skill_md = skill_path.read_text(encoding="utf-8")
    except OSError as exc:
        return SkillResult(skill, False, None, "", "", error=f"read failed: {exc}")

    cmd = [CLAUDE_BIN, "--print", "--append-system-prompt", skill_md]
    log.info(
        f"[apis] Spawning: claude --print --append-system-prompt "
        f"<{skill}.SKILL.md> timeout={timeout}s"
    )

    subprocess_env = {**os.environ, **(env_extra or {})}
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=subprocess_env,
    )

    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(input=prompt.encode()), timeout=timeout
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        msg = f"timed out after {timeout}s"
        log.error(f"[apis] {skill} dispatch {msg}")
        return SkillResult(skill, False, None, "", "", error=msg)

    result = SkillResult(
        skill=skill,
        ok=proc.returncode == 0,
        returncode=proc.returncode,
        stdout=stdout.decode("utf-8", errors="replace"),
        stderr=stderr.decode("utf-8", errors="replace"),
    )
    if result.ok:
        log.info(f"[apis] {skill} dispatch ok ({len(result.stdout)}B stdout)")
    else:
        log.error(
            f"[apis] {skill} dispatch failed (rc={proc.returncode}): "
            f"{result.stderr[:500]}"
        )
    return result
